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

Up to twelve agents claim concurrently. **Jira cannot arbitrate that race**, so the
filesystem does.

Two verified facts force this design, and both contradict the obvious approach:

- **`editJiraIssue` has no compare-and-swap.** Its schema is
  `{cloudId, issueIdOrKey, fields, contentFormat, responseContentFormat}` — no version, no
  ETag, no `update` verb. Writing labels is read-modify-write, so a loser's write silently
  **erases** the winner's label and exactly one survives. Any rule of the form "if two
  `agent-*` labels are present, ..." is therefore unreachable and must not be written.
- **Transitions are global** (`isGlobal: true` on every id). Moving an issue to
  `In Progress` never fails, even from `In Progress`. **A transition cannot be a claim** —
  both racers succeed.

So: ownership is decided by an atomic `mkdir` on the shared filesystem. The Jira label and
the `In Progress` transition are the **board-visible mirror** of a decision already made,
never the thing being contended.

**1. Find** — unclaimed work is a *status*, not a label. JQL cannot wildcard labels, so
never try to express "has no `agent-*` label" in JQL:

```
project = <projectKey> AND sprint in openSprints()
  AND status = "To Do" AND labels = role-coding
  ORDER BY Rank ASC
```

**2. Lock** — `mkdir` is a POSIX atomic test-and-set: exactly one caller creates the
directory, every other gets an error. This is the actual claim.

```bash
CLAIMS=~/.claude/logs/claims/<projectKey>
mkdir -p "$CLAIMS"
if mkdir "$CLAIMS/<ISSUE-KEY>" 2>/dev/null; then
  echo "<your-instance-name>" > "$CLAIMS/<ISSUE-KEY>/owner"
  date -u +%Y-%m-%dT%H:%M:%SZ > "$CLAIMS/<ISSUE-KEY>/heartbeat"
  echo "CLAIMED"
else
  echo "LOST -- owned by $(cat "$CLAIMS/<ISSUE-KEY>/owner" 2>/dev/null || echo unknown)"
fi
```

`LOST` is **deterministic, not inferred**. Return to step 1 and pick another issue; do not
touch the board and do not touch the files.

**3. Read the issue — with an explicit, complete field list.**

```
getJiraIssue(issueIdOrKey,
             fields=["labels","status","summary","description","issuelinks"])
```

> **`fields` REPLACES the default set.** `fields=["issuelinks"]` returns issuelinks and
> **no labels** — and step 4 then writes labels back, which would erase `role-*`,
> `spec-*` and `group-*` from the issue permanently. Any explicit list **must** include
> `labels`.

Check `issuelinks` before proceeding. Skip the issue (releasing the lock with
`rm -rf`, **not** `rmdir` — step 2 put `owner` and `heartbeat` inside it, so `rmdir`
fails and silently orphans the issue) if it has an inward `is blocked by` whose blocker
is still `To Do` or
`In Progress`. A blocker at **`In Review` counts as satisfied** — its `Files:` are on disk
by then, and nothing reaches `Done` until the group closes, so waiting for `Done` would
deadlock every intra-group dependency. If the `issuelinks` key is **absent** from the
response, treat it as UNKNOWN and escalate to the lead — never as "no blockers".

**4. Mirror the claim onto the board** — label first, then transition, both *before* you
edit any file. This is what tells the lead the issue is taken.

```
editJiraIssue(fields.labels = <labels from step 3> + ["agent-coding-2"])
transitionJiraIssue(transition.id = <To Do -> In Progress>)
addCommentToJiraIssue("Claimed by coding-2.")
```

Resolve the transition id from `config.transitions` (or `getTransitionsForJiraIssue`).
`transitionJiraIssue` takes a transition **id**, never a status name.

**5. Confirm — fail open, not closed.** Re-read with a **new `getJiraIssue`** (never read
labels off the `editJiraIssue` response: that is a write-time snapshot and cannot show a
later overwrite). Then:

| What you see | What it means | What to do |
|---|---|---|
| My `agent-*` label present | Claim confirmed | Work it |
| My label absent, **another** `agent-*` present | A peer's write erased mine | You still hold the lock, so this is a stale peer. Comment the conflict, re-apply your label, and continue |
| My label absent, **no** `agent-*` at all | Unconfirmed write, not a loss | Re-apply the label once, then re-read. Still absent → escalate |

