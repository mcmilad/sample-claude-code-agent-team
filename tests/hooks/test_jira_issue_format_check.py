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


def test_stringified_additional_fields_does_not_defeat_the_check(tmp_path):
    """A model that emits additional_fields as a JSON *string* is a documented
    failure mode. `(x or {}).get(...)` raises AttributeError on it, the outer
    fail-open handler exits 0, and a malformed issue sails through the gate --
    the check must still see the summary/description problems.
    """
    proc = run_hook(well_formed(
        summary="implement POST /login handler",
        additional_fields='{"labels": ["spec-auth-api", "role-coding"]}',
    ), tmp_path)
    assert proc.returncode == 2
    assert "role tag" in proc.stderr.lower()


def test_stringified_additional_fields_is_treated_as_no_labels(tmp_path):
    """Labels the hook cannot parse are labels it does not have: an unreadable
    additional_fields must not smuggle a missing role-*/spec-* past the check.
    """
    proc = run_hook(well_formed(additional_fields='{"labels": ["role-coding"]}'), tmp_path)
    assert proc.returncode == 2
    assert "role-" in proc.stderr


def test_stringified_additional_fields_names_the_field_in_the_block_message(tmp_path):
    """A JSON-string additional_fields yields no labels, so the generic
    'no role-* label' / 'no spec-* label' problems fire -- but that message
    reads as if no labels were passed at all, when in fact labels were passed
    in the wrong shape (a string instead of an object). A model reading only
    'no role-* label' has every reason to retry the identical string shape.
    The block message must name additional_fields and say it must be a JSON
    object, not a string.
    """
    proc = run_hook(well_formed(additional_fields='{"labels": ["role-coding"]}'), tmp_path)
    assert proc.returncode == 2
    assert "additional_fields" in proc.stderr
    assert "object" in proc.stderr.lower()
    assert "string" in proc.stderr.lower()


def test_stringified_additional_fields_cannot_smuggle_a_bypass_label(tmp_path):
    proc = run_hook(well_formed(
        summary="no tag here",
        additional_fields='{"labels": ["skip-format-check"]}',
    ), tmp_path)
    assert proc.returncode == 2, "an unparseable bypass label is not a bypass"


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


def seed_journal(tmp_path, events, project="AGENT"):
    d = os.path.join(str(tmp_path / "home"), ".claude", "logs", "jira-mirror")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, project + ".jsonl"), "a") as fh:
        for event in events:
            fh.write(json.dumps(event) + "\n")


def test_blocks_files_overlapping_a_sibling_in_the_same_scope(tmp_path):
    """fullstack-agent.md calls sprint-wide Files: disjointness 'the sole
    guarantee against conflicts under the shared-tree pool model'. Since no
    atomic claim primitive is reachable through Jira, this is the last layer
    between a claim race and two agents clobbering the same file."""
    seed_journal(tmp_path, [{
        "op": "create", "key": "AGENT-14", "status": "To Do",
        "labels": ["spec-auth-api", "role-coding", "group-2"],
        "files": ["src/auth/login.py"],
    }])
    proc = run_hook(well_formed(), tmp_path)
    assert proc.returncode == 2
    assert "AGENT-14" in proc.stderr
    assert "src/auth/login.py" in proc.stderr


def test_overlap_sees_through_a_journalled_path_in_another_shape(tmp_path):
    """The journal is never versioned, migrated or rotated, so it still holds
    paths recorded before the journaller normalised them. Compared raw, a
    './src/auth/login.py' entry clashes with nothing and two concurrent writers
    are cleared for the same file -- the exact collision this check exists for."""
    seed_journal(tmp_path, [{
        "op": "create", "key": "AGENT-14", "status": "To Do",
        "labels": ["spec-auth-api", "role-coding", "group-2"],
        "files": ["./src/auth/login.py"],
    }])
    proc = run_hook(well_formed(), tmp_path)
    assert proc.returncode == 2
    assert "AGENT-14" in proc.stderr


def test_allows_overlap_across_different_groups(tmp_path):
    """Sequencing two overlapping issues into different groups is the documented
    escape hatch, so it must not be blocked."""
    seed_journal(tmp_path, [{
        "op": "create", "key": "AGENT-14", "status": "To Do",
        "labels": ["spec-auth-api", "role-coding", "group-1"],
        "files": ["src/auth/login.py"],
    }])
    assert run_hook(well_formed(), tmp_path).returncode == 0


def test_allows_overlap_with_a_done_issue(tmp_path):
    seed_journal(tmp_path, [{
        "op": "create", "key": "AGENT-14", "status": "To Do",
        "labels": ["spec-auth-api", "role-coding", "group-2"],
        "files": ["src/auth/login.py"],
    }, {"op": "transition", "key": "AGENT-14", "status": "Done"}])
    assert run_hook(well_formed(), tmp_path).returncode == 0


