# Jira Backlog Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the agent team's `tasks.md` + built-in task store with Jira as the system of record, so a Jira board shows what every agent is working on, what is blocked, and what remains.

**Architecture:** Agents perform all per-issue work through the Atlassian MCP (OAuth, `read/write:jira-work`). Privileged operations the OAuth grant cannot perform — creating the project, discovering per-site IDs, and the sprint lifecycle — are confined to `scripts/jira_bootstrap.py` (adding an `In Review` status is *not* among them: the script only warns and asks the operator to make that board edit by hand), which uses a separate Jira API token and writes discovered IDs to `.claude/jira-config.json`. Four hooks enforce the protocol: two `PreToolUse` gates on the Jira MCP calls, one `PostToolUse` journaller that maintains a local mirror, and the rewritten `TeammateIdle` work-check that reads that mirror.

**Tech Stack:** Python 3 (stdlib only for runtime code — hooks must not depend on site-packages), pytest for tests, Jira Cloud REST v3 + Agile v1.0, Atlassian MCP.

## Global Constraints

- **Hooks must depend on the Python standard library only.** They run as bare `python3` subprocesses outside any virtualenv. `pytest` is a dev dependency for tests only.
- **Every hook fails open.** Any unexpected condition, parse failure, or internal exception must resolve to exit 0. A hook bug must never block a valid action or trap a teammate. This is preserved verbatim from the existing design.
- **Never police issues outside the configured project.** Every hook exits 0 immediately if the issue's project key is not `config["projectKey"]`. The site holds real client work in `SCRUM`; agent guardrails must not touch it.
- **Custom field IDs, status IDs, and transition IDs are never hardcoded.** They are per-site. Bootstrap discovers them; hooks and agents read them from `.claude/jira-config.json`.
- **`To Do` is a required status, not a discovered one.** The initial status of every issue in the agent project MUST be named exactly `To Do`. This is the one status name the system may treat as a literal, because "unclaimed" is encoded as a status rather than a label (JQL cannot wildcard labels). Bootstrap **enforces** this as a hard precondition and fails loudly when it is absent — it is never inferred, defaulted, or worked around at runtime. Every other status name is discovered.
- **All commands non-interactive** per `.claude/rules/execution-hygiene.md`: `-y`/`--no-input` flags, `GIT_PAGER=cat`, no blocking stdin reads. Shell scripts start with `set -euo pipefail`.
- **Dependency isolation**: dev tooling lives in `.venv/`; `.venv/` and `__pycache__/` are gitignored; `requirements-dev.txt` is pinned and committed.
- Role tags are exactly: `coding`, `devops`, `sa`, `review`.
- Label vocabulary is exactly: `spec-<slug>`, `role-<role>`, `agent-<instance>`, `group-<n>`, `skip-format-check`, `skip-verify`.

---

## File Structure

| File | Responsibility |
|---|---|
| `.claude/settings.json` | Harness config. Relocated from repo root; hook paths corrected |
| `.claude/jira-config.json` | Discovered site/project/field/status/transition IDs. Written by bootstrap, read by hooks and agents |
| `.claude/hooks/team_hook_common.py` | Generic hook plumbing: payload parse, audit log, allow/block, path sanitization, role mapping |
| `.claude/hooks/jira_mirror.py` | Config loading and the mirror journal: append events, fold JSONL into per-issue state |
| `.claude/hooks/jira_mirror_journal.py` | `PostToolUse` — records successful Jira mutations to the mirror |
| `.claude/hooks/jira_issue_format_check.py` | `PreToolUse` on `createJiraIssue` — blocks malformed issues |
| `.claude/hooks/jira_transition_verify_gate.py` | `PreToolUse` on `transitionJiraIssue` — sentinel gate on `In Review` / `Done` |
| `.claude/hooks/teammate_idle_workcheck.py` | `TeammateIdle` — nudges from mirror state instead of the task store |
| `scripts/jira_bootstrap.py` | Admin plane: project, status, sprint lifecycle, ID discovery |
| `.claude/skills/jira-workflow/SKILL.md` | Operational mechanics: JQL, claim protocol, comment templates |
| `tests/hooks/*.py` | Hook unit tests against fixture payloads |

---

## Task 1: Test harness and the settings.json fix

The hooks are not firing today: `settings.json` sits at the repo root where Claude Code does not read it, and its hook commands point at `$CLAUDE_PROJECT_DIR/hooks/` while the scripts live in `.claude/hooks/`. Nothing else in this plan works until this is fixed, so it goes first and gets a regression test.

**Files:**
- Create: `.gitignore`, `requirements-dev.txt`, `pytest.ini`, `tests/__init__.py`, `tests/hooks/__init__.py`
- Create: `tests/hooks/test_settings_wiring.py`
- Create: `.claude/settings.json` (content moved from `settings.json`)
- Delete: `settings.json`

**Interfaces:**
- Consumes: nothing
- Produces: a working `pytest` invocation (`.venv/bin/pytest`) that later tasks extend; `.claude/settings.json` with a `hooks` block that later tasks add matchers to

- [ ] **Step 1: Create the dev environment and ignore files**

```bash
cd "$(git rev-parse --show-toplevel)"
python3 -m venv .venv
.venv/bin/pip install --no-input --quiet --upgrade pip
.venv/bin/pip install --no-input --quiet pytest==8.3.4
.venv/bin/pip freeze --all | grep -E '^(pytest|iniconfig|pluggy|packaging)==' > requirements-dev.txt
```

Create `.gitignore`:

```gitignore
.venv/
__pycache__/
*.pyc
.pytest_cache/
.claude/jira-config.json
.claude/specs/*/jira-run.json
```

`jira-config.json` is gitignored because it holds site-specific IDs; each install regenerates it via bootstrap.

Create `pytest.ini`:

```ini
[pytest]
testpaths = tests
python_files = test_*.py
addopts = -q
```

- [ ] **Step 2: Write the failing test**

Create `tests/hooks/test_settings_wiring.py`:

```python
"""Regression test: settings.json must live where Claude Code reads it, and
every hook command it references must exist on disk.

This exists because the checked-out repo had settings.json at the repo root
(not read by Claude Code) with hook paths pointing at hooks/ (moved to
.claude/hooks/). Both faults are silent -- hooks simply never fire.
"""
import json
import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SETTINGS = os.path.join(REPO, ".claude", "settings.json")


def test_settings_lives_in_dot_claude():
    assert os.path.exists(SETTINGS), "Claude Code only reads .claude/settings.json"


def test_no_stray_settings_at_repo_root():
    assert not os.path.exists(os.path.join(REPO, "settings.json")), \
        "a repo-root settings.json is dead config and will confuse readers"


def test_every_hook_command_resolves_to_an_existing_file():
    with open(SETTINGS) as fh:
        settings = json.load(fh)

    referenced = []
    for event, entries in settings.get("hooks", {}).items():
        for entry in entries:
            for hook in entry.get("hooks", []):
                referenced.append((event, hook["command"]))

    assert referenced, "expected at least one configured hook"

    for event, command in referenced:
        match = re.search(r'\$CLAUDE_PROJECT_DIR/([^"\']+\.py)', command)
        assert match, "hook command for {} must reference a .py under $CLAUDE_PROJECT_DIR: {}".format(event, command)
        path = os.path.join(REPO, match.group(1))
        assert os.path.exists(path), "{} hook points at missing file: {}".format(event, path)
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `.venv/bin/pytest tests/hooks/test_settings_wiring.py -v`
Expected: FAIL — `test_settings_lives_in_dot_claude` fails (no `.claude/settings.json`), `test_no_stray_settings_at_repo_root` fails (root file present).

- [ ] **Step 4: Move and fix settings.json**

```bash
git mv settings.json .claude/settings.json
```

Then edit `.claude/settings.json` so all three existing hook commands point at `.claude/hooks/` instead of `hooks/`. Replace the entire `"hooks"` block with:

```json
  "hooks": {
    "TaskCreated": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python3 \"$CLAUDE_PROJECT_DIR/.claude/hooks/task_created_format_check.py\""
          }
        ]
      }
    ],
    "TaskCompleted": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python3 \"$CLAUDE_PROJECT_DIR/.claude/hooks/task_completed_verify_gate.py\""
          }
        ]
      }
    ],
    "TeammateIdle": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python3 \"$CLAUDE_PROJECT_DIR/.claude/hooks/teammate_idle_workcheck.py\""
          }
        ]
      }
    ]
  }
```

Leave `env`, `model`, `availableModels`, `enabledPlugins`, and `extraKnownMarketplaces` exactly as they are. Task 6 replaces the two task-store hook entries with the Jira ones; this task only relocates and repairs.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/hooks/test_settings_wiring.py -v`
Expected: PASS — 3 passed.

- [ ] **Step 6: Commit**

```bash
git add .gitignore pytest.ini requirements-dev.txt tests .claude/settings.json
git add -u settings.json
git commit -m "fix: relocate settings.json to .claude/ and repair hook paths

Claude Code reads .claude/settings.json, not the repo-root copy, and the
hook commands pointed at hooks/ after the scripts moved to .claude/hooks/.
Both faults are silent -- the hooks never fired. Adds a regression test that
every configured hook command resolves to a file that exists."
```

---

## Task 2: Config loading and the mirror journal module

The shared module every Jira hook depends on. Two responsibilities that always change together: reading discovered IDs, and maintaining the local view of Jira state.

**Files:**
- Create: `.claude/hooks/jira_mirror.py`
- Create: `tests/hooks/test_jira_mirror.py`

**Interfaces:**
- Consumes: nothing
- Produces:
  - `load_config(path=None) -> dict` — returns `{}` if the file is missing or invalid
  - `mirror_path(project_key) -> str`
  - `append_event(project_key, event) -> None` — best-effort, never raises
  - `load_state(project_key) -> dict` mapping issue key to `{"summary": str, "labels": [str], "status": str}`
  - `issue_project(issue_key) -> str | None` — `"AGENT-14"` → `"AGENT"`
  - `agent_label(labels) -> str | None` — the first `agent-*` label, or None
  - Constant `MIRROR_DIR`

- [ ] **Step 1: Write the failing test**

Create `tests/hooks/test_jira_mirror.py`:

```python
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
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/pytest tests/hooks/test_jira_mirror.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'jira_mirror'`

- [ ] **Step 3: Write the implementation**

Create `.claude/hooks/jira_mirror.py`:

```python
"""Config loading and the local Jira mirror journal.

Two responsibilities, both consumed by every Jira hook:

1. CONFIG. Custom field IDs, status IDs and transition IDs are per-site, so
   scripts/jira_bootstrap.py discovers them and writes .claude/jira-config.json.
   Nothing here may be hardcoded -- this repo is a public template and baked-in
   IDs from one site would fail silently on every other install.

2. MIRROR. A hook subprocess cannot reach Jira: the MCP's OAuth token lives
   inside Claude Code, not the environment. So the PostToolUse journaller
   records every successful mutation to ~/.claude/logs/jira-mirror/<KEY>.jsonl
   and the idle work-check folds that log into current per-issue state.

   Known blind spot: the mirror only sees mutations made through the MCP. Edits
   made by hand on the board are invisible until an agent next touches that
   issue. This is acceptable because the only consumer is an advisory,
   fail-open nudge.

Every function here is best-effort. Nothing raises; callers are hooks that must
fail open.
"""
import json
import os

HOME = os.path.expanduser("~")
LOG_DIR = os.path.join(HOME, ".claude", "logs")
MIRROR_DIR = os.path.join(LOG_DIR, "jira-mirror")

# .claude/hooks/jira_mirror.py -> repo root
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_CONFIG_PATH = os.path.join(_REPO, ".claude", "jira-config.json")

# Fields a mirror event may carry. Absent keys must not clobber known values.
_MERGEABLE = ("summary", "labels", "status")


def load_config(path=None):
    """Read .claude/jira-config.json. Returns {} if missing or unparseable."""
    try:
        with open(path or DEFAULT_CONFIG_PATH) as fh:
            cfg = json.load(fh)
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def mirror_path(project_key):
    safe = "".join(c for c in str(project_key) if c.isalnum() or c in "._-") or "_"
    return os.path.join(MIRROR_DIR, safe + ".jsonl")


def append_event(project_key, event):
    """Append one observed mutation. Never raises -- journalling must not break a hook."""
    try:
        os.makedirs(MIRROR_DIR, exist_ok=True)
        with open(mirror_path(project_key), "a") as fh:
            fh.write(json.dumps(event) + "\n")
    except Exception:
        pass


def load_state(project_key):
    """Fold the journal into {issue_key: {summary, labels, status}}.

    Later events win per field; a field absent from an event leaves the prior
    value intact (a transition event carries no labels, and must not erase them).
    Corrupt lines are skipped rather than aborting the fold.
    """
    state = {}
    try:
        with open(mirror_path(project_key)) as fh:
            lines = fh.readlines()
    except Exception:
        return state

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except Exception:
            continue
        key = event.get("key")
        if not key:
            continue
        issue = state.setdefault(key, {"summary": "", "labels": [], "status": ""})
        for field in _MERGEABLE:
            if event.get(field) is not None:
                issue[field] = event[field]
    return state


def issue_project(issue_key):
    """'AGENT-14' -> 'AGENT'. Returns None for a numeric id or malformed key."""
    s = str(issue_key or "")
    if "-" not in s:
        return None
    prefix = s.rsplit("-", 1)[0]
    return prefix or None


def agent_label(labels):
    """The first agent-* label in the list, or None if unclaimed."""
    for label in labels or []:
        if str(label).startswith("agent-"):
            return label
    return None
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/hooks/test_jira_mirror.py -v`
Expected: PASS — 10 passed.

