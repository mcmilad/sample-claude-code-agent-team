"""The claim protocol is stated in five places. They must not drift.

Four are prose (an always-on rule, a skill, two agent system prompts) and the
fifth is generated at runtime by `teammate_idle_workcheck.py`, which injects the
whole recipe into a teammate's context at the exact moment it is about to claim.
That fifth copy is the highest-leverage one and was previously guarded by
nothing: a doc-only fix would ship alongside a hook still teaching the old,
broken protocol.

Two properties matter, and both are load-bearing:

  1. Every copy teaches the `mkdir` lock as the thing that decides ownership.
     Jira cannot arbitrate the race -- `editJiraIssue` has no compare-and-swap,
     and transitions are global so both racers' move to `In Progress` succeeds.

  2. No copy teaches the unreachable tie-break. `editJiraIssue` REPLACES the
     labels array, so a loser's write erases the winner's label and exactly one
     survives -- which means "if two agent-* labels are present" is a state the
     API can never produce, and any rule keyed on it never fires.
"""
import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SKILL = os.path.join(REPO, ".claude", "skills", "jira-workflow", "SKILL.md")
PROTOCOL = os.path.join(REPO, ".claude", "rules", "agent-team-protocol.md")
CODING = os.path.join(REPO, ".claude", "agents", "coding-agent.md")
DEVOPS = os.path.join(REPO, ".claude", "agents", "devops-agent.md")
IDLE_HOOK = os.path.join(REPO, ".claude", "hooks", "teammate_idle_workcheck.py")

CLAIMING_DOCS = [SKILL, PROTOCOL, CODING, DEVOPS]
ALL_COPIES = CLAIMING_DOCS + [IDLE_HOOK]

# Phrasings of the tie-break that lost-update makes unreachable.
DEAD_TIEBREAK = re.compile(
    r"lowest instance name|more than one `?agent-\*|two `?agent-\* labels are present",
    re.I,
)


def read(path):
    with open(path) as fh:
        return fh.read()


def prose(path):
    """Body with markdown emphasis stripped, so a phrase match is not defeated
    by someone bolding a word inside it."""
    return read(path).replace("*", "").replace("`", "")


def test_every_copy_teaches_the_lock_as_the_claim():
    for path in ALL_COPIES:
        body = read(path)
        assert "claims/" in body and "mkdir" in body, (
            "{} must teach the mkdir claim lock -- it is the only step that "
            "actually decides ownership".format(os.path.relpath(path, REPO))
        )


def test_no_copy_teaches_the_unreachable_tiebreak():
    for path in ALL_COPIES:
        match = DEAD_TIEBREAK.search(prose(path))
        assert not match, (
            "{} still teaches the '{}' tie-break. editJiraIssue replaces the "
            "labels array, so two agent-* labels never coexist and this rule "
            "can never fire.".format(os.path.relpath(path, REPO), match.group(0))
        )


def test_every_claiming_copy_requires_the_in_progress_transition():
    """The reported symptom: 8 of 11 worked issues never entered In Progress, so
    the lead's monitor JQL reported in-flight work as unstarted."""
    for path in CLAIMING_DOCS:
        assert "In Progress" in read(path), (
            "{} must require the In Progress transition before any file is "
            "edited".format(os.path.relpath(path, REPO))
        )


def test_the_runtime_nudge_agrees_with_the_skill():
    """The hook's injected recipe is a real copy of the protocol, not a summary."""
    hook = read(IDLE_HOOK)
    for token in ("mkdir ~/.claude/logs/claims/", "In Progress", "issuelinks"):
        assert token in hook, (
            "the idle nudge must carry '{}' -- it is delivered at the moment of "
            "claiming and is the copy an agent is most likely to act on".format(token)
        )


def test_the_explicit_fields_projection_warns_that_it_replaces_defaults():
    """getJiraIssue's `fields` REPLACES the default set. A projection that omits
    `labels` makes the claim's read-modify-write erase role-*/spec-*/group-*."""
    for path in (SKILL, PROTOCOL, IDLE_HOOK):
        body = prose(path)
        assert "issuelinks" in body
        assert re.search(r"replaces? the default", body, re.I), (
            "{} prescribes an explicit fields list but never warns that it "
            "replaces the defaults".format(os.path.relpath(path, REPO))
        )


def test_confirm_step_is_fail_open():
    """An absent label with no competitor is an unconfirmed write, not a loss.
    Failing closed there turns a stale read into a self-inflicted orphan."""
    for path in (SKILL, PROTOCOL, CODING, DEVOPS):
        assert re.search(r"unconfirmed|fail open", prose(path), re.I), (
            "{} must say that an absent label with no competing agent-* is "
            "unconfirmed rather than lost".format(os.path.relpath(path, REPO))
        )


def test_reviewers_are_told_not_to_self_claim():
    """There is one role-review card per sprint and it belongs to the
    synthesizer; a reviewer pool that self-claims manufactures the duplicate
    verdict the one-synthesizer rule exists to prevent."""
    body = prose(os.path.join(REPO, ".claude", "agents", "review-agent.md"))
    assert re.search(r"do not self-claim", body, re.I)


def test_the_release_path_exists():
    """Without an explicit release, an issue orphaned at In Progress is
    unreachable: it is invisible to the claim JQL and to the idle check."""
    for path in (SKILL, os.path.join(REPO, ".claude", "agents", "fullstack-agent.md")):
        body = prose(path)
        assert re.search(r"releas", body, re.I), (
            "{} must document returning an abandoned claim to the pool".format(
                os.path.relpath(path, REPO))
        )
