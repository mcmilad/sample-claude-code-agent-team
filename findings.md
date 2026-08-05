# Code Review Findings — Agent Team Coordination

Review date: 2026-08-05
Scope: full repo, with emphasis on cross-agent coordination
Tree reviewed: `main` @ `a329a24` plus the uncommitted working set (`spec_gate.py`,
`spec-and-jira-required.md`, `.github/`, `examples/`, `tests/`)

**Status: reviewed, then adversarially validated.** The first pass produced 12 findings
and a 10-step fix order. A second pass — 3 ground-truth briefs plus 3 independent
reviewers per fix, using live Jira MCP calls, reproduced test runs, and replays of the
real mirror journal — invalidated a substantial part of it. This document is the
corrected version. Superseded claims are marked and kept, not silently deleted, because
the wrong diagnoses are themselves instructive.

Validation coverage: fixes 1-7 have three independent reviewers each. **Fixes 8-10 were
not reviewed** (session limit); their entries rest on the test-baseline brief alone. The
adversarial refutation pass did not run, so retained objections are unanimous-and-
empirical rather than survivor-of-refutation.

---

## Ground truth (established empirically, not inferred)

These four facts decide most of what follows. Each was verified against the live system.

**GT1 — Jira transitions in this project are GLOBAL.** `scripts/jira_bootstrap.py:58`
creates a team-managed ("next-gen") Scrum project, and `:222-231` carries an in-tree note
that its transitions are `isGlobal` — any status to any status. Confirmed live:
`getTransitionsForJiraIssue` on **AGENT-15** (`To Do`) and **AGENT-13** (`Done`) returned
byte-identical sets — ids 11/21/31/41, all `isGlobal: true`, `isConditional: false`,
`hasScreen: false`. A `Done` issue is still offered `41 -> Done` and `11 -> To Do`.

> **Consequence:** a transition can never serve as an atomic claim. Two agents both
> calling `transitionJiraIssue(21)` **both succeed** — the second from `In Progress` to
> `In Progress`, which is a legal target. No error, no loser, no signal.

**GT2 — No compare-and-swap primitive is reachable from the agent toolset.** The live
`editJiraIssue` MCP schema is exactly `{cloudId, issueIdOrKey, fields, contentFormat,
responseContentFormat}` with `additionalProperties: false`. No `version`, no `If-Match`,
no ETag — and critically **no `update` verb**. Jira's REST API *does* support
`update: {"labels": [{"add": "agent-x"}]}`, a server-side atomic list-append immune to
lost updates, but `update` is a sibling of `fields` in the request body and cannot be
smuggled through it. That path needs the admin token, which
`scripts/jira_bootstrap.py:9-18` deliberately keeps out of agent sessions.

**GT3 — Test baseline is 197 passed.** Via `.venv/bin/python -m pytest tests/ -q`
(pytest 8.3.4). Bare `python3` has no pytest and `python3 -m unittest` errors on
`test_spec_gate.py:12` — environment artifacts, not real failures. Any fix must land
against 197.

**GT4 — The reported symptom is real and measurable.** Of 11 worked issues in the live
mirror, **8 never touched `In Progress`** — AGENT-5, 6, 7, 8, 9, 10, 12, 13 went
`To Do -> In Review` directly. Jira changelog entries 10120 (AGENT-12) and 10121
(AGENT-13) confirm status `10033 -> 10035` with no intermediate step.

---

## Root cause (revised)

The original pass said: *"claiming is advisory rather than atomic, and the pick-up signal
is optional."* Half of that survives; half was wrong.

**What survives:** claiming is not atomic, and cannot be made atomic through Jira (GT1,
GT2). The claim protocol's race check is written against a state the API cannot produce.

**What was wrong:** the pick-up signal is not *absent from* the always-on channel — it is
present in four documented places and one runtime-injected place, and agents skip it
anyway (GT4). **This is an adherence failure, not a placement failure.** Every other
guardrail in this repo is a hook; the claim protocol is the one load-bearing rule backed
by nothing but prose. Adding a fifth prose copy is not the lever.

---

## Finding status at a glance

