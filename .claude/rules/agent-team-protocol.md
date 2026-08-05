# Agent Team Protocol

Shared protocol for all agent team teammates. The team lead (`fullstack-agent`) coordinates; all other agents are teammates. This file is loaded as a global rule for every session — every teammate inherits it as priming, no `Skill` invocation required.

## Teammate Lifecycle

1. Receive delegation via `SendMessage` from the team lead with the spec path and sprint scope
2. Invoke the `jira-workflow` skill, then read `spec.md` and `design.md`
3. Self-claim an unclaimed issue for your role (status `To Do` + `role-<yours>` label + no
   `agent-*` label). **The claim is the `mkdir` lock, not the label** — `jira-workflow` has
   the full protocol, but these steps hold even with no skill loaded, and all of them
   happen *before* you edit any file:
   1. `mkdir ~/.claude/logs/claims/<projectKey>/<ISSUE-KEY>` — succeeds for exactly one
      agent. If it fails, you lost: pick another issue, touch nothing.
   2. `getJiraIssue(..., fields=["labels","status","summary","description","issuelinks"])`
      — an explicit `fields` list **replaces** the defaults, so it must include `labels`.
   3. `editJiraIssue` adding your `agent-*` label, then `transitionJiraIssue` to
      **`In Progress`**, then comment `Claimed by <instance>.`
   4. Re-read and confirm — **fail open**: an absent label with no competing `agent-*` is
      an unconfirmed write, not a loss. You hold the lock; re-apply and continue.
4. Implement exactly what the issue describes, touching only its `Files:`
5. Run the issue's `Run:` command, write the verification sentinel, comment the result
6. Transition to `In Review`. Closing (`In Review` -> `Done`) is a protocol convention
   reserved for the review synthesizer, not machine-enforced — see `jira-workflow`
7. Release the lock (`rm -rf ~/.claude/logs/claims/<projectKey>/<ISSUE-KEY>`), notify the
   lead via `SendMessage`, and claim the next unclaimed issue for your role

## Communication Rules

- **Direct to teammates**: Interface clarifications, dependency questions, sharing outputs they need
- **To the lead**: Blockers needing decisions, completion reports, scope/spec questions
- Tools: `SendMessage` (any teammate); the Atlassian MCP for all issue state

## Completion Reporting

Three steps, in order:
1. Comment the verification output and a one-line summary on the issue
2. Transition the issue to `In Review`
3. `SendMessage` the lead (and any teammate depending on your output)

The board is the record. There is no file to update.

## Blocker Reporting

Set the impediment flag (`config.fields.flagged` = `Impediment`), comment the specific
blocker and what would unblock it, then `SendMessage` the lead. If the same blocker
persists after two attempts, the lead escalates to the user.

## Verification Gate (All Teammates)

Before transitioning ANY issue to `In Review`:
1. Run the issue's `Run:` command
2. Confirm the interface/output contract matches the issue exactly
3. Confirm you only modified files listed in the issue's `Files:`
4. Write the verification sentinel (machine-enforced), then transition

If verification fails and you cannot fix it in scope, flag the issue as an impediment
with the specific failure.

## Enforced Hooks (Automated Guardrails)

Eight hook entries in `.claude/settings.json` enforce this protocol automatically. Six are
described below; the other two are `spec_gate.py`, documented in
`spec-and-jira-required.md`, which governs the step before all of this — deciding that a
spec and a backlog are needed at all. That rule applies to **solo sessions too**; this file
is teammate-scoped and does not exempt anyone from it. All are
**fail-open** (a hook error never blocks you) and log every decision to
`~/.claude/logs/team-hooks.jsonl`. Scripts live at `.claude/hooks/`.

### 1. Issue format check (`PreToolUse` on `createJiraIssue`)
Issue creation is **blocked** unless the summary carries a `[coding|devops|sa|review]`
tag, the description has `Spec:` / `Files:` / `Acceptance:` / `Run:`, and `role-*` +
`spec-*` labels are present — and unless the summary tag agrees with the role label.
This is the lead's concern (the lead authors issues), but all agents should know the shape.
- **Bypass**: the `skip-format-check` label. Epics are exempt.

