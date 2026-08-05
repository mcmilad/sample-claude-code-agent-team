"""Nudge a teammate toward unclaimed work in its role before letting it idle.

Claimable means, in mirror state: status 'To Do', a role-<mine> label, and no
agent-* label. The two-nudge loop guard is preserved from the task-store
version -- without it a teammate can be trapped forever.
"""
import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HOOK = os.path.join(REPO, ".claude", "hooks", "teammate_idle_workcheck.py")

sys.path.insert(0, os.path.join(REPO, ".claude", "hooks"))
import teammate_idle_workcheck  # noqa: E402


def setup_env(tmp_path):
    cfg = tmp_path / "jira-config.json"
    cfg.write_text(json.dumps({"projectKey": "AGENT"}))
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    # CLAUDE_PROJECT_DIR must point at an isolated tree. Without it the hook
    # derives the repo root from its own location and resolves scope against
    # the developer's real checkout, so adding a jira-run.json to this repo
    # would silently change what these tests assert.
    project = tmp_path / "repo"
    (project / ".claude" / "specs").mkdir(parents=True, exist_ok=True)
    return dict(os.environ, HOME=str(home), JIRA_CONFIG_PATH=str(cfg),
                CLAUDE_PROJECT_DIR=str(project)), home


def journal(home, events, project="AGENT"):
    d = os.path.join(str(home), ".claude", "logs", "jira-mirror")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, project + ".jsonl"), "a") as fh:
        for event in events:
            fh.write(json.dumps(event) + "\n")


def run_hook(env, teammate="coding-2", team="auth-api"):
    payload = {"team_name": team, "teammate_name": teammate}
    return subprocess.run([sys.executable, HOOK], input=json.dumps(payload),
                          capture_output=True, text=True, env=env)


def test_nudges_when_unclaimed_role_work_exists(tmp_path):
    env, home = setup_env(tmp_path)
    journal(home, [{"op": "create", "key": "AGENT-14", "summary": "[coding] impl login",
                    "labels": ["role-coding", "spec-auth"], "status": "To Do"}])
    proc = run_hook(env)
    assert proc.returncode == 2
    assert "AGENT-14" in proc.stderr


def test_allows_idle_when_nothing_matches_role(tmp_path):
    env, home = setup_env(tmp_path)
    journal(home, [{"op": "create", "key": "AGENT-20", "summary": "[devops] terraform",
                    "labels": ["role-devops", "spec-auth"], "status": "To Do"}])
    assert run_hook(env, teammate="coding-2").returncode == 0


def test_allows_idle_when_work_is_already_claimed(tmp_path):
    env, home = setup_env(tmp_path)
    journal(home, [{"op": "create", "key": "AGENT-14",
                    "labels": ["role-coding", "agent-coding-1"], "status": "To Do"}])
    assert run_hook(env, teammate="coding-2").returncode == 0


def test_allows_idle_when_work_is_in_progress(tmp_path):
    env, home = setup_env(tmp_path)
    journal(home, [{"op": "create", "key": "AGENT-14", "labels": ["role-coding"],
                    "status": "To Do"},
                   {"op": "transition", "key": "AGENT-14", "status": "In Progress"}])
    assert run_hook(env, teammate="coding-2").returncode == 0


def test_allows_idle_after_two_nudges_for_the_same_set(tmp_path):
    env, home = setup_env(tmp_path)
    journal(home, [{"op": "create", "key": "AGENT-14", "labels": ["role-coding"],
                    "status": "To Do"}])
    assert run_hook(env).returncode == 2
    assert run_hook(env).returncode == 2
    assert run_hook(env).returncode == 0, "loop guard must release after MAX_NUDGES"


def test_nudge_counter_resets_when_the_claimable_set_changes(tmp_path):
    env, home = setup_env(tmp_path)
    journal(home, [{"op": "create", "key": "AGENT-14", "labels": ["role-coding"],
                    "status": "To Do"}])
    run_hook(env)
    run_hook(env)
    journal(home, [{"op": "create", "key": "AGENT-15", "labels": ["role-coding"],
                    "status": "To Do"}])
    assert run_hook(env).returncode == 2, "new work is a new set -- nudge again"


def test_allows_idle_for_unmapped_teammate_name(tmp_path):
    env, home = setup_env(tmp_path)
    journal(home, [{"op": "create", "key": "AGENT-14", "labels": ["role-coding"],
                    "status": "To Do"}])
    assert run_hook(env, teammate="mystery-bot").returncode == 0


def test_allows_idle_when_no_journal_exists(tmp_path):
    env, home = setup_env(tmp_path)
    assert run_hook(env).returncode == 0


def test_fails_open_on_garbage_payload(tmp_path):
    env, home = setup_env(tmp_path)
    proc = subprocess.run([sys.executable, HOOK], input="not json",
                          capture_output=True, text=True, env=env)
    assert proc.returncode == 0


def scoped_repo(tmp_path, slug="auth", group=None):
    """Give the isolated repo root a jira-run.json naming the live spec."""
    run_dir = tmp_path / "repo" / ".claude" / "specs" / slug
    run_dir.mkdir(parents=True, exist_ok=True)
    payload = {"epic": "AGENT-1"}
    if group is not None:
        payload["group"] = group
    (run_dir / "jira-run.json").write_text(json.dumps(payload))
    return str(tmp_path / "repo")