| # | Finding | Status after validation |
|---|---------|-------------------------|
| 1 | Claim race check unreachable | **Confirmed** |
| 2 | Lifecycle omits `In Progress` | **Symptom confirmed (GT4); stated cause wrong** |
| 3 | Tie-break assumes both labels survive | **Confirmed**; copy count wrong (5, not 3) |
| 4 | review/sa have no claim protocol | **WITHDRAWN — mis-diagnosed** |
| 5 | Sentinel consumed before success | **Confirmed by live incident**; stated trigger unreachable |
| 6 | No stale-claim recovery | **Confirmed** |
| 7 | No dependency filter at claim time | **Confirmed**; fix was a no-op |
| 8 | Idle nudge scoping | **Confirmed live**; "never journalled" claim was false |
| 9 | `spec_gate` per-session time anchor | Confirmed (unreviewed) |
| 10 | `spec_gate` disarms on intent | Confirmed (unreviewed) |
| 11 | Mirror hardcodes `To Do` | Confirmed (unreviewed); lowest value |
| 12 | CI points at empty `examples/` | Confirmed (unreviewed) |

Plus **8 new findings** surfaced by validation (N1-N8), several of which outrank items in
the original list.

---

## Findings

### 1. CRITICAL — The claim race check can never fire on the race it exists to catch

`.claude/skills/jira-workflow/SKILL.md:64`

Step 2 correctly documents that `editJiraIssue` **replaces** the labels array. Step 3
then detects a race only "if more than one `agent-*` label is present" — which
lost-update guarantees never happens.

**Failure:** `coding-1` and `coding-2` both read `[role-coding, spec-x]`. `coding-1`
writes `+agent-coding-1`. `coding-2` writes `+agent-coding-2` from its stale base,
erasing it. One `agent-*` label survives, so the `>1` test is false **for both**.

**Correction to the original fix.** The original prescribed read-back CAS and called it
*"the only pattern that works."* That was wrong on both counts. It is not a CAS — it is
read-then-check with no atomicity relative to later writes — and this interleaving defeats
it:

```
t1  A: read      -> [role-coding]
t2  B: read      -> [role-coding]              (B's base now stale)
t3  A: write +agent-1  -> server = {agent-1}
t4  A: read back -> sees agent-1 -> "I WON"
t5  B: write +agent-2  -> server = {agent-2}   (erases A, AFTER A confirmed)
t6  B: read back -> sees agent-2 -> "I WON"
```

Both pass. Neither ever sees two labels, so the tie-break still never fires — the fix
*relocates* the blind spot rather than closing it. It is also **strictly worse than the
status quo in one respect**: the board now shows a single clean owner, so A's duplicate
work is invisible to the lead *and* to any stale-claim sweep.

The "short backoff" is a heuristic, not a guarantee: B's read-to-write interval includes
LLM generation and MCP retry, so no finite delay covers it — and pool agents are spawned
together (`fullstack-agent.md:240`), so their attempts are phase-aligned, the exact regime
where an unjittered sleep fails to decorrelate.

**Corrected fix — use the atomic primitive that already exists.** All agents share one
filesystem and already use it for verification sentinels. A POSIX
`os.open(path, O_CREAT|O_EXCL)` (or `mkdir`) under
`~/.claude/logs/claims/<projectKey>/<ISSUE-KEY>` is a genuine test-and-set: exactly one
call succeeds, the loser gets `EEXIST` and moves on deterministically. The Jira label and
the `In Progress` transition become the board-visible **mirror** of a decision made
atomically on disk, rather than the contended resource itself.

Read-back on the label is still worth keeping as a cheap detector — but it must be
**fail-open, not fail-closed**: treat "my label is absent *and* another `agent-*` label is
present" as a loss; treat "absent with no competitor" as an unconfirmed write (retry once,
then escalate), never as a loss. Otherwise a laggy read turns into a self-inflicted orphan.
And the re-read must be a **new `getJiraIssue`** — reading labels off the `editJiraIssue`
response is a write-time snapshot that can never show a later overwrite.

The accepted alternative, which removes the race by construction, is **lead-assigns**
rather than self-claim (single writer). It costs the deep-ready-queue throughput
`fullstack-agent.md:292` is built around; record the trade in `design.md` either way.

---

### 2. CRITICAL (symptom) — Agents work issues the board still shows as `To Do`

`.claude/rules/agent-team-protocol.md:9`

**The symptom is confirmed and quantified (GT4): 8 of 11 worked issues never touched
`In Progress`.** The lead's monitor JQL (`fullstack-agent.md:294`, `ORDER BY status`)
therefore reports in-flight work as unstarted, and `:294` explicitly instructs the lead to
treat lingering `To Do` issues as "a dependency or too-coarse issue; split or unblock it."

