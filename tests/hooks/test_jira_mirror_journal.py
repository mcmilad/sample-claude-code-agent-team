"""The journaller must record successful mutations, ignore failures and other
projects, and always exit 0 -- it is observational and must never block."""
import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HOOK = os.path.join(REPO, ".claude", "hooks", "jira_mirror_journal.py")
TOOL = "mcp__plugin_atlassian_atlassian__"


def run_hook(payload, home):
    env = dict(os.environ, HOME=str(home))
    proc = subprocess.run(
        [sys.executable, HOOK], input=json.dumps(payload),
        capture_output=True, text=True, env=env,
    )
    return proc


def write_config(tmp_path, monkeypatch):
    cfg = tmp_path / "jira-config.json"
    cfg.write_text(json.dumps({"projectKey": "AGENT"}))
    monkeypatch.setenv("JIRA_CONFIG_PATH", str(cfg))
    return cfg


def read_journal(home, project="AGENT"):
    path = os.path.join(str(home), ".claude", "logs", "jira-mirror", project + ".jsonl")
    if not os.path.exists(path):
        return []
    with open(path) as fh:
        return [json.loads(line) for line in fh if line.strip()]


def test_records_created_issue(tmp_path, monkeypatch):
    write_config(tmp_path, monkeypatch)
    home = tmp_path / "home"
    proc = run_hook({
        "tool_name": TOOL + "createJiraIssue",
        "tool_input": {
            "projectKey": "AGENT",
            "summary": "[coding] impl login",
            "additional_fields": {"labels": ["role-coding", "spec-auth"]},
        },
        "tool_response": {"key": "AGENT-14"},
    }, home)
    assert proc.returncode == 0
    events = read_journal(home)
    assert len(events) == 1
    assert events[0]["key"] == "AGENT-14"
    assert events[0]["summary"] == "[coding] impl login"
    assert events[0]["labels"] == ["role-coding", "spec-auth"]
    assert events[0]["status"] == "To Do"


def read_audit(home):
    path = os.path.join(str(home), ".claude", "logs", "team-hooks.jsonl")
    if not os.path.exists(path):
        return []
    with open(path) as fh:
        return [json.loads(line) for line in fh if line.strip()]


def test_stringified_additional_fields_still_journals_the_create(tmp_path, monkeypatch):
    """`(x or {}).get(...)` raises AttributeError when a model emits
    additional_fields as a JSON string; the fail-open handler then swallows it
    and NO create event is written. Since only a create event ever writes
    status 'To Do', that issue becomes permanently invisible to the idle
    work-check -- even after later edits restore its labels.
    """
    write_config(tmp_path, monkeypatch)
    home = tmp_path / "home"
    proc = run_hook({
        "tool_name": TOOL + "createJiraIssue",
        "tool_input": {
            "projectKey": "AGENT",
            "summary": "[coding] impl login",
            "additional_fields": '{"labels": ["role-coding"]}',
        },
        "tool_response": {"key": "AGENT-14"},
    }, home)
    assert proc.returncode == 0
    events = read_journal(home)
    assert len(events) == 1, "the create must be journalled even if labels are unreadable"
    assert events[0]["key"] == "AGENT-14"
    assert events[0]["status"] == "To Do"


def test_accepts_a_wrapped_issue_key_in_the_response(tmp_path, monkeypatch):
    """The create path rests on an unverified tool_response shape. If the
    harness wraps the MCP result, _succeeded still says True and the key is
    None, so nothing is journalled while the hook looks installed."""
    write_config(tmp_path, monkeypatch)
    home = tmp_path / "home"
    run_hook({
        "tool_name": TOOL + "createJiraIssue",
        "tool_input": {"projectKey": "AGENT", "summary": "[coding] impl login"},
        "tool_response": {"issue": {"key": "AGENT-14"}},
    }, home)
    assert read_journal(home)[0]["key"] == "AGENT-14"


def test_accepts_a_key_shaped_top_level_id_when_no_key_field_is_present(tmp_path, monkeypatch):
    write_config(tmp_path, monkeypatch)
    home = tmp_path / "home"
    run_hook({
        "tool_name": TOOL + "createJiraIssue",
        "tool_input": {"projectKey": "AGENT", "summary": "[coding] impl login"},
        "tool_response": {"id": "AGENT-14"},
    }, home)
    assert read_journal(home)[0]["key"] == "AGENT-14"


