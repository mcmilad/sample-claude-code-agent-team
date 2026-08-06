"""Claim before you edit -- the enforcement layer behind the claim protocol.

The measured failure this exists for: 8 of 11 worked issues never entered
In Progress, so the lead's monitor JQL reported in-flight work as unstarted.
Prose alone did not move that number.

The hook cannot know WHO is writing -- a Write payload carries no teammate
identity -- so it checks a question it can answer from the journalled `Files:`
lists: is the issue that declares this file claimed at all?
"""
import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HOOK = os.path.join(REPO, ".claude", "hooks", "claim_gate.py")


def setup(tmp_path, subdir="project"):
    cfg = tmp_path / "jira-config.json"
    cfg.write_text(json.dumps({"projectKey": "AGENT"}))
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    project = tmp_path / subdir
    (project / "src").mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, HOME=str(home), JIRA_CONFIG_PATH=str(cfg),
               CLAUDE_PROJECT_DIR=str(project))
    env.pop("CLAUDE_CLAIM_GATE", None)
    return env, home, project


def journal(home, events, project="AGENT"):
    d = os.path.join(str(home), ".claude", "logs", "jira-mirror")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, project + ".jsonl"), "a") as fh:
        for event in events:
            fh.write(json.dumps(event) + "\n")


def lock(home, key, project="AGENT"):
    d = os.path.join(str(home), ".claude", "logs", "claims", project, key)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "owner"), "w") as fh:
        fh.write("coding-1")


def run(env, project, rel="src/login.py", session="s1", tool="Write"):
    payload = {
        "tool_name": tool,
        "session_id": session,
        "tool_input": {"file_path": str(project / rel)},
    }
    return subprocess.run([sys.executable, HOOK], input=json.dumps(payload),
                          capture_output=True, text=True, env=env)


def declared(key="AGENT-14", status="To Do", files=("src/login.py",)):
    return {"op": "create", "key": key, "status": status,
            "labels": ["role-coding", "spec-x"], "files": list(files)}


def test_blocks_editing_files_of_an_unclaimed_issue(tmp_path):
    env, home, project = setup(tmp_path)
    journal(home, [declared()])
    proc = run(env, project)
    assert proc.returncode == 2
    assert "AGENT-14" in proc.stderr
    assert "no lock is held for it" in proc.stderr
    assert 'mkdir "$CLAIMS/AGENT-14"' in proc.stderr
    assert "CLAUDE_CLAIM_GATE=off" in proc.stderr, "the bypass must be discoverable"


def test_the_printed_recipe_does_not_clobber_a_winner_on_a_lost_race(tmp_path):
    """The recipe is copy-pasted by whoever is blocked -- including a race loser.
    An unconditional `echo > owner` would overwrite the true owner's record and
    re-stamp its heartbeat, corrupting the one authoritative signal."""
    env, home, project = setup(tmp_path)
    journal(home, [declared()])
    err = run(env, project).stderr
    assert "if mkdir" in err and "else" in err
    assert "LOST" in err


def test_a_landed_sibling_does_not_exempt_an_unclaimed_declarer(tmp_path):
    """`return None` on the first landed declarer discarded every unclaimed one
    already collected. The journal is append-only, so the landed set only grows
    -- that would retire the gate one path at a time."""
    env, home, project = setup(tmp_path)
    journal(home, [
        declared(key="AGENT-14", status="To Do"),
        {"op": "create", "key": "AGENT-20", "status": "To Do",
         "labels": ["role-coding", "spec-x"], "files": ["src/login.py"]},
        {"op": "transition", "key": "AGENT-20", "status": "In Review"},
    ])
    proc = run(env, project)
    assert proc.returncode == 2, "a landed sibling must not launder an unclaimed issue"
    assert "AGENT-14" in proc.stderr


def test_holding_the_lock_allows_the_edit_even_when_the_board_lags(tmp_path):
    """The lock is authoritative and the mirror lags -- it only sees MCP
    mutations. Telling the true owner 'nobody has claimed it' is backwards."""
    env, home, project = setup(tmp_path)
    journal(home, [declared(status="To Do")])
    lock(home, "AGENT-14")
    assert run(env, project).returncode == 0