**The originally stated cause was false.** The first pass claimed the transition "appears
solely at `jira-workflow/SKILL.md:58`, which requires an explicit `Skill` invocation."
`grep` refutes it. There are five live statements of the claim sequence:

| # | Location | Channel |
|---|----------|---------|
| 1 | `agent-team-protocol.md:124` | auto-loaded rule |
| 2 | `jira-workflow/SKILL.md:64-67` | Skill-invoked |
| 3 | `coding-agent.md:39-41` | agent system prompt |
| 4 | `devops-agent.md:39-41` | agent system prompt |
| 5 | **`teammate_idle_workcheck.py:103-113`** | **injected at runtime** |

Two of those reach a teammate with no skill load at all. So the transition instruction was
already in the context of the agents that skipped it — **this is an adherence failure, not
a placement failure**, and the CRITICAL severity does not rest on the original argument.

The always-on rule's lifecycle step 3 genuinely is label-only and should still be
corrected. But note it also **buys no atomicity whatsoever** (GT1): the transition is a
visibility repair for the lead's monitor, nothing more. Any wording implying it makes the
claim exclusive is unsound.

Verified harmless side effect: the extra transition does not destroy a closer's sentinel —
`jira_transition_verify_gate.py:86-87` exits at "target status In Progress is not gated"
before reaching `os.remove` at `:108`, and `gatedStatuses` is `["In Review", "Done"]`.

**Corrected fix:** inline the sequence into lifecycle step 3 (claim on disk -> label ->
confirm -> transition -> only then edit), **and** add an enforcement point, because prose
alone has a measured ~27% adherence rate here. The cheapest credible one: a `PreToolUse`
gate on the session's first `Write` that requires an owned claim file. If enforcement is
declined, record in `design.md` that fixes 1-4 are documentation-only mitigations with
the observed adherence rate stated, so the residual risk is explicit rather than assumed
away.

---

### 3. HIGH — The lexicographic tie-break assumes both labels survive

`.claude/rules/agent-team-protocol.md:124`, `jira-workflow/SKILL.md:64-67`,
`coding-agent.md:40`, `devops-agent.md:40`, **`teammate_idle_workcheck.py:109-110`**

"The loser removes its own `agent-*` label" is unreachable under the lost update in
finding 1: the loser has no surviving label to remove.

**Correction:** the original said three copies and scoped the fix to three files. There
are **five live copies**. The one it missed is the worst of them — `teammate_idle_workcheck.py:103-113`
restates the full four-step protocol *including the broken tie-break* and injects it into
a teammate's context **at the exact moment it is about to claim**. No test asserts on that
string (`tests/hooks/test_teammate_idle_workcheck.py:47` checks only that `"AGENT-14"`
appears in stderr), so it will drift silently. Ship the doc fixes without it and the hook
actively re-teaches the rule they repaired.

**Also unresolved:** `agent-team-protocol.md` would be left internally contradictory.
Lifecycle step 3 (`:9-10`) and the claim rule (`:124`) prescribe structurally different
algorithms — read-**before**-write at `:124` versus read-**after**-write in the fix. Same
always-on file, two incompatible protocols. Both blocks must be reconciled.

**Fix:** correct all five copies in one commit, and add a consistency test asserting the
hook's nudge text and `SKILL.md`'s Claim Protocol agree on step order and loss condition.

---

### 4. WITHDRAWN — "review-agent and sa-agent have no claim protocol"

*Original claim: `review-agent.md` and `sa-agent.md` lack a claim block, so a 4-analyst
pool would duplicate one review and post four conflicting verdicts.*

**Mis-diagnosed. All three reviewers rejected it independently, and applying the proposed
fix would have caused the collision it predicted.**

The review pool does not partition by claiming. `review-agent.md:14` — "the lead's handoff
assigns you one of two roles"; `:16` — "Synthesizer (exactly one reviewer per group)";
`:17` — "Analyst (review-2..review-4) ... You write no file and post no verdict"; `:180` —
"Analysts never send this"; `fullstack-agent.md:298` — the lead designates the synthesizer
and assigns each analyst a slice. The predicted failure requires all four analysts to
violate an instruction already in their own system prompt.

For `sa-agent`, `fullstack-agent.md:241` hard-caps the pool at size 1, concurrency 1.
**There is no same-role racer to lose to.**