### 2. Verification gate (`PreToolUse` on `transitionJiraIssue`) — ACTION REQUIRED
A transition to `In Review` or `Done` is **blocked** unless you wrote a verification
sentinel. The hook cannot see your transcript, so the sentinel is your attestation that
verification actually passed. After your `Run:` command passes, and immediately before
the transition:

```bash
mkdir -p ~/.claude/logs/verified/<projectKey>
echo "<the Run command> PASSED" > ~/.claude/logs/verified/<projectKey>/<ISSUE-KEY>.verified
```

The sentinel is consumed on success, so it cannot be reused — **one sentinel permits one
transition.** `In Review` and `Done` are both gated, so the implementer's sentinel is
already consumed by the time the issue sits in `In Review`. **The closer writes its own**:
before the review synthesizer transitions `In Review` -> `Done` it writes a fresh sentinel
attesting the verdict, or the close is blocked and the issue can never reach `Done`.

```bash
mkdir -p ~/.claude/logs/verified/<projectKey>
echo "review verdict PASS" > ~/.claude/logs/verified/<projectKey>/<ISSUE-KEY>.verified
```

The gate checks only that a sentinel exists — not who is transitioning, nor the prior
status. Reserving the close for the synthesizer stays a protocol convention, not a
machine-enforced guardrail.
- **Bypass**: the `skip-verify` label.

### 3. Mirror journal (`PostToolUse` on the Jira mutation tools)
Records every successful mutation to `~/.claude/logs/jira-mirror/<projectKey>.jsonl`.
Observational only — it never blocks. It exists because a hook subprocess holds no
Jira credentials and would otherwise be blind.

### 4. Idle work-check (`TeammateIdle`)
Before you go idle, the hook checks mirror state for unclaimed issues carrying your
role label. If any exist you are kept working and nudged to claim one or confirm to the
lead you are done. After 2 nudges for the same set it lets you idle (loop-safe).

Because the mirror only sees MCP mutations, a card moved by hand on the board is
invisible to the idle check until an agent next touches it.

The nudge for `role-review` deliberately omits the claim recipe: reviewers are
partitioned by the lead's handoff, and the sprint's single `role-review` card belongs to
the synthesizer.

### 5. Claim gate (`PreToolUse` on `Write` / `Edit`) — ACTION REQUIRED
A write is **blocked** if the issue whose `Files:` declares that path is unclaimed —
either still `To Do`, or `In Progress` with no claim lock on disk. This is the
enforcement behind the claim protocol: it existed only as prose, and the measured result
was that **8 of 11 worked issues never entered `In Progress`**.

The hook cannot see who you are (a `Write` payload carries no teammate identity), so it
does not ask whether *you* own the issue — only whether *anyone* does. Take the `mkdir`
lock and transition before your first edit and you will never meet it.

It **blocks once per session**, then stays out of the way, because the mirror is
best-effort and a guardrail that traps a session is worse than none. `.claude/**`, the
spec directory, and dependency directories never count.
- **Bypass**: `CLAUDE_CLAIM_GATE=off`.

### 6. Sentinel finalizer (`PostToolUse` on `transitionJiraIssue`)
Phase two of the verification gate. The gate renames your sentinel to `.inflight` *before*
the MCP call (so a concurrent second transition is blocked); this hook then deletes it on
success or **restores it** if the call did not affirmatively succeed. That is why a 401 or
a timeout no longer destroys an attestation you legitimately earned — a failure that
previously left an issue permanently unclosable. Observational; it never blocks.

## Handling Ambiguity

- **Missing details**: Check `spec.md` and `design.md` first. If not there, `SendMessage` to lead or relevant teammate
- **Multiple valid approaches**: Pick the simplest that satisfies acceptance criteria
- **Out-of-scope issues**: Note in completion report; don't fix
- **Conflicting requirements**: Flag the issue as an impediment (see Blocker Reporting), never silently pick one interpretation
- **Dependency on another teammate**: `SendMessage` to them directly, then flag the issue as an impediment if not ready

## Coordination Under Unreliable Signals (Learned — All Teammates)