The lock in step 2 already decided ownership, so an absent label is a **bookkeeping**
failure, not an ownership one. Never abandon an issue you hold the lock on just because a
read came back stale — message delivery and the MCP are both laggy, and treating that as a
loss manufactures orphans.

**6. Work** — only the files listed in `Files:`. Peers run concurrently; editing outside
your declared paths clobbers them. Refresh the heartbeat at each verification step:

```bash
date -u +%Y-%m-%dT%H:%M:%SZ > ~/.claude/logs/claims/<projectKey>/<ISSUE-KEY>/heartbeat
```

**7. Release** — after the issue reaches `In Review`, drop the lock so the key is reusable:

```bash
rm -rf ~/.claude/logs/claims/<projectKey>/<ISSUE-KEY>
```

### Ground truth for claims is the lock; for state it is Jira

A deliberate split. Ownership is a mutual-exclusion question the filesystem can answer and
Jira cannot. Status, labels, comments and the verdict remain Jira's. The lock assumes a
shared `$HOME` — it does not span hosts, and under `isolation: worktree` it only works if
`$HOME` is shared.

### Recovering an abandoned claim

An issue stuck in `In Progress` is invisible to the Find JQL *and* to the idle work-check,
so nothing reclaims it automatically. Recovery is the lead's, and it is
**investigate-first**:

1. The lead's sweep surfaces candidates — `status = "In Progress"` with a `heartbeat`
   older than **60 minutes** (well clear of the ~29-minute legitimate verification pass
   that has caused a bad takeover before). **A stale heartbeat is a reason to look, never
   evidence of death.**
2. The lead then runs the full liveness protocol in `fullstack-agent.md` — direct
   `SendMessage` with a bounded reply window, check the disk for partial output and
   sentinels — before doing anything.
3. Only on positive evidence of death, the lead performs an explicit **logged release**:

```bash
rm -rf ~/.claude/logs/claims/<projectKey>/<ISSUE-KEY>       # free the lock
```
```
editJiraIssue(fields.labels = <labels minus the dead agent-* label>)
transitionJiraIssue(transition.id = <back to To Do>)
addCommentToJiraIssue("Released from <instance>: <the positive evidence>. Reclaimable.")
```

Recovery is **respawn-and-reclaim**, never lead takeover. Without the release the issue is
unreachable: a fresh instance cannot see it (wrong status) and a same-named respawn would
pass every ownership check trivially, so two live workers would edit the same files with
nothing detecting it.

## Verification Sentinel

Before transitioning to `In Review` or `Done`, run the issue's `Run:` command and attest
that it passed:

```bash
mkdir -p ~/.claude/logs/verified/<projectKey>
echo "<the Run command> PASSED" > ~/.claude/logs/verified/<projectKey>/<ISSUE-KEY>.verified
```

The transition is **blocked** without it. The sentinel is consumed on success, so one
sentinel permits **one** transition — the reviewer closing the issue later must write its
own (see "Closing"). A hook cannot watch you run tests — this file is your attestation, so
only write it after the command actually passed.

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

**The closer writes its own sentinel.** `In Review` and `Done` are *both* gated, and the
sentinel is consumed on success — so the one the implementer wrote is already gone by the
time the issue reaches `In Review`. Before transitioning `In Review` -> `Done`, the
synthesizer writes a fresh sentinel attesting the review verdict:

```bash
mkdir -p ~/.claude/logs/verified/<projectKey>
echo "review verdict PASS" > ~/.claude/logs/verified/<projectKey>/<ISSUE-KEY>.verified
```

Without it the `Done` transition is blocked and the issue can never close.

**Not enforced.** The gate hook checks only that a sentinel exists — not who is
transitioning, nor what the prior status was — and Jira transitions in a team-managed
project are any status to any status. So an implementer can transition `To Do` -> `Done`
directly, in a single call, skipping `In Review` entirely; this is not a two-step
workaround via a second sentinel, it takes one transition. This is a protocol convention,
not a guardrail. Treat a `Done` transition with no synthesizer verdict comment as a
review-gate violation.

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