def test_out_of_scope_work_is_flagged_but_never_withheld(tmp_path):
    """Scope RANKS, it does not filter. An issue from another spec must still
    reach the teammate -- flagged -- because a filter that drops it turns a
    stale scope into silent work-abandonment."""
    env, home = setup_env(tmp_path)
    env["CLAUDE_PROJECT_DIR"] = scoped_repo(tmp_path, slug="auth")
    journal(home, [
        {"op": "create", "key": "AGENT-14", "labels": ["role-coding", "spec-auth"],
         "status": "To Do"},
        {"op": "create", "key": "AGENT-99", "labels": ["role-coding", "spec-other"],
         "status": "To Do"},
    ])
    proc = run_hook(env)
    assert proc.returncode == 2
    assert "AGENT-14" in proc.stderr and "AGENT-99" in proc.stderr, \
        "out-of-scope work must still be surfaced"
    assert "Outside it" in proc.stderr
    in_line = next(l for l in proc.stderr.splitlines() if "In the current scope" in l)
    assert "AGENT-14" in in_line and "AGENT-99" not in in_line


def test_an_issue_with_no_spec_label_is_never_dropped(tmp_path):
    """Absence is unknown, not mismatch. Dropping on a MISSING label is how a
    scoping conjunct silently starves a pool."""
    env, home = setup_env(tmp_path)
    env["CLAUDE_PROJECT_DIR"] = scoped_repo(tmp_path, slug="auth")
    journal(home, [{"op": "create", "key": "AGENT-77", "labels": ["role-coding"],
                    "status": "To Do"}])
    proc = run_hook(env)
    assert proc.returncode == 2
    assert "AGENT-77" in proc.stderr


def test_unresolvable_scope_degrades_to_unpartitioned(tmp_path):
    env, home = setup_env(tmp_path)
    env["CLAUDE_PROJECT_DIR"] = str(tmp_path / "no-such-repo")
    journal(home, [{"op": "create", "key": "AGENT-14", "labels": ["role-coding", "spec-x"],
                    "status": "To Do"}])
    proc = run_hook(env)
    assert proc.returncode == 2
    assert "AGENT-14" in proc.stderr
    assert "Outside it" not in proc.stderr


def test_reviewers_are_told_not_to_self_claim(tmp_path):
    """There is exactly one role-review card per sprint and the pool is
    partitioned by the lead's handoff. Handing four reviewers a self-claim
    recipe would manufacture the collision it was meant to prevent."""
    env, home = setup_env(tmp_path)
    journal(home, [{"op": "create", "key": "AGENT-40", "labels": ["role-review", "spec-auth"],
                    "status": "To Do"}])
    proc = run_hook(env, teammate="review-2")
    assert proc.returncode == 2
    assert "do NOT self-claim" in proc.stderr
    assert "mkdir" not in proc.stderr, "reviewers must not be given the claim recipe"


def test_nudge_teaches_the_lock_not_the_label_race(tmp_path):
    """The nudge is a fifth, runtime-injected copy of the claim protocol,
    delivered at the exact moment an agent claims. If it still taught the
    unreachable '>1 agent-* label' tie-break, fixing the docs would achieve
    nothing."""
    env, home = setup_env(tmp_path)
    journal(home, [{"op": "create", "key": "AGENT-14", "labels": ["role-coding"],
                    "status": "To Do"}])
    err = run_hook(env).stderr
    assert "mkdir ~/.claude/logs/claims/" in err
    assert "In Progress" in err
    assert "lowest instance name" not in err, "the unreachable tie-break must be gone"
    assert "issuelinks" in err and "must include labels" in err


def test_state_path_is_byte_identical_to_the_old_naive_join_for_legitimate_names():
    """SECURITY.md claims the idle hook sanitises its nudge-state filename with
    the same discipline as the sentinel path. The old implementation was just
    `"{}__{}".format(team, teammate).replace("/", "_")` -- a bare replace that
    left every other unsafe character untouched. Fixing it must not silently
    reset the two-nudge loop guard for every team/teammate pair already on
    disk, so for ordinary names the new path must resolve to exactly what the
    old formula produced.
    """
    team, teammate = "jira-smoke", "coding-1"
    old_formula = "{}__{}".format(team, teammate).replace("/", "_") + ".json"
    new_path = teammate_idle_workcheck._state_path(team, teammate)
    assert os.path.basename(new_path) == old_formula
    assert new_path == os.path.join(teammate_idle_workcheck.nudge_dir(), old_formula)


def test_state_path_crafted_component_cannot_escape_the_nudge_directory():
    nudge_dir = teammate_idle_workcheck.nudge_dir()
    path = teammate_idle_workcheck._state_path("../../../../tmp/evil", "../../etc/passwd")
    assert os.path.dirname(path) == nudge_dir, \
        "a crafted team/teammate must still resolve inside the nudge directory"
    basename = os.path.basename(path)
    assert ".." not in basename
    assert os.sep not in basename