Worse, there is exactly **one** `role-review` issue per sprint. Adding a self-claim block
would teach four reviewers to race for that single card — manufacturing the duplicate-
verdict collision the finding imagined, and breaking the one-synthesizer invariant.

**Replacement action (inverted):** add the *opposite* text to `review-agent.md` —
"reviewers do not self-claim; your slice and your synthesizer/analyst role come from the
lead's handoff; the sprint's `role-review` card belongs to the synthesizer" — and teach
`teammate_idle_workcheck.py` to suppress the nudge for `role-review`, or scope it to the
synthesizer instance. The only genuine (much smaller) gap is what an analyst does with a
*formal* `[review]` issue assigned to it (`review-agent.md:53-54`).

---

### 5. HIGH — The sentinel is consumed before the transition succeeds

`.claude/hooks/jira_transition_verify_gate.py:108`

`os.remove(path)` runs in `PreToolUse`, destroying the attestation before the MCP call is
attempted.

**Confirmed by a live incident, not by argument.** `~/.claude/logs/team-hooks.jsonl`
records 20 `PreToolUse/allow` events for `transitionJiraIssue` against only 17
`PostToolUse` events. One unmatched allow is
`2026-08-05T05:47:48Z — "verified via sentinel for AGENT-11 -> Done"`, with no PostToolUse
and no transition event for AGENT-11 anywhere in the mirror (its only events are one
create and two comments). **The sentinel was destroyed and the close never landed.**

**Correction to the stated trigger.** The original grounded this in "a transition id not
guaranteed valid from the issue's current status." Per GT1 that is **unreachable** — every
id is valid from every status and the config map is exactly complete. The reachable
triggers are 401 / 429 / 5xx / timeout / permission denial. The defect stands; its
justification does not.

**Correction to the fix.** Moving consumption to `PostToolUse` is wrong in both halves:

- It converts the gate from test-and-set to **test-only** for the duration of the round
  trip — a measured 3-12 second window in which one sentinel authorizes **two** gated
  transitions, destroying "one sentinel permits one transition" and the "closer writes its
  own" convention that depends on it.
- "Gated on success" is not implementable with the repo's predicate: `_succeeded()`
  classifies any non-empty string as success, **including error strings**, and a bare
  string is the live `tool_response` shape for real teammate transitions.
- Consuming inside `jira_mirror_journal.py` would also violate its stated invariant
  (`:11`, "This hook observes; it never gates"), so it needs its own hook file — which
  `tests/hooks/test_settings_wiring.py:25` requires to exist on disk.

**Corrected fix — two-phase, atomic, stays in PreToolUse:** replace `os.remove` with
`os.rename(path, path + '.inflight-<tool_use_id>')` (`tool_use_id` is present in every
live PostToolUse payload). The sentinel stops authorizing further transitions immediately,
closing the existing sub-millisecond race rather than widening it. Then in PostToolUse,
delete the `.inflight` file on confirmed success, or restore it when the tool was never
invoked. Treat **non-invocation**, not response inspection, as the failure signal.

**Breaks 1 test** (reproduced): `test_consumes_the_sentinel_so_it_cannot_be_reused`
(`tests/hooks/test_jira_transition_verify_gate.py:89-94`) asserts consumption inside the
PreToolUse subprocess. It must be rewritten to drive both hooks.

---

### 6. HIGH — No stale-claim recovery: a dead owner orphans an issue silently

`.claude/skills/jira-workflow/SKILL.md:39`

Once an issue is `In Progress` with an `agent-*` label it is invisible to the claim JQL
(`status = "To Do"`, `SKILL.md:48-50`) and to `teammate_idle_workcheck.py` (`:67` skips
non-`To Do`, `:71` skips any `agent-*` label). No sweep exists.

**The interaction with finding 2 is confirmed and is worse than first stated.** Today a
label-only claim leaves the issue at `To Do`, so the claim JQL **still returns it** — a
crude but real recovery path. Landing the `In Progress` transition deletes that path. And
per GT1 the transition buys no exclusion, so on its own it purchases the orphan cost with
**zero** concurrency benefit. Ship the transition only together with a recovery path.

**Correction to the fix.** The original proposed a heartbeat plus a
`updated <= -30m` sweep. Both halves are defective:

- **The trigger is the exact object a Non-Negotiable rule forbids.** `updated <= -30m` is a
  time-since-last-signal threshold — structurally the "No message in N minutes" that
  `fullstack-agent.md:105` names *in bold* as "**NOT** positive evidence of failure",
  followed by "means **investigate**, not **take over**." `:106` enumerates what does
  count: "explicit error, confirmed terminated process, corrupt/empty output where
  completion was claimed." A stale timestamp is none of them.
