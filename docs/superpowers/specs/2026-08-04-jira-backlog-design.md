# Design: Jira as the Agent Team's Backlog and Task Tracker

**Date:** 2026-08-04
**Status:** Approved (design); implementation plan pending
**Supersedes:** the `tasks.md` + built-in task store coordination model

## Problem

The agent team currently tracks work in two places: `tasks.md` (a markdown checklist per spec)
and the harness's built-in task store (`TaskCreate` / `TaskUpdate` / `TaskList`, persisted at
`~/.claude/tasks/<team>/<id>.json`). Neither is visible outside a terminal session. There is no
way to open a page and see what each agent is working on, what is blocked, or what remains.

**Goal:** move backlog and task tracking wholly into Jira, so a Jira board is the live view of
the team — who is working on what, what is blocked, what is left.

## Decisions

| # | Decision | Rationale |
|---|---|---|
| D1 | Jira **replaces** the built-in task store; guardrails are rebuilt against Jira | The board must be the system of record, not a mirror that can drift |
| D2 | Agent identity is carried by an `agent-<instance>` **label**, not assignee | Agents have no Atlassian accounts; labels need no admin setup and no billable seats |
| D3 | Epic = spec, Task = work unit, **Sprint = parallel group** | Active sprint shows current work; backlog view shows remaining groups in order |
| D4 | The idle work-check hook reads a **local mirror journal**, not Jira | A hook subprocess has no access to the MCP's OAuth token |
| D5 | `tasks.md` is **retired entirely** | User directive: everything migrates to Jira. No duplicate representation to drift |
| D6 | Agents write **status transitions plus milestone comments** | Enough that a card tells the whole story, without burying it in narration |
| D7 | Admin operations use a **separate Jira API token**; agents never hold it | MCP OAuth lacks project-admin scope (see Constraints) |

## Constraints discovered by probing the live MCP

Probed against `mcmilad.atlassian.net` on 2026-08-04. These are measured, not assumed.

**OAuth scopes granted to the MCP: `read:jira-work`, `write:jira-work`.** No project-administration
scope. Consequently:

| Capability | Available via MCP | Notes |
|---|---|---|
| Create / edit / transition / comment issues | Yes | `createJiraIssue`, `editJiraIssue`, `transitionJiraIssue`, `addCommentToJiraIssue` |
| Labels, Epic parent, dependency links | Yes | `parent` is settable; `createIssueLink` + `getIssueLinkTypes` present |
| Backlog ordering (`Rank`) | Yes | Settable custom field |
| Blocked marker (`Flagged` = `Impediment`) | Yes | Jira's native impediment flag; renders on the board |
| Assign an issue **to** a sprint | Yes | `Sprint` custom field is settable |
| **Create a project** | **No** | No tool, no scope |
| **Create / start / close a sprint** | **No** | No Agile API tools exposed at all |
| **Add a workflow status** | **No** | No workflow administration |

The site's existing project `SCRUM` is team-managed with only `To Do` / `In Progress` / `Done`.
It holds real client work and must not receive agent traffic; the agent team gets its own project.

### Custom field IDs must be discovered, not hardcoded

Custom field IDs (`Sprint`, `Rank`, `Flagged`) are per-site. This repository is a public
template, so the implementation MUST discover them at bootstrap via
`getJiraIssueTypeMetaWithFields` and persist them to config. Hardcoding the IDs observed on one
site would silently break every other install.

## Architecture

### Credential split

Two planes, separated by privilege. The admin credential is powerful (it acts with the operator's
full Jira permissions), so its use is narrowed to one scripted, auditable surface.

| Plane | Credential | Holder | Operations |
|---|---|---|---|
| Admin | `JIRA_API_TOKEN`, `JIRA_EMAIL`, `JIRA_SITE` | `scripts/jira_bootstrap.py` only | Create project, discover per-site IDs, create/start/close sprints |
| Runtime | MCP OAuth | Every agent | Issues, transitions, comments, labels, links, rank |

The admin plane cannot add the `In Review` status. The Jira API does not reliably create statuses
on a team-managed project, so `jira_bootstrap.py` only *warns* and prints a one-time board edit for
the operator to make by hand; `discover` then picks the new status up.

