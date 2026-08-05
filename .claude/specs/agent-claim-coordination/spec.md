# Spec — Atomic Claim Coordination for the Agent Team

Slug: `agent-claim-coordination`
Created: 2026-08-05
Source: `findings.md` (12 findings + 8 validation findings, corrected fix order C1–C12)

## Problem

Agents working the Jira board pick up issues without any mutual exclusion and, in
practice, without telling the board they did. Measured on the live project: **8 of 11
worked issues never entered `In Progress`** — they went `To Do → In Review` directly
(Jira changelog 10120, 10121). The lead's monitor JQL therefore reports in-flight work as
unstarted and may split or reassign it onto a second instance.

Underneath the visibility problem is a correctness one. The documented claim protocol
detects a race only when two `agent-*` labels are present, which the API can never
produce: `editJiraIssue` replaces the labels array, so the loser's write erases the
winner's label and exactly one survives. Two agents then edit the same `Files:`.

## Constraints discovered before design

These are load-bearing and were verified against the live system, not assumed.

1. **Jira transitions in this project are global.** `getTransitionsForJiraIssue` on
   AGENT-15 (`To Do`) and AGENT-13 (`Done`) return identical sets — ids 11/21/31/41, all
   `isGlobal: true`. A transition can never fail due to a wrong source status, so it
   cannot serve as a claim: both racers' `transitionJiraIssue(21)` succeed.
2. **No compare-and-swap primitive is reachable.** The live `editJiraIssue` MCP schema is
   `{cloudId, issueIdOrKey, fields, contentFormat, responseContentFormat}` with
   `additionalProperties: false` — no version, no ETag, and no `update` verb, so Jira's
   atomic server-side list-append is unavailable to agents.
3. **All agents share one filesystem** and already depend on it for verification
   sentinels under `~/.claude/logs/verified/`.
4. **`getJiraIssue`'s `fields` parameter replaces the default set.** `fields=["issuelinks"]`
   returns issuelinks and *no labels* — feeding that into the claim's read-modify-write
   would erase `role-*`/`spec-*`/`group-*` permanently.
5. **The mirror journal is unversioned and never rotated**, and a live 56-line journal
   already exists. Adding a top-level field without widening `load_state`'s default dict
   raises `KeyError` on every pre-existing event, which the fail-open handler swallows —
   silently disabling the guardrail.

## Goals

- **G1** A claim is atomic: exactly one agent can own an issue, decided by a primitive
  that actually provides mutual exclusion.
- **G2** A claim is visible on the board before the owner edits any file.
- **G3** An abandoned claim is recoverable, through a mechanism that does not treat
  silence as evidence of death.
- **G4** Every statement of the claim protocol agrees, including the one injected at
  runtime by a hook.
- **G5** A verification sentinel survives a failed transition, and still authorizes at
  most one successful transition.
- **G6** The idle nudge cannot silently withhold work from a teammate.
- **G7** No two issues in the same scope declare overlapping `Files:`.

## Non-goals

- Making Jira itself transactional. It is not, and no amount of protocol fixes that.
- Replacing self-claim with lead-assignment. That would remove the race by construction
  but costs the deep-ready-queue throughput the design is built around. Considered and
  rejected in `design.md`; revisit if G1 proves insufficient in practice.
- Cross-host operation. The claim lock assumes a shared `$HOME`.

## Acceptance criteria

| # | Criterion | Verification |
|---|-----------|--------------|
| A1 | Two concurrent claims on one issue produce exactly one winner | `pytest tests/hooks/test_claim_lock.py` |
| A2 | The loser learns it lost deterministically, not by inference | same |
| A3 | All five copies of the claim protocol agree on step order and loss condition | `pytest tests/test_claim_protocol_consistency.py` |
| A4 | A failed transition leaves the sentinel usable | `pytest tests/hooks/test_jira_transition_verify_gate.py` |
| A5 | One sentinel authorizes at most one *successful* transition | same |
| A6 | The idle nudge never suppresses an issue for a missing label | `pytest tests/hooks/test_teammate_idle_workcheck.py` |
| A7 | Reviewers are told not to self-claim | `pytest tests/test_skill_consistency.py` |
| A8 | Overlapping `Files:` across issues in one scope is blocked at create | `pytest tests/hooks/test_jira_issue_format_check.py` |
| A9 | `spec_gate` does not false-block a spawned teammate | `pytest tests/test_spec_gate.py` |
| A10 | Whole suite green | `.venv/bin/python -m pytest tests/ -q` (baseline 197) |

## Out of scope for this spec

Finding 12 (the `serverless-3tier` CI workflow pointing at an empty `examples/`) is
unrelated to coordination and is handled as a standalone change.