Message delivery, idle pings, and the Jira/Atlassian MCP are all **laggy and occasionally lossy** in practice. Whole sessions have been derailed by treating these transient signals as ground truth. These rules are load-bearing:

- **Ground truth is Jira, then the disk.** The authoritative state of the work is the
  issue's status and labels in Jira, then verification sentinels, then the actual
  `git diff` — NOT the last message you received. Message delivery lags and reorders.
  Before acting on ANY status claim (yours or a peer's), confirm it against Jira. If
  Jira is unreachable, stall and escalate; do not invent state.
- **Delivery lag ≠ death.** Messages routinely arrive delayed, batched, and out of order. "No message in N minutes" or "no OS process visible" is **not** evidence a teammate is stalled or dead — it is evidence the channel is quiet. Teammates running long verification passes (a multi-minute plugin review, an uncached test suite, a `terraform plan`) look identical to a dead one over the wire. Do not conclude a peer has failed from silence alone.
- **Ignore stale replays silently.** If you receive a stale assignment (or re-assignment) message for an issue that is already `Done`, or for which a verification sentinel is already present on disk (proof that verification ran, even before the transition lands and consumes it), or for a key that `getJiraIssue` reports as "not found", treat it as a stale roll-forward artifact: take no action and do **not** re-run verification or re-report. Reply at most once if a peer needs confirmation. Re-verifying completed work on every replay is a documented time sink that starves the pool.
- **Claim atomically, one owner per issue — via the lock, not the label.** `mkdir ~/.claude/logs/claims/<projectKey>/<ISSUE-KEY>` is a POSIX atomic test-and-set and is the *only* step that decides ownership. It cannot be done with a `getJiraIssue` read: `editJiraIssue` replaces the labels array, so a loser's write erases the winner's label and exactly one survives — which means "if two `agent-*` labels are present" is a condition the API can never produce, and a rule built on it never fires. Transitions cannot arbitrate either; they are global, so both racers' move to `In Progress` succeeds. Once you hold the lock, the label and the transition are bookkeeping: apply them, and if a re-read does not show your label, re-apply it rather than surrendering the issue. Never edit a file outside your claimed issue's declared paths — peers run concurrently and will clobber.
- **Globally-unique instance names.** With multiple specs/teams possibly active, a bare role name (e.g. `review-2`) can misroute to a same-named instance on a different spec. Use the names the lead assigned and address peers by their exact instance name.

## Shutdown (Implicit Team — Auto-Cleanup)

There is **one implicit team per session**; the standalone `TeamCreate`/`TeamDelete` tools no longer exist. Teammates are background `Agent` spawns addressable by `name`, and the team is **torn down automatically when the session ends** — there is no `TeamDelete` step and no member-drain gate to satisfy.

**Teammate side.** A teammate's process ends when its work is done and it goes idle, or when the lead sends a `{type: "shutdown_request"}` — a *legacy* mechanism still supported by `SendMessage`, used only when the lead wants to stop a still-running background teammate early. On receiving one, finish current work, ensure the issue's status, sentinel, and comments are current in Jira, then reply `{type: "shutdown_response", request_id: <echoed>, approve: true}`. Approving terminates your process — do it only once your work is durably recorded.

**Lead side.** Do **not** hand-roll a `TeamDelete` teardown — it no longer exists, and there is no `~/.claude/teams/<team>/config.json` member list to drain. When all work is complete: confirm every issue is `In Review`/`Done`, then either let idle background teammates terminate on their own, or — if any are still running and you want them stopped now — `SendMessage` each a `{type: "shutdown_request"}` and wait for its `approve: true`. Finally, sweep this run's residue (session auto-cleanup touches neither path):

```bash
rm -f  ~/.claude/logs/verified/<projectKey>/*.verified   # spent attestations
rm -rf ~/.claude/logs/claims/<projectKey>/               # released claim locks
```

**Only `*.verified`, never the whole directory.** That directory also holds `.inflight`
slots — sentinels consumed by a transition that is still in the air. Deleting one mid-flight
destroys an attestation a live teammate earned: the finalizer then finds nothing to restore
and the retry is blocked with "no sentinel", which reads as a verification failure rather
than the teardown that actually caused it. Run this only once every teammate has stopped.