- [ ] **Step 5: Commit**

```bash
git add .claude/hooks/jira_mirror.py tests/hooks/test_jira_mirror.py
git commit -m "feat: add Jira config loader and mirror journal module

Hook subprocesses cannot reach Jira -- the MCP OAuth token lives inside
Claude Code, not the environment. The mirror journal gives hooks a local
view of issue state, folded from observed mutations. Field and status IDs
are loaded from discovered config, never hardcoded, because they are
per-site and this repo is a public template."
```

---

## Task 3: The PostToolUse mirror journaller

Populates the mirror. Runs after the call so it records what actually succeeded, not what was attempted.

**Files:**
- Create: `.claude/hooks/jira_mirror_journal.py`
- Create: `tests/hooks/test_jira_mirror_journal.py`

**Interfaces:**
- Consumes: `jira_mirror.load_config`, `append_event`, `issue_project`; `team_hook_common.read_payload`, `allow`, `audit`
- Produces: journal entries consumed by Task 4's gate and Task 6's idle check

- [ ] **Step 1: Write the failing test**

Create `tests/hooks/test_jira_mirror_journal.py`:

```python
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
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/pytest tests/hooks/test_jira_mirror_journal.py -v`
Expected: FAIL — every test errors because the hook file does not exist.

- [ ] **Step 3: Add config-path override support to jira_mirror.py**

The tests set `JIRA_CONFIG_PATH` so they can point at a fixture. Edit `.claude/hooks/jira_mirror.py`, replacing the body of `load_config`:

```python
def load_config(path=None):
    """Read the Jira config. Returns {} if missing or unparseable.

    Resolution order: explicit path, then $JIRA_CONFIG_PATH (used by tests and
    by installs that keep config outside the repo), then the repo default.
    """
    candidate = path or os.environ.get("JIRA_CONFIG_PATH") or DEFAULT_CONFIG_PATH
    try:
        with open(candidate) as fh:
            cfg = json.load(fh)
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}
```

Also make `MIRROR_DIR` resolve `HOME` at call time so the subprocess tests can override it. Replace the module-level constants and `mirror_path`:

```python
def _log_dir():
    return os.path.join(os.path.expanduser("~"), ".claude", "logs")


def _mirror_dir():
    return MIRROR_DIR if MIRROR_DIR is not None else os.path.join(_log_dir(), "jira-mirror")


# Tests monkeypatch this to redirect the journal; None means "derive from $HOME".
MIRROR_DIR = None


def mirror_path(project_key):
    safe = "".join(c for c in str(project_key) if c.isalnum() or c in "._-") or "_"
    return os.path.join(_mirror_dir(), safe + ".jsonl")


def append_event(project_key, event):
    """Append one observed mutation. Never raises -- journalling must not break a hook."""
    try:
        os.makedirs(_mirror_dir(), exist_ok=True)
        with open(mirror_path(project_key), "a") as fh:
            fh.write(json.dumps(event) + "\n")
    except Exception:
        pass
```

Delete the now-unused `HOME`, `LOG_DIR` module constants from `jira_mirror.py`.

- [ ] **Step 4: Re-run Task 2's tests to confirm no regression**

Run: `.venv/bin/pytest tests/hooks/test_jira_mirror.py -v`
Expected: PASS — 10 passed. (The existing `monkeypatch.setattr(jira_mirror, "MIRROR_DIR", ...)` calls still work because `_mirror_dir()` reads the module global.)

- [ ] **Step 5: Write the journaller**

Create `.claude/hooks/jira_mirror_journal.py`:

```python
#!/usr/bin/env python3
"""PostToolUse hook -- journal successful Jira mutations to the local mirror.

Runs AFTER the MCP call so it records what actually succeeded. The mirror is
the only way a hook subprocess can know anything about Jira state: it holds no
OAuth token and cannot call the API.

Scope: only the configured agent project. Issues in any other project (the
operator's real work) are ignored entirely.

Always exits 0. This hook observes; it never gates.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from team_hook_common import read_payload, allow, audit  # noqa: E402
import jira_mirror  # noqa: E402

EVENT = "PostToolUse"
PREFIX = "mcp__plugin_atlassian_atlassian__"
CREATE = PREFIX + "createJiraIssue"
TRANSITION = PREFIX + "transitionJiraIssue"
EDIT = PREFIX + "editJiraIssue"
COMMENT = PREFIX + "addCommentToJiraIssue"
WATCHED = (CREATE, TRANSITION, EDIT, COMMENT)


def _succeeded(response):
    """A response carrying an error key is a failed call -- do not journal it."""
    if not isinstance(response, dict):
        return bool(response)
    return not any(k in response for k in ("error", "errors", "errorMessages"))


def _labels_from_create(tool_input):
    extra = tool_input.get("additional_fields") or {}
    labels = extra.get("labels")
    return labels if isinstance(labels, list) else None


def main():
    p = read_payload()
    tool = p.get("tool_name", "")
    if tool not in WATCHED:
        allow()

    tool_input = p.get("tool_input") or {}
    if not _succeeded(p.get("tool_response")):
        allow(EVENT, p, reason="tool call did not succeed -- not journalled")

    cfg = jira_mirror.load_config()
    project = cfg.get("projectKey")
    if not project:
        allow(EVENT, p, reason="no projectKey configured -- nothing to journal")

    event = {"op": None, "key": None}

    if tool == CREATE:
        if (tool_input.get("projectKey") or "") != project:
            allow(EVENT, p, reason="create targets another project -- ignored")
        response = p.get("tool_response") or {}
        key = response.get("key") if isinstance(response, dict) else None
        if not key:
            allow(EVENT, p, reason="create response carried no issue key")
        event.update({
            "op": "create",
            "key": key,
            "summary": tool_input.get("summary"),
            "labels": _labels_from_create(tool_input),
            "status": "To Do",
        })
    else:
        key = tool_input.get("issueIdOrKey")
        if jira_mirror.issue_project(key) != project:
            allow(EVENT, p, reason="issue belongs to another project -- ignored")
        if tool == TRANSITION:
            transition_id = str((tool_input.get("transition") or {}).get("id", ""))
            # transitionJiraIssue takes a transition id, not a target status.
            # Bootstrap discovers the mapping; an unknown id journals no status
            # rather than guessing.
            status = (cfg.get("transitions") or {}).get(transition_id)
            event.update({"op": "transition", "key": key, "status": status})
        elif tool == EDIT:
            fields = tool_input.get("fields") or {}
            labels = fields.get("labels")
            event.update({
                "op": "edit",
                "key": key,
                "labels": labels if isinstance(labels, list) else None,
                "summary": fields.get("summary"),
            })
        else:
            event.update({"op": "comment", "key": key})

    jira_mirror.append_event(project, {k: v for k, v in event.items() if v is not None})
    allow(EVENT, p, reason="journalled {} for {}".format(event["op"], event["key"]))


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:  # FAIL OPEN
        audit(EVENT, {}, "allow", reason="hook error (fail-open): {}".format(e))
        sys.exit(0)
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/hooks/test_jira_mirror_journal.py -v`
Expected: PASS — 7 passed.

- [ ] **Step 7: Commit**

```bash
git add .claude/hooks/jira_mirror_journal.py .claude/hooks/jira_mirror.py tests/hooks/
git commit -m "feat: journal successful Jira mutations to the local mirror

PostToolUse rather than PreToolUse so the journal records what succeeded.
Scoped strictly to the configured agent project so the operator's real Jira
work is never touched. Transition events resolve the transition id to a
status name via discovered config, since transitionJiraIssue takes an id."
```

---

## Task 4: The issue format check gate

Replaces `task_created_format_check.py`. Strictly stronger than what it replaces: it prevents the malformed issue instead of rolling it back after creation.

**Files:**
- Create: `.claude/hooks/jira_issue_format_check.py`
- Create: `tests/hooks/test_jira_issue_format_check.py`

**Interfaces:**
- Consumes: `jira_mirror.load_config`; `team_hook_common.read_payload`, `allow`, `block`, `audit`
- Produces: the enforced issue shape that `.claude/skills/jira-workflow/SKILL.md` (Task 8) documents

- [ ] **Step 1: Write the failing test**

Create `tests/hooks/test_jira_issue_format_check.py`:

```python
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
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/pytest tests/hooks/test_jira_issue_format_check.py -v`
Expected: FAIL — hook file does not exist.

- [ ] **Step 3: Write the implementation**

Create `.claude/hooks/jira_issue_format_check.py`:

```python
#!/usr/bin/env python3
"""PreToolUse hook on createJiraIssue -- enforce the task issue shape.

Replaces the old TaskCreated format check. Exit 2 prevents the malformed issue
from being created at all, which is strictly better than the rollback-after-
creation the task store allowed.

Required shape for a Task in the agent project:
    Summary:     [coding|devops|sa|review] <verb> <what>
    Description: Spec: / Files: / Acceptance: / Run: sections
    Labels:      one role-*, one spec-*

Epics are exempt -- they describe a spec, not a unit of work.
Bypass: the skip-format-check label, for coordination or research issues.

Scope: only the configured agent project. The site holds real client work; a
guardrail that blocked issue creation there would be a serious defect.

FAIL OPEN: unparseable payload, missing config, or any internal error allows.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from team_hook_common import read_payload, allow, block, audit  # noqa: E402
import jira_mirror  # noqa: E402

EVENT = "PreToolUse"
CREATE = "mcp__plugin_atlassian_atlassian__createJiraIssue"
ROLES = ("coding", "devops", "sa", "review")
SUMMARY_TAG = re.compile(r"^\s*\[(%s)\]\s*\S" % "|".join(ROLES), re.I)
REQUIRED_SECTIONS = ("Spec:", "Files:", "Acceptance:", "Run:")


def main():
    p = read_payload()
    if p.get("tool_name") != CREATE:
        allow()

    tool_input = p.get("tool_input") or {}
    cfg = jira_mirror.load_config()
    project = cfg.get("projectKey")
    if not project:
        allow(EVENT, p, reason="no projectKey configured -- fail-open")
    if (tool_input.get("projectKey") or "") != project:
        allow(EVENT, p, reason="issue targets another project -- not policed")

    if str(tool_input.get("issueTypeName", "")).lower() == "epic":
        allow(EVENT, p, reason="epics are exempt from the task shape")

    labels = ((tool_input.get("additional_fields") or {}).get("labels")) or []
    labels = [str(x) for x in labels] if isinstance(labels, list) else []
    if "skip-format-check" in labels:
        allow(EVENT, p, reason="bypass label present")

    summary = str(tool_input.get("summary") or "")
    description = tool_input.get("description")
    description = description if isinstance(description, str) else ""

    problems = []

    tag_match = SUMMARY_TAG.match(summary)
    if not tag_match:
        problems.append(
            "summary must start with a role tag -- one of {}".format(
                " ".join("[%s]" % r for r in ROLES))
        )

    missing_sections = [s for s in REQUIRED_SECTIONS if s not in description]
    if missing_sections:
        problems.append("description is missing: {}".format(", ".join(missing_sections)))

    role_labels = [l for l in labels if l.startswith("role-")]
    if not role_labels:
        problems.append("no role-* label (e.g. role-coding)")
    if not any(l.startswith("spec-") for l in labels):
        problems.append("no spec-* label (e.g. spec-auth-api)")

    # A summary tag that disagrees with the role label would route the issue to
    # one pool while reading as another's -- catch it rather than let the board lie.
    if tag_match and role_labels:
        tagged = "role-" + tag_match.group(1).lower()
        if tagged not in role_labels:
            problems.append(
                "summary tag and role label disagree: summary says {}, labels say {}".format(
                    tagged, ", ".join(role_labels))
            )

    if problems:
        block(EVENT, p, (
            "Jira issue creation blocked by the format check:\n  - {}\n\n"
            "Required shape:\n"
            "  Summary:     [coding|devops|sa|review] <verb> <what>\n"
            "  Description: Spec: <path>\\nFiles: <paths>\\nAcceptance: <criteria>\\nRun: <command>\n"
            "  Labels:      spec-<slug>, role-<role>\n\n"
            "For a coordination or research issue that genuinely has no verification, "
            "add the skip-format-check label."
        ).format("\n  - ".join(problems)))

    allow(EVENT, p, reason="issue format ok")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:  # FAIL OPEN
        audit(EVENT, {}, "allow", reason="hook error (fail-open): {}".format(e))
        sys.exit(0)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/hooks/test_jira_issue_format_check.py -v`
Expected: PASS — 13 passed.

- [ ] **Step 5: Commit**

```bash
git add .claude/hooks/jira_issue_format_check.py tests/hooks/test_jira_issue_format_check.py
git commit -m "feat: enforce task issue shape at createJiraIssue

Replaces the TaskCreated format check. Blocking at PreToolUse prevents the
malformed issue entirely rather than rolling it back after creation. Also
catches a summary role tag that disagrees with the role label, which would
otherwise route work to one pool while reading as another's."
```

---

## Task 5: The transition verification gate

Replaces `task_completed_verify_gate.py`. The sentinel remains the teammate's attestation that it actually ran the verification command, because the hook still cannot observe the teammate's transcript.