- **The constant is a coin flip on the motivating incident.** `:103` records a lead
  self-authoring a verdict while the "stalled" synthesizer was running a legitimate
  **~29-minute** verification pass. The proposal was 30 minutes.
- **`updated` carries no actor dimension.** A peer's comment, a lead applying
  `skip-verify`, or a reviewer posting findings all refresh it — so the signal is unsound
  in both directions regardless of the heartbeat.
- **The heartbeat is not a new mechanism.** `SKILL.md:94-97` already mandates a claim
  comment, `SKILL.md:99-103` and `agent-team-protocol.md:26` a verification comment, and
  `review-agent.md:145` a long-pass heartbeat (via `SendMessage`, the channel the protocol
  itself says must not be treated as ground truth).
- **It does not un-wedge anything.** Verified by exhaustive grep: the repo contains **no
  path that returns an `In Progress` issue to the claimable pool** — no instruction to
  transition back to `To Do`, and the only `agent-*` removal anywhere is the loser's
  self-removal. The sweep turns a silent orphan into a visible orphan and stops.

**Corrected fix — three parts, all required:**

1. **Liveness signal on disk, not in Jira.** Have the owner refresh a heartbeat file under
   `~/.claude/logs/claims/<PROJECT>/<KEY>` — the same shared surface as the claim lock in
   finding 1, and unambiguous in a way `updated` is not.
2. **Sweep is investigate-only, threshold ≥60 minutes.** The JQL surfaces a candidate;
   the lead must still run the full `:106` protocol (direct `SendMessage` with a bounded
   window, disk/sentinel check) before acting. This contradicts nothing — `:105` already
   prescribes "investigate."
3. **An explicit, logged RELEASE.** On confirmed death: remove exactly the dead
   `agent-*` label, transition back to `To Do` (id 11), delete the claim lock, and comment
   the evidence. Without this, respawn-and-reclaim has no mechanical path to the issue —
   a fresh instance cannot see it (wrong status), and a same-named respawn passes the
   read-back check trivially, so if the original was *not* dead there are then two live
   workers with **no detection at all**. Whichever branch is chosen must be written down;
   the original specified neither.

---

### 7. MEDIUM — No dependency filter at claim time

`.claude/skills/jira-workflow/SKILL.md:47`

The find JQL is correctly scoped to `sprint in openSprints()` but filters only on `status`
and the role label. `fullstack-agent.md:66` and `:292` mandate `blocks` / `is blocked by`
links, and nothing consults them.

**Correction to the fix — as written it was a guaranteed silent no-op.** Verified twice
against live Jira: `getJiraIssue(cloudId, 'AGENT-15')` with no `fields` param returns
13 fields — summary, issuetype, components, created, description, project, reporter,
priority, resolution, labels, assignee, updated, status — and **no `issuelinks` key at
all**. "Inspect `issuelinks` on the step-2 `getJiraIssue`" reads a key that is never there.

**And the obvious repair is destructive.** Passing `fields` **replaces** the default set:
`getJiraIssue(AGENT-15, fields=['issuelinks'])` returns `{'issuelinks': []}` and **no
labels**. Feeding that into the claim's read-modify-write would wipe `role-*`, `spec-*`
and `group-*` off the issue permanently.

**Corrected fix:**
- Prescribe the explicit projection **including everything the claim depends on**:
  `getJiraIssue(issueIdOrKey, fields=["labels","status","summary","description","issuelinks"])`.
- Add a warning in `SKILL.md` step 2 that `fields` replaces the defaults and any explicit
  list **must** include `labels`.
- Threshold on **output landed, not closed**: skip only if the blocker is `To Do` or
  `In Progress`; treat `In Review` and `Done` as satisfied. The repo's own close
  convention means nothing reaches `Done` until the group is over, so a `non-Done`
  threshold would deadlock every intra-group dependency.
- Treat an absent `issuelinks` key as UNKNOWN and escalate — never as "no blockers."

Note a hook-side alternative is impossible: `createIssueLink` is absent from the
PostToolUse mirror matcher (`.claude/settings.json:82` covers create/edit/transition/
comment only), so links are never journalled. The claim-side check is the only workable
placement.

---

