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


def test_every_shipped_hook_entrypoint_is_wired_into_settings():
    """The reverse direction of the check above, and the one that actually
    catches the silent fault: that one fails when an entry points at a missing
    file, but deleting the entry outright makes it pass. A hook script shipped
    in .claude/hooks/ that settings.json never invokes simply never fires --
    tested, reviewed, and dead.

    An entry point is a script with a `__main__` guard, so the shared modules
    (team_hook_common.py, jira_mirror.py) are exempt by construction rather than
    by a hand-maintained name list that would drift the same way.
    """
    with open(SETTINGS) as fh:
        wired = json.dumps(json.load(fh).get("hooks", {}))

    hooks_dir = os.path.join(REPO, ".claude", "hooks")
    entrypoints = []
    for name in sorted(os.listdir(hooks_dir)):
        if not name.endswith(".py"):
            continue
        with open(os.path.join(hooks_dir, name)) as fh:
            if '__name__ == "__main__"' in fh.read():
                entrypoints.append(name)

    assert entrypoints, "expected at least one hook entry point in .claude/hooks/"

    for name in entrypoints:
        assert name in wired, (
            ".claude/hooks/{} is an executable hook (it has a __main__ guard) "
            "but settings.json never invokes it -- it will never fire".format(name)
        )