**Files:**
- Create: `.claude/hooks/jira_transition_verify_gate.py`
- Create: `tests/hooks/test_jira_transition_verify_gate.py`
- Modify: `.claude/hooks/team_hook_common.py`
- Delete: `.claude/hooks/task_created_format_check.py`, `.claude/hooks/task_completed_verify_gate.py`

**Interfaces:**
- Consumes: `jira_mirror.load_config`, `load_state`, `issue_project`; `team_hook_common.read_payload`, `allow`, `block`, `audit`, `safe_path_component`, `VERIFIED_DIR`
- Produces: the sentinel path convention `~/.claude/logs/verified/<PROJECT>/<ISSUE-KEY>.verified`, documented in Task 8's skill and Task 10's agent files

- [ ] **Step 1: Write the failing test**

Create `tests/hooks/test_jira_transition_verify_gate.py`:

```python
"""A transition into a gated status requires a consumed sentinel."""
import json
import os
import subprocess
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


def test_allows_in_review_with_sentinel(tmp_path):
    env, home = setup_env(tmp_path)
    sentinel(home)
    assert run_hook(env, transition_id="31").returncode == 0


def test_consumes_the_sentinel_so_it_cannot_be_reused(tmp_path):
    env, home = setup_env(tmp_path)
    path = sentinel(home)
    assert run_hook(env, transition_id="31").returncode == 0
    assert not os.path.exists(path)
    assert run_hook(env, transition_id="31").returncode == 2


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
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/pytest tests/hooks/test_jira_transition_verify_gate.py -v`
Expected: FAIL — hook file does not exist.

- [ ] **Step 3: Write the implementation**

Create `.claude/hooks/jira_transition_verify_gate.py`:

```python
#!/usr/bin/env python3
"""PreToolUse hook on transitionJiraIssue -- the verification gate.

Replaces the old TaskCompleted gate. A transition into a gated status
(In Review, Done) is blocked unless the acting teammate left a sentinel at
    ~/.claude/logs/verified/<PROJECT>/<ISSUE-KEY>.verified
written after that issue's `Run:` command passed. The sentinel is consumed on
success so it cannot be reused for a later re-transition.

Why a sentinel rather than reading the transcript: the hook payload's
transcript belongs to whichever session triggered it, and a hook cannot observe
a teammate actually running tests. The sentinel is the teammate's attestation.

transitionJiraIssue takes a TRANSITION ID, not a target status name, so the id
is resolved through the transitions map that bootstrap discovers. An id that is
not in the map resolves to unknown, and unknown never blocks.

Bypass: the skip-verify label, read from the mirror journal.
FAIL OPEN: unparseable payload, missing config, unknown transition, or any
internal error allows the transition.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from team_hook_common import (  # noqa: E402
    read_payload, allow, block, audit, safe_path_component, verified_dir,
)
import jira_mirror  # noqa: E402

EVENT = "PreToolUse"
TRANSITION = "mcp__plugin_atlassian_atlassian__transitionJiraIssue"
DEFAULT_GATED = ("In Review", "Done")


def sentinel_path(project, issue_key):
    # Both components come from the payload and are attacker-influenceable; this
    # path is passed to os.remove on success, so each is reduced to a single
    # safe path component that cannot escape the verified directory.
    return os.path.join(
        verified_dir(),
        safe_path_component(project, default="_noproject"),
        safe_path_component(issue_key, default="_noissue") + ".verified",
    )


def main():
    p = read_payload()
    if p.get("tool_name") != TRANSITION:
        allow()

    tool_input = p.get("tool_input") or {}
    cfg = jira_mirror.load_config()
    project = cfg.get("projectKey")
    if not project:
        allow(EVENT, p, reason="no projectKey configured -- fail-open")

    issue_key = str(tool_input.get("issueIdOrKey") or "")
    if jira_mirror.issue_project(issue_key) != project:
        allow(EVENT, p, reason="issue belongs to another project -- not gated")

    transition_id = str((tool_input.get("transition") or {}).get("id", ""))
    target = (cfg.get("transitions") or {}).get(transition_id)
    if not target:
        allow(EVENT, p, reason="transition id {} not in discovered map -- fail-open".format(
            transition_id))

    gated = cfg.get("gatedStatuses") or list(DEFAULT_GATED)
    if target not in gated:
        allow(EVENT, p, reason="target status {} is not gated".format(target))

    issue = jira_mirror.load_state(project).get(issue_key) or {}
    if "skip-verify" in (issue.get("labels") or []):
        allow(EVENT, p, reason="skip-verify label present")

    path = sentinel_path(project, issue_key)
    if not os.path.exists(path):
        block(EVENT, p, (
            "Transition of {} to '{}' blocked by the verification gate: no sentinel at\n"
            "  {}\n\n"
            "Run the issue's `Run:` command, then attest:\n"
            "  mkdir -p {}\n"
            "  echo '<the Run command> PASSED' > {}\n\n"
            "If this issue genuinely has no runnable verification, ask the lead to add "
            "the skip-verify label."
        ).format(issue_key, target, path, os.path.dirname(path), path))

    try:
        os.remove(path)  # consume -- one sentinel, one transition
    except Exception:
        pass
    allow(EVENT, p, reason="verified via sentinel for {} -> {}".format(issue_key, target))


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:  # FAIL OPEN
        audit(EVENT, {}, "allow", reason="hook error (fail-open): {}".format(e))
        sys.exit(0)
```

- [ ] **Step 4: Make `VERIFIED_DIR` resolve `HOME` at call time**

The subprocess tests override `HOME`, but `team_hook_common` captures it at import. Edit `.claude/hooks/team_hook_common.py`, replacing the module-level path constants with functions plus backward-compatible aliases:

```python
def _home():
    return os.path.expanduser("~")


def log_dir():
    return os.path.join(_home(), ".claude", "logs")


def log_path():
    return os.path.join(log_dir(), "team-hooks.jsonl")


def verified_dir():
    """Completion sentinels: ~/.claude/logs/verified/<scope>/"""
    return os.path.join(log_dir(), "verified")


def nudge_dir():
    """Idle loop-guard state: ~/.claude/logs/idle-nudges/"""
    return os.path.join(log_dir(), "idle-nudges")
```

Delete the old `HOME`, `LOG_DIR`, `LOG_PATH`, `TASKS_DIR`, `VERIFIED_DIR`, `NUDGE_DIR` constants.

**`NUDGE_DIR` has a live consumer.** The current `teammate_idle_workcheck.py` imports it by name and uses it in `_state_path` and `os.makedirs`. Task 6 rewrites that hook, but it must not be broken in the meantime, so make the two-line mechanical swap now: import `nudge_dir` instead of `NUDGE_DIR`, and call `nudge_dir()` at both use sites. Change nothing else about that hook — its rewrite belongs to Task 6. (`load_team_tasks` still exists at this point, so its other import is fine.)

Then update `audit()` to call the functions:

```python
def audit(event, payload, decision, reason=None, extra=None):
    rec = {"captured_at": _now(), "event": event, "decision": decision}
    if reason:
        rec["reason"] = reason
    if extra:
        rec.update(extra)
    rec["payload"] = payload
    try:
        os.makedirs(log_dir(), exist_ok=True)
        with open(log_path(), "a") as fh:
            fh.write(json.dumps(rec) + "\n")
    except Exception:
        pass  # logging must never break a hook
```

Also make `allow()` callable with no arguments (the new hooks use a bare `allow()` for unrelated tools, to avoid writing an audit record on every single tool call):

```python
def allow(event=None, payload=None, reason=None, extra=None):
    """Permit the action (exit 0). Audits only when an event is given."""
    if event is not None:
        audit(event, payload, "allow", reason, extra)
    sys.exit(0)
```

`load_team_tasks` and its `TASKS_DIR` are removed in Task 6 along with their last caller.

- [ ] **Step 5: Delete the two obsolete task-store hooks**

`task_completed_verify_gate.py` imports `VERIFIED_DIR`, which step 4 just removed, so it
must go in the same commit — otherwise it crashes on import for one commit's worth of
history. Its replacement (`jira_transition_verify_gate.py`) exists as of this task, and
`jira_issue_format_check.py` replaced the other in Task 4.

```bash
git rm .claude/hooks/task_created_format_check.py .claude/hooks/task_completed_verify_gate.py
```

Also delete their two registrations from `.claude/settings.json` — remove the entire
`"TaskCreated"` and `"TaskCompleted"` keys from the `hooks` block, leaving only
`"TeammateIdle"`. Task 1's wiring test asserts every configured hook command resolves to
a file that exists, so a registration left pointing at a deleted script turns the suite
red. Task 6 adds the `PreToolUse` and `PostToolUse` blocks.

- [ ] **Step 6: Run the new tests to verify they pass**

Run: `.venv/bin/pytest tests/hooks/test_jira_transition_verify_gate.py -v`
Expected: PASS — 12 passed.

- [ ] **Step 7: Run the whole suite to confirm no regression**

Run: `.venv/bin/pytest -q`
Expected: PASS — all tests from Tasks 1–5 green, including the wiring test against the
now-shorter hooks block.

- [ ] **Step 8: Commit**

```bash
git add -A .claude/hooks .claude/settings.json tests/hooks/
git commit -m "feat: gate In Review and Done transitions on a verification sentinel

Replaces the TaskCompleted gate. transitionJiraIssue takes a transition id
rather than a status name, so the target is resolved through the map
bootstrap discovers; an unmapped id fails open rather than guessing.
Sentinel is consumed on success so it cannot be replayed. Retires the two
task-store hooks in the same commit as the constant their imports depended
on, so no commit in history carries a hook that crashes on import."
```

---

## Task 6: Rewrite the idle work-check against the mirror

Same loop-guard semantics, new data source. Also retires the two obsolete hooks and the dead task-store helper.

**Files:**
- Modify: `.claude/hooks/teammate_idle_workcheck.py` (full rewrite)
- Modify: `.claude/hooks/team_hook_common.py` (remove `load_team_tasks`)
- Modify: `.claude/settings.json` (add the PreToolUse and PostToolUse registrations)
- Create: `tests/hooks/test_teammate_idle_workcheck.py`

**Interfaces:**
- Consumes: `jira_mirror.load_config`, `load_state`, `agent_label`; `team_hook_common.role_of_teammate`, `nudge_dir`
- Produces: the settings.json hook wiring that all four hooks run under

- [ ] **Step 1: Write the failing test**

Create `tests/hooks/test_teammate_idle_workcheck.py`:

```python
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


def setup_env(tmp_path):
    cfg = tmp_path / "jira-config.json"
    cfg.write_text(json.dumps({"projectKey": "AGENT"}))
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return dict(os.environ, HOME=str(home), JIRA_CONFIG_PATH=str(cfg)), home


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
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/pytest tests/hooks/test_teammate_idle_workcheck.py -v`
Expected: FAIL — the current hook reads `~/.claude/tasks/` and never nudges from journal state.

- [ ] **Step 3: Rewrite the hook**

Replace `.claude/hooks/teammate_idle_workcheck.py` entirely:

```python
#!/usr/bin/env python3
"""TeammateIdle hook -- nudge a teammate to claim claimable work before idling.

Exit 2 keeps the teammate working and delivers the nudge via stderr.

Data source is the local mirror journal, not Jira: a hook subprocess holds no
OAuth token. Claimable means, in mirror state:
  - status 'To Do' (unclaimed is a status, not a label -- JQL cannot wildcard
    labels, so claim state is encoded in status),
  - a role-<mine> label matching this teammate's role, and
  - no agent-* label.

Known blind spot: the mirror only sees mutations made through the MCP, so a
card dragged by hand on the board is invisible here until an agent next touches
it. Acceptable -- this nudge is advisory and fail-open.

LOOP GUARD (critical): a naive nudge would trap a teammate forever. State at
~/.claude/logs/idle-nudges/<team>__<teammate>.json tracks how many times we have
nudged for the SAME claimable set. After MAX_NUDGES the teammate may idle. A
changed set resets the counter; an emptied set clears the state file.

FAIL OPEN: any internal error allows idle.
"""
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from team_hook_common import (  # noqa: E402
    read_payload, allow, block, audit, role_of_teammate, nudge_dir,
)
import jira_mirror  # noqa: E402

EVENT = "TeammateIdle"
MAX_NUDGES = 2


def _state_path(team, teammate):
    safe = "{}__{}".format(team, teammate).replace("/", "_")
    return os.path.join(nudge_dir(), safe + ".json")


def main():
    p = read_payload()
    team = p.get("team_name")
    teammate = p.get("teammate_name")
    role = role_of_teammate(teammate)
    if not team or not role:
        allow(EVENT, p, reason="no team or unmapped role -- not nudging")

    cfg = jira_mirror.load_config()
    project = cfg.get("projectKey")
    if not project:
        allow(EVENT, p, reason="no projectKey configured -- not nudging")

    role_label = "role-" + role
    claimable = []
    for key, issue in jira_mirror.load_state(project).items():
        labels = issue.get("labels") or []
        if issue.get("status") != "To Do":
            continue
        if role_label not in labels:
            continue
        if jira_mirror.agent_label(labels):
            continue
        claimable.append(key)
    claimable.sort()

    state_path = _state_path(team, teammate)
    if not claimable:
        try:
            os.remove(state_path)
        except Exception:
            pass
        allow(EVENT, p, reason="no claimable {} work".format(role_label))

    sig = hashlib.sha1(",".join(claimable).encode()).hexdigest()[:12]
    state = {}
    try:
        with open(state_path) as fh:
            state = json.load(fh)
    except Exception:
        state = {}
    count = state.get("count", 0) if state.get("sig") == sig else 0

    if count >= MAX_NUDGES:
        allow(EVENT, p, reason="nudge cap reached for set {} -- allowing idle".format(sig))

    try:
        os.makedirs(nudge_dir(), exist_ok=True)
        with open(state_path, "w") as fh:
            json.dump({"sig": sig, "count": count + 1}, fh)
    except Exception:
        pass

    block(EVENT, p, (
        "Before idling: {} unclaimed {} issue(s) are available -- {}.\n"
        "Claim one:\n"
        "  1. getJiraIssue to read its current labels\n"
        "  2. editJiraIssue fields.labels = <existing labels> + ['agent-{}']\n"
        "  3. transitionJiraIssue to In Progress\n"
        "  4. re-read; if another agent-* label appeared, lowest instance name "
        "wins and the loser drops its label and picks another issue\n"
        "Or send the lead a one-line note that you are genuinely done. (nudge {}/{})"
    ).format(len(claimable), role_label, ", ".join(claimable), teammate,
             count + 1, MAX_NUDGES))


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:  # FAIL OPEN
        audit(EVENT, {}, "allow", reason="hook error (fail-open): {}".format(e))
        sys.exit(0)
```

