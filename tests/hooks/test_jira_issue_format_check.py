"""Exit 0 allows creation; exit 2 blocks it and feeds stderr back to the model."""
import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HOOK = os.path.join(REPO, ".claude", "hooks", "jira_issue_format_check.py")
CREATE = "mcp__plugin_atlassian_atlassian__createJiraIssue"

GOOD_DESCRIPTION = (
    "Spec: .claude/specs/auth-api/spec.md#login\n"
    "Files: src/auth/login.py, tests/auth/test_login.py\n"
    "Acceptance: POST /login returns 200 + AuthToken on valid creds, 401 otherwise\n"
    "Run: pytest tests/auth/test_login.py -q"
)


def run_hook(tool_input, tmp_path, tool_name=CREATE, config=None):
    cfg = tmp_path / "jira-config.json"
    cfg.write_text(json.dumps(config or {"projectKey": "AGENT"}))
    env = dict(os.environ, HOME=str(tmp_path / "home"), JIRA_CONFIG_PATH=str(cfg))
    payload = {"tool_name": tool_name, "tool_input": tool_input}
    return subprocess.run([sys.executable, HOOK], input=json.dumps(payload),
                          capture_output=True, text=True, env=env)


def well_formed(**overrides):
    issue = {
        "projectKey": "AGENT",
        "issueTypeName": "Task",
        "summary": "[coding] implement POST /login handler",
        "description": GOOD_DESCRIPTION,
        "additional_fields": {"labels": ["spec-auth-api", "role-coding", "group-2"]},
    }
    issue.update(overrides)
    return issue


def test_allows_well_formed_issue(tmp_path):
    assert run_hook(well_formed(), tmp_path).returncode == 0


def test_blocks_missing_role_tag_in_summary(tmp_path):
    proc = run_hook(well_formed(summary="implement POST /login handler"), tmp_path)
    assert proc.returncode == 2
    assert "role tag" in proc.stderr.lower()


def test_blocks_missing_run_command(tmp_path):
    description = GOOD_DESCRIPTION.replace("Run: pytest tests/auth/test_login.py -q", "")
    proc = run_hook(well_formed(description=description), tmp_path)
    assert proc.returncode == 2
    assert "Run:" in proc.stderr


def test_blocks_missing_files_and_acceptance(tmp_path):
    proc = run_hook(well_formed(description="Run: pytest -q"), tmp_path)
    assert proc.returncode == 2
    assert "Files:" in proc.stderr
    assert "Acceptance:" in proc.stderr


def test_blocks_missing_role_label(tmp_path):
    proc = run_hook(well_formed(additional_fields={"labels": ["spec-auth-api"]}), tmp_path)
    assert proc.returncode == 2
    assert "role-" in proc.stderr


def test_blocks_missing_spec_label(tmp_path):
    proc = run_hook(well_formed(additional_fields={"labels": ["role-coding"]}), tmp_path)
    assert proc.returncode == 2
    assert "spec-" in proc.stderr


def test_blocks_role_label_disagreeing_with_summary_tag(tmp_path):
    proc = run_hook(well_formed(
        summary="[devops] terraform the secrets store",
        additional_fields={"labels": ["spec-auth-api", "role-coding"]},
    ), tmp_path)
    assert proc.returncode == 2
    assert "disagree" in proc.stderr.lower()


def test_bypass_label_allows_anything(tmp_path):
    proc = run_hook(well_formed(
        summary="coordinate the group-2 handoff",
        description="no structure here",
        additional_fields={"labels": ["skip-format-check"]},
    ), tmp_path)
    assert proc.returncode == 0


def test_epics_are_exempt(tmp_path):
    proc = run_hook(well_formed(
        issueTypeName="Epic",
        summary="auth-api",
        description="Spec: .claude/specs/auth-api/spec.md",
        additional_fields={"labels": ["spec-auth-api"]},
    ), tmp_path)
    assert proc.returncode == 0


def test_ignores_issues_in_other_projects(tmp_path):
    proc = run_hook(well_formed(projectKey="SCRUM", summary="MUFG - SCCM",
                                description="", additional_fields={}), tmp_path)
    assert proc.returncode == 0, "must never police the operator's own project"


def test_ignores_unrelated_tools(tmp_path):
    proc = run_hook({"command": "ls"}, tmp_path, tool_name="Bash")
    assert proc.returncode == 0


def test_fails_open_when_config_missing(tmp_path):
    env = dict(os.environ, HOME=str(tmp_path / "home"),
               JIRA_CONFIG_PATH=str(tmp_path / "absent.json"))
    payload = {"tool_name": CREATE, "tool_input": well_formed(summary="no tag")}
    proc = subprocess.run([sys.executable, HOOK], input=json.dumps(payload),
                          capture_output=True, text=True, env=env)
    assert proc.returncode == 0, "unconfigured means unknown, and unknown must not block"


def test_fails_open_on_garbage_payload(tmp_path):
    env = dict(os.environ, HOME=str(tmp_path / "home"))
    proc = subprocess.run([sys.executable, HOOK], input="not json",
                          capture_output=True, text=True, env=env)
    assert proc.returncode == 0
