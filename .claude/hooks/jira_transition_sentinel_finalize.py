#!/usr/bin/env python3
"""PostToolUse hook on transitionJiraIssue -- phase two of the sentinel consume.

jira_transition_verify_gate.py (PreToolUse) renames <ISSUE-KEY>.verified to
<ISSUE-KEY>.inflight before the MCP call runs. That rename is the consume: it
takes effect immediately, so a second concurrent transition finds no .verified
and is blocked. This hook decides what happens to the .inflight once the call
has actually returned:

  affirmative success -> delete it. The sentinel is spent.
  anything else       -> restore it to .verified, so the retry is not blocked.

SCOPE: only for the transitions the gate actually consumes for, i.e. GATED
ones. Gated-ness is resolved by importing the gate's own target_status() /
is_gated() rather than re-deriving them, so the two phases of the consume
cannot drift. A PostToolUse for an ungated transition (In Progress -- the
documented claim step) is not the other half of any consume and must leave the
sentinel strictly alone.

BIAS ON AMBIGUITY: RESTORE. The failure this exists to fix was a *destroyed*
sentinel permanently wedging a legitimate close (AGENT-11, 2026-08-05T05:47:48Z:
the gate allowed and consumed, no transition ever landed, and the issue could
never reach Done). Restoring risks at most one extra authorized transition for a
sentinel that was legitimately earned; not restoring wedges the board. That
asymmetry is the whole reason for the two-phase design, and it matches the
repo-wide fail-open rule.

Why success cannot be decided the way jira_mirror_journal.py decides it: that
module's _succeeded() treats ANY non-empty string as success, and a bare string
is the live tool_response shape for a real teammate transition -- so an error
string reads as success there. That is the actual hole, and it is where this
module is stricter: a bare string must not merely be non-empty, it must not read
as an error.

For STRUCTURED responses (a dict, or a content block that decodes to one) this
module keeps the journal's live-verified convention: no error key means success.
Requiring a specific success key instead would misfire on real successes -- the
response shape is explicitly not contractual -- and restoring a sentinel that was
genuinely spent would permit a second gated transition, breaking the one property
the sentinel exists to guarantee.

Shapes carrying no evidence either way (None, empty string, empty list, a bare
number) are treated as failure, i.e. restore.

This hook never gates -- PostToolUse cannot block anyway. Always exits 0.
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from team_hook_common import (  # noqa: E402
    read_payload, allow, audit, as_dict, safe_path_component, verified_dir,
)
import jira_mirror  # noqa: E402
import jira_transition_verify_gate as verify_gate  # noqa: E402

EVENT = "PostToolUse"
TRANSITION = "mcp__plugin_atlassian_atlassian__transitionJiraIssue"

_ERROR_KEYS = ("error", "errors", "errorMessages", "errorMessage")

# Substrings that mark an error rendered as prose. Deliberately broad: a false
# "this failed" only restores a sentinel the agent had legitimately earned,
# whereas a false "this succeeded" deletes it and wedges the issue.
_ERROR_TEXT = re.compile(
    r"\b(error|errors|failed|failure|unauthor\w*|forbidden|not found|"
    r"invalid|denied|timeout|timed out|exception|rate limit)\b|"
    r"\b(4\d\d|5\d\d)\b",
    re.I,
)


def _base(project, issue_key):
    return os.path.join(
        verified_dir(),
        safe_path_component(project, default="_noproject"),
        safe_path_component(issue_key, default="_noissue"),
    )


def _decode_content_blocks(response):
    """MCP results often arrive as [{'type':'text','text':'<json>'}]."""
    if not isinstance(response, list) or not response:
        return None
    for item in response:
        text = as_dict(item).get("text")
        if not isinstance(text, str):
            continue
        try:
            parsed = json.loads(text)
        except Exception:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _structured_verdict(obj):
    """Decide a decoded object. Explicit beats inferred."""
    if any(k in obj for k in _ERROR_KEYS):
        return False
    # {"success": false} carries no error KEY but is unambiguously a failure;
    # reading it as success would delete a sentinel for a transition that never
    # landed -- the AGENT-11 wedge.
    if isinstance(obj.get("success"), bool):
        return obj["success"]
    return True


def transition_succeeded(response):
    """True only on affirmative evidence the transition landed.

    Order matters, and the string branch is the one that bites. A JSON object
    SERIALIZED AS A STRING is a live shape here, and the prose regex must never
    see it: keyword-scanning serialized JSON reads the issue's own embedded text,
    so a description containing "memory 512 MB" trips the 4xx pattern and a
    landed transition is misread as failed (resurrecting a spent sentinel), while
    '{"errorMessages": [...]}' contains no standalone word "error" and is misread
    as SUCCESS (deleting the sentinel for a transition that failed). Both were
    reproduced against real recorded responses. So: always try to decode a string
    before falling back to prose.

    Structured responses otherwise follow the journal's live-verified convention
    (no error key -> success). Evidence-free shapes return False so the sentinel
    is restored -- the safe direction.
    """
    if response is None:
        return False

    decoded = _decode_content_blocks(response)
    if decoded is not None:
        return _structured_verdict(decoded)

    obj = as_dict(response)
    if obj:
        return _structured_verdict(obj)

    if isinstance(response, bool):
        return response

    if isinstance(response, list):
        text = " ".join(
            str(as_dict(i).get("text") or "") for i in response
        ).strip()
    elif isinstance(response, str):
        text = response.strip()
    else:
        return False

    if not text:
        return False

    # Decode before scanning prose -- see the docstring.
    try:
        parsed = json.loads(text)
    except Exception:
        parsed = None
    if isinstance(parsed, dict):
        return _structured_verdict(parsed)
    if isinstance(parsed, bool):
        return parsed

    return not _ERROR_TEXT.search(text)


def main():
    p = read_payload()
    if p.get("tool_name") != TRANSITION:
        allow()

    tool_input = as_dict(p.get("tool_input"))
    cfg = jira_mirror.load_config()
    project = cfg.get("projectKey")
    if not project:
        allow(EVENT, p, reason="no projectKey configured -- nothing to finalize")

    issue_key = safe_path_component(
        tool_input.get("issueIdOrKey"), default="_noissue"
    )
    if jira_mirror.issue_project(issue_key) != project:
        allow(EVENT, p, reason="issue belongs to another project -- ignored")

    # Only a GATED transition consumes a sentinel, so only a gated transition
    # may finalize one. Resolved through the gate's own helpers so the two
    # phases cannot drift. Without this check a PostToolUse for an UNGATED
    # transition -- In Progress, the documented claim step, run on every issue
    # -- adjudicates an .inflight it never created: on success it deletes a
    # legitimate attestation (the next gated retry gets "no sentinel"), and on
    # failure it restores one that is still in flight, so the gated call that
    # then lands finds nothing to spend and a single attestation authorizes
    # both In Review and Done.
    #
    # Unknown id, or config missing/changed -> target is None -> do nothing.
    # Fail-open here means touch NOTHING: the gate fails open the same way, so
    # it created no .inflight either, and if it did (config changed mid-flight)
    # the file is stale-reclaimable rather than wrongly spent.
    target = verify_gate.target_status(cfg, tool_input)
    if not target or not verify_gate.is_gated(cfg, target):
        allow(EVENT, p, reason=(
            "transition of {} to {} is not gated -- no sentinel of ours to finalize"
        ).format(issue_key, target or "<unknown>"))

    base = _base(project, issue_key)
    inflight = base + ".inflight"
    if not os.path.exists(inflight):
        # skip-verify, or the gate failed open without consuming.
        allow(EVENT, p, reason="no in-flight sentinel for {} -- nothing to do".format(
            issue_key))

    if transition_succeeded(p.get("tool_response")):
        try:
            os.remove(inflight)
        except OSError:
            pass
        allow(EVENT, p, reason="transition of {} succeeded -- sentinel spent".format(
            issue_key))

    try:
        os.rename(inflight, base + ".verified")
    except OSError:
        pass
    allow(EVENT, p, reason=(
        "transition of {} did not affirmatively succeed -- sentinel restored".format(
            issue_key)))


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:  # FAIL OPEN
        audit(EVENT, {}, "allow", reason="hook error (fail-open): {}".format(e))
        sys.exit(0)