- [ ] **Step 4: Delete the dead task-store helper**

Delete `load_team_tasks` from `.claude/hooks/team_hook_common.py` — its only caller was the old idle hook, which step 3 just replaced — along with the `TASKS_DIR` reference in the module docstring. (The two obsolete hook scripts were already removed in Task 5.) Update the docstring's first paragraph to read:

```python
"""Shared helpers for the agent-team enforcement hooks.

Imported by jira_issue_format_check.py, jira_transition_verify_gate.py,
jira_mirror_journal.py and teammate_idle_workcheck.py. Responsibilities:
  - parse the stdin JSON payload the harness delivers to a hook,
  - append an audit record to ~/.claude/logs/team-hooks.jsonl for every decision,
  - implement the documented exit-code contract: 0 = proceed, 2 = block + the
    stderr text is fed back as the reason.

Design rule for ALL hooks: FAIL OPEN. Any unexpected condition must resolve to
allow() -- a hook bug must never block a valid action or trap a teammate.
Enforcement is a guardrail, not a tripwire.
"""
```

- [ ] **Step 5: Rewire settings.json**

Replace the `"hooks"` block in `.claude/settings.json` with:

```json
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "mcp__plugin_atlassian_atlassian__createJiraIssue",
        "hooks": [
          {
            "type": "command",
            "command": "python3 \"$CLAUDE_PROJECT_DIR/.claude/hooks/jira_issue_format_check.py\""
          }
        ]
      },
      {
        "matcher": "mcp__plugin_atlassian_atlassian__transitionJiraIssue",
        "hooks": [
          {
            "type": "command",
            "command": "python3 \"$CLAUDE_PROJECT_DIR/.claude/hooks/jira_transition_verify_gate.py\""
          }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "mcp__plugin_atlassian_atlassian__(createJiraIssue|editJiraIssue|transitionJiraIssue|addCommentToJiraIssue)",
        "hooks": [
          {
            "type": "command",
            "command": "python3 \"$CLAUDE_PROJECT_DIR/.claude/hooks/jira_mirror_journal.py\""
          }
        ]
      }
    ],
    "TeammateIdle": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python3 \"$CLAUDE_PROJECT_DIR/.claude/hooks/teammate_idle_workcheck.py\""
          }
        ]
      }
    ]
  }
```

- [ ] **Step 6: Run the full suite**

Run: `.venv/bin/pytest -q`
Expected: PASS — including `test_settings_wiring.py`, which now validates the four new hook paths.

- [ ] **Step 7: Commit**

```bash
git add -A .claude/hooks .claude/settings.json tests/hooks
git commit -m "feat: drive the idle work-check from Jira mirror state

Retires the task-store reader the old idle hook depended on and wires up the
two PreToolUse gates and the PostToolUse journaller alongside the rewritten
idle check. Loop-guard semantics are unchanged -- two nudges per claimable
set, then idle is allowed."
```

---

## Task 7: The bootstrap script

The admin plane. Everything the MCP's `read/write:jira-work` grant cannot do, plus the ID discovery every other component depends on.

**Files:**
- Create: `scripts/jira_bootstrap.py`
- Create: `tests/test_jira_bootstrap.py`

**Interfaces:**
- Consumes: `JIRA_SITE`, `JIRA_EMAIL`, `JIRA_API_TOKEN` from the environment
- Produces:
  - CLI: `ensure-project`, `discover`, `sprint-open <name>`, `sprint-close <id>`
  - `.claude/jira-config.json` with keys `cloudId`, `site`, `projectKey`, `boardId`, `fields{sprint,rank,flagged}`, `statuses{}`, `transitions{}`, `gatedStatuses[]`, `missingRequiredStatus`
  - Exit code 3 when the board lacks the required `To Do` status
  - Python: `JiraAdmin(site, email, token)` with `.request(method, path, body=None)`, `.find_project(key)`, `.discover_ids(project_key)`, `.open_sprint(board_id, name)`, `.close_sprint(sprint_id)`

- [ ] **Step 1: Write the failing test**

Create `tests/test_jira_bootstrap.py`:

```python
"""Bootstrap uses stdlib urllib so hooks and scripts share a zero-dependency
runtime. Tests stub the transport rather than the network.
"""
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))

import jira_bootstrap  # noqa: E402


class FakeTransport:
    """Records requests and replays queued responses keyed by 'METHOD path'."""

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, method, url, body, headers):
        self.calls.append((method, url, body))
        for key, value in self.responses.items():
            verb, fragment = key.split(" ", 1)
            if method == verb and fragment in url:
                return value
        raise AssertionError("unstubbed request: {} {}".format(method, url))


def admin(responses):
    a = jira_bootstrap.JiraAdmin("example.atlassian.net", "me@example.com", "tok")
    a.transport = FakeTransport(responses)
    return a


def test_find_project_returns_none_when_absent():
    a = admin({"GET /rest/api/3/project/search": {"values": []}})
    assert a.find_project("AGENT") is None


def test_find_project_returns_matching_project():
    a = admin({"GET /rest/api/3/project/search": {
        "values": [{"key": "AGENT", "id": "10001"}, {"key": "SCRUM", "id": "10000"}]}})
    assert a.find_project("AGENT")["id"] == "10001"


def test_ensure_project_is_idempotent():
    a = admin({"GET /rest/api/3/project/search": {"values": [{"key": "AGENT", "id": "10001"}]}})
    a.ensure_project("AGENT", "Agent Team")
    assert not any(m == "POST" for m, _, _ in a.transport.calls), \
        "an existing project must not be recreated"


def test_ensure_project_creates_a_team_managed_scrum_project():
    a = admin({
        "GET /rest/api/3/project/search": {"values": []},
        "POST /rest/api/3/project": {"key": "AGENT", "id": "10001"},
    })
    a.ensure_project("AGENT", "Agent Team")
    posts = [(u, b) for m, u, b in a.transport.calls if m == "POST"]
    assert len(posts) == 1
    body = json.loads(posts[0][1])
    assert body["key"] == "AGENT"
    assert body["projectTypeKey"] == "software"
    assert "gh-simplified-agility-scrum" in body["projectTemplateKey"]


def test_discover_ids_maps_fields_statuses_and_transitions():
    a = admin({
        "GET /rest/api/3/field": [
            {"id": "customfield_10020", "name": "Sprint"},
            {"id": "customfield_10019", "name": "Rank"},
            {"id": "customfield_10021", "name": "Flagged"},
            {"id": "customfield_10016", "name": "Story point estimate"},
        ],
        "GET /rest/api/3/project/AGENT/statuses": [
            {"name": "Task", "statuses": [
                {"id": "10000", "name": "To Do"},
                {"id": "10001", "name": "In Progress"},
                {"id": "10002", "name": "Done"},
            ]},
        ],
        "GET /rest/api/3/search": {"issues": [{"key": "AGENT-1"}]},
        "GET /rest/api/3/issue/AGENT-1/transitions": {"transitions": [
            {"id": "11", "to": {"name": "To Do"}},
            {"id": "21", "to": {"name": "In Progress"}},
            {"id": "31", "to": {"name": "Done"}},
        ]},
        "GET /rest/agile/1.0/board": {"values": [{"id": 1, "location": {"projectKey": "AGENT"}}]},
    })
    cfg = a.discover_ids("AGENT")
    assert cfg["fields"]["sprint"] == "customfield_10020"
    assert cfg["fields"]["rank"] == "customfield_10019"
    assert cfg["fields"]["flagged"] == "customfield_10021"
    assert cfg["statuses"]["To Do"] == "10000"
    assert cfg["transitions"]["21"] == "In Progress"
    assert cfg["boardId"] == 1


def test_discover_ids_flags_a_project_with_no_to_do_status():
    """'To Do' is a required contract, not a discovered name. A board without it
    must fail loudly at setup rather than silently producing a mirror whose
    create-events claim a status no issue ever has."""
    a = admin({
        "GET /rest/api/3/field": [{"id": "customfield_10020", "name": "Sprint"}],
        "GET /rest/api/3/project/AGENT/statuses": [
            {"name": "Task", "statuses": [
                {"id": "10000", "name": "Backlog"},
                {"id": "10002", "name": "Done"},
            ]}],
        "GET /rest/api/3/search": {"issues": []},
        "GET /rest/agile/1.0/board": {"values": [{"id": 1, "location": {"projectKey": "AGENT"}}]},
    })
    cfg = a.discover_ids("AGENT")
    assert cfg["missingRequiredStatus"] == "To Do"


def test_discover_ids_does_not_flag_a_project_that_has_to_do():
    a = admin({
        "GET /rest/api/3/field": [{"id": "customfield_10020", "name": "Sprint"}],
        "GET /rest/api/3/project/AGENT/statuses": [
            {"name": "Task", "statuses": [{"id": "10000", "name": "To Do"}]}],
        "GET /rest/api/3/search": {"issues": []},
        "GET /rest/agile/1.0/board": {"values": [{"id": 1, "location": {"projectKey": "AGENT"}}]},
    })
    assert a.discover_ids("AGENT").get("missingRequiredStatus") is None


def test_main_exits_nonzero_when_to_do_is_absent(monkeypatch, tmp_path, capsys):
    """The precondition must break the build, not warn past it."""
    monkeypatch.setenv("JIRA_SITE", "example.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", "me@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "tok")
    monkeypatch.setattr(jira_bootstrap, "CONFIG_PATH", str(tmp_path / "jira-config.json"))
    monkeypatch.setattr(
        jira_bootstrap.JiraAdmin, "discover_ids",
        lambda self, key: {"projectKey": key, "statuses": {"Backlog": "1"},
                           "missingRequiredStatus": "To Do"},
    )
    code = jira_bootstrap.main(["discover", "--key", "AGENT"])
    assert code != 0
    assert "To Do" in capsys.readouterr().err


def test_discover_ids_reports_missing_in_review_status():
    a = admin({
        "GET /rest/api/3/field": [{"id": "customfield_10020", "name": "Sprint"}],
        "GET /rest/api/3/project/AGENT/statuses": [
            {"name": "Task", "statuses": [{"id": "10000", "name": "To Do"}]}],
        "GET /rest/api/3/search": {"issues": []},
        "GET /rest/agile/1.0/board": {"values": [{"id": 1, "location": {"projectKey": "AGENT"}}]},
    })
    cfg = a.discover_ids("AGENT")
    assert "In Review" not in cfg["statuses"]
    assert cfg["gatedStatuses"] == ["Done"], \
        "gate only on statuses that actually exist, or every transition fails open"


def test_open_sprint_creates_then_starts():
    a = admin({
        "POST /rest/agile/1.0/sprint": {"id": 42, "name": "Group 1"},
        "POST /rest/agile/1.0/sprint/42": {"id": 42, "state": "active"},
    })
    sprint = a.open_sprint(board_id=1, name="Group 1")
    assert sprint["id"] == 42
    methods = [(m, u) for m, u, _ in a.transport.calls]
    assert methods[0][1].endswith("/rest/agile/1.0/sprint")
    assert "/sprint/42" in methods[1][1]


def test_close_sprint_sets_closed_state():
    a = admin({"POST /rest/agile/1.0/sprint/42": {"id": 42, "state": "closed"}})
    a.close_sprint(42)
    body = json.loads(a.transport.calls[0][2])
    assert body["state"] == "closed"


def test_write_config_is_json_and_round_trips(tmp_path):
    path = tmp_path / "jira-config.json"
    jira_bootstrap.write_config(str(path), {"projectKey": "AGENT", "transitions": {"21": "In Progress"}})
    with open(path) as fh:
        assert json.load(fh)["transitions"]["21"] == "In Progress"


def test_missing_credentials_exits_nonzero_without_prompting(monkeypatch, capsys):
    for var in ("JIRA_SITE", "JIRA_EMAIL", "JIRA_API_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    code = jira_bootstrap.main(["discover"])
    assert code != 0
    assert "JIRA_API_TOKEN" in capsys.readouterr().err
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/pytest tests/test_jira_bootstrap.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'jira_bootstrap'`

- [ ] **Step 3: Write the implementation**

Create `scripts/jira_bootstrap.py`:

