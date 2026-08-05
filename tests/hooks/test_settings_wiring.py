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