def test_a_locked_declarer_does_not_exempt_an_unclaimed_sibling(tmp_path):
    """The lock-held branch must `continue`, never `return None`.

    With a single declarer the two are indistinguishable, which is why this was
    unpinned. It takes two declarers of the same path -- one properly locked, one
    unclaimed -- to tell them apart: `continue` still reports the unclaimed one,
    while `return None` exempts the file entirely on the strength of a DIFFERENT
    issue's lock. The journal is append-only, so the exempting set only grows."""
    env, home, project = setup(tmp_path)
    journal(home, [
        {"op": "create", "key": "AGENT-A", "status": "In Progress",
         "labels": ["role-coding", "spec-x"], "files": ["src/login.py"]},
        {"op": "create", "key": "AGENT-B", "status": "To Do",
         "labels": ["role-coding", "spec-x"], "files": ["src/login.py"]},
    ])
    lock(home, "AGENT-A")
    proc = run(env, project)
    assert proc.returncode == 2, \
        "a locked declarer must not launder an unclaimed sibling's file"
    assert "AGENT-B" in proc.stderr


def test_the_stop_budget_is_keyed_on_the_issue_not_the_session(tmp_path):
    """session_id is shared by a whole team -- 13 teammates in a real run
    reported one. Keyed on the session, 'block once' means one stop for the
    entire fleet, which is indistinguishable from no gate."""
    env, home, project = setup(tmp_path)
    journal(home, [
        declared(key="AGENT-14", files=("src/login.py",)),
        {"op": "create", "key": "AGENT-21", "status": "To Do",
         "labels": ["role-coding", "spec-x"], "files": ["src/other.py"]},
    ])
    assert run(env, project, rel="src/login.py").returncode == 2
    assert run(env, project, rel="src/login.py").returncode == 0, "one stop per issue"
    assert run(env, project, rel="src/other.py").returncode == 2, \
        "a DIFFERENT unclaimed issue must still earn its own stop"


def test_notebook_edit_is_actually_gated(tmp_path):
    """settings.json matches NotebookEdit, but its payload names the target
    notebook_path -- reading only file_path made the hook inert for notebooks
    while advertising that it covered them."""
    env, home, project = setup(tmp_path)
    journal(home, [declared(files=("nb/analysis.ipynb",))])
    payload = {"tool_name": "NotebookEdit", "session_id": "s1",
               "tool_input": {"notebook_path": str(project / "nb/analysis.ipynb")}}
    proc = subprocess.run([sys.executable, HOOK], input=json.dumps(payload),
                          capture_output=True, text=True, env=env)
    assert proc.returncode == 2


def test_blocks_when_in_progress_but_no_lock_was_taken(tmp_path):
    """A label-only claim provides no mutual exclusion: editJiraIssue has no
    compare-and-swap and transitions are global, so neither can arbitrate."""
    env, home, project = setup(tmp_path)
    journal(home, [declared(), {"op": "transition", "key": "AGENT-14",
                                "status": "In Progress"}])
    proc = run(env, project)
    assert proc.returncode == 2
    assert "holds no claim lock" in proc.stderr


def test_allows_when_properly_claimed(tmp_path):
    env, home, project = setup(tmp_path)
    journal(home, [declared(), {"op": "transition", "key": "AGENT-14",
                                "status": "In Progress"}])
    lock(home, "AGENT-14")
    assert run(env, project).returncode == 0


def test_allows_files_no_issue_declares(tmp_path):
    env, home, project = setup(tmp_path)
    journal(home, [declared(files=("src/other.py",))])
    assert run(env, project, rel="src/login.py").returncode == 0


def test_allows_follow_up_edits_after_the_work_landed(tmp_path):
    env, home, project = setup(tmp_path)
    journal(home, [declared(), {"op": "transition", "key": "AGENT-14",
                                "status": "In Review"}])
    assert run(env, project).returncode == 0


def test_never_gates_the_meta_work(tmp_path):
    """Editing the rules and hooks IS the meta-work. Gating it would make this
    guardrail impossible to repair from inside a session."""
    env, home, project = setup(tmp_path)
    journal(home, [declared(files=(".claude/hooks/claim_gate.py",))])
    (project / ".claude" / "hooks").mkdir(parents=True, exist_ok=True)
    assert run(env, project, rel=".claude/hooks/claim_gate.py").returncode == 0


