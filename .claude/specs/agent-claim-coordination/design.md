# Design — Atomic Claim Coordination

Companion to `spec.md`. Records *why* each mechanism was chosen and what was rejected.

## D1 — The claim primitive: `mkdir` on the shared filesystem

**Decision.** Ownership is decided by `mkdir ~/.claude/logs/claims/<PROJECT>/<ISSUE-KEY>`.
POSIX `mkdir` is an atomic test-and-set: exactly one caller creates the directory, every
other gets `EEXIST`. The Jira label and the `In Progress` transition are written *after*
the lock is held, and are a **board-visible mirror** of a decision already made — never
the contended resource.

**Why not read-back CAS on labels** (the original proposal). It is read-then-check with no
atomicity relative to later writes. This interleaving defeats it:

```
A read → B read → A write(+agent-1) → A read-back: "mine survived" → A WINS
                  B write(+agent-2)  → B read-back: "mine survived" → B WINS
```

Both proceed, neither ever observes two labels, and the board shows one clean owner — so
the duplicate work is *less* visible than before the fix. The prescribed "short backoff"
cannot close this: the peer's read→write interval includes LLM generation and MCP retry,
so it is unbounded, and pool agents spawn together, so their attempts are phase-aligned.

**Why not the `In Progress` transition as the lock.** Transitions are global (spec
constraint 1). Both racers succeed; there is no loser to detect.

**Why not lead-assignment.** It removes the race by construction (single writer) but
serializes dispatch and discards the deep ready-queue that `fullstack-agent.md:292` is
built around. Recorded as the fallback if D1 proves insufficient.

**Residual risk, accepted and recorded.** This makes **disk authoritative for claims**
while `agent-team-protocol.md:117` names Jira as ground truth for *state*. The split is
deliberate: ownership is a mutual-exclusion question the filesystem can answer and Jira
cannot; status/labels/comments remain Jira's. Documented in the protocol so the two are
not read as contradictory. It also assumes a shared `$HOME` — false across hosts, and
false under `isolation: worktree` if `$HOME` differs.

**Label read-back is retained but demoted** to a cheap detector, and made **fail-open**:
- my label present → confirmed;
- my label absent **and another `agent-*` present** → I lost (release the lock, move on);
- my label absent **with no competitor** → *unconfirmed*, not lost. Retry the write once,
  then escalate.

The original fix was fail-*closed* on absence, which on a channel the docs themselves call
laggy converts a benign stale read into a self-inflicted orphan.

## D2 — Recovery: heartbeat file, investigate-only sweep, explicit release

**Decision.** Three parts, all required:
1. The owner refreshes `<claim>/heartbeat` (a file, on the same shared surface as the
   lock). Liveness lives where the lock lives.
2. The lead's sweep is **investigate-only** at a **≥60 minute** threshold. It surfaces a
   candidate; the lead must still run the `fullstack-agent.md:106` protocol before acting.
3. On confirmed death, an explicit **logged release**: remove exactly the dead `agent-*`
   label, transition back to `To Do` (id 11), delete the lock, comment the evidence.

**Why not `updated <= -30m`** (the original proposal). Three independent defects:
- `updated` is a time-since-last-signal threshold — structurally the "No message in N
  minutes" that `fullstack-agent.md:105` names *in bold* as **not** positive evidence of
  failure, in a section the repo labels Non-Negotiable.
- 30 minutes sits one minute above the **~29-minute** legitimate verification pass that
  `:103` records as the motivating incident.
- `updated` has no actor dimension: a peer's comment or a lead's label edit refreshes it.

**Why the release step is mandatory, not optional.** Verified by exhaustive grep: the repo
contains **no path returning an `In Progress` issue to the claimable pool**. Without it,
"respawn and reclaim" has no mechanical route — a fresh instance cannot see the issue
(wrong status), and a same-named respawn passes any label check trivially, so if the
original was not actually dead there are two live workers with no detection at all.

**Ordering constraint.** Landing the `In Progress` transition (G2) *deletes* the only
existing crude recovery path, because a label-only claim leaves the issue at `To Do` where
the claim JQL still returns it. G2 and G3 must ship together.

## D3 — Sentinel: two-phase, atomic, still in PreToolUse

**Decision.** `PreToolUse` renames `<key>.verified` → `<key>.inflight` (atomic, and it
stops authorizing further transitions immediately). `PostToolUse` on `transitionJiraIssue`
deletes `.inflight` on affirmative success, or restores it to `.verified` otherwise. A
`.inflight` older than `INFLIGHT_STALE_SECONDS` is reclaimable, covering the case where
the tool was never invoked and `PostToolUse` never fired.

**Why not "consume in PostToolUse gated on success"** (the original proposal). It converts
the gate from test-and-set to **test-only** for the duration of the round trip, opening a
window in which one sentinel authorizes two gated transitions — destroying the
"closer writes its own" convention. And "gated on success" is not implementable with the
repo's `_succeeded()`, which classifies any non-empty string as success **including error
strings**, and a bare string is the live response shape.

