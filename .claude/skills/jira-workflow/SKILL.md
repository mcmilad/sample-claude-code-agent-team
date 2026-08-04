---
name: jira-workflow
description: Claim, work, and close Jira issues as an agent-team teammate — claim protocol, issue shape, comment templates, and the verification sentinel. Load before claiming any work when the team is Jira-backed.
---

# Jira Workflow

Jira is the system of record for the agent team's backlog. There is no `tasks.md`.
All per-issue work goes through the Atlassian MCP.

Read `.claude/jira-config.json` first — it holds this site's `cloudId`, `projectKey`,
custom field IDs, status IDs, and transition IDs. **Never hardcode an ID**; they differ
per site.

## Issue Shape

Every Task issue in the agent project must carry:

```
Summary:     [coding|devops|sa|review] <verb> <what>
Parent:      the spec Epic
Sprint:      the group's sprint (config fields.sprint)
Labels:      spec-<slug>, role-<role>, group-<n>
Description:
  Spec:       .claude/specs/<slug>/spec.md#<section>
  Files:      comma-separated paths this issue may write
  Acceptance: what must be true when it is done
  Run:        the verification command
```

This is machine-enforced. `createJiraIssue` is **blocked** if the summary has no role tag,
the description is missing any of `Spec:`, `Files:`, `Acceptance:`, `Run:`, or the
`role-*` / `spec-*` labels are absent. It is also blocked if the summary tag and the
`role-*` label disagree.

Bypass with the `skip-format-check` label for a coordination or research issue that
genuinely has no verification. Epics are exempt.

## Claim Protocol

Jira has no compare-and-swap, and up to twelve agents claim concurrently. Correctness
comes from read-after-write with a deterministic tie-break.

**1. Find** — unclaimed work is a *status*, not a label. JQL cannot wildcard labels, so
never try to express "has no `agent-*` label" in JQL:

```
project = <projectKey> AND sprint in openSprints()
  AND status = "To Do" AND labels = role-coding
  ORDER BY Rank ASC
```

**2. Claim** — `editJiraIssue` **replaces** the labels array, so this is read-modify-write:

```
getJiraIssue(issueIdOrKey)                      -> current labels
editJiraIssue(fields.labels = current + ["agent-coding-2"])
transitionJiraIssue(transition.id = <To Do -> In Progress>)
```

Resolve the transition id from `config.transitions` (or `getTransitionsForJiraIssue`).
`transitionJiraIssue` takes a transition **id**, never a status name.

**3. Confirm** — re-read the issue. If more than one `agent-*` label is present, two
instances raced. **The lowest instance name wins, lexicographically.** The loser removes
its own label and returns to step 1. Both agents evaluate the same rule on the same data
and reach the same verdict without talking to each other.

**4. Work** — only the files listed in `Files:`. Peers run concurrently; editing outside
your declared paths clobbers them.

## Verification Sentinel

Before transitioning to `In Review` or `Done`, run the issue's `Run:` command and attest
that it passed:

```bash
mkdir -p ~/.claude/logs/verified/<projectKey>
echo "<the Run command> PASSED" > ~/.claude/logs/verified/<projectKey>/<ISSUE-KEY>.verified
```

The transition is **blocked** without it. The sentinel is consumed on success, so one
sentinel permits one transition. A hook cannot watch you run tests — this file is your
attestation, so only write it after the command actually passed.

Bypass: the `skip-verify` label, for issues with no runnable verification.

## Comment Protocol

Four comments per issue. Enough that the card tells the whole story; not so many that
the signal drowns.

**On claim:**
```
Claimed by coding-2.
```

**On verification:**
```
Verification: `pytest tests/auth -q` PASSED
14 passed in 2.1s
```

**On completion:**
```
Done. Added POST /login with token issuance via AuthToken.
Files: src/auth/login.py, tests/auth/test_login.py
```

**On blocker** — see below.

Review findings are comments on the issue they concern. The synthesizer's PASS/FAIL
verdict is a comment on the sprint's `role-review` issue.

## Blocked

Do not invent a Blocked status — the project may not have one. Use Jira's native
impediment flag, whose field id is `config.fields.flagged`:

```
editJiraIssue(fields = {<flagged field>: [{"value": "Impediment"}]})
addCommentToJiraIssue("BLOCKED: <specific blocker>. Needs: <what would unblock>.")
```

Then `SendMessage` the lead. Clear the flag by setting the field to `null` when unblocked.

## Closing

An implementer must **not** close its own issue: transition to `In Review` and stop
there. Only the review synthesizer moves `In Review` -> `Done`, and only on a PASS
verdict.

**Not enforced.** The gate hook checks only that a sentinel exists — not who is
transitioning, nor what the prior status was. This is a protocol convention, not a
guardrail: an agent that writes a second sentinel can self-close. Treat a `Done`
transition with no synthesizer verdict comment as a review-gate violation.

## Label Vocabulary

| Label | Meaning | Hook-enforced? |
|---|---|---|
| `spec-<slug>` | Which spec this belongs to | Yes |
| `role-coding` \| `role-devops` \| `role-sa` \| `role-review` | Which pool may claim it | Yes |
| `agent-<instance>` | Who claimed it — drives board swimlanes | No (convention only) |
| `group-<n>` | Which parallel group; survives sprint closure | No (convention only) |
| `skip-format-check` | Exempt from the create-time shape check | Yes |
| `skip-verify` | Exempt from the sentinel gate | Yes |

## When Jira Is Unreachable

Stall and escalate to the lead. Do not invent state, do not work an issue you could not
claim, and do not mark anything complete out of band. There is no `tasks.md` fallback by
design — a half-tracked run is worse than a paused one.