def test_a_checkout_under_a_build_directory_is_still_gated(tmp_path):
    """Exclusions match the repo-RELATIVE path, never the absolute one.

    Matched against the absolute path, a repo that merely *lives* under a
    directory named build/dist/venv made every file in it look like build
    output, so the gate allowed every write -- and that allow() carries no
    event, so not even an audit record was left behind.
    """
    env, home, project = setup(tmp_path, subdir="build/checkout")
    journal(home, [declared()])
    proc = run(env, project)
    assert proc.returncode == 2, "the checkout location must not disable the gate"
    assert "AGENT-14" in proc.stderr


def test_build_output_inside_the_repo_is_still_never_gated(tmp_path):
    """The intent of the exclusions is unchanged: generated output *within* the
    repo is not board work."""
    env, home, project = setup(tmp_path)
    journal(home, [declared(files=("build/bundle.js",))])
    assert run(env, project, rel="build/bundle.js").returncode == 0


def test_a_declared_path_in_another_shape_still_gates_the_file(tmp_path):
    """The journal is never migrated, so it holds paths recorded before the
    journaller normalised them. claim_gate compares os.path.relpath() output;
    against a raw './src/login.py' that comparison silently matched nothing and
    the file dropped out of the gate entirely."""
    env, home, project = setup(tmp_path)
    journal(home, [declared(files=("./src/login.py",))])
    proc = run(env, project, rel="src/login.py")
    assert proc.returncode == 2
    assert "AGENT-14" in proc.stderr


def test_blocks_only_once_for_the_same_issue(tmp_path):
    """A guardrail that traps a session is worse than no guardrail -- and the
    mirror is best-effort, so it can be wrong."""
    env, home, project = setup(tmp_path)
    journal(home, [declared()])
    assert run(env, project).returncode == 2
    assert run(env, project).returncode == 0
    assert run(env, project, rel="src/login.py").returncode == 0


def test_sessions_are_independent(tmp_path):
    env, home, project = setup(tmp_path)
    journal(home, [declared()])
    assert run(env, project, session="one").returncode == 2
    assert run(env, project, session="two").returncode == 2


def test_covers_edit_as_well_as_write(tmp_path):
    env, home, project = setup(tmp_path)
    journal(home, [declared()])
    assert run(env, project, tool="Edit").returncode == 2


def test_ignores_paths_outside_the_project(tmp_path):
    env, home, project = setup(tmp_path)
    journal(home, [declared()])
    payload = {"tool_name": "Write", "session_id": "s1",
               "tool_input": {"file_path": "/tmp/elsewhere/src/login.py"}}
    proc = subprocess.run([sys.executable, HOOK], input=json.dumps(payload),
                          capture_output=True, text=True, env=env)
    assert proc.returncode == 0


def test_env_bypass(tmp_path):
    env, home, project = setup(tmp_path)
    env["CLAUDE_CLAIM_GATE"] = "off"
    journal(home, [declared()])
    assert run(env, project).returncode == 0


def test_tolerates_journal_events_predating_the_files_field(tmp_path):
    """The journal is never versioned or migrated. An event without `files`
    must yield the default, not raise into the fail-open handler and silently
    disable the gate."""
    env, home, project = setup(tmp_path)
    journal(home, [{"op": "create", "key": "AGENT-9", "status": "To Do",
                    "labels": ["role-coding"], "summary": "[coding] old"}])
    assert run(env, project).returncode == 0


def test_fails_open_on_garbage_payload(tmp_path):
    env, home, project = setup(tmp_path)
    proc = subprocess.run([sys.executable, HOOK], input="not json",
                          capture_output=True, text=True, env=env)
    assert proc.returncode == 0


def test_fails_open_when_config_is_missing(tmp_path):
    env, home, project = setup(tmp_path)
    env["JIRA_CONFIG_PATH"] = str(tmp_path / "nope.json")
    journal(home, [declared()])
    assert run(env, project).returncode == 0
