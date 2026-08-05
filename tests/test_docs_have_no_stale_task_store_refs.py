"""After the migration, no agent-facing doc may still instruct agents to use
the built-in task store or tasks.md. A teammate that follows a stale
instruction calls a tool that no longer coordinates anything.
"""
import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DOCS = [
    ".claude/rules/agent-team-protocol.md",
    ".claude/agents/coding-agent.md",
    ".claude/agents/devops-agent.md",
    ".claude/agents/review-agent.md",
    ".claude/agents/sa-agent.md",
    ".claude/agents/fullstack-agent.md",
    ".claude/skills/spec-workflow/SKILL.md",
    # Loaded at every task close-out, so a stale row here is read in the same
    # session as "there is no tasks.md" from the jira-workflow skill.
    ".claude/skills/documentation/SKILL.md",
    # Agent-facing: a slash command the lead runs during brainstorming.
    "commands/brainstorm.md",
    # Not agent-facing, but documents the hook payload/event names directly --
    # just as prone to drifting stale after a migration as the agent docs.
    "SECURITY.md",
    "commands/optimize-my-claude.md",
]

STALE = re.compile(r"\bTaskCreate\b|\bTaskUpdate\b|\bTaskList\b|\bTaskGet\b|tasks\.md"
                   r"|TaskCreated|TaskCompleted")

# Rewritten one task at a time; each task adds its file here.
MIGRATED = [
    ".claude/rules/agent-team-protocol.md",
    ".claude/agents/coding-agent.md",
    ".claude/agents/devops-agent.md",
    ".claude/agents/review-agent.md",
    ".claude/agents/sa-agent.md",
    ".claude/agents/fullstack-agent.md",
    ".claude/skills/spec-workflow/SKILL.md",
    ".claude/skills/documentation/SKILL.md",
    "commands/brainstorm.md",
    "SECURITY.md",
    "commands/optimize-my-claude.md",
]

TEAMMATES = [
    ".claude/agents/coding-agent.md",
    ".claude/agents/devops-agent.md",
    ".claude/agents/review-agent.md",
    ".claude/agents/sa-agent.md",
]


def test_migrated_docs_have_no_task_store_references():
    offenders = []
    for rel in MIGRATED:
        path = os.path.join(REPO, rel)
        with open(path) as fh:
            for lineno, line in enumerate(fh, 1):
                if STALE.search(line):
                    offenders.append("{}:{}: {}".format(rel, lineno, line.strip()))
    assert not offenders, "stale task-store references:\n" + "\n".join(offenders)


def test_every_doc_is_eventually_migrated():
    assert set(MIGRATED) <= set(DOCS)


def test_every_teammate_requires_the_jira_workflow_skill():
    for rel in TEAMMATES:
        with open(os.path.join(REPO, rel)) as fh:
            assert "jira-workflow" in fh.read(), \
                "{} must load jira-workflow before claiming work".format(rel)


def test_spec_directory_blocks_list_every_artifact_an_agent_writes():
    """sa-agent writes .claude/specs/<slug>/sa-review.md, so both documents
    that show the spec directory must list it -- otherwise the lead reads a
    structure that has no room for a file a teammate is told to create."""
    assert "sa-review.md" in open(
        os.path.join(REPO, ".claude", "agents", "sa-agent.md")).read()
    for rel in (".claude/skills/spec-workflow/SKILL.md",
                ".claude/agents/fullstack-agent.md"):
        with open(os.path.join(REPO, rel)) as fh:
            block = re.search(r"\.claude/specs/<slug>/\n(.*?)```", fh.read(), re.S)
        assert block and "sa-review.md" in block.group(1), \
            "{} omits sa-review.md from the spec directory structure".format(rel)


def test_lead_documents_the_bootstrap_and_sprint_lifecycle():
    with open(os.path.join(REPO, ".claude", "agents", "fullstack-agent.md")) as fh:
        text = fh.read()
    for token in ("jira_bootstrap.py", "sprint-open", "sprint-close", "jira-run.json"):
        assert token in text, "lead must document " + token


def test_lead_forbids_handing_the_admin_token_to_teammates():
    with open(os.path.join(REPO, ".claude", "agents", "fullstack-agent.md")) as fh:
        text = fh.read()
    assert "JIRA_API_TOKEN" in text
    assert "never" in text.lower()
