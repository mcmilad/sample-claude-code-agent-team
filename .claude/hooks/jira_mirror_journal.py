#!/usr/bin/env python3
"""PostToolUse hook -- journal successful Jira mutations to the local mirror.

Runs AFTER the MCP call so it records what actually succeeded. The mirror is
the only way a hook subprocess can know anything about Jira state: it holds no
OAuth token and cannot call the API.

Scope: only the configured agent project. Issues in any other project (the
operator's real work) are ignored entirely.

Always exits 0. This hook observes; it never gates.
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from team_hook_common import read_payload, allow, audit, as_dict  # noqa: E402
import jira_mirror  # noqa: E402

EVENT = "PostToolUse"
PREFIX = "mcp__plugin_atlassian_atlassian__"
CREATE = PREFIX + "createJiraIssue"
TRANSITION = PREFIX + "transitionJiraIssue"
EDIT = PREFIX + "editJiraIssue"
COMMENT = PREFIX + "addCommentToJiraIssue"
WATCHED = (CREATE, TRANSITION, EDIT, COMMENT)

_ERROR_KEYS = ("error", "errors", "errorMessages")

# Verified live against a real Jira site: the harness
# delivers tool_response as a list of content blocks --
# [{"type": "text", "text": "<issue JSON as a string>"}] -- not the bare MCP
# result object. That is the confirmed, primary shape for createJiraIssue
# responses. The `isinstance(response, dict)` branches in _succeeded/_created_key
# below are a fallback for a bare-object shape this repo has not observed live,
# kept in case the harness ever changes what it hands back.


def _parse_content_block_object(response):
    """Extract the first JSON object embedded in a content-block list.

    The harness does not always hand back the bare MCP result. Live capture
    from a real createJiraIssue call shows it instead wraps the result as a
    list of content blocks:
    `[{"type": "text", "text": "<issue JSON as a string>"}, ...]`.
    Walk the entries and decode the first dict entry whose `text` is a
    string that parses to a JSON object.

    Returns None if `response` is not a list, or no block's `text` decodes
    to an object. None means "could not determine" -- it is NOT proof of
    failure. Only an explicit error marker inside a successfully decoded
    object is treated as a failure signal; an undecodable block is a
    shape-recognition problem for the caller to diagnose, not evidence the
    call itself failed.
    """
    if not isinstance(response, list):
        return None
    for block in response:
        if not isinstance(block, dict):
            continue
        text = block.get("text")
        if not isinstance(text, str):
            continue
        try:
            parsed = json.loads(text)
        except Exception:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _succeeded(response):
    """A response carrying an error key is a failed call -- do not journal it.

    `tool_response` does not always arrive as a bare dict -- see
    _parse_content_block_object. An empty list carries no evidence of
    success at all and stays a failure, matching the old `bool(response)`
    behaviour for falsy values. A non-empty list that decodes to an object
    is judged the same way a bare dict is: an error key means failure.
    A non-empty list that does NOT decode to an object is not reported as a
    failure here -- misreporting an unrecognized-shape success as "the call
    failed" would bury it under the wrong, non-diagnosable audit reason.
    Whether a key can be found in it is _created_key's job, and that path
    logs its own distinguishable reason when it comes up empty.
    """
    if isinstance(response, dict):
        return not any(k in response for k in _ERROR_KEYS)
    if isinstance(response, list):
        if not response:
            return False
        parsed = _parse_content_block_object(response)
        if parsed is not None:
            return not any(k in parsed for k in _ERROR_KEYS)
        return True
    return bool(response)


def parse_files(description):
    """Extract the `Files:` list from an issue description, or None.

    fullstack-agent.md calls sprint-wide Files: disjointness "the sole guarantee
    against conflicts under the shared-tree pool model", but nothing could check
    it: the mirror recorded summary/labels/status and never the description, so
    no hook could see the declared paths. Journalling the parsed list is what
    makes jira_issue_format_check's overlap check possible at all.

    Deliberately forgiving -- a path this fails to parse is simply not enforced,
    which is the status quo, whereas over-parsing would block a valid create.

    Every parsed path is normalised (jira_mirror.normalize_path) so it is
    directly comparable with the os.path.relpath() output claim_gate matches
    against. Unnormalised, a single './'-prefixed or doubled-slash entry
    journalled a string nothing would ever equal, taking that file out of both
    the overlap check and the claim gate with no signal at all.
    """
    if not isinstance(description, str):
        return None
    match = re.search(r"^\s*Files:\s*(.+)$", description, re.M)
    if not match:
        return None
    paths = []
    for raw in match.group(1).split(","):
        path = jira_mirror.normalize_path(raw.strip().strip("`").rstrip(".").strip())
        if path:
            paths.append(path)
    return paths or None


def _files_from_description(description):
    return parse_files(description)


def _labels_from_create(tool_input):
    # as_dict, not `or {}`: additional_fields commonly arrives as a JSON string,
    # on which .get() raises. The outer handler would then fail open and journal
    # NOTHING -- and since only a create event ever writes status 'To Do', that
    # issue would stay invisible to the idle work-check forever, even after
    # later edits restore its labels. Unreadable labels lose the labels, not the
    # create event.
    labels = as_dict(tool_input.get("additional_fields")).get("labels")
    return labels if isinstance(labels, list) else None


def _created_key(response):
    """Extract the new issue key from a create response.

    The exact shape is not contractual: the harness may hand back the raw MCP
    result, a wrapper around it, or -- confirmed via live capture from a real
    createJiraIssue call -- a list of content blocks whose `text` is the
    issue JSON serialized as a string (see _parse_content_block_object).
    Guessing wrong is silent -- _succeeded still says True, the key is None,
    nothing is journalled, and the idle check nudges nobody while the hook
    looks correctly installed. So accept the shapes we know about and make
    the miss loud in the audit log.

    Bare-dict handling comes first and is untouched: if `response` is
    already a non-empty dict, the content-block path is never consulted.

    The `id` arm is a last resort and only accepted when it is key-shaped
    (contains a '-', e.g. "AGENT-14"). A bare numeric id (the Jira REST
    internal id, e.g. "10042") is NOT an issue key: every later event for the
    issue is addressed by tool_input.issueIdOrKey using the real key, so
    journalling under the numeric id would fork off a second mirror entry
    that stays 'To Do' forever and is unfetchable by any agent -- a phantom
    claimable issue that survives the idle-check's loop guard because it
    never changes state. Reject it the same way as no key at all. This guard
    applies identically whether the id arrived in a bare dict or was decoded
    out of a content block.
    """
    r = as_dict(response)
    if not r:
        block_obj = _parse_content_block_object(response)
        if block_obj is not None:
            r = block_obj
    for candidate in (r.get("key"), as_dict(r.get("issue")).get("key")):
        if candidate:
            return str(candidate)
    raw_id = r.get("id")
    if raw_id and "-" in str(raw_id):
        return str(raw_id)
    return None


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
        key = _created_key(p.get("tool_response"))
        if not key:
            # Distinguishable on purpose: this is the diagnosable signature of a
            # journaller that is installed but blind, and it is only ever
            # visible in ~/.claude/logs/team-hooks.jsonl.
            allow(EVENT, p, reason=(
                "create succeeded but no issue key could be extracted -- "
                "unrecognized create response shape (keys: {})".format(
                    ",".join(sorted(as_dict(p.get("tool_response")))) or "none")))
        # createJiraIssue exposes a top-level `transition` (verified against the
        # live schema), so an issue can be created straight into In Progress.
        # Hardcoding 'To Do' mirrored such an issue as unclaimed work and -- since
        # a create carries no agent-* label -- advertised it to every teammate of
        # that role. An absent or unrecognized id still falls back to To Do.
        created_id = str(as_dict(tool_input.get("transition")).get("id", ""))
        created_status = (cfg.get("transitions") or {}).get(created_id) or "To Do"
        event.update({
            "op": "create",
            "key": key,
            "summary": tool_input.get("summary"),
            "labels": _labels_from_create(tool_input),
            "status": created_status,
            "files": _files_from_description(tool_input.get("description")),
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
