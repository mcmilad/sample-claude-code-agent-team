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
from team_hook_common import read_payload, allow, audit  # noqa: E402
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
    extra = tool_input.get("additional_fields") or {}
    labels = extra.get("labels")
    return labels if isinstance(labels, list) else None


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
        response = p.get("tool_response") or {}
        key = response.get("key") if isinstance(response, dict) else None
        if not key:
            allow(EVENT, p, reason="create response carried no issue key")
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