```python
#!/usr/bin/env python3
"""Jira admin plane for the agent team.

The MCP's OAuth grant carries only read:jira-work and write:jira-work, so it
cannot create a project, add a workflow status, or run the sprint lifecycle.
This script does those things with a Jira API token, and discovers the per-site
IDs everything else reads from .claude/jira-config.json.

SECURITY: the API token acts with the operator's full Jira permissions. It is
deliberately confined to this one scripted surface. Teammates never hold it and
never invoke this script -- the team lead does, at group boundaries.

Non-interactive by contract (see .claude/rules/execution-hygiene.md): every
input arrives via argument or environment variable, nothing reads stdin, and a
missing input exits non-zero rather than prompting.

Usage:
    export JIRA_SITE=your-site.atlassian.net
    export JIRA_EMAIL=you@example.com
    export JIRA_API_TOKEN=...            # id.atlassian.com > Security > API tokens

    python3 scripts/jira_bootstrap.py ensure-project --key AGENT --name "Agent Team"
    python3 scripts/jira_bootstrap.py discover --key AGENT
    python3 scripts/jira_bootstrap.py sprint-open --name "Group 1 - interfaces"
    python3 scripts/jira_bootstrap.py sprint-close --id 42
"""
import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(REPO, ".claude", "jira-config.json")

# Team-managed ("next-gen") Scrum. Team-managed matters: board columns and
# statuses can then be changed without a site admin.
SCRUM_TEMPLATE = "com.pyxis.greenhopper.jira:gh-simplified-agility-scrum"

FIELD_ALIASES = {"sprint": "Sprint", "rank": "Rank", "flagged": "Flagged"}
PREFERRED_GATED = ("In Review", "Done")

# The one status name the system treats as a literal rather than discovering.
# See "To Do is a required status" in the plan's Global Constraints.
REQUIRED_STATUS = "To Do"


def _http(method, url, body, headers):
    """Default transport. Tests replace JiraAdmin.transport with a stub."""
    data = body.encode() if isinstance(body, str) else body
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode() or "{}"
    return json.loads(raw) if raw.strip() else {}


class JiraAdmin:
    def __init__(self, site, email, token):
        self.site = site.replace("https://", "").rstrip("/")
        self.auth = base64.b64encode("{}:{}".format(email, token).encode()).decode()
        self.transport = _http

    def request(self, method, path, body=None):
        url = "https://{}{}".format(self.site, path)
        headers = {
            "Authorization": "Basic " + self.auth,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        payload = json.dumps(body) if body is not None else None
        return self.transport(method, url, payload, headers)

    # -- project ---------------------------------------------------------

    def find_project(self, key):
        result = self.request("GET", "/rest/api/3/project/search?query=" + key)
        for project in result.get("values", []):
            if project.get("key") == key:
                return project
        return None

    def ensure_project(self, key, name):
        existing = self.find_project(key)
        if existing:
            return existing
        account_id = self.request("GET", "/rest/api/3/myself").get("accountId")
        return self.request("POST", "/rest/api/3/project", {
            "key": key,
            "name": name,
            "projectTypeKey": "software",
            "projectTemplateKey": SCRUM_TEMPLATE,
            "leadAccountId": account_id,
            "assigneeType": "UNASSIGNED",
        })

    # -- discovery -------------------------------------------------------

    def discover_ids(self, project_key):
        """Resolve per-site field, status, transition and board IDs.

        Nothing here may be hardcoded: these IDs differ per site, and this repo
        is a public template.
        """
        fields = {}
        by_name = {f.get("name"): f.get("id") for f in self.request("GET", "/rest/api/3/field")}
        for alias, jira_name in FIELD_ALIASES.items():
            if by_name.get(jira_name):
                fields[alias] = by_name[jira_name]

        statuses = {}
        for issue_type in self.request(
                "GET", "/rest/api/3/project/{}/statuses".format(project_key)):
            for status in issue_type.get("statuses", []):
                statuses[status["name"]] = status["id"]

        # Transition ids are only readable from a real issue. Absent one, the
        # map stays empty and the verify gate fails open until re-run.
        transitions = {}
        probe = self.request(
            "GET", "/rest/api/3/search?jql=project%3D{}&maxResults=1".format(project_key))
        issues = probe.get("issues", [])
        if issues:
            key = issues[0]["key"]
            for t in self.request(
                    "GET", "/rest/api/3/issue/{}/transitions".format(key)).get("transitions", []):
                target = (t.get("to") or {}).get("name")
                if target:
                    transitions[str(t["id"])] = target

        board_id = None
        for board in self.request("GET", "/rest/agile/1.0/board").get("values", []):
            if (board.get("location") or {}).get("projectKey") == project_key:
                board_id = board.get("id")
                break

        # Gate only on statuses that exist. Gating on an absent 'In Review'
        # would make every transition resolve to unknown and fail open --
        # a guardrail that silently does nothing is worse than none.
        gated = [s for s in PREFERRED_GATED if s in statuses]

        return {
            "site": self.site,
            "projectKey": project_key,
            "boardId": board_id,
            "fields": fields,
            "statuses": statuses,
            "transitions": transitions,
            "gatedStatuses": gated,
            # 'To Do' is a contract, not a discovery: "unclaimed" is encoded as a
            # status because JQL cannot wildcard labels, so the journaller and the
            # idle check both treat it as a literal. A board lacking it must fail
            # at setup -- otherwise every create-event journals a status no issue
            # ever holds, the idle check finds nothing claimable, and the guardrail
            # looks installed while doing nothing.
            "missingRequiredStatus": None if REQUIRED_STATUS in statuses else REQUIRED_STATUS,
        }

    # -- sprints ---------------------------------------------------------

    def open_sprint(self, board_id, name):
        sprint = self.request("POST", "/rest/agile/1.0/sprint",
                              {"name": name, "originBoardId": board_id})
        self.request("POST", "/rest/agile/1.0/sprint/{}".format(sprint["id"]),
                     {"state": "active"})
        return sprint

    def close_sprint(self, sprint_id):
        return self.request("POST", "/rest/agile/1.0/sprint/{}".format(sprint_id),
                            {"state": "closed"})


def write_config(path, config):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        json.dump(config, fh, indent=2, sort_keys=True)
        fh.write("\n")


def read_config(path):
    try:
        with open(path) as fh:
            return json.load(fh)
    except Exception:
        return {}


def _admin_from_env():
    site = os.environ.get("JIRA_SITE")
    email = os.environ.get("JIRA_EMAIL")
    token = os.environ.get("JIRA_API_TOKEN")
    missing = [n for n, v in
               (("JIRA_SITE", site), ("JIRA_EMAIL", email), ("JIRA_API_TOKEN", token)) if not v]
    if missing:
        print("Missing required environment: {}.\n"
              "Create a token at id.atlassian.com > Security > API tokens, then export "
              "JIRA_SITE, JIRA_EMAIL and JIRA_API_TOKEN.".format(", ".join(missing)),
              file=sys.stderr)
        return None
    return JiraAdmin(site, email, token)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Jira admin plane for the agent team")
    sub = parser.add_subparsers(dest="command", required=True)

    p_project = sub.add_parser("ensure-project")
    p_project.add_argument("--key", default="AGENT")
    p_project.add_argument("--name", default="Agent Team")

    p_discover = sub.add_parser("discover")
    p_discover.add_argument("--key", default="AGENT")

    p_open = sub.add_parser("sprint-open")
    p_open.add_argument("--name", required=True)

    p_close = sub.add_parser("sprint-close")
    p_close.add_argument("--id", required=True)

    args = parser.parse_args(argv)
    admin = _admin_from_env()
    if admin is None:
        return 1

    try:
        if args.command == "ensure-project":
            project = admin.ensure_project(args.key, args.name)
            print("project {} ready (id {})".format(project.get("key"), project.get("id")))
            config = admin.discover_ids(args.key)
            config["cloudId"] = read_config(CONFIG_PATH).get("cloudId", "")
            write_config(CONFIG_PATH, config)
            if _fail_if_required_status_missing(config):
                return 3
            _warn_if_no_in_review(config)
            return 0

        if args.command == "discover":
            config = admin.discover_ids(args.key)
            config["cloudId"] = read_config(CONFIG_PATH).get("cloudId", "")
            write_config(CONFIG_PATH, config)
            print("wrote {}".format(CONFIG_PATH))
            if _fail_if_required_status_missing(config):
                return 3
            _warn_if_no_in_review(config)
            return 0

        config = read_config(CONFIG_PATH)
        if args.command == "sprint-open":
            board_id = config.get("boardId")
            if not board_id:
                print("no boardId in {} -- run `discover` first".format(CONFIG_PATH),
                      file=sys.stderr)
                return 1
            sprint = admin.open_sprint(board_id, args.name)
            print(json.dumps({"id": sprint["id"], "name": sprint.get("name")}))
            return 0

        if args.command == "sprint-close":
            admin.close_sprint(args.id)
            print("sprint {} closed".format(args.id))
            return 0
    except urllib.error.HTTPError as e:
        print("Jira API error {} on {}: {}".format(e.code, args.command, e.read().decode()[:500]),
              file=sys.stderr)
        return 2

    return 1


def _fail_if_required_status_missing(config):
    """Hard precondition: the board MUST have a status named 'To Do'.

    Returns True when the setup should abort. This is deliberately an error and
    not a warning: 'unclaimed' is encoded as this status, so a board without it
    produces a mirror full of create-events claiming a status no issue holds,
    and an idle check that never finds claimable work. That failure is invisible
    at runtime, so it has to be loud here.
    """
    missing = config.get("missingRequiredStatus")
    if not missing:
        return False
    print(
        "\nERROR: project {} has no '{}' status, and the agent team requires one.\n"
        "'Unclaimed' is encoded as this status, so without it the idle work-check\n"
        "will silently never find claimable work.\n"
        "Statuses found: {}\n\n"
        "Fix it, then re-run this command:\n"
        "  1. Open the board > Board settings > Columns\n"
        "  2. Rename the first column to exactly 'To Do' (or add one)\n"
        "  3. python3 scripts/jira_bootstrap.py discover --key {}\n".format(
            config.get("projectKey", "?"), missing,
            ", ".join(sorted(config.get("statuses") or {})) or "(none)",
            config.get("projectKey", "AGENT")),
        file=sys.stderr)
    return True


def _warn_if_no_in_review(config):
    if "In Review" in (config.get("statuses") or {}):
        return
    print(
        "\nNOTE: the project has no 'In Review' status, so only 'Done' is gated.\n"
        "Adding it needs a board edit the API cannot reliably perform on a\n"
        "team-managed project. One-time, ~30 seconds:\n"
        "  1. Open the board > Board settings (or the '...' menu) > Columns\n"
        "  2. Add a column named 'In Review' between 'In Progress' and 'Done'\n"
        "  3. Re-run: python3 scripts/jira_bootstrap.py discover --key {}\n".format(
            config.get("projectKey", "AGENT")),
        file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_jira_bootstrap.py -v`
Expected: PASS — 10 passed.

- [ ] **Step 5: Commit**

```bash
git add scripts/jira_bootstrap.py tests/test_jira_bootstrap.py
git commit -m "feat: add Jira admin bootstrap with per-site ID discovery

Does what the MCP OAuth grant cannot: create the team-managed Scrum project
and run the sprint lifecycle. Discovers field, status, transition and board
IDs into .claude/jira-config.json rather than hardcoding them, since they
differ per site and this repo is a public template. Gates only on statuses
that actually exist -- gating on an absent 'In Review' would make every
transition fail open, which is worse than not gating."
```

---

## Task 8: The jira-workflow skill

The operational mechanics live here so the agent definitions stay readable. This is what every teammate loads before claiming work.

**Files:**
- Create: `.claude/skills/jira-workflow/SKILL.md`
- Create: `tests/test_skill_consistency.py`

**Interfaces:**
- Consumes: label vocabulary and sentinel path from Tasks 4–6; config keys from Task 7
- Produces: the documented claim protocol that Tasks 9–11 reference by name

- [ ] **Step 1: Write the failing test**

Create `tests/test_skill_consistency.py`:

```python
"""Documentation drift is a real failure mode here: the skill is the only
thing agents read before claiming work. If it names a label or path the hooks
do not enforce, agents follow the doc and the guardrail silently rejects them.
"""
import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILL = os.path.join(REPO, ".claude", "skills", "jira-workflow", "SKILL.md")
FORMAT_HOOK = os.path.join(REPO, ".claude", "hooks", "jira_issue_format_check.py")
GATE_HOOK = os.path.join(REPO, ".claude", "hooks", "jira_transition_verify_gate.py")


def read(path):
    with open(path) as fh:
        return fh.read()


def test_skill_exists_with_frontmatter():
    text = read(SKILL)
    assert text.startswith("---\n")
    assert re.search(r"^name:\s*jira-workflow$", text, re.M)
    assert re.search(r"^description:\s*\S", text, re.M)


def test_skill_documents_every_required_section():
    text = read(SKILL)
    for heading in ("Claim Protocol", "Issue Shape", "Comment Protocol",
                    "Blocked", "Verification Sentinel"):
        assert heading in text, "skill must document: " + heading


def test_skill_documents_the_labels_the_hook_enforces():
    text = read(SKILL)
    for label in ("role-", "spec-", "agent-", "group-",
                  "skip-format-check", "skip-verify"):
        assert label in text


def test_skill_required_sections_match_the_format_hook():
    required = re.search(r"REQUIRED_SECTIONS = \(([^)]*)\)", read(FORMAT_HOOK)).group(1)
    sections = re.findall(r'"([^"]+)"', required)
    text = read(SKILL)
    for section in sections:
        assert section in text, \
            "hook requires {} but the skill never mentions it".format(section)


def test_skill_sentinel_path_matches_the_gate_hook():
    assert ".verified" in read(GATE_HOOK)
    text = read(SKILL)
    assert "~/.claude/logs/verified/" in text
    assert ".verified" in text
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/pytest tests/test_skill_consistency.py -v`
Expected: FAIL — `FileNotFoundError` for `SKILL.md`.

