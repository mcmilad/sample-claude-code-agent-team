#!/usr/bin/env python3
"""PreToolUse hook on createJiraIssue -- enforce the task issue shape.

Replaces the old TaskCreated format check. Exit 2 prevents the malformed issue
from being created at all, which is strictly better than the rollback-after-
creation the task store allowed.

Required shape for a Task in the agent project:
    Summary:     [coding|devops|sa|review] <verb> <what>
    Description: Spec: / Files: / Acceptance: / Run: sections
    Labels:      one role-*, one spec-*

Epics are exempt -- they describe a spec, not a unit of work.
Bypass: the skip-format-check label, for coordination or research issues.

Scope: only the configured agent project. The site holds real client work; a
guardrail that blocked issue creation there would be a serious defect.

FAIL OPEN: unparseable payload, missing config, or any internal error allows.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from team_hook_common import read_payload, allow, block, audit, as_dict  # noqa: E402
import jira_mirror  # noqa: E402

EVENT = "PreToolUse"
CREATE = "mcp__plugin_atlassian_atlassian__createJiraIssue"
ROLES = ("coding", "devops", "sa", "review")
SUMMARY_TAG = re.compile(r"^\s*\[(%s)\]\s*\S" % "|".join(ROLES), re.I)
REQUIRED_SECTIONS = ("Spec:", "Files:", "Acceptance:", "Run:")


def main():
    p = read_payload()
    if p.get("tool_name") != CREATE:
        allow()

    tool_input = p.get("tool_input") or {}
    cfg = jira_mirror.load_config()
    project = cfg.get("projectKey")
    if not project:
        allow(EVENT, p, reason="no projectKey configured -- fail-open")
    if (tool_input.get("projectKey") or "") != project:
        allow(EVENT, p, reason="issue targets another project -- not policed")

    if str(tool_input.get("issueTypeName", "")).lower() == "epic":
        allow(EVENT, p, reason="epics are exempt from the task shape")

    # as_dict, not `or {}`: additional_fields commonly arrives as a JSON string.
    # `or {}` lets that through and .get() raises, the outer handler fails open,
    # and a role-tagless, section-less, label-less issue is created unchecked.
    # Unreadable labels are treated as no labels -- including bypass labels.
    labels = (as_dict(tool_input.get("additional_fields")).get("labels")) or []
    labels = [str(x) for x in labels] if isinstance(labels, list) else []
    if "skip-format-check" in labels:
        allow(EVENT, p, reason="bypass label present")

    summary = str(tool_input.get("summary") or "")
    description = tool_input.get("description")
    description = description if isinstance(description, str) else ""

    problems = []

    tag_match = SUMMARY_TAG.match(summary)
    if not tag_match:
        problems.append(
            "summary must start with a role tag -- one of {}".format(
                " ".join("[%s]" % r for r in ROLES))
        )

    missing_sections = [s for s in REQUIRED_SECTIONS if s not in description]
    if missing_sections:
        problems.append("description is missing: {}".format(", ".join(missing_sections)))

    role_labels = [l for l in labels if l.startswith("role-")]
    if not role_labels:
        problems.append("no role-* label (e.g. role-coding)")
    if not any(l.startswith("spec-") for l in labels):
        problems.append("no spec-* label (e.g. spec-auth-api)")

    # A summary tag that disagrees with the role label would route the issue to
    # one pool while reading as another's -- catch it rather than let the board lie.
    if tag_match and role_labels:
        tagged = "role-" + tag_match.group(1).lower()
        if tagged not in role_labels:
            problems.append(
                "summary tag and role label disagree: summary says {}, labels say {}".format(
                    tagged, ", ".join(role_labels))
            )

    if problems:
        block(EVENT, p, (
            "Jira issue creation blocked by the format check:\n  - {}\n\n"
            "Required shape:\n"
            "  Summary:     [coding|devops|sa|review] <verb> <what>\n"
            "  Description: Spec: <path>\\nFiles: <paths>\\nAcceptance: <criteria>\\nRun: <command>\n"
            "  Labels:      spec-<slug>, role-<role>\n\n"
            "For a coordination or research issue that genuinely has no verification, "
            "add the skip-format-check label."
        ).format("\n  - ".join(problems)))

    allow(EVENT, p, reason="issue format ok")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:  # FAIL OPEN
        audit(EVENT, {}, "allow", reason="hook error (fail-open): {}".format(e))
        sys.exit(0)
