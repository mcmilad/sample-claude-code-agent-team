#!/usr/bin/env python3
"""PostToolUse hook -- journal successful Jira mutations to the local mirror.

Runs AFTER the MCP call so it records what actually succeeded. The mirror is
the only way a hook subprocess can know anything about Jira state: it holds no
OAuth token and cannot call the API.

Scope: only the configured agent project. Issues in any other project (the
operator's real work) are ignored entirely.

Always exits 0. This hook observes; it never gates.
"""
import os
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


def _succeeded(response):
    """A response carrying an error key is a failed call -- do not journal it."""
    if not isinstance(response, dict):
        return bool(response)
    return not any(k in response for k in ("error", "errors", "errorMessages"))


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
    result or a wrapper around it. Guessing wrong is silent -- _succeeded still
    says True, the key is None, nothing is journalled, and the idle check nudges
    nobody while the hook looks correctly installed. So accept the shapes we
    know about and make the miss loud in the audit log.

    The `id` arm is a last resort and only accepted when it is key-shaped
    (contains a '-', e.g. "AGENT-14"). A bare numeric id (the Jira REST
    internal id, e.g. "10042") is NOT an issue key: every later event for the
    issue is addressed by tool_input.issueIdOrKey using the real key, so
    journalling under the numeric id would fork off a second mirror entry
    that stays 'To Do' forever and is unfetchable by any agent -- a phantom
    claimable issue that survives the idle-check's loop guard because it
    never changes state. Reject it the same way as no key at all.
    """
    r = as_dict(response)
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
        event.update({
            "op": "create",
            "key": key,
            "summary": tool_input.get("summary"),
            "labels": _labels_from_create(tool_input),
            "status": "To Do",
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
