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


def test_records_created_issue_from_real_content_block_response(tmp_path, monkeypatch):
    """Live capture from a real createJiraIssue call: the harness delivers
    tool_response as a list of content blocks -- each a dict with `type` and
    `text`, where `text` is the issue JSON serialized as a *string* -- not
    the bare issue dict `_created_key` used to assume. Reproduced against the
    shipped (buggy) hook: exit 0, no journal file written, audit reason
    "unrecognized create response shape (keys: none)". This is the ground
    truth payload from that live capture, copied verbatim.
    """
    write_config(tmp_path, monkeypatch)
    home = tmp_path / "home"
    proc = run_hook({
        "tool_name": TOOL + "createJiraIssue",
        "tool_input": {
            "projectKey": "AGENT",
            "summary": "[coding] impl login",
            "additional_fields": {"labels": ["role-coding", "spec-auth"]},
        },
        "tool_response": [
            {"type": "text",
             "text": "{\n  \"id\": \"10074\",\n  \"key\": \"AGENT-1\",\n  "
                      "\"self\": \"https://api.atlassian.com/ex/jira/.../issue/10074\"\n}"}
        ],
    }, home)
    assert proc.returncode == 0
    events = read_journal(home)
    assert len(events) == 1
    assert events[0]["key"] == "AGENT-1"
    assert events[0]["labels"] == ["role-coding", "spec-auth"]
    assert events[0]["status"] == "To Do"


def test_content_block_response_carrying_error_messages_is_not_journalled(tmp_path, monkeypatch):
    """A content-block list whose text parses to an object carrying
    errorMessages is a failed call, not a success -- must not be journalled,
    unlike the old `bool(response)` check which counted any non-empty list
    as success regardless of content."""
    write_config(tmp_path, monkeypatch)
    home = tmp_path / "home"
    proc = run_hook({
        "tool_name": TOOL + "createJiraIssue",
        "tool_input": {"projectKey": "AGENT", "summary": "[coding] x"},
        "tool_response": [
            {"type": "text",
             "text": json.dumps({"errorMessages": ["summary is required"], "errors": {}})}
        ],
    }, home)
    assert proc.returncode == 0
    assert read_journal(home) == []


def test_content_block_response_with_invalid_json_text_is_diagnosable(tmp_path, monkeypatch):
    """A content-block list whose `text` is not valid JSON must journal
    nothing, exit 0, and still emit the distinguishable unrecognized-shape
    audit reason -- the same diagnosability the bare-dict case already had."""
    write_config(tmp_path, monkeypatch)
    home = tmp_path / "home"
    proc = run_hook({
        "tool_name": TOOL + "createJiraIssue",
        "tool_input": {"projectKey": "AGENT", "summary": "[coding] x"},
        "tool_response": [{"type": "text", "text": "not valid json {{{"}],
    }, home)
    assert proc.returncode == 0
    assert read_journal(home) == []
    reasons = [r.get("reason", "") for r in read_audit(home)]
    assert any("unrecognized create response shape" in r for r in reasons), reasons


def test_bare_dict_response_still_journals_after_content_block_support_added(tmp_path, monkeypatch):
    """Backward compatibility: the previously-assumed bare-issue-dict shape
    must keep working once content-block list support is added."""
    write_config(tmp_path, monkeypatch)
    home = tmp_path / "home"
    proc = run_hook({
        "tool_name": TOOL + "createJiraIssue",
        "tool_input": {"projectKey": "AGENT", "summary": "[coding] impl signup"},
        "tool_response": {"key": "AGENT-9"},
    }, home)
    assert proc.returncode == 0
    events = read_journal(home)
    assert len(events) == 1
    assert events[0]["key"] == "AGENT-9"
    assert events[0]["status"] == "To Do"


