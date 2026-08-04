"""load_team_tasks must not raise for a nonexistent team -- it is called
unconditionally, outside any local try/except, by teammate_idle_workcheck.py.

Regression coverage for a bug where team_hook_common.tasks_dir() didn't exist:
load_team_tasks referenced the bare name TASKS_DIR, which had been deleted when
the module's other path constants were converted to call-time functions. The
NameError was swallowed by the hook's own top-level fail-open wrapper, so the
hook looked healthy (exit 0) while silently never nudging anyone.
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, ".claude", "hooks"))

import team_hook_common  # noqa: E402


def test_load_team_tasks_returns_dict_for_nonexistent_team(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert team_hook_common.load_team_tasks("no-such-team") == {}


def test_load_team_tasks_reads_task_files_under_tasks_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    team_dir = tmp_path / ".claude" / "tasks" / "myteam"
    team_dir.mkdir(parents=True)
    (team_dir / "1.json").write_text('{"id": "1", "status": "pending"}')
    tasks = team_hook_common.load_team_tasks("myteam")
    assert tasks == {"1": {"id": "1", "status": "pending"}}


def test_tasks_dir_resolves_home_at_call_time(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert team_hook_common.tasks_dir() == os.path.join(str(tmp_path), ".claude", "tasks")
