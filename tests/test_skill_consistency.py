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

# Every doc that tells an agent how an issue gets closed. The gate consumes the
# sentinel on success and gates `Done` as well as `In Review`, so a closer that
# does not write its own sentinel can never close anything -- if any of these
# omits that, the documented happy path is unrunnable.
CLOSING_DOCS = [
    SKILL,
    os.path.join(REPO, ".claude", "agents", "review-agent.md"),
    os.path.join(REPO, ".claude", "rules", "agent-team-protocol.md"),
]


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


def test_closing_docs_tell_the_closer_to_write_its_own_sentinel():
    """The gate consumes the sentinel on success and gates `Done` too, so the
    implementer's sentinel is gone by the time the reviewer closes. Every doc
    that describes closing must say the closer writes a fresh one, or the
    documented happy path terminates at `In Review` forever.
    """
    for path in CLOSING_DOCS:
        text = read(path)
        assert "review verdict PASS" in text, \
            "{} must show the closer writing its own sentinel".format(path)
        assert ".verified" in text


def test_no_closing_doc_claims_the_gate_spares_the_reviewer():
    """The gate keys on the target status, not the actor. A doc saying it
    'normally doesn't gate you' teaches the reviewer to skip the one step
    without which no issue can ever close.
    """
    for path in CLOSING_DOCS:
        text = read(path).lower()
        for claim in ("doesn't gate you", "does not gate you", "not you directly"):
            assert claim not in text, "{} claims the gate spares the reviewer".format(path)


def test_skill_sentinel_path_matches_the_gate_hook():
    assert ".verified" in read(GATE_HOOK)
    text = read(SKILL)
    assert "~/.claude/logs/verified/" in text
    assert ".verified" in text


# --- Reverse-direction checks -----------------------------------------------
#
# The tests above only catch under-claiming: "the hook requires X, does the
# skill mention X?" They cannot catch a skill that claims a section or a label
# is machine-enforced when no hook actually checks it -- an over-claim that
# reads as a guardrail but is not one. That is precisely the failure class a
# prior review caught in the Closing section ("An implementer cannot close its
# own issue", stated in the document's enforced-fact register, when nothing in
# jira_transition_verify_gate.py checks who is transitioning). These tests
# assert the reverse direction: everything the skill's structured blocks (the
# Issue Shape code block, the Label Vocabulary table's "Yes" rows) present as
# required/enforced must actually appear in the hook source as a real check.
#
# Anchored to those structured blocks rather than a whole-file scan, so
# unrelated substrings -- e.g. "spec-" inside "spec-workflow" -- can't
# false-positive a match.

def _issue_shape_description_sections(text):
    """Section labels indented two spaces under `Description:` in the Issue
    Shape code block -- these are the ones the skill presents as required
    sub-fields, as distinct from top-level issue metadata like Summary:/Labels:.
    """
    block = re.search(r"## Issue Shape\n.*?```\n(.*?)```", text, re.S).group(1)
    return set(re.findall(r"^  ([A-Z][a-zA-Z]*:)", block, re.M))


def _split_table_row(row):
    """Split a markdown table row into cells, respecting `\\|`-escaped pipes
    inside a cell (used by the role-* alternatives cell) as literal characters
    rather than column separators.
    """
    protected = row.strip().strip("|").replace(r"\|", "\x00")
    return [c.strip().replace("\x00", "|") for c in protected.split("|")]


def _label_vocabulary_rows(text):
    section = re.search(r"## Label Vocabulary\n\n(.*?)\n\n", text, re.S).group(1)
    lines = [ln for ln in section.splitlines() if ln.startswith("|")]
    return [_split_table_row(ln) for ln in lines[2:]]  # skip header + separator


def _label_token(label):
    """Reduce a concrete label like "role-coding" or "spec-<slug>" (the latter
    already truncated to "spec-" by the backtick-token regex, which stops at
    the first non [a-z-] character) to the prefix a hook check would test:
    "role-coding" -> "role-", "spec-" -> "spec-", "skip-verify" -> "skip-verify"
    (skip-* labels are matched as exact literals by the hooks, not prefixes).
    """
    if label.startswith("skip-"):
        return label
    base = label.rstrip("-")
    if "-" in base:
        return base.rsplit("-", 1)[0] + "-"
    return base + "-"


def _hook_enforced_label_tokens():
    fmt, gate = read(FORMAT_HOOK), read(GATE_HOOK)
    tokens = set(re.findall(r'startswith\("([a-z]+-)"\)', fmt))
    tokens.update(re.findall(r'"(skip-[a-z-]+)"', fmt + gate))
    return tokens


def test_skill_issue_shape_required_sections_are_all_hook_enforced():
    """Reverse of test_skill_required_sections_match_the_format_hook: every
    section the Issue Shape block presents as required must be one the format
    hook's REQUIRED_SECTIONS actually checks.
    """
    text = read(SKILL)
    required = re.search(r"REQUIRED_SECTIONS = \(([^)]*)\)", read(FORMAT_HOOK)).group(1)
    hook_sections = set(re.findall(r'"([^"]+)"', required))
    for section in _issue_shape_description_sections(text):
        assert section in hook_sections, \
            "skill's Issue Shape claims {} is required but the hook does not check it".format(section)


def test_skill_label_vocabulary_enforced_labels_are_all_hook_checked():
    """Reverse of test_skill_documents_the_labels_the_hook_enforces: a label
    the Label Vocabulary table marks "Hook-enforced? Yes" must correspond to
    an actual check in one of the shipped hooks -- catching the over-claim
    class of bug (a label presented as guarded when it is convention only).
    """
    text = read(SKILL)
    hook_tokens = _hook_enforced_label_tokens()
    for cells in _label_vocabulary_rows(text):
        label_cell, _meaning, enforced_cell = cells
        if not enforced_cell.lower().startswith("yes"):
            continue
        for label in re.findall(r"`([a-z][a-z-]*)", label_cell):
            token = _label_token(label)
            assert token in hook_tokens, \
                "skill marks {} (label {}) as hook-enforced but no hook checks it".format(token, label)