def test_content_block_response_with_only_numeric_id_is_rejected(tmp_path, monkeypatch):
    """The anti-poisoning guard (reject a bare numeric id, e.g. the Jira REST
    internal id "10074", as a key) must also hold when the id arrives inside
    a content-block list rather than a bare dict."""
    write_config(tmp_path, monkeypatch)
    home = tmp_path / "home"
    proc = run_hook({
        "tool_name": TOOL + "createJiraIssue",
        "tool_input": {"projectKey": "AGENT", "summary": "[coding] x"},
        "tool_response": [{"type": "text", "text": json.dumps({"id": "10074"})}],
    }, home)
    assert proc.returncode == 0
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


def test_create_with_a_transition_records_the_real_status(tmp_path, monkeypatch):
    """createJiraIssue exposes a top-level transition (verified against the live
    schema). Hardcoding 'To Do' mirrored an issue created straight into
    In Progress as unclaimed -- and since a create carries no agent-* label, the
    idle check then advertised it to every teammate of that role."""
    cfg = tmp_path / "jira-config.json"
    cfg.write_text(json.dumps({
        "projectKey": "AGENT",
        "transitions": {"11": "To Do", "21": "In Progress", "31": "In Review"},
    }))
    monkeypatch.setenv("JIRA_CONFIG_PATH", str(cfg))
    home = tmp_path / "home"
    run_hook({
        "tool_name": TOOL + "createJiraIssue",
        "tool_input": {
            "projectKey": "AGENT",
            "summary": "[coding] already underway",
            "transition": {"id": "21"},
            "additional_fields": {"labels": ["role-coding", "spec-x"]},
        },
        "tool_response": {"key": "AGENT-50"},
    }, home)
    events = [e for e in read_journal(home) if e.get("key") == "AGENT-50"]
    assert events and events[0]["status"] == "In Progress"


def test_create_without_a_transition_still_defaults_to_to_do(tmp_path, monkeypatch):
    write_config(tmp_path, monkeypatch)
    home = tmp_path / "home"
    run_hook({
        "tool_name": TOOL + "createJiraIssue",
        "tool_input": {"projectKey": "AGENT", "summary": "[coding] fresh",
                       "additional_fields": {"labels": ["role-coding", "spec-x"]}},
        "tool_response": {"key": "AGENT-51"},
    }, home)
    events = [e for e in read_journal(home) if e.get("key") == "AGENT-51"]
    assert events and events[0]["status"] == "To Do"


def test_create_journals_the_declared_files(tmp_path, monkeypatch):
    write_config(tmp_path, monkeypatch)
    home = tmp_path / "home"
    run_hook({
        "tool_name": TOOL + "createJiraIssue",
        "tool_input": {
            "projectKey": "AGENT",
            "summary": "[coding] impl",
            "description": ("Spec: x\nFiles: src/a.py, `src/b.py`\n"
                            "Acceptance: y\nRun: pytest -q"),
            "additional_fields": {"labels": ["role-coding", "spec-x"]},
        },
        "tool_response": {"key": "AGENT-52"},
    }, home)
    events = [e for e in read_journal(home) if e.get("key") == "AGENT-52"]
    assert events and events[0]["files"] == ["src/a.py", "src/b.py"]


def test_declared_files_are_journalled_normalised(tmp_path, monkeypatch):
    """claim_gate compares os.path.relpath() output against these strings, and
    the overlap check compares them to another issue's parsed list. Journalled
    raw, a './'-prefixed or doubled-slash path equals neither -- so that one
    file silently left both guardrails while the issue looked perfectly formed."""
    write_config(tmp_path, monkeypatch)
    home = tmp_path / "home"
    run_hook({
        "tool_name": TOOL + "createJiraIssue",
        "tool_input": {
            "projectKey": "AGENT",
            "summary": "[coding] impl",
            "description": ("Spec: x\nFiles: ./src/a.py, src//b.py, `./src/c.py` \n"
                            "Acceptance: y\nRun: pytest -q"),
            "additional_fields": {"labels": ["role-coding", "spec-x"]},
        },
        "tool_response": {"key": "AGENT-53"},
    }, home)
    events = [e for e in read_journal(home) if e.get("key") == "AGENT-53"]
    assert events and events[0]["files"] == ["src/a.py", "src/b.py", "src/c.py"]
