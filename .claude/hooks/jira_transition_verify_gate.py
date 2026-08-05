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

    # Sanitize before the project check (not just before the path join): a
    # crafted key like 'AGENT-../../../../etc/passwd' still has the real
    # project as its literal prefix, which would pass an unsanitized project
    # check and only get caught at path-build time -- by then we've already
    # decided this issue is "ours" and gone down the blocking path. Reducing
    # to a safe component first means a traversal payload is judged by what it
    # resolves to (an unrecognized bare key, project-less), so it falls out at
    # the project-scope check instead, before any path is built or blocked on.
    issue_key = safe_path_component(
        tool_input.get("issueIdOrKey"), default="_noissue"
    )
    if jira_mirror.issue_project(issue_key) != project:
        allow(EVENT, p, reason="issue belongs to another project -- not gated")

    transition_id = str((tool_input.get("transition") or {}).get("id", ""))
    target = (cfg.get("transitions") or {}).get(transition_id)
    if not target:
        allow(EVENT, p, reason="transition id {} not in discovered map -- fail-open".format(
            transition_id))

    # isinstance, not truthiness: `x or DEFAULT` cannot tell "discovery found no
    # gated status on this board" ([]) from "nobody configured this" (absent).
    # Falling back on [] gates two status names the board does not have, so
    # nothing is really gated while the audit log claims a gate is in force. An
    # explicit [] is honoured as-is; bootstrap fails at setup (exit 5) rather
    # than shipping an empty gate set.
    configured = cfg.get("gatedStatuses")
    gated = configured if isinstance(configured, list) else list(DEFAULT_GATED)
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
            "Attest, then retry -- whichever applies to you: if you ran this issue's "
            "`Run:` command, attest that it passed; if you are the reviewer closing after "
            "a PASS verdict, attest that verdict instead.\n"
            "  mkdir -p {}\n"
            "  echo '<what you attested> PASSED' > {}\n\n"
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