**Confinement is a convention plus an instruction, not a mechanism.** Teammates are *told* never to
hold the admin credential and never to invoke the bootstrap script, and the lead is *told* it is the
only actor that runs it — nothing enforces either. Every agent `Bash` call is a child of the process
that launched Claude Code and inherits its environment, so a token exported into that shell is
readable by any teammate. The one real control available is where the operator exports it: running
the bootstrap commands in a separate terminal keeps the token out of the agent session's environment
entirely. That is the documented, recommended path; anything else trades the confinement for
convenience.

### Jira schema

**Project:** team-managed Scrum, key `AGENT` (configurable). Team-managed so board columns and
statuses can be adjusted without a Jira site admin.

**Issue types:** `Epic` (one per spec) and `Task` (one per unit of work). Review and SA passes are
Tasks carrying `role-review` / `role-sa`, which keeps the model to two concepts.

**Statuses:** `To Do` → `In Progress` → `In Review` → `Done`. "Unclaimed" is `status = "To Do"`,
not a label question — JQL cannot wildcard labels, so encoding claim state in status is what makes
the queue race-free to query.

**Blocked** is `Flagged = Impediment` rather than a fifth status. It is Jira-native, renders on the
board, and needs no workflow change.

**Labels** carry the metadata:

| Label | Purpose |
|---|---|
| `spec-<slug>` | Which spec the issue belongs to |
| `role-coding` \| `role-devops` \| `role-sa` \| `role-review` | Which role may claim it |
| `agent-<instance>` | Which instance claimed it, e.g. `agent-coding-2`. Drives swimlanes |
| `group-<n>` | Redundant with sprint, but survives sprint closure for history |
| `skip-format-check`, `skip-verify` | The existing bypass tokens, now first-class labels |

**Issue shape:**

```
Summary:     [coding] implement POST /login handler
Parent:      AGENT-1  (the spec Epic)
Sprint:      Group 2 — handlers
Labels:      spec-auth-api, role-coding, group-2
Description:
  Spec:       .claude/specs/auth-api/spec.md#login
  Files:      src/auth/login.py, tests/auth/test_login.py
  Acceptance: POST /login returns 200 + AuthToken on valid creds, 401 otherwise
  Run:        pytest tests/auth/test_login.py -q
```

**Dependencies** become native Jira issue links (`blocks` / `is blocked by`), replacing prose
dependency declarations and yielding dependency visualization for free.

### Run state

`.claude/specs/<slug>/jira-run.json` holds the Epic key and the sprint ID per group. It is a
pointer file, not a backlog — it is how the lead knows which sprint receives group 3's issues.

`.claude/jira-config.json` holds site cloud ID, project key, and the discovered custom-field and
status IDs. Written by bootstrap, read by agents and hooks.

## The claim protocol

Jira offers no compare-and-swap, and up to twelve agents self-claim concurrently against one
queue. Correctness comes from read-after-write with a deterministic tie-break.

```
1. FIND     JQL: project = AGENT AND sprint in openSprints()
                 AND status = "To Do" AND labels = role-coding
                 ORDER BY Rank ASC
2. CLAIM    add label agent-coding-2  →  transition to In Progress
3. CONFIRM  re-read the issue
            if more than one agent-* label is present:
                lowest instance name wins (lexicographic)
                loser removes its own label and returns to step 1
4. WORK     run the task's `Run:` command
5. ATTEST   write ~/.claude/logs/verified/AGENT/<ISSUE-KEY>.verified
6. HANDOFF  comment verification output + summary → transition to In Review
7. CLOSE    only the synthesizer (review-1) moves In Review → Done, on PASS
```

Both racing agents evaluate the same rule on the same data and reach the same verdict without a
coordination round-trip.

Two deliberate semantic changes: an implementer can no longer close its own work (it can only
reach `In Review`), and `Blocked` becomes a flag on the card rather than a `[!]` marker in a file.
Both make the board honest about where the review gate sits.

### Comment protocol

Four milestones per issue — claim, blocker, verification result, completion summary. Review
findings are comments on the issue they concern; the synthesizer's PASS/FAIL verdict is a comment
on the sprint's `role-review` issue. Roughly 5–8 writes per issue.

## Guardrails

The three existing hooks are fail-open by design — a hook bug must never roll back a task, block a
valid completion, or trap a teammate. That rule is preserved verbatim.

With the built-in task tools gone, `TaskCreated` and `TaskCompleted` no longer fire. The two
guardrails re-anchor onto `PreToolUse` matchers against the Jira MCP calls. Journaling uses
`PostToolUse` so it records what succeeded rather than what was attempted.

