"""A transition into a gated status requires a consumed sentinel."""
import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HOOK = os.path.join(REPO, ".claude", "hooks", "jira_transition_verify_gate.py")
TRANSITION = "mcp__plugin_atlassian_atlassian__transitionJiraIssue"

CONFIG = {
    "projectKey": "AGENT",
    "transitions": {"11": "To Do", "21": "In Progress", "31": "In Review", "41": "Done"},
    "gatedStatuses": ["In Review", "Done"],
}


def setup_env(tmp_path, config=None):
    cfg = tmp_path / "jira-config.json"
    cfg.write_text(json.dumps(config or CONFIG))
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return dict(os.environ, HOME=str(home), JIRA_CONFIG_PATH=str(cfg)), home


def run_hook(env, issue="AGENT-14", transition_id="31", tool_name=TRANSITION):
    payload = {
        "tool_name": tool_name,
        "tool_input": {"issueIdOrKey": issue, "transition": {"id": transition_id}},
    }
    return subprocess.run([sys.executable, HOOK], input=json.dumps(payload),
                          capture_output=True, text=True, env=env)


def sentinel(home, issue="AGENT-14", project="AGENT"):
    d = os.path.join(str(home), ".claude", "logs", "verified", project)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, issue + ".verified")
    with open(path, "w") as fh:
        fh.write("pytest tests/auth -q PASSED\n")
    return path


def journal(home, project, events):
    d = os.path.join(str(home), ".claude", "logs", "jira-mirror")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, project + ".jsonl"), "a") as fh:
        for event in events:
            fh.write(json.dumps(event) + "\n")


def test_blocks_in_review_without_sentinel(tmp_path):
    env, home = setup_env(tmp_path)
    proc = run_hook(env, transition_id="31")
    assert proc.returncode == 2
    assert "sentinel" in proc.stderr.lower()
    assert "AGENT-14.verified" in proc.stderr


def test_blocks_done_without_sentinel(tmp_path):
    env, home = setup_env(tmp_path)
    assert run_hook(env, transition_id="41").returncode == 2


def test_block_message_covers_both_the_implementer_and_reviewer_case(tmp_path):
    """Transitions are any->any (no workflow ordering in a team-managed project),
    so an implementer can go To Do -> Done directly and a reviewer can hit this
    same block while closing after a PASS verdict. The message must not assume
    either actor -- it must not tell a reviewer transitioning straight to Done
    to go run the issue's `Run:` command, which they never ran. Covers both
    routes to the same gated transition (41 = Done) since the message text does
    not depend on which transition id triggered it."""
    env, home = setup_env(tmp_path)
    proc = run_hook(env, transition_id="41")  # e.g. To Do -> Done in one call
    assert proc.returncode == 2
    lower = proc.stderr.lower()
    assert "run:` command" in lower or "run:" in lower  # implementer case
    assert "reviewer" in lower and "verdict" in lower    # reviewer case
    assert "mkdir -p" in proc.stderr
    assert "skip-verify" in proc.stderr


def test_allows_in_review_with_sentinel(tmp_path):
    env, home = setup_env(tmp_path)
    sentinel(home)
    assert run_hook(env, transition_id="31").returncode == 0


def test_consumes_the_sentinel_so_it_cannot_be_reused(tmp_path):
    env, home = setup_env(tmp_path)
    path = sentinel(home)
    assert run_hook(env, transition_id="31").returncode == 0
    assert not os.path.exists(path)
    assert run_hook(env, transition_id="31").returncode == 2


def test_allows_ungated_transitions_without_sentinel(tmp_path):
    env, home = setup_env(tmp_path)
    assert run_hook(env, transition_id="21").returncode == 0, "In Progress is not gated"


def test_skip_verify_label_bypasses_the_gate(tmp_path):
    env, home = setup_env(tmp_path)
    journal(home, "AGENT", [{"op": "create", "key": "AGENT-14",
                             "labels": ["role-sa", "skip-verify"], "status": "To Do"}])
    assert run_hook(env, transition_id="31").returncode == 0


def test_ignores_other_projects(tmp_path):
    env, home = setup_env(tmp_path)
    assert run_hook(env, issue="SCRUM-2", transition_id="41").returncode == 0


def test_empty_gated_statuses_is_honoured_not_treated_as_unconfigured(tmp_path):
    """`cfg.get(...) or DEFAULT` cannot tell "no gated statuses exist on this
    board" from "nobody configured this". Falling back to the defaults there
    gates two status names the board does not have, so nothing is really gated
    while the audit log claims otherwise. An explicit [] means [] -- and
    bootstrap is where an empty gate set must fail loudly (exit 5).
    """
    config = dict(CONFIG, gatedStatuses=[])
    env, home = setup_env(tmp_path, config)
    assert run_hook(env, transition_id="41").returncode == 0, \
        "an explicitly empty gate set gates nothing -- it is not a fallback trigger"


def test_missing_gated_statuses_key_still_falls_back_to_the_defaults(tmp_path):
    config = {"projectKey": "AGENT", "transitions": {"41": "Done"}}
    env, home = setup_env(tmp_path, config)
    assert run_hook(env, transition_id="41").returncode == 2, \
        "genuinely unconfigured must keep gating the default statuses"


def test_non_list_gated_statuses_falls_back_to_the_defaults(tmp_path):
    config = dict(CONFIG, gatedStatuses="Done")
    env, home = setup_env(tmp_path, config)
    assert run_hook(env, transition_id="41").returncode == 2, \
        "a malformed value is unconfigured, not a licence to gate nothing"


def test_fails_open_on_unknown_transition_id(tmp_path):
    env, home = setup_env(tmp_path)
    assert run_hook(env, transition_id="99").returncode == 0, \
        "an unmapped id means unknown target, and unknown must not block"


def test_fails_open_when_config_missing(tmp_path):
    env = dict(os.environ, HOME=str(tmp_path / "home"),
               JIRA_CONFIG_PATH=str(tmp_path / "absent.json"))
    assert run_hook(env, transition_id="41").returncode == 0


def test_ignores_unrelated_tools(tmp_path):
    env, home = setup_env(tmp_path)
    assert run_hook(env, tool_name="Bash").returncode == 0


def test_fails_open_on_garbage_payload(tmp_path):
    env, home = setup_env(tmp_path)
    proc = subprocess.run([sys.executable, HOOK], input="not json",
                          capture_output=True, text=True, env=env)
    assert proc.returncode == 0


def test_sentinel_path_cannot_traverse_out_of_verified_dir(tmp_path):
    env, home = setup_env(tmp_path)
    proc = run_hook(env, issue="AGENT-../../../../etc/passwd", transition_id="31")
    # Project prefix no longer matches AGENT, so it is ignored; either way it
    # must not touch anything outside the verified dir.
    assert proc.returncode == 0
    assert os.path.exists("/etc/passwd"), "a traversal must never reach a real path"