def test_rejects_a_purely_numeric_top_level_id(tmp_path, monkeypatch):
    """A bare numeric `id` (e.g. the Jira REST numeric issue id, "10042") is not
    an issue key. Every later event for the issue is keyed by the real key
    (e.g. "AGENT-14") via issueIdOrKey, so journalling under the numeric id
    would create a second, permanently-'To Do' mirror entry that no agent can
    ever fetch or transition -- a phantom claimable issue. Treat a numeric-only
    id as no key found rather than accepting it.
    """
    write_config(tmp_path, monkeypatch)
    home = tmp_path / "home"
    run_hook({
        "tool_name": TOOL + "createJiraIssue",
        "tool_input": {"projectKey": "AGENT", "summary": "[coding] impl login"},
        "tool_response": {"id": "10042"},
    }, home)
    assert read_journal(home) == []
    reasons = [r.get("reason", "") for r in read_audit(home)]
    assert any("unrecognized create response shape" in r for r in reasons), reasons


def test_unextractable_create_key_is_distinguishable_in_the_audit_log(tmp_path, monkeypatch):
    """A create that succeeds but yields no key is the failure that makes the
    idle check nudge nobody. It must be diagnosable from team-hooks.jsonl, not
    indistinguishable from an ordinary skip."""
    write_config(tmp_path, monkeypatch)
    home = tmp_path / "home"
    run_hook({
        "tool_name": TOOL + "createJiraIssue",
        "tool_input": {"projectKey": "AGENT", "summary": "[coding] impl login"},
        "tool_response": {"self": "https://example.atlassian.net/rest/api/3/issue/10042"},
    }, home)
    assert read_journal(home) == []
    reasons = [r.get("reason", "") for r in read_audit(home)]
    assert any("unrecognized create response shape" in r for r in reasons), reasons


def test_records_transition_with_resolved_status(tmp_path, monkeypatch):
    cfg = tmp_path / "jira-config.json"
    cfg.write_text(json.dumps({"projectKey": "AGENT", "transitions": {"21": "In Progress"}}))
    monkeypatch.setenv("JIRA_CONFIG_PATH", str(cfg))
    home = tmp_path / "home"
    proc = run_hook({
        "tool_name": TOOL + "transitionJiraIssue",
        "tool_input": {"issueIdOrKey": "AGENT-14", "transition": {"id": "21"}},
        "tool_response": {"ok": True},
    }, home)
    assert proc.returncode == 0
    events = read_journal(home)
    assert events[0]["key"] == "AGENT-14"
    assert events[0]["status"] == "In Progress"


def test_records_label_edit(tmp_path, monkeypatch):
    write_config(tmp_path, monkeypatch)
    home = tmp_path / "home"
    run_hook({
        "tool_name": TOOL + "editJiraIssue",
        "tool_input": {
            "issueIdOrKey": "AGENT-14",
            "fields": {"labels": ["role-coding", "agent-coding-2"]},
        },
        "tool_response": {"ok": True},
    }, home)
    events = read_journal(home)
    assert events[0]["labels"] == ["role-coding", "agent-coding-2"]


def test_ignores_other_projects(tmp_path, monkeypatch):
    write_config(tmp_path, monkeypatch)
    home = tmp_path / "home"
    run_hook({
        "tool_name": TOOL + "editJiraIssue",
        "tool_input": {"issueIdOrKey": "SCRUM-2", "fields": {"labels": ["AGL"]}},
        "tool_response": {"ok": True},
    }, home)
    assert read_journal(home, "SCRUM") == []
    assert read_journal(home, "AGENT") == []


def test_ignores_failed_calls(tmp_path, monkeypatch):
    write_config(tmp_path, monkeypatch)
    home = tmp_path / "home"
    run_hook({
        "tool_name": TOOL + "createJiraIssue",
        "tool_input": {"projectKey": "AGENT", "summary": "[coding] x"},
        "tool_response": {"error": "field 'summary' is required"},
    }, home)
    assert read_journal(home) == []


def test_ignores_unrelated_tools(tmp_path, monkeypatch):
    write_config(tmp_path, monkeypatch)
    home = tmp_path / "home"
    proc = run_hook({
        "tool_name": "Bash", "tool_input": {"command": "ls"}, "tool_response": {},
    }, home)
    assert proc.returncode == 0
    assert read_journal(home) == []


def test_exits_zero_on_garbage_payload(tmp_path, monkeypatch):
    write_config(tmp_path, monkeypatch)
    home = tmp_path / "home"
    env = dict(os.environ, HOME=str(home))
    proc = subprocess.run([sys.executable, HOOK], input="not json",
                          capture_output=True, text=True, env=env)
    assert proc.returncode == 0
