"""The spec/Jira guardrail must stop real drift and never trap a session.

Every case runs the hook as a subprocess, the way the harness invokes it, so
the exit-code contract (0 = proceed, 2 = block) is exercised end to end rather
than asserted against internal functions.
"""
import json
import os
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOK = os.path.join(REPO, ".claude", "hooks", "spec_gate.py")


@pytest.fixture
def run(tmp_path, monkeypatch):
    """Invoke the hook with an isolated project root and log dir."""
    home = tmp_path / "home"
    project = tmp_path / "project"
    (project / ".claude" / "specs").mkdir(parents=True)
    home.mkdir()

    def _run(mode, payload, env=None):
        environ = dict(os.environ)
        environ["HOME"] = str(home)
        environ["CLAUDE_PROJECT_DIR"] = str(project)
        environ.pop("CLAUDE_SPEC_GATE", None)
        environ.update(env or {})
        return subprocess.run(
            [sys.executable, HOOK, mode],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=environ,
        )

    _run.project = project
    return _run


def write_payload(path, session="s1"):
    return {
        "session_id": session,
        "tool_name": "Write",
        "tool_input": {"file_path": str(path)},
    }


# --- prompt mode ------------------------------------------------------------


@pytest.mark.parametrize(
    "prompt",
    [
        "generate a serverless 3-tier application for a lab",
        "build me a REST api with auth",
        "add a caching module to the backend",
        "set up the deployment pipeline",
    ],
)
def test_build_prompts_inject_the_checklist(run, prompt):
    result = run("prompt", {"prompt": prompt})

    assert result.returncode == 0
    context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "spec-workflow" in context
    assert "SOLO session" in context


@pytest.mark.parametrize(
    "prompt",
    [
        "what does line 12 do?",
        "why did the last test fail",
        "add a comment explaining this regex",
        "fix the typo in the readme",
    ],
)
def test_ordinary_prompts_stay_silent(run, prompt):
    result = run("prompt", {"prompt": prompt})

    assert result.returncode == 0
    assert result.stdout.strip() == ""


# --- write mode -------------------------------------------------------------


def test_blocks_on_the_third_new_source_file(run):
    src = run.project / "src"
    for name in ("a.py", "b.ts"):
        assert run("write", write_payload(src / name)).returncode == 0

    blocked = run("write", write_payload(src / "c.tf"))

    assert blocked.returncode == 2
    assert "spec-workflow" in blocked.stderr
    assert "3 new source files" in blocked.stderr


def test_blocks_only_once_per_session(run):
    """A guardrail that traps a session is worse than no guardrail."""
    src = run.project / "src"
    for name in ("a.py", "b.py", "c.py"):
        run("write", write_payload(src / name))

    assert run("write", write_payload(src / "d.py")).returncode == 0
    assert run("write", write_payload(src / "e.py")).returncode == 0


def test_writing_a_spec_clears_the_gate(run):
    """The spec must actually reach disk. This hook is PreToolUse, so it fires
    *before* the write -- announcing a spec write cannot satisfy an existence
    check at the moment it is announced, and trusting the announcement would let
    a denied or cancelled write disarm the gate for the whole session."""
    spec = run.project / ".claude" / "specs" / "demo" / "spec.md"
    assert run("write", write_payload(spec)).returncode == 0
    spec.parent.mkdir(parents=True, exist_ok=True)
    spec.write_text("# spec")

    for name in ("a.py", "b.py", "c.py", "d.py"):
        assert run("write", write_payload(run.project / "src" / name)).returncode == 0


def test_an_announced_spec_that_never_lands_does_not_disarm_the_gate(run):
    """A Write denied by permissions, cancelled, or errored must not count."""
    spec = run.project / ".claude" / "specs" / "demo" / "spec.md"
    assert run("write", write_payload(spec)).returncode == 0  # announced only

    src = run.project / "src"
    run("write", write_payload(src / "a.py"))
    run("write", write_payload(src / "b.py"))
    assert run("write", write_payload(src / "c.py")).returncode == 2


def test_a_spec_from_an_earlier_session_still_satisfies_the_gate(run):
    """Each spawned teammate has its own session_id, so anchoring on session
    start false-blocked every teammate of a lead that had already written the
    spec -- the exact multi-agent flow this repo promotes."""
    spec = run.project / ".claude" / "specs" / "demo" / "spec.md"
    spec.parent.mkdir(parents=True, exist_ok=True)
    spec.write_text("# written by the lead, before this teammate existed")
    old = 1
    os.utime(spec, (old, old))

    src = run.project / "src"
    for name in ("a.py", "b.py", "c.py", "d.py"):
        assert run("write", write_payload(src / name, session="teammate")).returncode == 0


def test_sessions_are_counted_independently(run):
    src = run.project / "src"
    for name in ("a.py", "b.py"):
        run("write", write_payload(src / name, session="one"))

    # A third file in a *different* session must not inherit the first's count.
    assert run("write", write_payload(src / "c.py", session="two")).returncode == 0


@pytest.mark.parametrize(
    "relative",
    [
        ".claude/rules/new-rule.md",
        ".claude/hooks/new_hook.py",
        "README.md",
        "docs/design.md",
        "notes.txt",
        "node_modules/pkg/index.js",
        ".venv/lib/thing.py",
    ],
)
def test_docs_and_tooling_never_count(run, relative):
    """`.claude/**` is exempt so the gate can always be repaired from inside."""
    for _ in range(5):
        result = run("write", write_payload(run.project / relative))
        assert result.returncode == 0


def test_existing_files_are_not_scaffolding(run):
    """Editing established code is not the signal we are watching for."""
    src = run.project / "src"
    src.mkdir()
    for name in ("a.py", "b.py", "c.py", "d.py"):
        target = src / name
        target.write_text("# already here\n")
        assert run("write", write_payload(target)).returncode == 0


def test_files_outside_the_project_do_not_count(run, tmp_path):
    outside = tmp_path / "elsewhere"
    for name in ("a.py", "b.py", "c.py", "d.py"):
        assert run("write", write_payload(outside / name)).returncode == 0


def test_the_bypass_switch_works(run):
    src = run.project / "src"
    for name in ("a.py", "b.py", "c.py", "d.py"):
        result = run(
            "write", write_payload(src / name), env={"CLAUDE_SPEC_GATE": "off"}
        )
        assert result.returncode == 0


# --- fail-open contract -----------------------------------------------------


@pytest.mark.parametrize("mode", ["prompt", "write", "", "bogus-mode"])
@pytest.mark.parametrize(
    "raw",
    [
        "",
        "not json at all",
        "[]",
        '{"tool_input": "a string, not an object"}',
        '{"tool_input": {"file_path": null}}',
        '{"prompt": 12345}',
    ],
)
def test_malformed_payloads_always_fail_open(run, tmp_path, mode, raw):
    """A bug in the hook must never block a valid action."""
    environ = dict(os.environ)
    environ["HOME"] = str(tmp_path / "home")
    environ["CLAUDE_PROJECT_DIR"] = str(run.project)
    environ.pop("CLAUDE_SPEC_GATE", None)

    result = subprocess.run(
        [sys.executable, HOOK, mode] if mode else [sys.executable, HOOK],
        input=raw,
        capture_output=True,
        text=True,
        env=environ,
    )

    assert result.returncode == 0


def test_session_ids_cannot_escape_the_state_directory(run, tmp_path):
    """session_id reaches a filesystem path, so traversal must be neutralised."""
    hostile = "../../../../tmp/pwned"
    run("write", write_payload(run.project / "src" / "a.py", session=hostile))

    assert not os.path.exists("/tmp/pwned.json")
