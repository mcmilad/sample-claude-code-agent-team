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


def test_agent_label_handles_non_iterable_input():
    """Regression: agent_label must not raise on truthy non-iterables."""
    assert jira_mirror.agent_label(42) is None
    assert jira_mirror.agent_label(True) is None
    assert jira_mirror.agent_label({"key": "value"}) is None


def test_folded_issues_do_not_share_one_defaults_list(tmp_path, monkeypatch):
    """Regression: dict(_DEFAULTS) is a SHALLOW copy.

    Every folded issue held the *same* labels list and the same files list, so
    one consumer doing issue["labels"].append() rewrote every other issue and
    poisoned the module-level default for the rest of the process -- every
    later-folded issue then arrived carrying a phantom agent-* claim. The dict
    literal this replaced built fresh lists per call.

    _DEFAULTS is monkeypatched with an equivalent dict so that a reverted fix
    poisons a throwaway copy instead of the real module state for the rest of
    the run.
    """
    monkeypatch.setattr(jira_mirror, "MIRROR_DIR", str(tmp_path))
    monkeypatch.setattr(
        jira_mirror, "_DEFAULTS",
        {"summary": "", "labels": [], "status": "", "files": []},
    )
    jira_mirror.append_event("AGENT", {"op": "create", "key": "AGENT-1", "status": "To Do"})
    jira_mirror.append_event("AGENT", {"op": "create", "key": "AGENT-2", "status": "To Do"})

    state = jira_mirror.load_state("AGENT")
    assert state["AGENT-1"]["labels"] is not state["AGENT-2"]["labels"]
    assert state["AGENT-1"]["files"] is not state["AGENT-2"]["files"]

    state["AGENT-1"]["labels"].append("agent-coding-1")
    assert state["AGENT-2"]["labels"] == [], "issues must not alias one list"
    assert jira_mirror._DEFAULTS["labels"] == [], "the module default must survive"
    assert jira_mirror.load_state("AGENT")["AGENT-2"]["labels"] == [], \
        "a later fold must still see the issue as unclaimed"


def test_normalize_path_makes_both_sides_of_a_files_comparison_agree():
    """claim_gate compares os.path.relpath() output against journalled paths.
    Unnormalised, './src/a.py' equals nothing and that one file drops out of
    both the overlap check and the claim gate, silently."""
    assert jira_mirror.normalize_path("./src/a.py") == "src/a.py"
    assert jira_mirror.normalize_path("src//a.py") == "src/a.py"
    assert jira_mirror.normalize_path("  src/a.py  ") == "src/a.py"
    assert jira_mirror.normalize_path("src/") == "src"
    assert jira_mirror.normalize_path("src/a.py") == "src/a.py", "no-op on a clean path"
    # Best-effort, never raises: unusable values normalise to '' and are dropped.
    assert jira_mirror.normalize_path("") == ""
    assert jira_mirror.normalize_path(None) == ""


def test_load_state_skips_valid_json_non_object_lines(tmp_path, monkeypatch):
    """Regression: load_state must skip valid JSON that isn't an object."""
    monkeypatch.setattr(jira_mirror, "MIRROR_DIR", str(tmp_path))
    jira_mirror.append_event("AGENT", {"op": "create", "key": "AGENT-1", "status": "To Do"})
    # Append valid JSON that isn't a dict
    with open(jira_mirror.mirror_path("AGENT"), "a") as fh:
        fh.write("42\n")  # valid JSON, not an object
        fh.write("null\n")  # valid JSON, not an object
        fh.write("[1, 2, 3]\n")  # valid JSON, not an object
    jira_mirror.append_event("AGENT", {"op": "create", "key": "AGENT-2", "status": "To Do"})
    state = jira_mirror.load_state("AGENT")
    assert set(state) == {"AGENT-1", "AGENT-2"}, "should skip non-object JSON lines"
