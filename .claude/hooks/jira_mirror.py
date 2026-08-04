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
