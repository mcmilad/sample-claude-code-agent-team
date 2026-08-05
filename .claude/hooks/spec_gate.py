#!/usr/bin/env python3
"""Guardrail that keeps non-trivial work on the spec + Jira rails.

Two modes, one script:

  prompt  (UserPromptSubmit)  Injects the pre-flight checklist when the prompt
                              looks like build work, so the spec/Jira decision
                              is made *before* any file is written.

  write   (PreToolUse: Write) Counts new project source files created this
                              session. Once the work is demonstrably multi-file
                              and no spec has been written, blocks ONCE with the
                              reason. After that single stop it never blocks
                              again for the rest of the session.

Why both: the prompt hook is advisory and can miss unusually-worded requests;
the write hook watches actual behaviour and so cannot be talked out of firing.
Neither can catch a session that legitimately has no spec, hence "block once,
then get out of the way".

The gap this closes: `.claude/rules/agent-team-protocol.md` is written for
*teammates*, so a solo session with no teammates spawned reads as exempt from
the board. `spec-workflow` is not exempt -- its trigger is "touches multiple
files OR involves architectural choices OR will be delegated". A serverless
3-tier app was built across 22 files with no spec and no Epic on exactly that
misreading. See `.claude/rules/spec-and-jira-required.md`.

Design rule, inherited from team_hook_common: FAIL OPEN. Any unexpected
condition resolves to allow. Bypass entirely with CLAUDE_SPEC_GATE=off.
"""
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from team_hook_common import (  # noqa: E402
    allow,
    as_dict,
    audit,
    log_dir,
    read_payload,
    safe_path_component,
)

# Fire the checklist only when a prompt names an action AND a thing to act on.
# Requiring both keeps "add a comment to line 12" from tripping it.
_BUILD_VERB = re.compile(
    r"\b(build|create|generate|implement|add|write|scaffold|set\s?up|"
    r"migrate|refactor|redesign|port|integrate|design|develop|make|stand\s?up)\b",
    re.I,
)
_BUILD_NOUN = re.compile(
    r"\b(app|application|service|api|endpoint|feature|component|module|"
    r"infra|infrastructure|stack|pipeline|system|backend|frontend|schema|"
    r"database|cli|website|site|dashboard|integration|lambda|function|"
    r"architecture|prototype|poc|mvp|tier)\b",
    re.I,
)

_SOURCE_SUFFIXES = (
    ".py", ".js", ".mjs", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".rb",
    ".php", ".sh", ".bash", ".yaml", ".yml", ".json", ".tf", ".tfvars",
    ".html", ".css", ".scss", ".sql", ".toml", ".Dockerfile",
)

# Paths that must never count toward the threshold. `.claude/` is excluded on
# purpose: authoring rules, hooks, and agents IS the meta-work, and blocking it
# would make this gate impossible to repair from inside a session.
_EXCLUDED_SEGMENTS = (
    "/.claude/", "/node_modules/", "/.venv/", "/venv/", "/.aws-sam/",
    "/__pycache__/", "/.superpowers/", "/.git/", "/dist/", "/build/",
    "/.pytest_cache/",
)

NEW_FILE_THRESHOLD = 3

CHECKLIST = """\
[spec-gate] This prompt looks like build work. Before writing any file, settle \
the spec + Jira question and say which way you went:

  1. Does it touch multiple files, involve architectural choices, or get \
delegated? If ANY is true, `spec-workflow` applies -- write \
`.claude/specs/<slug>/spec.md` and `design.md` FIRST.
  2. If a spec applies, the backlog belongs in Jira: create the Epic and the \
role-tagged issues per `jira-workflow` (read `.claude/jira-config.json`; never \
hardcode IDs). This holds for a SOLO session too -- `agent-team-protocol.md` \
is teammate-scoped, but `spec-workflow` is not conditional on teammates \
existing.
  3. Only skip both if the work is genuinely single-file and mechanical. Say so \
in one line, and continue.

Stating assumptions in the reply is not a substitute for a spec."""

BLOCK_REASON = """\
[spec-gate] {count} new source files created this session and no \
`.claude/specs/*/spec.md` has been written.

That crosses the `spec-workflow` trigger ("touches multiple files, involves \
architectural choices, or will be delegated"), which applies whether or not \
teammates were spawned. Do one of these now:

  a. Write `.claude/specs/<slug>/spec.md` + `design.md`, then create the Jira \
Epic and role-tagged issues (see `jira-workflow`, read \
`.claude/jira-config.json` for IDs).
  b. If this really is trivial or the user explicitly opted out of the spec \
loop, say so in one line and retry -- this gate blocks only once per session \
and will not stop you again.

Recent new files: {sample}"""


