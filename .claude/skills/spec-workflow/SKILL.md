---
name: spec-workflow
description: Spec-driven development loop with parallel sprints and the .claude/specs/<slug>/ structure (spec.md, design.md, decisions.md); the backlog itself lives in Jira. Use before any non-trivial work that touches multiple files, involves architectural choices, or will be delegated to an agent team.
---

# Spec-Driven Workflow

## When to Create a Spec

Create a spec before any non-trivial work — if it touches multiple files, involves architectural choices, or will be delegated to an agent team.

## Directory Structure

```
.claude/specs/<slug>/
  spec.md          # Design decisions, requirements, constraints
  design.md        # Architecture, repo structure, infrastructure design
  jira-run.json    # Generated: Epic key + sprint id per group
  decisions.md     # Mid-flight decision log
  sa-review.md     # Well-Architected findings (only when sa-agent runs)
  requirements.md  # From /brainstorm (optional)
  prd/             # Product requirements docs (optional)
```

The backlog is in Jira: one Epic per spec, one Task per unit of work, one sprint per
parallel group. Review verdicts are comments on the sprint's `role-review` issue.

Use short kebab-case slugs (e.g., `auth-api`, `vpc-redesign`).

## Issue Format

Issues organized into parallel groups, one sprint per group. All issues in a sprint
execute simultaneously; sprints execute sequentially. Every Task issue carries:

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

See the `jira-workflow` skill for the full issue shape, the create-time format check,
and the claim protocol.

### Issue Rules

- Each issue is self-contained — completable without knowledge of sibling issues
- Summary tagged `[coding]`, `[devops]`, `[sa]`, or `[review]`, matching the `role-*` label
- Explicit file paths (no two issues in the same sprint write to the same file)
- Interface contracts inline if producing/consuming shared interfaces (exact signatures)
- Verification command included
- Small enough for one teammate in a single session

### Coordination

State lives in Jira and nowhere else. Teammates self-claim per the `jira-workflow` skill;
the lead monitors with JQL. Load `jira-workflow` for the claim protocol and comment
templates.

### Parallelization Guidelines

Maximize parallelism: no shared file writes -> same sprint. Infrastructure before app code. Shared interfaces before consumers. SA review runs parallel with implementation (reviews design, not code).

**Author for a worker pool, not a single worker.** The lead spawns *parallel pools* of same-role agents (up to 6 `coding`, 2 `devops`, 4 `review`) that self-claim from a shared queue. To keep them saturated:
- Make sprints **wide** — as many file-disjoint same-role tasks per group as there are instances of that role. A group with one fat task starves the pool; split fat tasks along file/module boundaries.
- Make sprints **few** — only start a new group when a real dependency forces a barrier. Independent work stays in the same group.
- Front-load a shared interface as a small early group so all dependents then run in parallel.
- Keep `[coding]` and `[devops]` work file-disjoint so both pools run at once.

**Parallel review (synthesizer + analysts).** When a group's changes span multiple areas, the lead assigns one reviewer as **synthesizer** (sole author of the verdict comment, owns the single group verdict) and the rest as **analysts** who each review a disjoint slice and message structured findings to the synthesizer — they write no file. There is always exactly one verdict comment and one PASS/FAIL per cycle; a single reviewer handles small, cohesive groups alone. This ownership is a protocol convention, not a machine-enforced guardrail — see `jira-workflow` → "Closing" for what the gate actually checks.

## Development Loop

Plan -> Build (per group) -> Review -> Fix (if FAIL) -> Cleanup. See `fullstack-agent` system prompt for the detailed phase steps.

**Security scan remediation priority**: (1) Critical findings — immediate fix required. Run scans: `bandit -r src/ -f json -o .claude/specs/<slug>/bandit-results.json`, `semgrep --config auto --json -o .claude/specs/<slug>/semgrep-results.json`, `safety check --json > .claude/specs/<slug>/safety-results.json`, `checkov -d infra/ -o json > .claude/specs/<slug>/checkov-results.json`. (2) High findings — fix or document risk acceptance with compensating controls before merge, (3) Medium findings — fix within sprint or document acceptance.

**Acceptance criteria MUST include verification in priority order**: (1) Encryption at rest verified via `aws <service> describe-<resource> | jq '.EncryptionConfiguration'` (expect: AWS KMS key ARN present) — blocks deployment, (2) Encryption in transit verified via `aws <service> get-<resource>-policy` (expect: `aws:SecureTransport` condition present) — blocks deployment, (3) Access logging enabled via `aws <service> get-<resource>-logging` (expect: logging target configured) — required for review PASS, (4) Data classification tags via `aws <service> list-tags-of-resource` (expect: `data-classification` tag present) — required for review PASS.

**Serverless-specific acceptance criteria**: For Lambda tasks, verify event schema conformance via `get_lambda_event_schemas` from `aws-serverless` plugin. For SAM deployments, verification MUST include `sam_build` + `sam_local_invoke` (local test) before `sam_deploy`. For API Gateway, verify authorization is configured on all routes. For Aurora DSQL tasks, verify schema via `get_schema` and test queries via `readonly_query` from `databases-on-aws` plugin. For Amplify tasks, verify sandbox deployment succeeds before production.

**Completion criteria**: Zero criticals + zero warnings + all tests passing + all issues `Done`. Suggestions don't block.

**Live-validation gate (IaC / deploy / shell tooling)**: Static checks (`terraform validate`, `cfn-lint`, `shellcheck`, `bash -n`, `checkov`, `helm lint`) are necessary but NOT sufficient — they cannot catch runtime/cloud-semantics bugs (wrong build context, a config file clobbering an env var, a missing `--region`, an SSE-S3-not-KMS backend, a wrong-kubeconfig-context deploy). Any group that changes a deploy script, IaC, or CI MUST be exercised by a real `deploy → smoke → teardown` (or the closest executable equivalent) before it is "done". If it cannot run in the current environment, record the affected criteria as author-and-static-validate-only and escalate that the live gate is outstanding — never a PASS that implies it ran.

**Verify the verifier**: A green gate is not proof the gate is adequate. When a check passes, confirm it actually asserts what it claims (a license-header check that greps one line passed truncated headers; a `verify-codegen` target was itself broken; a task `Run:` of `go build`+`go vet` never ran the CI-blocking linter). When you find a silent-gap class, fix the *check*, not just the instances.

**Safeguards**: Max 3 review cycles per group, then escalate. Log decisions in `decisions.md`. Same blocker twice -> escalate to user.
Ground truth is Jira issue state, then verification sentinels, then `git diff` — not the
mailbox. If Jira is unreachable, stall and escalate rather than inventing state.

## Spec and Document Formats

Reference templates live in `docs/specs/templates/` (`spec.md`, `design.md`, `sa-review.md`, `decisions.md`, `prd.md`) — copy them into the working spec at `.claude/specs/<slug>/` as starting points; they are examples, not rigid constraints. If the directory is absent, follow the section structures described in this file and in the agent definitions. Any `design.md` MUST include a Security Considerations section regardless of template availability.
