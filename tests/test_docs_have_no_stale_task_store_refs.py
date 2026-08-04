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
]

STALE = re.compile(r"\bTaskCreate\b|\bTaskUpdate\b|\bTaskList\b|\bTaskGet\b|tasks\.md"
                   r"|TaskCreated|TaskCompleted")

# Rewritten one task at a time; each task adds its file here.
MIGRATED = [".claude/rules/agent-team-protocol.md"]


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