def state_path(session_id):
    return os.path.join(
        log_dir(), "spec-gate", safe_path_component(session_id, "nosession") + ".json"
    )


def load_state(session_id):
    try:
        with open(state_path(session_id)) as fh:
            return json.load(fh)
    except Exception:
        # First write of the session: anchor "session start" to now, so a spec
        # authored later is distinguishable from one left over from last week.
        return {"started_at": time.time(), "new_files": [], "blocked": False}


def save_state(session_id, state):
    try:
        os.makedirs(os.path.dirname(state_path(session_id)), exist_ok=True)
        with open(state_path(session_id), "w") as fh:
            json.dump(state, fh)
    except Exception:
        pass  # state is best-effort; losing it only makes the gate quieter


def project_dir(payload):
    return (
        os.environ.get("CLAUDE_PROJECT_DIR")
        or payload.get("cwd")
        or os.getcwd()
    )


def is_spec_file(path):
    return "/.claude/specs/" in path and os.path.basename(path) in (
        "spec.md",
        "design.md",
    )


def spec_exists(root):
    """True if any .claude/specs/*/spec.md is on disk right now.

    Deliberately NOT anchored to session start. The previous version compared
    mtime against the session's first Write, which false-blocks the multi-agent
    flow this repo is built around: every spawned teammate gets its own
    session_id, so its anchor post-dates the lead's spec write, and the teammate
    is stopped on its third source file while the spec sits on disk and the Epic
    sits on the board. Freshness bought little and cost that.
    """
    specs = os.path.join(root, ".claude", "specs")
    try:
        for slug in os.listdir(specs):
            if os.path.isfile(os.path.join(specs, slug, "spec.md")):
                return True
    except Exception:
        pass
    return False


def counts_toward_threshold(path, root):
    if not path.startswith(root.rstrip("/") + "/"):
        return False
    if any(seg in path for seg in _EXCLUDED_SEGMENTS):
        return False
    if not path.endswith(_SOURCE_SUFFIXES):
        return False
    # Only brand-new files: editing existing code is not scaffolding.
    return not os.path.exists(path)


def run_prompt_mode(payload):
    prompt = str(payload.get("prompt") or "")
    if not (_BUILD_VERB.search(prompt) and _BUILD_NOUN.search(prompt)):
        allow()

    audit("spec_gate_prompt", {"prompt_len": len(prompt)}, "allow", "checklist injected")
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": CHECKLIST,
                },
                "suppressOutput": True,
            }
        )
    )
    sys.exit(0)


def run_write_mode(payload):
    session_id = payload.get("session_id") or "nosession"
    path = as_dict(payload.get("tool_input")).get("file_path") or ""
    if not isinstance(path, str) or not path:
        allow()

    root = project_dir(payload)
    state = load_state(session_id)

    # Never stand in the way of authoring a spec -- but do NOT mark the session
    # satisfied here. This is PreToolUse: the write has not happened yet, and if
    # it is denied, cancelled, or errors, trusting the intent would disarm the
    # gate for the rest of the session with no spec ever reaching disk.
    if is_spec_file(path):
        save_state(session_id, state)
        allow()

    if state.get("blocked") or state.get("spec_written"):
        save_state(session_id, state)
        allow()

    if counts_toward_threshold(path, root) and path not in state["new_files"]:
        state["new_files"].append(path)

    if len(state["new_files"]) < NEW_FILE_THRESHOLD:
        save_state(session_id, state)
        allow()

    # Checked at threshold time, against the filesystem: by now any spec write
    # announced earlier has either landed or it has not.
    if spec_exists(root):
        state["spec_written"] = True
        save_state(session_id, state)
        allow()

    # Threshold crossed with no spec: stop once, then stay out of the way.
    state["blocked"] = True
    save_state(session_id, state)
    reason = BLOCK_REASON.format(
        count=len(state["new_files"]),
        sample=", ".join(os.path.relpath(p, root) for p in state["new_files"][-5:]),
    )
    audit("spec_gate_write", {"file_path": path}, "block", reason)
    print(reason, file=sys.stderr)
    sys.exit(2)


def main():
    if os.environ.get("CLAUDE_SPEC_GATE", "").lower() in ("off", "0", "false"):
        allow()

    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    payload = read_payload()

    if mode == "prompt":
        run_prompt_mode(payload)
    elif mode == "write":
        run_write_mode(payload)
    allow()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        # A bug in this hook must never block a valid action.
        sys.exit(0)
