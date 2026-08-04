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