- [ ] **Step 3: Write the skill**

Create `.claude/skills/jira-workflow/SKILL.md`:

````markdown
---
name: jira-workflow
description: Claim, work, and close Jira issues as an agent-team teammate — claim protocol, issue shape, comment templates, and the verification sentinel. Load before claiming any work when the team is Jira-backed.
---

# Jira Workflow

Jira is the system of record for the agent team's backlog. There is no `tasks.md`.
All per-issue work goes through the Atlassian MCP.

Read `.claude/jira-config.json` first — it holds this site's `cloudId`, `projectKey`,
custom field IDs, status IDs, and transition IDs. **Never hardcode an ID**; they differ
per site.

## Issue Shape

Every Task issue in the agent project must carry:

```
Summary:     [coding|devops|sa|review] <verb> <what>
Parent:      the spec Epic
Sprint:      the group's sprint (config fields.sprint)
Labels:      spec-<slug>, role-<role>, group-<n>
Description:
  Spec:       .claude/specs/<slug>/spec.md#<section>
  Files:      comma-separated paths this issue may write
  Acceptance: what must be true when it is done
  Run:        the verification command
```

This is machine-enforced. `createJiraIssue` is **blocked** if the summary has no role tag,
the description is missing any of `Spec:`, `Files:`, `Acceptance:`, `Run:`, or the
`role-*` / `spec-*` labels are absent. It is also blocked if the summary tag and the
`role-*` label disagree.

Bypass with the `skip-format-check` label for a coordination or research issue that
genuinely has no verification. Epics are exempt.

## Claim Protocol

Jira has no compare-and-swap, and up to twelve agents claim concurrently. Correctness
comes from read-after-write with a deterministic tie-break.

**1. Find** — unclaimed work is a *status*, not a label. JQL cannot wildcard labels, so
never try to express "has no `agent-*` label" in JQL:

```
project = <projectKey> AND sprint in openSprints()
  AND status = "To Do" AND labels = role-coding
  ORDER BY Rank ASC
```

**2. Claim** — `editJiraIssue` **replaces** the labels array, so this is read-modify-write:

```
getJiraIssue(issueIdOrKey)                      -> current labels
editJiraIssue(fields.labels = current + ["agent-coding-2"])
transitionJiraIssue(transition.id = <To Do -> In Progress>)
```

Resolve the transition id from `config.transitions` (or `getTransitionsForJiraIssue`).
`transitionJiraIssue` takes a transition **id**, never a status name.

**3. Confirm** — re-read the issue. If more than one `agent-*` label is present, two
instances raced. **The lowest instance name wins, lexicographically.** The loser removes
its own label and returns to step 1. Both agents evaluate the same rule on the same data
and reach the same verdict without talking to each other.

**4. Work** — only the files listed in `Files:`. Peers run concurrently; editing outside
your declared paths clobbers them.

## Verification Sentinel

Before transitioning to `In Review` or `Done`, run the issue's `Run:` command and attest
that it passed:

```bash
mkdir -p ~/.claude/logs/verified/<projectKey>
echo "<the Run command> PASSED" > ~/.claude/logs/verified/<projectKey>/<ISSUE-KEY>.verified
```

The transition is **blocked** without it. The sentinel is consumed on success, so one
sentinel permits one transition. A hook cannot watch you run tests — this file is your
attestation, so only write it after the command actually passed.

Bypass: the `skip-verify` label, for issues with no runnable verification.

## Comment Protocol

Four comments per issue. Enough that the card tells the whole story; not so many that
the signal drowns.

**On claim:**
```
Claimed by coding-2.
```

**On verification:**
```
Verification: `pytest tests/auth -q` PASSED
14 passed in 2.1s
```

**On completion:**
```
Done. Added POST /login with token issuance via AuthToken.
Files: src/auth/login.py, tests/auth/test_login.py
```

**On blocker** — see below.

Review findings are comments on the issue they concern. The synthesizer's PASS/FAIL
verdict is a comment on the sprint's `role-review` issue.

## Blocked

Do not invent a Blocked status — the project may not have one. Use Jira's native
impediment flag, whose field id is `config.fields.flagged`:

```
editJiraIssue(fields = {<flagged field>: [{"value": "Impediment"}]})
addCommentToJiraIssue("BLOCKED: <specific blocker>. Needs: <what would unblock>.")
```

Then `SendMessage` the lead. Clear the flag by setting the field to `null` when unblocked.

## Closing

An implementer **cannot close its own issue**. Transition to `In Review` and stop there.
Only the review synthesizer moves `In Review` -> `Done`, and only on a PASS verdict.

## Label Vocabulary

| Label | Meaning |
|---|---|
| `spec-<slug>` | Which spec this belongs to |
| `role-coding` \| `role-devops` \| `role-sa` \| `role-review` | Which pool may claim it |
| `agent-<instance>` | Who claimed it — drives board swimlanes |
| `group-<n>` | Which parallel group; survives sprint closure |
| `skip-format-check` | Exempt from the create-time shape check |
| `skip-verify` | Exempt from the sentinel gate |

## When Jira Is Unreachable

Stall and escalate to the lead. Do not invent state, do not work an issue you could not
claim, and do not mark anything complete out of band. There is no `tasks.md` fallback by
design — a half-tracked run is worse than a paused one.
````

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_skill_consistency.py -v`
Expected: PASS — 5 passed.

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/jira-workflow tests/test_skill_consistency.py
git commit -m "feat: add the jira-workflow skill

Operational mechanics for teammates: claim protocol with the lexicographic
tie-break, issue shape, comment templates, sentinel attestation, and the
native impediment flag for blockers. Tests assert the skill and the hooks
agree on required sections and label vocabulary -- drift here would have
agents follow the doc into a silent block."
```

---

## Task 9: Rewrite the agent team protocol rule

The always-on rule every teammate inherits. It currently describes `TaskUpdate`, `tasks.md`, and the two retired hooks.

**Files:**
- Modify: `.claude/rules/agent-team-protocol.md`
- Create: `tests/test_docs_have_no_stale_task_store_refs.py`

**Interfaces:**
- Consumes: the claim protocol and sentinel convention from Task 8
- Produces: the lifecycle that Tasks 10–11 assume

- [ ] **Step 1: Write the failing test**

Create `tests/test_docs_have_no_stale_task_store_refs.py`:

```python
"""After the migration, no agent-facing doc may still instruct agents to use
the built-in task store or tasks.md. A teammate that follows a stale
instruction calls a tool that no longer coordinates anything.
"""
import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DOCS = [
    ".claude/rules/agent-team-protocol.md",
    ".claude/agents/coding-agent.md",
    ".claude/agents/devops-agent.md",
    ".claude/agents/review-agent.md",
    ".claude/agents/sa-agent.md",
    ".claude/agents/fullstack-agent.md",
    ".claude/skills/spec-workflow/SKILL.md",
]

STALE = re.compile(r"\bTaskCreate\b|\bTaskUpdate\b|\bTaskList\b|\bTaskGet\b|tasks\.md"
                   r"|TaskCreated|TaskCompleted")

# Rewritten one task at a time; each task adds its file here.
MIGRATED = [".claude/rules/agent-team-protocol.md"]


def test_migrated_docs_have_no_task_store_references():
    offenders = []
    for rel in MIGRATED:
        path = os.path.join(REPO, rel)
        with open(path) as fh:
            for lineno, line in enumerate(fh, 1):
                if STALE.search(line):
                    offenders.append("{}:{}: {}".format(rel, lineno, line.strip()))
    assert not offenders, "stale task-store references:\n" + "\n".join(offenders)


def test_every_doc_is_eventually_migrated():
    assert set(MIGRATED) <= set(DOCS)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/pytest tests/test_docs_have_no_stale_task_store_refs.py -v`
Expected: FAIL — `agent-team-protocol.md` contains `TaskUpdate`, `tasks.md`, `TaskCreated`, `TaskCompleted`.

- [ ] **Step 3: Rewrite the protocol rule**

In `.claude/rules/agent-team-protocol.md`, make these replacements. Preserve every section not listed — particularly "Coordination Under Unreliable Signals" and "Shutdown", whose lessons still apply.

Replace the **Teammate Lifecycle** section:

```markdown
## Teammate Lifecycle

1. Receive delegation via `SendMessage` from the team lead with the spec path and sprint scope
2. Invoke the `jira-workflow` skill, then read `spec.md` and `design.md`
3. Self-claim an unclaimed issue for your role per the claim protocol in `jira-workflow`
   (status `To Do` + `role-<yours>` label + no `agent-*` label)
4. Implement exactly what the issue describes, touching only its `Files:`
5. Run the issue's `Run:` command, write the verification sentinel, comment the result
6. Transition to `In Review` — you may not close your own issue
7. Notify the lead via `SendMessage`; claim the next unclaimed issue for your role
```

Replace the **Communication Rules** tool line:

```markdown
- Tools: `SendMessage` (any teammate); the Atlassian MCP for all issue state
```

Replace **Completion Reporting**:

```markdown
## Completion Reporting

Three steps, in order:
1. Comment the verification output and a one-line summary on the issue
2. Transition the issue to `In Review`
3. `SendMessage` the lead (and any teammate depending on your output)

The board is the record. There is no file to update.
```

Replace **Blocker Reporting**:

```markdown
## Blocker Reporting

Set the impediment flag (`config.fields.flagged` = `Impediment`), comment the specific
blocker and what would unblock it, then `SendMessage` the lead. If the same blocker
persists after two attempts, the lead escalates to the user.
```

Replace the **Verification Gate** section:

```markdown
## Verification Gate (All Teammates)

Before transitioning ANY issue to `In Review`:
1. Run the issue's `Run:` command
2. Confirm the interface/output contract matches the issue exactly
3. Confirm you only modified files listed in the issue's `Files:`
4. Write the verification sentinel (machine-enforced), then transition

If verification fails and you cannot fix it in scope, flag the issue as an impediment
with the specific failure.
```

Replace the entire **Enforced Hooks** section:

```markdown
## Enforced Hooks (Automated Guardrails)

Four hooks in `.claude/settings.json` enforce this protocol automatically. All are
**fail-open** (a hook error never blocks you) and log every decision to
`~/.claude/logs/team-hooks.jsonl`. Scripts live at `.claude/hooks/`.

### 1. Issue format check (`PreToolUse` on `createJiraIssue`)
Issue creation is **blocked** unless the summary carries a `[coding|devops|sa|review]`
tag, the description has `Spec:` / `Files:` / `Acceptance:` / `Run:`, and `role-*` +
`spec-*` labels are present — and unless the summary tag agrees with the role label.
This is the lead's concern (the lead authors issues), but all agents should know the shape.
- **Bypass**: the `skip-format-check` label. Epics are exempt.

### 2. Verification gate (`PreToolUse` on `transitionJiraIssue`) — ACTION REQUIRED
A transition to `In Review` or `Done` is **blocked** unless you wrote a verification
sentinel. The hook cannot see your transcript, so the sentinel is your attestation that
verification actually passed. After your `Run:` command passes, and immediately before
the transition:

```bash
mkdir -p ~/.claude/logs/verified/<projectKey>
echo "<the Run command> PASSED" > ~/.claude/logs/verified/<projectKey>/<ISSUE-KEY>.verified
```

The sentinel is consumed on success, so it cannot be reused.
- **Bypass**: the `skip-verify` label.

### 3. Mirror journal (`PostToolUse` on the Jira mutation tools)
Records every successful mutation to `~/.claude/logs/jira-mirror/<projectKey>.jsonl`.
Observational only — it never blocks. It exists because a hook subprocess holds no
Jira credentials and would otherwise be blind.

### 4. Idle work-check (`TeammateIdle`)
Before you go idle, the hook checks mirror state for unclaimed issues carrying your
role label. If any exist you are kept working and nudged to claim one or confirm to the
lead you are done. After 2 nudges for the same set it lets you idle (loop-safe).

Because the mirror only sees MCP mutations, a card moved by hand on the board is
invisible to the idle check until an agent next touches it.
```

Replace the **Coordination Under Unreliable Signals** ground-truth bullet (keep every other bullet verbatim):

```markdown
- **Ground truth is Jira, then the disk.** The authoritative state of the work is the
  issue's status and labels in Jira, then verification sentinels, then the actual
  `git diff` — NOT the last message you received. Message delivery lags and reorders.
  Before acting on ANY status claim (yours or a peer's), confirm it against Jira. If
  Jira is unreachable, stall and escalate; do not invent state.
```

Replace the shutdown sweep path reference:

```markdown
Finally, sweep any leftover verification sentinels for this run:
`rm -rf ~/.claude/logs/verified/<projectKey>/`
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/pytest tests/test_docs_have_no_stale_task_store_refs.py -v`
Expected: PASS — 2 passed.

- [ ] **Step 5: Commit**