| Hook | Event | Behavior |
|---|---|---|
| `jira_issue_format_check.py` | `PreToolUse` → `createJiraIssue` | Blocks creation unless the summary starts with a role tag, the description carries `Spec:`/`Files:`/`Acceptance:`/`Run:`, and `role-*` + `spec-*` labels are present. Bypass: `skip-format-check` label |
| `jira_transition_verify_gate.py` | `PreToolUse` → `transitionJiraIssue` | A transition to `In Review` or `Done` requires the sentinel `~/.claude/logs/verified/AGENT/<ISSUE-KEY>.verified`, consumed on success. Bypass: `skip-verify` label, read from the mirror |
| `jira_mirror_journal.py` | `PostToolUse` → create/edit/transition/comment | Appends the observed mutation to `~/.claude/logs/jira-mirror/AGENT.jsonl` |
| `teammate_idle_workcheck.py` | `TeammateIdle` | Rewritten to read the mirror instead of `~/.claude/tasks/`. Same two-nudge loop guard |

The format check is strictly stronger than what it replaces: it prevents the malformed issue
rather than rolling it back after creation.

**Mirror staleness is a known blind spot.** The journal only records mutations agents made through
the MCP. Board edits made by hand are invisible to it until an agent next touches that issue. This
affects only the idle nudge, which is advisory and fail-open.

## Files changed

**Rewritten**
- `.claude/agents/{fullstack,coding,devops,review,sa}-agent.md` — claim protocol, comment protocol, sentinel path, Jira dispatch
- `.claude/rules/agent-team-protocol.md` — lifecycle, communication, completion reporting, blocker reporting, verification gate
- `.claude/skills/spec-workflow/SKILL.md` — task format becomes issue format; `tasks.md` removed from the directory structure
- `README.md`

**New**
- `.claude/skills/jira-workflow/SKILL.md` — JQL snippets, claim protocol, comment templates, field discovery, tie-break rule. Mechanics live here so agent files stay readable
- `scripts/jira_bootstrap.py` — `ensure-project`, `ensure-status`, `sprint-open`, `sprint-close`; idempotent, non-interactive
- `.claude/hooks/jira_issue_format_check.py`, `jira_transition_verify_gate.py`, `jira_mirror_journal.py`

**Deleted**
- `.claude/hooks/task_created_format_check.py`, `.claude/hooks/task_completed_verify_gate.py`
- All `tasks.md` machinery and its template

**Fixed as a prerequisite**
- `settings.json` → `.claude/settings.json` (Claude Code does not read the repo-root copy)
- Hook commands `$CLAUDE_PROJECT_DIR/hooks/` → `$CLAUDE_PROJECT_DIR/.claude/hooks/`

As checked out, the hooks are not firing at all. This is not a drive-by cleanup; nothing in this
design works until it is fixed.

## Verification

**Unit:** each hook against fixture payloads — block, allow, bypass label, malformed input, and
fail-open on unexpected conditions.

**Live end-to-end (required):** bootstrap a real `AGENT` project, run a throwaway three-task spec
through a two-agent pool, and confirm on the board: issues created in the right sprint, claims
appearing as swimlane moves, a verification comment, a blocked flag, and a synthesizer close.

Per the project's own live-validation rule, static tests alone are not a PASS for tooling that
mutates a remote system.

## Risks

| Risk | Severity | Mitigation |
|---|---|---|
| Adding `In Review` to a team-managed workflow via REST may not be supported | Medium | Bootstrap attempts it; on failure prints exact UI steps and exits non-zero. One-time manual click, not a per-run gate |
| Retiring `tasks.md` leaves no disk-side plan if Jira is unreachable mid-run | Medium | Accepted by the user (D5). Agents stall and escalate rather than inventing state |
| The admin API token acts with full operator permissions | Medium | Confined to the bootstrap script; never passed to teammates; documented as security-sensitive |
| Rate limits with a twelve-agent pool | Low | D6 caps writes at ~5–8 per issue; claim path is two calls |
| Mirror journal misses manual board edits | Low | Affects only the advisory idle nudge, which is fail-open |
| Jira has no compare-and-swap for claims | Low | Read-after-write with deterministic lexicographic tie-break |

## Out of scope

- Bidirectional sync (a human creating an issue in Jira that agents then pick up)
- Confluence publishing of specs
- Migrating the existing `SCRUM` project
- Per-agent Atlassian accounts
