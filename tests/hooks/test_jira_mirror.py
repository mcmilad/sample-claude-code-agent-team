import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, ".claude", "hooks"))

import jira_mirror  # noqa: E402


def test_load_config_returns_empty_dict_when_file_missing(tmp_path):
    assert jira_mirror.load_config(str(tmp_path / "nope.json")) == {}


def test_load_config_returns_empty_dict_on_invalid_json(tmp_path):
    bad = tmp_path / "jira-config.json"
    bad.write_text("{not json")
    assert jira_mirror.load_config(str(bad)) == {}


def test_load_config_reads_discovered_ids(tmp_path):
    cfg = tmp_path / "jira-config.json"
    cfg.write_text(json.dumps({"projectKey": "AGENT", "fields": {"sprint": "customfield_10020"}}))
    loaded = jira_mirror.load_config(str(cfg))
    assert loaded["projectKey"] == "AGENT"
    assert loaded["fields"]["sprint"] == "customfield_10020"


def test_issue_project_extracts_key_prefix():
    assert jira_mirror.issue_project("AGENT-14") == "AGENT"
    assert jira_mirror.issue_project("SCRUM-2") == "SCRUM"


def test_issue_project_returns_none_for_numeric_id():
    assert jira_mirror.issue_project("10073") is None


def test_agent_label_finds_claimant():
    assert jira_mirror.agent_label(["role-coding", "agent-coding-2"]) == "agent-coding-2"


def test_agent_label_returns_none_when_unclaimed():
    assert jira_mirror.agent_label(["role-coding", "group-1"]) is None


def test_append_then_load_state_reconstructs_issue(tmp_path, monkeypatch):
    monkeypatch.setattr(jira_mirror, "MIRROR_DIR", str(tmp_path))
    jira_mirror.append_event("AGENT", {
        "op": "create", "key": "AGENT-14",
        "summary": "[coding] impl login", "labels": ["role-coding", "spec-auth"],
        "status": "To Do",
    })
    state = jira_mirror.load_state("AGENT")
    assert state["AGENT-14"]["summary"] == "[coding] impl login"
    assert state["AGENT-14"]["status"] == "To Do"
    assert "role-coding" in state["AGENT-14"]["labels"]


def test_load_state_folds_later_events_over_earlier(tmp_path, monkeypatch):
    monkeypatch.setattr(jira_mirror, "MIRROR_DIR", str(tmp_path))
    jira_mirror.append_event("AGENT", {
        "op": "create", "key": "AGENT-14", "summary": "[coding] impl login",
        "labels": ["role-coding"], "status": "To Do",
    })
    jira_mirror.append_event("AGENT", {
        "op": "edit", "key": "AGENT-14", "labels": ["role-coding", "agent-coding-2"],
    })
    jira_mirror.append_event("AGENT", {
        "op": "transition", "key": "AGENT-14", "status": "In Progress",
    })
    issue = jira_mirror.load_state("AGENT")["AGENT-14"]
    assert issue["status"] == "In Progress"
    assert issue["labels"] == ["role-coding", "agent-coding-2"]
    assert issue["summary"] == "[coding] impl login", "absent fields must not clobber known ones"


def test_load_state_skips_corrupt_lines(tmp_path, monkeypatch):
    monkeypatch.setattr(jira_mirror, "MIRROR_DIR", str(tmp_path))
    jira_mirror.append_event("AGENT", {"op": "create", "key": "AGENT-1", "status": "To Do"})
    with open(jira_mirror.mirror_path("AGENT"), "a") as fh:
        fh.write("{{{ truncated\n")
    jira_mirror.append_event("AGENT", {"op": "create", "key": "AGENT-2", "status": "To Do"})
    state = jira_mirror.load_state("AGENT")
    assert set(state) == {"AGENT-1", "AGENT-2"}


def test_load_state_returns_empty_when_no_journal(tmp_path, monkeypatch):
    monkeypatch.setattr(jira_mirror, "MIRROR_DIR", str(tmp_path))
    assert jira_mirror.load_state("NOPE") == {}