```bash
git add .claude/rules/agent-team-protocol.md tests/test_docs_have_no_stale_task_store_refs.py
git commit -m "docs: rewrite the team protocol for Jira-backed coordination

Lifecycle, completion, blockers and the verification gate now describe Jira
issues rather than the task store. Adds a test that migrated docs carry no
stale TaskCreate/TaskUpdate/tasks.md instructions -- a teammate following one
would call a tool that no longer coordinates anything."
```

---

## Task 10: Rewrite the four teammate agent definitions

**Files:**
- Modify: `.claude/agents/coding-agent.md`, `devops-agent.md`, `sa-agent.md`, `review-agent.md`
- Modify: `tests/test_docs_have_no_stale_task_store_refs.py` (extend `MIGRATED`)

**Interfaces:**
- Consumes: `jira-workflow` skill (Task 8), protocol rule (Task 9)
- Produces: teammates that load `jira-workflow` before claiming

- [ ] **Step 1: Extend the failing test**

In `tests/test_docs_have_no_stale_task_store_refs.py`, extend `MIGRATED`:

```python
MIGRATED = [
    ".claude/rules/agent-team-protocol.md",
    ".claude/agents/coding-agent.md",
    ".claude/agents/devops-agent.md",
    ".claude/agents/review-agent.md",
    ".claude/agents/sa-agent.md",
]
```

Add a test that each teammate loads the skill:

```python
TEAMMATES = [
    ".claude/agents/coding-agent.md",
    ".claude/agents/devops-agent.md",
    ".claude/agents/review-agent.md",
    ".claude/agents/sa-agent.md",
]


def test_every_teammate_requires_the_jira_workflow_skill():
    for rel in TEAMMATES:
        with open(os.path.join(REPO, rel)) as fh:
            assert "jira-workflow" in fh.read(), \
                "{} must load jira-workflow before claiming work".format(rel)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/pytest tests/test_docs_have_no_stale_task_store_refs.py -v`
Expected: FAIL — all four files contain stale references and none mention `jira-workflow`.

- [ ] **Step 3: Apply the edits**

In **each** of the four files:

1. **Frontmatter `description`** — replace "Claims tasks from the shared task list" with "Claims issues from the Jira board".

2. **Always-On Context** — replace the specs sentence with:

```markdown
Specs live at `.claude/specs/<slug>/` with `spec.md`, `design.md`, `decisions.md`. The
backlog is in Jira, not on disk — claim issues per the `jira-workflow` skill and respect
the interface contracts in each issue's description.
```

3. **Required Skills table** — add a row above the existing rows:

```markdown
| `jira-workflow` | Claim protocol, issue shape, comment templates, verification sentinel — load before claiming any issue |
```

4. **Working as One of a Parallel Pool** (in `coding-agent.md` and `devops-agent.md`) — replace the first two bullets with:

```markdown
- **Self-claim immediately and continuously.** Don't wait to be handed an issue. On start,
  run the role JQL from `jira-workflow` and claim any unclaimed issue for your role. The
  moment you finish one, claim the next. Keep the board draining.
- **Claim atomically.** Follow the claim protocol exactly: add your `agent-*` label,
  transition to In Progress, then re-read. If two `agent-*` labels are present, the lowest
  instance name wins; the loser drops its label and picks another issue.
```

5. **Communication Patterns** — replace the trailing "self-claim ... from `TaskList`" bullet with:

```markdown
- After finishing, run the role JQL again and self-claim the next unclaimed issue
```

6. **Verification sentinel bullet** (in `coding-agent.md:110`, `devops-agent.md:102`, `sa-agent.md:109`) — replace with:

```markdown
- **Write the verification sentinel before transitioning** (machine-enforced by the
  `transitionJiraIssue` gate). After the issue's `Run:` command passes:
  `mkdir -p ~/.claude/logs/verified/<projectKey> && echo "<Run cmd> PASSED" > ~/.claude/logs/verified/<projectKey>/<ISSUE-KEY>.verified`.
  Without it the transition to `In Review` is blocked. See `rules/agent-team-protocol.md`
  → "Enforced Hooks".
```

7. **Workflow numbered list** (`coding-agent.md` steps 2 and 10, `devops-agent.md` equivalent) — step 2 becomes "Read the spec, then claim an issue per `jira-workflow`"; the final step becomes "Write the verification sentinel, comment the result, transition to `In Review`, and notify the lead".

8. In **`review-agent.md`** only, replace the `tasks.md` sentence at line 95 with:

```markdown
Do the issue's completion comments match the code? Were verification commands run?
**Check that the issue's `Run:` command actually exercised what the completion claims** —
an issue whose `Run:` was `go build && go vet` but not the CI-blocking `golangci-lint`
once let 9 lint failures slip straight past the gate to review. If the stated verification
is narrower than the acceptance criteria, that gap is itself a finding.
```

Also add to `review-agent.md`, after its Required Skills table:

```markdown
## Closing Issues (Synthesizer Only)

You are the only role that may transition an issue `In Review` -> `Done`, and only on a
PASS verdict. Implementers stop at `In Review` by design — a self-closed issue defeats
the gate. Post the group verdict as a comment on the sprint's `role-review` issue; there
is exactly one verdict per cycle.
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_docs_have_no_stale_task_store_refs.py -v`
Expected: PASS — 3 passed.

- [ ] **Step 5: Commit**

```bash
git add .claude/agents tests/test_docs_have_no_stale_task_store_refs.py
git commit -m "docs: point the four teammate agents at Jira

Each now loads jira-workflow before claiming, uses the role JQL and the
lexicographic claim tie-break, and stops at In Review. Only the review
synthesizer closes issues -- a self-closed issue defeats the gate."
```

---

## Task 11: Rewrite the team lead

The largest single rewrite. The lead authors the backlog, drives the sprint lifecycle, and owns the only surface that touches the admin credential.

**Files:**
- Modify: `.claude/agents/fullstack-agent.md`
- Modify: `tests/test_docs_have_no_stale_task_store_refs.py` (extend `MIGRATED`)

**Interfaces:**
- Consumes: `scripts/jira_bootstrap.py` CLI (Task 7), `jira-workflow` skill (Task 8)
- Produces: the dispatch sequence the whole build phase runs on

- [ ] **Step 1: Extend the failing test**

Add `".claude/agents/fullstack-agent.md"` to `MIGRATED`, and add:

```python
def test_lead_documents_the_bootstrap_and_sprint_lifecycle():
    with open(os.path.join(REPO, ".claude", "agents", "fullstack-agent.md")) as fh:
        text = fh.read()
    for token in ("jira_bootstrap.py", "sprint-open", "sprint-close", "jira-run.json"):
        assert token in text, "lead must document " + token


def test_lead_forbids_handing_the_admin_token_to_teammates():
    with open(os.path.join(REPO, ".claude", "agents", "fullstack-agent.md")) as fh:
        text = fh.read()
    assert "JIRA_API_TOKEN" in text
    assert "never" in text.lower()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/pytest tests/test_docs_have_no_stale_task_store_refs.py -v`
Expected: FAIL — the lead still describes `tasks.md` authoring and `TaskCreate` dispatch.

- [ ] **Step 3: Apply the edits**

In `.claude/agents/fullstack-agent.md`:

1. **Spec Structure** section — replace the directory listing and the `tasks.md` bullets with:

````markdown
Specs live at `.claude/specs/<slug>/` (short kebab-case slug, e.g. `auth-api`):

```
.claude/specs/<slug>/
  spec.md          # design decisions, requirements, constraints
  design.md        # architecture, repo structure (MUST include Security Considerations)
  jira-run.json    # generated: Epic key + sprint id per group
  decisions.md     # mid-flight decision log
  requirements.md  # from /brainstorm (optional)
  prd/             # product requirements docs (optional)
```

**The backlog lives in Jira. There is no `tasks.md`.** You author the work as Jira
issues: one Epic per spec, one Task per unit of work, one sprint per parallel group.
Review verdicts are comments, not files.

Issue authoring rules — **the structure of the backlog is the primary lever on build
speed**, so decompose aggressively toward many small independent issues:

- **Maximize the width of each sprint** — split work so the most same-role issues
  possible can run at once (one issue per module/handler/endpoint/IaC stack, not one
  per layer). Wide sprints keep the whole pool busy.
- **Minimize the number of sprints** — only open a new one when there is a *real*
  data/interface dependency.
- **No two issues in the same sprint may write the same file.** This is what makes
  shared-tree parallelism safe.
- Declare cross-issue dependencies as Jira issue links (`blocks` / `is blocked by`).
- Front-load interface contracts as their own tiny first-sprint issue so the wide
  implementation sprint can fan out behind it.
- Infrastructure issues creating stateful resources MUST follow
  `rules/AWS-security-guidelines.md`.
````

2. **Team Tools** table — replace with:

```markdown
| Tool | Purpose |
|---|---|
| `Agent` | Spawn a named background teammate into the implicit team |
| `SendMessage` | Direct messages to any teammate |
| Atlassian MCP | Create issues, transition, comment, link, set sprint/rank |
| `scripts/jira_bootstrap.py` | Admin plane — project, ID discovery, sprint lifecycle |
```

3. Insert a new section immediately after **Team Tools**:

````markdown
## The Jira Admin Credential

`scripts/jira_bootstrap.py` needs `JIRA_SITE`, `JIRA_EMAIL` and `JIRA_API_TOKEN`. That
token acts with the operator's **full Jira permissions** — far beyond the MCP's
`read/write:jira-work` grant.

**Never** pass it to a teammate, never echo it, never put it in an issue, a comment, a
spec, or a spawn prompt. You are the only actor that runs the bootstrap script. If a
teammate needs a sprint opened or closed, it messages you and you run it.

If the credential is absent, the script exits non-zero with instructions. Escalate to the
user — do not fall back to a run without sprints, and do not ask a teammate to work
around it.

### One-time setup per repository

```bash
python3 scripts/jira_bootstrap.py ensure-project --key AGENT --name "Agent Team"
python3 scripts/jira_bootstrap.py discover --key AGENT
```

If `discover` reports no `In Review` status, follow its printed instructions (one board
edit, ~30 seconds) and re-run `discover`. Until then only `Done` is gated, and the
review handoff is weaker than designed — tell the user rather than proceeding quietly.
````

4. **Phase 2: Build** — replace the numbered steps 5–13 with:

````markdown
**Build Phase Entry Gate**: After the user approves the spec, the FIRST tool call in the
build phase MUST be an `Agent` teammate spawn. Not a code edit, not a `Bash` command, not
a `Write` of scaffolding. This first spawn doubles as your **subagent probe**: if it
errors because you are nested, switch to the subagent hand-off (emit a Spawn Plan). Do
not edit any code until the pool is online.

You author and review. You do NOT claim issues.

5. Spawn the **full worker pool** via the `Agent` tool (FIRST action — no exceptions),
   one spawn per instance, all in a single message so the pool comes up concurrently
6. Open the group's sprint:
   `python3 scripts/jira_bootstrap.py sprint-open --name "Group 1 - interfaces"`.
   Record the returned id in `.claude/specs/<slug>/jira-run.json`
7. Create the Epic (once per spec), then **every issue in the group up front** — full
   description with `Spec:`/`Files:`/`Acceptance:`/`Run:`, `role-*` + `spec-*` +
   `group-*` labels, parent set to the Epic, sprint field set to the group's sprint id,
   and `blocks` links for real dependencies. A deep ready-queue lets all instances
   self-claim and load-balance immediately. Do not drip issues one by one
8. `SendMessage` the pool with the spec path, the sprint name, key context, and interface
   contracts. Tell instances to self-claim from the queue rather than assigning issues
9. Monitor with JQL, not memory:
   `project = AGENT AND sprint in openSprints() ORDER BY status` .
   Respond to impediment flags promptly. Watch for idle instances while `To Do` issues
   remain — that means a dependency or too-coarse issue; split or unblock it.
   **Before you go idle yourself, advance the graph**: after any issue reaches `In Review`,
   dispatch whatever you own next (notably spawning the reviewer once there is something
   to review). A past incident wedged an entire run because the lead idled with unblocked
   work sitting unclaimed
10. Handle blockers: unblock with a decision (log in `decisions.md`), or escalate
11. Teammates run their own verification — do not run it for them; read their comments
11a. Security scans are delegated per the `spec-workflow` skill. Artifacts under
   `.claude/specs/<slug>/`; accepted risks in `security-exceptions.md`
12. **Pipelined parallel review** — designate `review-1` as the **synthesizer** and
    `review-2`..`review-4` as **analysts**, one per reviewable slice. State each
    reviewer's role in its handoff `SendMessage`, and for analysts name the synthesizer
    to report to. Analysts review their slice as it lands and message findings to the
    synthesizer; they close nothing. The synthesizer merges all findings, posts the
    single verdict as a comment on the sprint's `role-review` issue, and — only on PASS —
    transitions the group's issues to `Done`
13. Wait for the synthesizer's single verdict before advancing. Then close the sprint:
    `python3 scripts/jira_bootstrap.py sprint-close --id <id>`, and open the next.
    Do NOT post a verdict yourself (see Review Gate Authority)
13a. **Live-validation gate for IaC / deploy / shell tooling** — unchanged from the
    existing section; static checks are necessary but never sufficient
````

5. **Phase 3: Fix** — replace with:

```markdown
### Phase 3: Fix (if FAIL)
14. Open a fix sprint, create issues for each finding (labelled `group-<n>-fix`), link
    them `blocks` to the findings they resolve, message the pool. Loop to step 9
```

6. **Phase 5: Cleanup** — replace step 15 and 17 with:

