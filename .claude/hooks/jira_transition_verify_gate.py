#!/usr/bin/env python3
"""PreToolUse hook on transitionJiraIssue -- the verification gate.

Replaces the old TaskCompleted gate. A transition into a gated status
(In Review, Done) is blocked unless the acting teammate left a sentinel at
    ~/.claude/logs/verified/<PROJECT>/<ISSUE-KEY>.verified
written after that issue's `Run:` command passed. One sentinel authorizes at
most one SUCCESSFUL transition.

TWO-PHASE CONSUME. This hook does not delete the sentinel -- it atomically
renames it to <ISSUE-KEY>.inflight. The rename is the consume: it takes effect
immediately, so a second concurrent transition finds no .verified and is
blocked. jira_transition_sentinel_finalize.py (PostToolUse) then deletes the
.inflight on affirmative success, or restores it to .verified otherwise.

Why not delete here: the MCP call had not happened yet. A 401, 429, 5xx or
timeout destroyed the attestation and permanently blocked the retry -- observed
live on AGENT-11 (2026-08-05T05:47:48Z), where the gate allowed and consumed but
no transition ever landed, so the issue could never close. Why not move the
whole consume to PostToolUse: that would leave the gate check-only for the
duration of the round trip, letting one sentinel authorize two gated
transitions.

If the tool is never invoked at all (denied, cancelled), no PostToolUse fires
and the .inflight lingers; it becomes reclaimable after INFLIGHT_STALE_SECONDS
so a legitimate retry is never permanently wedged.

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
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from team_hook_common import (  # noqa: E402
    read_payload, allow, block, audit, as_dict, tool_input_of,
    safe_path_component, verified_dir,
)
import jira_mirror  # noqa: E402

EVENT = "PreToolUse"
TRANSITION = "mcp__plugin_atlassian_atlassian__transitionJiraIssue"
DEFAULT_GATED = ("In Review", "Done")

# How long an .inflight may sit before a retry may reclaim it. Covers a normal
# MCP round trip with wide margin; only reached when PostToolUse never fired,
# i.e. the tool call was denied or cancelled rather than executed.
INFLIGHT_STALE_SECONDS = 300


# --- gated-ness resolution -------------------------------------------------
# Shared with jira_transition_sentinel_finalize.py, which imports these rather
# than re-deriving them. The finalizer must act on exactly the transitions this
# gate consumes a sentinel for and no others: a second copy of this rule lets
# the two phases of the consume drift, and an ungated PostToolUse then spends
# or resurrects an .inflight that belongs to a gated transition.

def target_status(cfg, tool_input):
    """The status this transitionJiraIssue call lands the issue in, or None.

    transitionJiraIssue takes a TRANSITION ID, not a target status name, so the
    id is resolved through the transitions map that bootstrap discovers. An id
    that is absent or not in the map resolves to unknown -- and unknown is never
    gated, so it never blocks here and is never finalized there.

    as_dict on `transition` too, not `or {}`: every field here is model-authored,
    and a truthy non-dict (transition arriving as the bare id STRING) sails past
    `or {}` and raises on the next .get(). Both hooks would then resolve this
    call through their outer fail-open handler instead of through this function
    -- exiting 0 with an empty audit payload, indistinguishable from "not
    gated". This must be total so the two phases agree even on junk.
    """
    tid = str(as_dict(as_dict(tool_input).get("transition")).get("id", ""))
    return (cfg.get("transitions") or {}).get(tid) or None


def is_gated(cfg, target):
    """Whether landing in `target` requires a consumed sentinel.

    isinstance, not truthiness: `x or DEFAULT` cannot tell "discovery found no
    gated status on this board" ([]) from "nobody configured this" (absent).
    Falling back on [] gates two status names the board does not have, so
    nothing is really gated while the audit log claims a gate is in force. An
    explicit [] is honoured as-is; bootstrap fails at setup (exit 5) rather
    than shipping an empty gate set.
    """
    configured = cfg.get("gatedStatuses")
    gated = configured if isinstance(configured, list) else list(DEFAULT_GATED)
    return target in gated


def _sentinel_base(project, issue_key):
    # Both components come from the payload and are attacker-influenceable; this
    # path is passed to os.rename/os.remove, so each is reduced to a single safe
    # path component that cannot escape the verified directory.
    return os.path.join(
        verified_dir(),
        safe_path_component(project, default="_noproject"),
        safe_path_component(issue_key, default="_noissue"),
    )


def sentinel_path(project, issue_key):
    return _sentinel_base(project, issue_key) + ".verified"


def inflight_path(project, issue_key):
    return _sentinel_base(project, issue_key) + ".inflight"


def main():
    p = read_payload()
    if p.get("tool_name") != TRANSITION:
        allow()

    # tool_input_of, not as_dict: a tool_input arriving as a JSON STRING is a
    # documented model failure mode, and as_dict() turns it into {} -- whereupon
    # issueIdOrKey is empty, the project-scope check below decides the issue is
    # not ours, and the gate exits 0. An unverified transition sails through AND
    # the audit log blames "another project", which is false and sends whoever
    # reads it looking in the wrong place. tool_input_of decodes the string
    # instead, so the gate judges the real issue key.
    tool_input, unreadable = tool_input_of(p)
    if unreadable:
        # Genuinely unreadable: the issue key is unknowable, so we cannot tell
        # an issue in our project from the operator's real client work. Fail
        # open per the house rule -- but name the actual cause. (Nothing is
        # smuggled past the gate this way: a tool_input the hook cannot parse
        # as JSON is not one the MCP server can act on either.)
        allow(EVENT, p, reason=(
            "verification gate did not run: {} -- issue key and project "
            "unknowable, fail-open".format(unreadable)))
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

    # as_dict here too: this line is only message material, but it runs BEFORE
    # target_status and would otherwise raise past it, undoing its totality.
    transition_id = str(as_dict(tool_input.get("transition")).get("id", ""))
    target = target_status(cfg, tool_input)
    if not target:
        allow(EVENT, p, reason="transition id {} not in discovered map -- fail-open".format(
            transition_id))

    if not is_gated(cfg, target):
        allow(EVENT, p, reason="target status {} is not gated".format(target))

    issue = jira_mirror.load_state(project).get(issue_key) or {}
    if "skip-verify" in (issue.get("labels") or []):
        allow(EVENT, p, reason="skip-verify label present")

    path = sentinel_path(project, issue_key)
    inflight = inflight_path(project, issue_key)

    if os.path.exists(path):
        try:
            # Atomic consume. A peer that renamed first makes this raise, and
            # that peer now owns the one transition this sentinel authorizes.
            os.rename(path, inflight)
        except OSError:
            if not os.path.exists(path):
                block(EVENT, p, (
                    "Transition of {} to '{}' blocked: its sentinel was consumed by "
                    "another transition that is still in flight. One sentinel authorizes "
                    "one transition -- wait for that one to land, or attest again."
                ).format(issue_key, target))
            # Rename failed but the sentinel is still there (e.g. a read-only
            # log dir). Fail open rather than trap a verified transition.
            allow(EVENT, p, reason="sentinel present but not consumable -- fail-open")
        allow(EVENT, p, reason="verified via sentinel for {} -> {}".format(
            issue_key, target))

    if os.path.exists(inflight):
        try:
            stale = (time.time() - os.path.getmtime(inflight)) > INFLIGHT_STALE_SECONDS
        except OSError:
            stale = False
        if stale:
            # PostToolUse never fired, so the previous call was never executed.
            # Permit the retry and re-stamp so two retries cannot both pass.
            try:
                os.utime(inflight, None)
            except OSError:
                pass
            allow(EVENT, p, reason="stale in-flight sentinel reclaimed for {}".format(
                issue_key))
        block(EVENT, p, (
            "Transition of {} to '{}' blocked: a transition using this sentinel is "
            "already in flight. If that call failed, the sentinel is restored "
            "automatically -- retry in a moment."
        ).format(issue_key, target))

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


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:  # FAIL OPEN
        audit(EVENT, {}, "allow", reason="hook error (fail-open): {}".format(e))
        sys.exit(0)