def test_overlap_check_tolerates_journal_events_predating_the_files_field(tmp_path):
    """The journal is never versioned, migrated or rotated, so live journals
    hold events written before `files` existed. Those must yield the default,
    not a KeyError that fails the hook open with the guardrail silently off."""
    seed_journal(tmp_path, [{
        "op": "create", "key": "AGENT-9", "status": "To Do",
        "labels": ["spec-auth-api", "role-coding", "group-2"],
        "summary": "[coding] older issue with no files key",
    }])
    proc = run_hook(well_formed(), tmp_path)
    assert proc.returncode == 0, proc.stderr


def test_overlap_does_not_fire_without_a_spec_label(tmp_path):
    """A missing label is unknown, not a match."""
    seed_journal(tmp_path, [{
        "op": "create", "key": "AGENT-14", "status": "To Do",
        "labels": ["role-coding"], "files": ["src/auth/login.py"],
    }])
    assert run_hook(well_formed(), tmp_path).returncode == 0


def test_bypass_label_still_skips_the_overlap_check(tmp_path):
    seed_journal(tmp_path, [{
        "op": "create", "key": "AGENT-14", "status": "To Do",
        "labels": ["spec-auth-api", "role-coding", "group-2"],
        "files": ["src/auth/login.py"],
    }])
    issue = well_formed()
    issue["additional_fields"] = {
        "labels": ["spec-auth-api", "role-coding", "group-2", "skip-format-check"]}
    assert run_hook(issue, tmp_path).returncode == 0


def test_blocks_a_files_section_the_journaller_cannot_parse(tmp_path):
    """The check accepted a bare `Files:` substring while the journaller needs it
    line-anchored, so a bolded or bulleted label passed the create and journalled
    NO paths -- silently disabling both the overlap check and the claim gate."""
    proc = run_hook(well_formed(description=(
        "Spec: .claude/specs/auth-api/spec.md\n"
        "**Files:**\n  - src/auth/login.py\n"
        "Acceptance: works\nRun: pytest -q"
    )), tmp_path)
    assert proc.returncode == 2
    assert "no paths could be parsed" in proc.stderr


def test_does_not_block_a_create_whose_only_clash_is_at_in_review(tmp_path):
    """Disjointness stops two CONCURRENT writers. An issue at In Review has
    finished writing, and claim_gate already treats it as landed -- the two
    guardrails must not disagree about what 'landed' means."""
    seed_journal(tmp_path, [
        {"op": "create", "key": "AGENT-14", "status": "To Do",
         "labels": ["spec-auth-api", "role-coding", "group-2"],
         "files": ["src/auth/login.py"]},
        {"op": "transition", "key": "AGENT-14", "status": "In Review"},
    ])
    assert run_hook(well_formed(), tmp_path).returncode == 0


def run_hook_raw(tool_input, tmp_path):
    """Send tool_input verbatim, so a test can hand the hook a non-dict."""
    cfg = tmp_path / "jira-config.json"
    cfg.write_text(json.dumps({"projectKey": "AGENT"}))
    env = dict(os.environ, HOME=str(tmp_path / "home"), JIRA_CONFIG_PATH=str(cfg))
    payload = {"tool_name": CREATE, "tool_input": tool_input}
    return subprocess.run([sys.executable, HOOK], input=json.dumps(payload),
                          capture_output=True, text=True, env=env)


def read_audit(tmp_path):
    path = os.path.join(str(tmp_path / "home"), ".claude", "logs", "team-hooks.jsonl")
    if not os.path.exists(path):
        return []
    with open(path) as fh:
        return [json.loads(line) for line in fh if line.strip()]


def test_a_json_string_tool_input_does_not_disable_the_check(tmp_path):
    """`or {}` passed a truthy non-dict straight through; .get() then raised and
    the fail-open handler exited 0. as_dict() fixed the crash but not the
    bypass: {} names no projectKey, so the scope check waves the create through
    as another project's and the format check never runs. Whether a malformed
    issue is caught must not depend on whether the model stringified its
    arguments."""
    proc = run_hook_raw(json.dumps(well_formed(summary="no tag here")), tmp_path)
    assert proc.returncode == 2
    assert "role tag" in proc.stderr.lower()


def test_a_json_string_tool_input_still_allows_a_well_formed_issue(tmp_path):
    assert run_hook_raw(json.dumps(well_formed()), tmp_path).returncode == 0


def test_a_json_string_tool_input_for_another_project_is_still_unpoliced(tmp_path):
    """Decoding widens what is checked, never what is policed: the site holds
    real client work and blocking a create there would be a serious defect."""
    proc = run_hook_raw(json.dumps(well_formed(
        projectKey="SCRUM", summary="MUFG - SCCM", description="",
        additional_fields={})), tmp_path)
    assert proc.returncode == 0


def test_an_unreadable_tool_input_fails_open_with_a_truthful_reason(tmp_path):
    """Fail-open is right -- with no projectKey the hook cannot tell its own
    project from the operator's client work. But the audit reason must name the
    real cause; 'issue targets another project' is a claim the hook never had
    the input to make, and it sends a reader looking in the wrong place."""
    proc = run_hook_raw("create a coding task for login", tmp_path)
    assert proc.returncode == 0
    reason = read_audit(tmp_path)[-1]["reason"]
    assert "tool_input" in reason
    assert "another project" not in reason