```markdown
15. **Confirm completion.** Every issue `Done` on the board and the sprint closed; review
    PASSED; docs updated. Use JQL as the source of truth, not memory of who you spawned:
    `project = AGENT AND labels = spec-<slug> AND status != Done` must return nothing
17. **Sweep teardown residue.** `rm -rf ~/.claude/logs/verified/<projectKey>/` — session
    auto-cleanup does not touch this path. The mirror journal at
    `~/.claude/logs/jira-mirror/` is an audit record; leave it
```

7. **Review Gate Authority** — replace every "`review.md`" with "the review verdict comment", and replace the opening line with:

```markdown
You do NOT post the review verdict. The `review-agent` synthesizer does, as a comment on
the sprint's `role-review` issue, and it is the only role that may transition an issue to
`Done`. Self-review is a category error — grading your own homework defeats the gate.
```

8. **Task Authoring Rules** heading — rename to **Issue Authoring Rules**, replace the `[coding]`-prefix item with "Summary role tag `[coding]` / `[devops]` / `[sa]` / `[review]`, matching the `role-*` label — the format hook blocks a mismatch", and replace the machine-enforcement paragraph with:

```markdown
**This format is machine-enforced** (see `rules/agent-team-protocol.md` → "Enforced Hooks"):
- `createJiraIssue` is **blocked** if the issue lacks the summary role tag, any of the
  `Spec:`/`Files:`/`Acceptance:`/`Run:` sections, or the `role-*`/`spec-*` labels. Author
  the full shape, or add the `skip-format-check` label for a coordination issue.
- The transition to `In Review`/`Done` is **blocked** unless the owning teammate wrote a
  verification sentinel. For analysis issues with no runnable verification (often `[sa]`
  or docs-only), add the `skip-verify` label — otherwise the teammate physically cannot
  advance it. Prefer a real `Run:` command (a lint, validate, `--dry-run`, or query
  check) over a skip label where one exists.
```

9. **Required Skills** table — add:

```markdown
| `jira-workflow` | Issue shape, claim protocol, comment templates — you author issues, so you must know the enforced shape |
```

And in the spawn-prompt guidance, change the required-skills instruction so teammates are told to invoke **`spec-workflow` and `jira-workflow`**. Update the example spawn prompt's claim sentence to:

```markdown
*"...Before claiming any issues: invoke the `spec-workflow` and `jira-workflow` skills via
the Skill tool. Then read the spec at <path>, and immediately self-claim any unclaimed
`role-coding` issue in the open sprint per the claim protocol — do not wait to be assigned.
When you finish one, claim the next. Coordinate with the other `coding-*` instances via
`SendMessage` only on shared interfaces."*
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_docs_have_no_stale_task_store_refs.py -v`
Expected: PASS — 5 passed.

- [ ] **Step 5: Commit**

```bash
git add .claude/agents/fullstack-agent.md tests/test_docs_have_no_stale_task_store_refs.py
git commit -m "docs: rewrite the team lead for a Jira-backed build phase

Backlog authoring moves from tasks.md to Epic + sprint + issues, dispatch
becomes create-all-up-front plus self-claim, monitoring becomes JQL, and the
sprint lifecycle is driven through the bootstrap script. Adds an explicit
rule that the admin API token never reaches a teammate."
```

---

## Task 12: Update the spec-workflow skill and templates

**Files:**
- Modify: `.claude/skills/spec-workflow/SKILL.md`
- Modify: `tests/test_docs_have_no_stale_task_store_refs.py` (extend `MIGRATED`)
- Delete: `docs/specs/templates/review.md`

**Interfaces:**
- Consumes: issue shape from Task 8
- Produces: the workflow narrative both the lead and teammates load

- [ ] **Step 1: Extend the failing test**

Add `".claude/skills/spec-workflow/SKILL.md"` to `MIGRATED`.

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/pytest tests/test_docs_have_no_stale_task_store_refs.py -v`
Expected: FAIL — the skill documents `tasks.md` and the shared task list.

- [ ] **Step 3: Apply the edits**

In `.claude/skills/spec-workflow/SKILL.md`:

1. Replace the **Directory Structure** block:

```markdown
    .claude/specs/<slug>/
      spec.md          # Design decisions, requirements, constraints
      design.md        # Architecture, repo structure, infrastructure design
      jira-run.json    # Generated: Epic key + sprint id per group
      decisions.md     # Mid-flight decision log
      requirements.md  # From /brainstorm (optional)
      prd/             # Product requirements docs (optional)

The backlog is in Jira: one Epic per spec, one Task per unit of work, one sprint per
parallel group. Review verdicts are comments on the sprint's `role-review` issue.
```

2. Replace **Task Format (`tasks.md`)** with **Issue Format**, using the shape from the `jira-workflow` skill (summary role tag, the four description sections, the three labels).

3. Replace **Task Coordination** with:

```markdown
### Coordination

State lives in Jira and nowhere else. Teammates self-claim per the `jira-workflow` skill;
the lead monitors with JQL. Load `jira-workflow` for the claim protocol and comment
templates.
```

4. In **Parallelization Guidelines**, replace "no shared file writes -> same group" with "no shared file writes -> same sprint", and "Make groups **wide**/**few**" with "Make sprints **wide**/**few**".

5. In **Parallel review**, replace "sole author of `review.md`" with "sole author of the verdict comment" and "exactly one `review.md` and one PASS/FAIL per cycle" with "exactly one verdict comment and one PASS/FAIL per cycle".

6. In **Completion criteria**, replace "all tasks `[x]`" with "all issues `Done`".

7. In **Safeguards**, replace the ground-truth sentence with:

```markdown
Ground truth is Jira issue state, then verification sentinels, then `git diff` — not the
mailbox. If Jira is unreachable, stall and escalate rather than inventing state.
```

8. Delete `docs/specs/templates/review.md` (verdicts are comments now) and remove its
   mention from the **Spec and Document Formats** section.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest -q`
Expected: PASS — the whole suite.

- [ ] **Step 5: Commit**

```bash
git add -A .claude/skills/spec-workflow docs/specs/templates tests
git commit -m "docs: update spec-workflow for the Jira backlog

Directory structure drops tasks.md, task format becomes issue format, and
groups become sprints. Removes the review.md template -- verdicts are now
comments on the sprint's review issue."
```

---

## Task 13: Update the README

**Files:**
- Modify: `README.md`

**Interfaces:**
- Consumes: everything above
- Produces: the operator-facing setup path

- [ ] **Step 1: Read the current README to find the sections that describe task tracking**

Run: `grep -n "tasks.md\|TaskCreate\|TaskUpdate\|task store\|settings.json\|hooks/" README.md`

- [ ] **Step 2: Apply the edits**

Rewrite the affected sections so they describe:

1. **Setup** — a new "Jira setup" section placed before any usage instructions:

````markdown
### Jira setup (one time)

The agent team tracks all work in Jira. Two credentials, split by privilege:

**Runtime** — authenticate the Atlassian MCP:
```
/mcp
```
This grants `read:jira-work` and `write:jira-work`, which is everything the agents need.

**Admin** — create a Jira API token at *id.atlassian.com > Security > API tokens*, then:
```bash
export JIRA_SITE=your-site.atlassian.net
export JIRA_EMAIL=you@example.com
export JIRA_API_TOKEN=...

python3 scripts/jira_bootstrap.py ensure-project --key AGENT --name "Agent Team"
python3 scripts/jira_bootstrap.py discover --key AGENT
```

The token acts with your full Jira permissions and is used only by this script — agents
never receive it. `discover` writes `.claude/jira-config.json` with your site's field,
status and transition IDs; nothing is hardcoded, so the same repo works on any site.

**Board prerequisite: the project must have a status named exactly `To Do`.** It is the
one status name the system treats as a literal, because "unclaimed" is encoded as a
status rather than a label. `discover` exits non-zero if it is missing — fix the column
name and re-run rather than working around it, since the failure is invisible at runtime.

If `discover` reports no `In Review` status, add a board column of that name and re-run
it. That one is a warning, not an error: until you add it, only `Done` is gated.
````

2. **Watching a run** — a new section describing the board:

```markdown
### Watching a run

Open the project board. Swimlanes group by the `agent-*` label, so each agent has its own
lane showing exactly what it is working on. The active sprint is the current parallel
group; the backlog holds the groups still to come. A flagged card is blocked, and the
comment on it says why.

Useful filters:
- `project = AGENT AND status = "To Do"` — unclaimed work
- `project = AGENT AND labels = agent-coding-2` — one agent's history
- `project = AGENT AND status = "In Review"` — waiting on the reviewer
```

3. Replace every description of `tasks.md` and the shared task list with the Jira model, and update the hooks section to list the four current hooks with their events.

4. Correct the settings path to `.claude/settings.json` wherever the README references it.

- [ ] **Step 3: Verify no stale references remain**

Run: `grep -n "tasks\.md\|TaskCreate\|TaskUpdate\|TaskList" README.md`
Expected: no output.

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "docs: document Jira setup and board-watching in the README"
```

---

## Task 14: Live end-to-end validation

Static tests cannot catch cloud semantics. Per the project's own live-validation rule, this gate is required before the work is done.

**Files:**
- Create: `.claude/specs/jira-smoke/spec.md` (throwaway)
- Modify: none

**Interfaces:**
- Consumes: every preceding task
- Produces: evidence, or a specific named failure

- [ ] **Step 1: Bootstrap against the real site**

```bash
export JIRA_SITE=mcmilad.atlassian.net
export JIRA_EMAIL=<your atlassian email>
export JIRA_API_TOKEN=<token>
python3 scripts/jira_bootstrap.py ensure-project --key AGENT --name "Agent Team"
python3 scripts/jira_bootstrap.py discover --key AGENT
cat .claude/jira-config.json
```

Expected: a project `AGENT` distinct from `SCRUM`, and a config carrying non-empty
`fields.sprint`, `boardId`, and `statuses`. If `In Review` is absent, follow the printed
instructions, add the column, and re-run `discover`.

- [ ] **Step 2: Confirm the format check blocks a malformed issue**

Ask the lead agent to create an issue in `AGENT` with summary `do the thing` and no
description. Expected: blocked, with stderr naming the missing role tag and sections.

- [ ] **Step 3: Confirm a well-formed issue is created and journalled**

Create three trivial issues (e.g. `[coding] add a greeting module | src/greet.py | greet("x") returns "hello x". Run: python3 -c "..."`), all in one sprint.

```bash
cat ~/.claude/logs/jira-mirror/AGENT.jsonl
```

Expected: three `create` events with correct keys, labels and `status: "To Do"`.

- [ ] **Step 4: Confirm the verify gate blocks and then permits**

Have a teammate claim an issue and attempt `In Review` without a sentinel.
Expected: blocked, stderr naming the exact sentinel path.
Then write the sentinel and retry. Expected: succeeds, and the sentinel file is gone.

- [ ] **Step 5: Confirm the board reads correctly**

Open `https://mcmilad.atlassian.net/jira/software/projects/AGENT/boards/<boardId>` and
confirm: swimlanes by `agent-*` label separate the agents, the active sprint is the group,
a flagged issue shows as blocked, and the completion comment is on the card.

- [ ] **Step 6: Confirm the idle nudge fires**

Leave one `To Do` `role-coding` issue unclaimed and let a coding teammate go idle.
Expected: it is nudged with that issue key, twice, then allowed to idle.

- [ ] **Step 7: Close the sprint and tear down the smoke data**

```bash
python3 scripts/jira_bootstrap.py sprint-close --id <id>
rm -rf ~/.claude/logs/verified/AGENT/
rm -rf .claude/specs/jira-smoke
```

Delete the three smoke issues from the board.

- [ ] **Step 8: Record the result**

If every step passed, note it in the PR description. **If any step failed, do not record a
PASS** — name the specific step, what was expected, and what happened. A green static
suite with a failed live gate is a FAIL.

- [ ] **Step 9: Commit**

```bash
git add -A
git commit -m "chore: complete live end-to-end validation of the Jira backlog"
```

---

## Self-Review

**Spec coverage.** Every decision D1–D7 maps to tasks: D1 → 3–7, D2 → 4, 6, 8, D3 → 7, 11,
D4 → 2, 3, 6, D5 → 9–12, D6 → 8, 10, D7 → 7, 11. Every constraint in the spec's Constraints
table is exercised by a test. The two risks that needed a coded response — absent
`In Review` and unknown transition IDs — are handled in `discover_ids` and the gate's
fail-open path, with tests for both.

**Placeholder scan.** No TBDs, no "add error handling", no "similar to Task N". Every code
step carries the actual code; every doc step carries the actual replacement prose.

**Type consistency.** `load_config` / `mirror_path` / `append_event` / `load_state` /
`issue_project` / `agent_label` are defined in Task 2 and used with those exact names in
Tasks 3, 5 and 6. `verified_dir()` / `nudge_dir()` / `log_dir()` are introduced in Task 5
step 4 and used in Tasks 5 and 6. `JiraAdmin.discover_ids` returns the exact key set the
hooks read (`projectKey`, `transitions`, `gatedStatuses`, `fields`, `boardId`).

**One ordering note:** Task 3 modifies `jira_mirror.py` (config-path override and
call-time `MIRROR_DIR`) that Task 2 created. This is deliberate — Task 2's tests pass
without it, and the need only appears once a hook runs as a subprocess. Task 3 step 4
re-runs Task 2's suite to prove no regression.
