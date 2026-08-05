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
import jira_mirror_journal  # noqa: E402  (parse_files -- one parser, two hooks)

EVENT = "PreToolUse"
CREATE = "mcp__plugin_atlassian_atlassian__createJiraIssue"
ROLES = ("coding", "devops", "sa", "review")
SUMMARY_TAG = re.compile(r"^\s*\[(%s)\]\s*\S" % "|".join(ROLES), re.I)
REQUIRED_SECTIONS = ("Spec:", "Files:", "Acceptance:", "Run:")

# Statuses at which an issue has stopped writing. Must agree with claim_gate.py.
TERMINAL_STATUSES = ("In Review", "Done")


def _scope_of(labels):
    """(spec-*, group-*) for an issue, either possibly None."""
    spec = next((l for l in labels if l.startswith("spec-")), None)
    group = next((l for l in labels if l.startswith("group-")), None)
    return spec, group


def files_overlap(project, labels, description):
    """(other_issue_key, overlapping_paths) if this create collides, else None.

    fullstack-agent.md calls sprint-wide Files: disjointness "the sole guarantee
    against conflicts under the shared-tree pool model" and nothing enforced it.
    Since no atomic claim primitive is reachable through Jira, this is the last
    layer between a claim race and clobbered work.

    Scope is spec + group, because the mirror has no sprint field. An issue
    without a spec label is not compared at all -- a missing label is unknown,
    not a match, and blocking on it would reject valid creates.

    TERMINAL_STATUSES must match claim_gate's notion of "landed", or the two
    guardrails contradict: this hook would refuse to create a follow-up issue for
    files whose only other declarer is at In Review, while claim_gate happily
    lets those same files be edited. The disjointness rule exists to stop two
    CONCURRENT writers, and an issue at In Review has finished writing.
    """
    spec, group = _scope_of(labels)
    if not spec:
        return None
    mine = set(jira_mirror_journal.parse_files(description) or [])
    if not mine:
        return None
    try:
        state = jira_mirror.load_state(project)
    except Exception:
        return None
    for key, issue in sorted(state.items()):
        other_labels = [str(l) for l in (issue.get("labels") or [])]
        other_spec, other_group = _scope_of(other_labels)
        if other_spec != spec or other_group != group:
            continue
        if str(issue.get("status") or "") in TERMINAL_STATUSES:
            continue
        clash = mine & set(issue.get("files") or [])
        if clash:
            return key, clash
    return None


def main():
    p = read_payload()
    if p.get("tool_name") != CREATE:
        allow()

    # as_dict, not `or {}` -- see jira_transition_verify_gate: a JSON-string
    # tool_input would otherwise disable this check entirely via fail-open.
    tool_input = as_dict(p.get("tool_input"))
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
    raw_additional_fields = tool_input.get("additional_fields")
    additional_fields_unreadable = (
        raw_additional_fields is not None
        and not isinstance(raw_additional_fields, dict)
    )
    labels = (as_dict(raw_additional_fields).get("labels")) or []
    labels = [str(x) for x in labels] if isinstance(labels, list) else []
    if "skip-format-check" in labels:
        allow(EVENT, p, reason="bypass label present")

    summary = str(tool_input.get("summary") or "")
    description = tool_input.get("description")
    description = description if isinstance(description, str) else ""

    problems = []

    if additional_fields_unreadable:
        # Distinct from "no role-* label" / "no spec-* label": those read as
        # if no labels were supplied at all, when the real defect is that
        # additional_fields arrived as a JSON string rather than an object,
        # so as_dict() correctly yielded no labels out of it. Without naming
        # the field and its shape, a model sees only the label complaints and
        # has every reason to retry with the identical string.
        problems.append(
            "additional_fields was a {} (\"{}\"), not a JSON object -- it must be "
            "a JSON object (e.g. {{\"labels\": [...]}}), not a JSON-encoded "
            "string; as a result no labels could be read from it".format(
                type(raw_additional_fields).__name__, raw_additional_fields)
        )

    tag_match = SUMMARY_TAG.match(summary)
    if not tag_match:
        problems.append(
            "summary must start with a role tag -- one of {}".format(
                " ".join("[%s]" % r for r in ROLES))
        )

    missing_sections = [s for s in REQUIRED_SECTIONS if s not in description]
    if missing_sections:
        problems.append("description is missing: {}".format(", ".join(missing_sections)))

    # A bare-substring `Files:` check accepted shapes the journaller's anchored
    # parser cannot read (a bolded label, a bullet list, the word mid-sentence).
    # Those creates passed while journalling no paths at all, silently blinding
    # both the overlap check and claim_gate. Require a form both agree on.
    elif not jira_mirror_journal.parse_files(description):
        problems.append(
            "Files: is present but no paths could be parsed from it. Put it on its "
            "own line as `Files: a/b.py, c/d.py` -- comma-separated, no bullets and "
            "no markdown emphasis on the label. The paths are journalled and drive "
            "the overlap check and the claim gate, so an unparseable list silently "
            "disables both."
        )

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

    overlap = files_overlap(project, labels, description)
    if overlap:
        problems.append(
            "Files: overlaps {} in the same spec+group, which already declares {}. "
            "No two issues in one scope may write the same file -- this is the only "
            "guarantee against concurrent teammates clobbering each other. Split the "
            "work so the paths are disjoint, or sequence the two issues into "
            "different groups.".format(overlap[0], ", ".join(sorted(overlap[1])))
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