### 8. MEDIUM — The idle nudge is scoped differently from the claim JQL it mirrors

`.claude/hooks/teammate_idle_workcheck.py:65-68`

**Confirmed live.** Replaying the real 56-line `~/.claude/logs/jira-mirror/AGENT.jsonl`
(15 folded issues spanning three spec slugs) through the *unmodified* hook nudges a single
`coding-2` with **"AGENT-26, AGENT-3"** — `spec-serverless-3tier`/`group-2` and
`spec-jira-smoke`/`group-1` in one message, exit 2. The claim JQL is sprint-scoped; the
hook cannot be, so the two disagree about what is claimable. A teammate pulling
cross-sprint work escapes the guarantee at `fullstack-agent.md:359`.

**Two corrections to the original.**

*The journalling half is a no-op.* The claim that "sprint, spec slug, group, and issue
links are never journalled" is **false for spec and group**: `_labels_from_create`
(`jira_mirror_journal.py:100-108`) journals the entire labels array and `jira_mirror.py:31`
folds it — AGENT-26 already resolves with `group-2` attached. Only **sprint** and
**issuelinks** are genuinely absent.

*The scoping half has no data source.* The `TeammateIdle` payload carries only
`team_name` / `teammate_name` (`SECURITY.md:61`; consumed at `:52-53`). Nothing tells the
hook which spec or group is current, so "filter to the current spec + group" **cannot be
implemented as specified** — a faithful attempt is silently inert and still passes the
suite.

*Mis-citation:* the "unclaimed, **unblocked**" over-claim is not in the hook docstring at
line 10. It is at `README.md:38` and `README.md:330`, which no test reads.

**Corrected fix:**
- Derive scope **at read time from the labels already journalled** — do not add top-level
  mirror fields (see N7).
