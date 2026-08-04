"""Documentation drift is a real failure mode here: the skill is the only
thing agents read before claiming work. If it names a label or path the hooks
do not enforce, agents follow the doc and the guardrail silently rejects them.
"""
import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILL = os.path.join(REPO, ".claude", "skills", "jira-workflow", "SKILL.md")
FORMAT_HOOK = os.path.join(REPO, ".claude", "hooks", "jira_issue_format_check.py")
GATE_HOOK = os.path.join(REPO, ".claude", "hooks", "jira_transition_verify_gate.py")


def read(path):
    with open(path) as fh:
        return fh.read()


def test_skill_exists_with_frontmatter():
    text = read(SKILL)
    assert text.startswith("---\n")
    assert re.search(r"^name:\s*jira-workflow$", text, re.M)
    assert re.search(r"^description:\s*\S", text, re.M)


def test_skill_documents_every_required_section():
    text = read(SKILL)
    for heading in ("Claim Protocol", "Issue Shape", "Comment Protocol",
                    "Blocked", "Verification Sentinel"):
        assert heading in text, "skill must document: " + heading


def test_skill_documents_the_labels_the_hook_enforces():
    text = read(SKILL)
    for label in ("role-", "spec-", "agent-", "group-",
                  "skip-format-check", "skip-verify"):
        assert label in text


def test_skill_required_sections_match_the_format_hook():
    required = re.search(r"REQUIRED_SECTIONS = \(([^)]*)\)", read(FORMAT_HOOK)).group(1)
    sections = re.findall(r'"([^"]+)"', required)
    text = read(SKILL)
    for section in sections:
        assert section in text, \
            "hook requires {} but the skill never mentions it".format(section)


def test_skill_sentinel_path_matches_the_gate_hook():
    assert ".verified" in read(GATE_HOOK)
    text = read(SKILL)
    assert "~/.claude/logs/verified/" in text
    assert ".verified" in text
