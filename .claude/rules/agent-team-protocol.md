# Agent Team Protocol

Shared protocol for all agent team teammates. The team lead (`fullstack-agent`) coordinates; all other agents are teammates. This file is loaded as a global rule for every session — every teammate inherits it as priming, no `Skill` invocation required.

## Teammate Lifecycle

1. Receive delegation via `SendMessage` from the team lead with the spec path and sprint scope
2. Invoke the `jira-workflow` skill, then read `spec.md` and `design.md`
3. Self-claim an unclaimed issue for your role per the claim protocol in `jira-workflow`
   (status `To Do` + `role-<yours>` label + no `agent-*` label)
4. Implement exactly what the issue describes, touching only its `Files:`
5. Run the issue's `Run:` command, write the verification sentinel, comment the result
6. Transition to `In Review`. Closing (`In Review` -> `Done`) is a protocol convention
   reserved for the review synthesizer, not machine-enforced — see `jira-workflow`
7. Notify the lead via `SendMessage`; claim the next unclaimed issue for your role

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

Four hooks in `.claude/settings.json` enforce this protocol automatically. All are
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

The sentinel is consumed on success, so it cannot be reused.
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

## Handling Ambiguity

- **Missing details**: Check `spec.md` and `design.md` first. If not there, `SendMessage` to lead or relevant teammate
- **Multiple valid approaches**: Pick the simplest that satisfies acceptance criteria
- **Out-of-scope issues**: Note in completion report; don't fix
- **Conflicting requirements**: Mark `[!]`, never silently pick one interpretation
- **Dependency on another teammate**: `SendMessage` to them directly, then `[!]` if not ready

## Coordination Under Unreliable Signals (Learned — All Teammates)

Message delivery, idle pings, and the Jira/Atlassian MCP are all **laggy and occasionally lossy** in practice. Whole sessions have been derailed by treating these transient signals as ground truth. These rules are load-bearing:

- **Ground truth is Jira, then the disk.** The authoritative state of the work is the
  issue's status and labels in Jira, then verification sentinels, then the actual
  `git diff` — NOT the last message you received. Message delivery lags and reorders.
  Before acting on ANY status claim (yours or a peer's), confirm it against Jira. If
  Jira is unreachable, stall and escalate; do not invent state.
- **Delivery lag ≠ death.** Messages routinely arrive delayed, batched, and out of order. "No message in N minutes" or "no OS process visible" is **not** evidence a teammate is stalled or dead — it is evidence the channel is quiet. Teammates running long verification passes (a multi-minute plugin review, an uncached test suite, a `terraform plan`) look identical to a dead one over the wire. Do not conclude a peer has failed from silence alone.
- **Ignore stale replays silently.** If you receive a stale assignment (or re-assignment) message for an issue that is already `Done`/sentinel-consumed, or for a key that `getJiraIssue` reports as "not found", treat it as a stale roll-forward artifact: take no action and do **not** re-run verification or re-report. Reply at most once if a peer needs confirmation. Re-verifying completed work on every replay is a documented time sink that starves the pool.
- **Claim atomically, one owner per issue.** Before working an issue, set yourself as owner and confirm no peer already owns it via a single authoritative `getJiraIssue`. If two instances race, the later one backs off to a different issue. Never edit a file outside your claimed issue's declared paths — peers run concurrently and will clobber.
- **Globally-unique instance names.** With multiple specs/teams possibly active, a bare role name (e.g. `review-2`) can misroute to a same-named instance on a different spec. Use the names the lead assigned and address peers by their exact instance name.

## Shutdown (Implicit Team — Auto-Cleanup)

There is **one implicit team per session**; the standalone `TeamCreate`/`TeamDelete` tools no longer exist. Teammates are background `Agent` spawns addressable by `name`, and the team is **torn down automatically when the session ends** — there is no `TeamDelete` step and no member-drain gate to satisfy.

**Teammate side.** A teammate's process ends when its work is done and it goes idle, or when the lead sends a `{type: "shutdown_request"}` — a *legacy* mechanism still supported by `SendMessage`, used only when the lead wants to stop a still-running background teammate early. On receiving one, finish current work, ensure the issue's status, sentinel, and comments are current in Jira, then reply `{type: "shutdown_response", request_id: <echoed>, approve: true}`. Approving terminates your process — do it only once your work is durably recorded.

**Lead side.** Do **not** hand-roll a `TeamDelete` teardown — it no longer exists, and there is no `~/.claude/teams/<team>/config.json` member list to drain. When all work is complete: confirm every issue is `In Review`/`Done`, then either let idle background teammates terminate on their own, or — if any are still running and you want them stopped now — `SendMessage` each a `{type: "shutdown_request"}` and wait for its `approve: true`. Finally, sweep any leftover verification sentinels for this run: `rm -rf ~/.claude/logs/verified/<projectKey>/` (session auto-cleanup does not touch that path).