**Bias on ambiguity: restore.** If success cannot be affirmatively proven, the sentinel is
restored. The observed real-world failure was a *destroyed* sentinel permanently blocking a
legitimate close (AGENT-11, 2026-08-05T05:47:48Z). Restoring risks at most one extra
authorized transition for a sentinel that was legitimately earned; the alternative wedges
the board. Consistent with the repo-wide fail-open rule.

**Why a separate hook file.** `jira_mirror_journal.py` documents "This hook observes; it
never gates" (`:11`). Consuming there would violate its stated invariant.

## D4 — Idle nudge: rank, never filter

**Decision.** Keep every role-matched issue in the nudge, partitioned into "in your scope"
and "outside it — confirm with the lead". Scope is derived **at read time from the labels
already journalled**, with "current" resolved from the most recent
`.claude/specs/*/jira-run.json`. A missing or unreadable scope means no partitioning.

**Why not a hard filter** (the original proposal). Every conjunct added to the claimable
set fails in the *silent allow-idle* direction, inverting the hook's risk posture from
bounded noise (a nudge toward the wrong issue, which a human or the lead catches) to
unbounded, unlogged work-abandonment. An issue is dropped only for a **contradicting**
label, never a **missing** one, and the suppressed count is logged.

**Why not new mirror fields.** Spec and group are *already* journalled — the entire labels
array is recorded and folded. The original's "journal spec-*/group-*" step was a no-op.
Adding top-level fields would also trip the unversioned-journal hazard (spec constraint 5).

**Also fixed here:** the nudge currently hands out issue **keys**, bypassing the
sprint-scoped Find JQL entirely. It now points at the JQL and treats keys as candidates.

## D5 — Reviewers do not self-claim

**Decision.** Add the *opposite* of a claim block to `review-agent.md`, and give
`role-review` a distinct nudge that does not instruct self-claiming.

**Why.** The original finding held that a 4-analyst pool would race for one issue. It is
mis-diagnosed: the pool is partitioned by the lead's handoff (`review-agent.md:14-17`,
`fullstack-agent.md:298`) — "Synthesizer (exactly one per group)", "Analyst: you write no
file and post no verdict". Adding a self-claim block would have *created* the collision,
because there is exactly one `role-review` card per sprint. The `sa` pool is capped at 1
(`fullstack-agent.md:241`), so it has no same-role racer either.

## D6 — Dependency check with an explicit, complete projection

**Decision.** `getJiraIssue(issueIdOrKey, fields=["labels","status","summary","description","issuelinks"])`,
threshold at **`In Review`** (output has landed) rather than `Done`, and an absent
`issuelinks` key treated as UNKNOWN → escalate, never as "no blockers".

**Why the full field list.** `fields` replaces the defaults (spec constraint 4). The
obvious `fields=["issuelinks"]` would drop `labels`, and the claim's read-modify-write
would then erase them permanently.

**Why `In Review`, not `Done`.** The repo's close convention means nothing reaches `Done`
until the group is over, so a non-`Done` threshold would deadlock every intra-group
dependency.

**Why claim-side and not a hook.** `createIssueLink` is absent from the PostToolUse mirror
matcher, so links are never journalled and no hook can see them.

## D7 — `Files:` disjointness enforcement

**Decision.** Journal the parsed `Files:` list on create; `jira_issue_format_check.py`
rejects a create whose paths overlap another issue sharing the same `spec-*` and `group-*`
labels. `load_state`'s default dict is widened in the same change.

**Why now.** `fullstack-agent.md:359` calls disjointness "the sole guarantee against
conflicts under the shared-tree pool model" and nothing enforced it. With no atomic Jira
primitive available, this is the last layer between a claim race and clobbered work.

**Hazard handled.** Widening `_MERGEABLE` without widening the default dict at
`jira_mirror.py:103` raises `KeyError` on every pre-existing event — swallowed fail-open,
silently disabling the hook. Both are changed together and call sites use `.get()`.

## D8 — `spec_gate`: split the two halves

**Decision.** Drop the `getmtime >= started_at` comparison (fixes the false block on
spawned teammates, whose `session_id` differs so `started_at` post-dates the lead's spec).
Keep the intent flag, but confirm the file on disk at threshold time.

**Why split.** Replacing the intent flag with a pure existence check breaks
`test_writing_a_spec_clears_the_gate` **structurally**: `spec_gate.py` is a `PreToolUse`
hook, so it runs before the Write — the write that creates the spec can never satisfy an
existence check at the moment it is announced. Treating them as one "self-contained" step
hid that only the second half costs anything.

## Order of landing

C1–C3 are one commit; the intermediate states are worse than not starting. Specifically,
**C1 without C3** removes the issue from the claim JQL and the idle hook — deleting the
only existing recovery path — while providing zero exclusion on its own.

C4–C10 are independent.