- Source "current" from the lead's `.claude/specs/<slug>/jira-run.json`
  (`fullstack-agent.md:45`, `:291`), resolved via the payload's `cwd`. Missing file means
  no scoping (fail open to today's behaviour). Do **not** scrape `transcript_path` —
  `SECURITY.md:65` limits hook input handling to JSON parsing.
- **Rank, do not filter.** Every conjunct added here fails in the *silent allow-idle*
  direction, inverting the hook's risk posture from bounded noise to unbounded, unlogged
  work-abandonment. Keep every role-matched issue in the nudge, partitioned into "in your
  scope" and "outside it — confirm with the lead." Never let a scope mismatch reach the
  empty-set/allow-idle path at `:77-82`.
- Never drop an issue for a **missing** label, only for a **contradicting** one.
- Emit the suppressed count in the audit reason so over-filtering is diagnosable.
- Fix the nudge to point at the JQL rather than handing out specific keys: the deeper
  problem is that `:103-113` gives the teammate issue **keys** to claim, bypassing the
  sprint-scoped Find step entirely.

**Breaks 3 tests** (reproduced): `test_nudges_when_unclaimed_role_work_exists` (`:41`),
`test_allows_idle_after_two_nudges_for_the_same_set` (`:72`),
`test_nudge_counter_resets_when_the_claimable_set_changes` (`:81`) — fixtures are labelled
`["role-coding", "spec-auth"]` or bare `["role-coding"]`. **The last two are the
loop-guard tests**, so the protection against a teammate being trapped forever stops being
exercised until they are relabelled. Separately, marking `group-*` "Yes" in the SKILL.md
Label Vocabulary fails `tests/test_skill_consistency.py:166`, because
`_hook_enforced_label_tokens()` only greps two hook files and cannot see a check added to
`teammate_idle_workcheck.py`.

---

### 9. MEDIUM — `spec_gate` false-blocks the multi-agent flow this repo promotes

`.claude/hooks/spec_gate.py:224` — *not reviewed in validation; test impact measured*

`load_state` anchors `started_at` at the session's first Write, and every teammate has its
own `session_id`, so `spec_written_since` compares the spec's mtime against a timestamp
later than when the lead wrote it. A teammate is blocked on its third source file even
though the spec exists.

**Test-safe in isolation:** dropping the `getmtime >= started_at` compare while keeping the
intent flag gives **47 passed**. Land this half on its own.

---

### 10. MEDIUM — `spec_gate` disarms on write *intent*, not write *success*

`.claude/hooks/spec_gate.py:208` — *not reviewed in validation; test impact measured*

`is_spec_file(path)` sets `spec_written = True` before the write happens, so a denied or
cancelled spec write permanently marks the session satisfied.

**Correction: this half is not free, and it does not belong in the same step as finding 9.**
Replacing the intent flag with an on-disk existence check breaks
`test_writing_a_spec_clears_the_gate` (`tests/test_spec_gate.py:114-119`) **structurally,
not as a stale fixture**: `spec_gate.py` is a `PreToolUse` hook, so it runs *before* the
Write — the write that creates the spec can never satisfy an existence check at the moment
it is announced. Bundling 9+10 as one "self-contained" step hid the fact that only 10 costs
anything. Split them; 10 requires a deliberate test rewrite.

*(Related: `.claude/specs/` is currently **empty** — see N8.)*

---

### 11. LOW — Mirror create events hardcode `status: "To Do"`

`.claude/hooks/jira_mirror_journal.py:185` — *not reviewed in validation*

Premise verified against the live schema: `createJiraIssue` does expose top-level
`transition: {id: string}`. An issue created directly into `In Progress` is mirrored as
`To Do` and, carrying no `agent-*` label, is advertised as claimable.

**0 tests break**, but there is **zero coverage** of the new branch. Lowest value in the
set: it requires the lead to use a parameter no doc in the repo tells it to use.

---

### 12. LOW — CI `working-directory` points at an empty `examples/`

`.github/workflows/serverless-3tier.yml:32` — *not reviewed in validation*

Workflow-level `working-directory: examples/serverless-3tier` applies to both jobs;
`examples/` is confirmed empty (0 files). A `workflow_dispatch` run fails immediately —
`infra`'s `npm ci` cannot chdir, and `web` fails at both
`node-version-file: examples/serverless-3tier/.nvmrc` (`:76`) and the `check-web` step
(`:84`). No test reads `.github/`. Land the example or drop the workflow.

---

## New findings from validation

**N1 — A runtime-injected copy of the broken protocol, guarded by no test.**
`teammate_idle_workcheck.py:103-113` restates the full claim protocol — including
`"3. transitionJiraIssue to In Progress"` and the unreachable tie-break — and injects it
at the exact moment an agent claims. It is the highest-leverage copy in the system and no
proposed fix touched it. `tests/hooks/test_teammate_idle_workcheck.py:47` asserts only
that `"AGENT-14"` appears in stderr, so it drifts silently.

**N2 — `agent-team-protocol.md` prescribes two incompatible claim algorithms.**
Lifecycle step 3 (`:9-10`) and the claim rule (`:124`) disagree on read-before-write vs.
read-after-write. Same always-on file.

**N3 — No release path exists.** Verified by exhaustive grep over `.claude/` and
`README.md`: nothing returns an `In Progress` issue to the claimable pool. This is what
makes finding 6 unrecoverable rather than merely invisible.

**N4 — `fields` projection replaces defaults (destructive).** See finding 7. Any explicit
`fields` list omitting `labels` will wipe `role-*`/`spec-*`/`group-*` via the claim's
read-modify-write. Worth a hardening check on `editJiraIssue` that blocks a labels write
dropping an existing `role-*` or `spec-*`.

**N5 — `_succeeded()` treats error strings as success.** Any fix keying on it is a no-op
for the failure mode it targets.

**N6 — Issue links are never journalled.** `createIssueLink` is absent from the PostToolUse
mirror matcher, so no hook can ever see dependencies.

**N7 — The mirror journal is unversioned, unmigrated, and never rotated.** `jira_mirror.py`
folds a hardcoded `_MERGEABLE = ("summary","labels","status")` into a hardcoded default
dict at `:103`, and `fullstack-agent.md:324` instructs teardown to leave the file in place.
Adding a top-level field without widening the default raises `KeyError` on every
pre-existing event; `teammate_idle_workcheck.py:121-123` swallows it fail-open and **the
guardrail silently disables itself**, indistinguishable from "nothing claimable." A live
56-line journal on this machine is already exposed.

**N8 — `.claude/specs/` is empty**, while this document's own rule set requires a spec for
work of this shape.

---

## Cross-cutting note

`fullstack-agent.md:359` calls "no two issues in the same sprint may write to the same
file" *"non-negotiable ... the sole guarantee against conflicts under the shared-tree pool
model"* — and nothing enforces it. `jira_issue_format_check.py` validates that `Files:` is
present, never that paths are disjoint. The mirror stores `summary`/`labels`/`status` but
not `description`, so the check is not currently possible from a hook.

Given that finding 1 lets two agents onto one issue *and* the corrected analysis shows no
atomic Jira primitive exists, this disjointness property is the **last** layer between a
claim race and clobbered work — and it is unguarded. Journalling the parsed `Files:` list
on create would make it enforceable.

---

## Corrected fix order

Ordered by dependency. C1-C3 are one commit; the intermediate states between them are
worse than not starting.

| # | Change | Addresses | Breaks |
|---|--------|-----------|--------|
| C1 | `O_EXCL` claim lock under `~/.claude/logs/claims/<PROJECT>/<KEY>`; label + `In Progress` become its board mirror; read-back kept as a **fail-open** detector | 1, 2, 3 | 0 |
| C2 | Correct **all five** copies incl. `teammate_idle_workcheck.py:103-113`; reconcile `agent-team-protocol.md:9` vs `:124`; add a drift test | 3, N1, N2 | 0 |
| C3 | Explicit logged **release** (strip label, transition to `To Do`, delete lock, comment evidence) + investigate-only sweep at ≥60m keyed on the claim file | 6, N3 | 0 |
| C4 | Invert the review/sa text: "reviewers do not self-claim"; suppress the idle nudge for `role-review` | 4 (withdrawn) | 0 |
| C5 | Sentinel: `os.rename` to `.inflight-<tool_use_id>` in PreToolUse; PostToolUse deletes on success / restores on non-invocation | 5, N5 | **1** |
| C6 | Idle hook: rank-don't-filter, scope from journalled labels + `jira-run.json`, fail open, log suppressions, point at the JQL not keys | 8 | **3** |
| C7 | Dependency check with full `fields` projection incl. `labels`; threshold at `In Review` | 7, N4 | 0 |
| C8 | `spec_gate`: drop the mtime compare **only** | 9 | 0 |
| C9 | `spec_gate`: existence-at-threshold + deliberate test rewrite | 10 | **1** |
| C10 | Mirror reads `transition.id` on create | 11 | 0 |
| C11 | Land or drop the `serverless-3tier` workflow | 12 | 0 |
| C12 | Journal parsed `Files:`; enforce sprint-wide disjointness | cross-cutting | 0 |
| C13 | `claim_gate.py`: block a write to a file declared by an unclaimed issue | 2, GT4 | 0 |

**Worst stopped-halfway state:** C1 landed without C3. The `In Progress` transition removes
the issue from the claim JQL and the idle hook, deleting the only existing recovery path,
while providing zero exclusion (GT1). Strictly worse than the status quo.

**Explicitly not doing:** read-back CAS as the primary mechanism (finding 1); a self-claim
block in `review-agent.md`/`sa-agent.md` (finding 4, inverted into C4); `updated`-based
staleness as positive evidence of death (finding 6); top-level mirror schema fields (N7).

---

## Residual risk after the corrected plan

- **Jira remains eventually consistent and non-transactional.** C1 moves the authoritative
  decision to disk, which means **disk becomes ground truth for claims** while
  `agent-team-protocol.md:117` names Jira as ground truth for state. That tension is real
  and should be written down in `design.md` rather than left implicit.
- **The claim lock assumes a shared filesystem.** It does not survive agents on separate
  hosts or in `isolation: worktree` mode if `$HOME` differs.
- **Adherence is no longer unguarded — but the guard is deliberately weak.** GT4 measured
  27% compliance, and C13 (`claim_gate.py`) now blocks a write to a file declared by an
  unclaimed issue. Two limits are structural: a `Write` payload carries no teammate
  identity, so the gate can only ask whether *anyone* claimed the issue, not whether *you*
  did; and it blocks **once per session** by design, because the mirror is best-effort and
  a guardrail that traps a session is worse than none. It converts a silent failure into a
  loud one; it does not make the protocol unbreakable.
- **The gate only sees work with a declared `Files:` list.** An issue created without one,
  or a file edited outside every issue's declaration, is invisible to it.
- **Fixes 8-12 were never adversarially reviewed.** Their diagnoses stand on one pass.

---

## Scope note

This document records review and validation only; no fixes have been applied. Implementing
the corrected plan touches the rules, agent definitions, the `jira-workflow` skill, five
hooks, and their tests, and changes where claim authority lives — so per
`.claude/rules/spec-and-jira-required.md` it requires `.claude/specs/<slug>/spec.md` +
`design.md` and a Jira Epic before the first code change, solo session or not. See N8.
