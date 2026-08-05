"""A transition into a gated status requires a consumed sentinel."""
import json
import os
import subprocess
import time
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


FINALIZE = os.path.join(REPO, ".claude", "hooks", "jira_transition_sentinel_finalize.py")


def run_finalize(env, issue="AGENT-14", response="Issue transitioned successfully"):
    payload = {
        "tool_name": TRANSITION,
        "tool_input": {"issueIdOrKey": issue, "transition": {"id": "31"}},
        "tool_response": response,
    }
    return subprocess.run([sys.executable, FINALIZE], input=json.dumps(payload),
                          capture_output=True, text=True, env=env)


def inflight_of(path):
    return path[: -len(".verified")] + ".inflight"


def test_consumes_the_sentinel_so_it_cannot_be_reused(tmp_path):
    """One sentinel authorizes one SUCCESSFUL transition. The PreToolUse rename
    is the consume -- it takes effect before the MCP call, so a second
    concurrent transition is blocked -- and PostToolUse spends it on success."""
    env, home = setup_env(tmp_path)
    path = sentinel(home)
    assert run_hook(env, transition_id="31").returncode == 0
    assert not os.path.exists(path), "the .verified must be consumed immediately"
    assert os.path.exists(inflight_of(path)), "consumed means in-flight, not gone"

    # A second transition while the first is in flight is blocked.
    assert run_hook(env, transition_id="31").returncode == 2

    assert run_finalize(env).returncode == 0
    assert not os.path.exists(inflight_of(path)), "success spends the sentinel"
    assert run_hook(env, transition_id="31").returncode == 2


def test_a_failed_transition_restores_the_sentinel(tmp_path):
    """The regression this whole two-phase design exists for. Observed live on
    AGENT-11: the gate allowed and destroyed the sentinel, the MCP call never
    landed, and the retry was blocked with 'no sentinel' -- which reads to an
    agent as a verification failure, not an API failure."""
    env, home = setup_env(tmp_path)
    path = sentinel(home)
    assert run_hook(env, transition_id="31").returncode == 0

    assert run_finalize(env, response="Error: 401 Unauthorized").returncode == 0
    assert os.path.exists(path), "a failed transition must leave the sentinel usable"
    assert not os.path.exists(inflight_of(path))
    assert run_hook(env, transition_id="31").returncode == 0, "the retry must pass"


def test_an_error_string_is_not_treated_as_success(tmp_path):
    """jira_mirror_journal._succeeded() counts any non-empty string as success,
    and a bare string is the live tool_response shape. Reusing it here would
    make the finalizer a no-op for exactly the case it targets."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("finalize", FINALIZE)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["finalize"] = mod
    spec.loader.exec_module(mod)

    assert mod.transition_succeeded("Issue transitioned") is True
    assert mod.transition_succeeded({"status": "ok"}) is True
    assert mod.transition_succeeded("Error: could not transition") is False
    assert mod.transition_succeeded("Request failed with 403") is False
    assert mod.transition_succeeded({"errorMessages": ["nope"]}) is False
    assert mod.transition_succeeded("") is False
    assert mod.transition_succeeded(None) is False


def test_evidence_free_response_restores_rather_than_spends(tmp_path):
    """A response carrying no evidence either way must resolve to restore: a
    wrongly-restored sentinel costs at most one extra authorized transition, a
    wrongly-spent one wedges the issue -- which is the failure actually observed.

    Note the deliberate limit: a STRUCTURED response with no error key still
    counts as success, matching jira_mirror_journal's live-verified convention.
    Demanding a positive success key there would restore genuinely-spent
    sentinels and break single-use, since the response shape is not contractual."""
    env, home = setup_env(tmp_path)
    path = sentinel(home)
    assert run_hook(env, transition_id="31").returncode == 0
    assert run_finalize(env, response=[]).returncode == 0
    assert os.path.exists(path), "an evidence-free response must restore the sentinel"


def test_stale_inflight_is_reclaimable_when_the_tool_never_ran(tmp_path):
    """If the call is denied or cancelled, PostToolUse never fires and the
    .inflight would otherwise wedge the issue forever."""
    env, home = setup_env(tmp_path)
    path = sentinel(home)
    assert run_hook(env, transition_id="31").returncode == 0
    inflight = inflight_of(path)
    assert os.path.exists(inflight)

    assert run_hook(env, transition_id="31").returncode == 2, "fresh in-flight blocks"
    old = time.time() - 3600
    os.utime(inflight, (old, old))
    assert run_hook(env, transition_id="31").returncode == 0, "stale in-flight is reclaimable"


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


def _finalize_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location("finalize_mod", FINALIZE)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["finalize_mod"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_a_json_object_serialized_as_a_string_is_decoded_not_keyword_scanned():
    """The live wire carries JSON-as-a-string. Falling through to the prose
    regex keyword-scans the issue's OWN embedded text, and both directions of
    that mistake are damaging -- verified against real recorded responses."""
    mod = _finalize_module()

    # FAILURE misread as success -> the sentinel is deleted for a transition
    # that never landed. This is the AGENT-11 wedge. Note "errorMessages"
    # contains no standalone word "error", so the prose regex misses it.
    assert mod.transition_succeeded('{"errorMessages": ["Issue does not exist"]}') is False
    assert mod.transition_succeeded('{"success": false}') is False
    assert mod.transition_succeeded({"success": False}) is False

    # SUCCESS misread as failure -> a spent sentinel is resurrected and a second
    # gated transition is authorized with no re-verification. "512" here comes
    # from an issue description ("memory 512 MB"), not an HTTP status.
    assert mod.transition_succeeded(
        '{"success": true, "fields": {"description": "memory 512 MB, timeout 10 s"}}'
    ) is True
    assert mod.transition_succeeded('{"success": true}') is True


def test_prose_screening_still_applies_to_genuinely_unstructured_text():
    mod = _finalize_module()
    assert mod.transition_succeeded("Issue transitioned") is True
    assert mod.transition_succeeded("Error: could not transition") is False
    assert mod.transition_succeeded("not valid json {") is True


def test_a_failure_encoded_as_a_json_string_restores_the_sentinel(tmp_path):
    """End-to-end on the shape that matters, through both real hooks."""
    env, home = setup_env(tmp_path)
    path = sentinel(home)
    assert run_hook(env, transition_id="31").returncode == 0
    assert run_finalize(
        env, response='{"errorMessages": ["Issue does not exist"]}').returncode == 0
    assert os.path.exists(path), "a failed transition must leave the sentinel usable"
    assert run_hook(env, transition_id="31").returncode == 0, "the retry must pass"
