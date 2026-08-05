# Spec and Jira Are Required

Always-on rule governing **when** a spec and a Jira backlog are mandatory. Applies
universally — every session, every task, every agent, **including a solo session with
no teammates spawned**. This file is auto-loaded as a global rule.

`agent-team-protocol.md` describes how *teammates* work the board. This file
describes who has to *open* it. They are different questions, and conflating them is
the exact failure this rule exists to prevent.

## The Trigger

Before writing any file, evaluate these three conditions:

1. Does the work touch **multiple files**?
2. Does it involve **architectural choices** (service selection, data model, auth
   model, deployment topology, dependency or framework decisions)?
3. Will it be **delegated** to an agent team?

**If ANY one is true, a spec is mandatory** — `.claude/specs/<slug>/spec.md` and
`design.md`, per the `spec-workflow` skill — and the backlog for it goes in Jira per
`jira-workflow`.

This is an OR, not an AND. In particular, **condition 3 is not a precondition for 1
and 2.** Work that will never be delegated still needs a spec if it is multi-file or
architectural. There is no solo exemption.

## What Counts As "Used Jira"

For any work that meets the trigger:

- One **Epic** per spec, labelled `spec-<slug>`
- One **Task** per unit of work, in the shape `jira-workflow` mandates (role tag,
  `Spec:` / `Files:` / `Acceptance:` / `Run:`, `role-*` + `spec-*` labels) — the
  `createJiraIssue` hook blocks anything else
- Issues transitioned as the work lands, with the verification sentinel written
  before each gated transition

Read `.claude/jira-config.json` for `cloudId`, `projectKey`, field IDs, status IDs,
and transition IDs. **Never hardcode an ID** — they differ per site.

If the work meets the trigger but is being done solo, the lead-and-teammate roles
collapse into one session: author the Epic and issues, then work and close them
yourself. A board with one worker is still the record.

## Not Substitutes For A Spec

None of the following discharge the requirement. Each has been mistaken for
compliance:

| Not a spec | Why it fails |
|------------|--------------|
| Stating assumptions in the chat reply | Vanishes with the context window; not a durable record |
| A thorough `README.md` | User-facing docs, not a decision record — it says what, not why-not |
| Inline code comments | Local rationale only; no architecture-level view |
| "The user said make assumptions" | That waives *clarifying questions*, not the spec |
| "No teammates were spawned" | Conditions 1 and 2 are independent of delegation |
| "It's an example / a lab / a sample" | Scope affects spec *size*, never its existence |

## When A Spec Is Genuinely Not Needed

Single-file, mechanical work with no architectural content: a typo, a version bump, a
one-function bugfix, a config tweak, answering a question. **Say so explicitly in one
line** rather than silently skipping — an unstated skip is indistinguishable from an
oversight.

If you are unsure, write the spec. A short spec for work that did not need one costs
minutes; skipped architecture rationale is unrecoverable once the session ends.

## Order Of Operations

1. Evaluate the trigger **before** the first `Write`
2. If it fires: `spec.md` + `design.md` first, then the Jira Epic and issues, then code
3. If a spec becomes necessary mid-flight (the work grew), stop and write it then —
   backfilling at the end loses the decisions that were live during the work

## Enforced Hook (Automated Guardrail)

`.claude/hooks/spec_gate.py` backs this rule in two modes, both **fail-open**, both
logging to `~/.claude/logs/team-hooks.jsonl`:

- **`UserPromptSubmit`** — when a prompt reads as build work, injects the pre-flight
  checklist so the decision happens before the first file is written.
- **`PreToolUse` on `Write`** — counts new project source files per session. At the
  third one with **no `.claude/specs/*/spec.md` present on disk**, it **blocks once**
  with the reason, then never blocks again that session. `.claude/**`, docs, and
  dependency directories never count, so repairing the guardrail itself can never be
  trapped by it.

  Deliberately **existence, not freshness**. An earlier version required the spec to have
  been written *this session*, which false-blocked the multi-agent flow this repo is built
  around, and its premise was wrong besides — teammates spawned into one run share a
  `session_id`. The cost is real and worth naming: once any spec exists in a repo, the
  write arm is effectively satisfied for every later session. **The rule above is the
  requirement; this hook only ever was a reminder that it exists.**

Bypass with `CLAUDE_SPEC_GATE=off` when a session legitimately has no spec.

The hook is a backstop for when this rule is misread. It is deliberately weak — one
stop, easily bypassed — because a guardrail that traps a session is worse than none.
**The rule above is the requirement; the hook is only a reminder that it exists.**
