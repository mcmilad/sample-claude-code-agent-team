# Spec — Shorty, a Backend-Only Serverless 3-Tier URL Shortener

Slug: `shorty`
Created: 2026-08-07
Status: Approved

## Problem / Goal

This repository demonstrates a multi-agent, spec-driven workflow but ships no worked
example of the thing that workflow produces: a real AWS application. An earlier attempt
(`serverless-3tier`) included a static SPA tier and was removed from the tree; the `apps/`
directory it would have lived beside is still empty.

Build **Shorty** — a URL shortener with three genuinely separate *runtime* tiers (managed
API edge, serverless compute, managed data store) — as a **deploy-ready reference that is
authored, synthesized, and tested, but not deployed by the agent team**. The audience is
someone evaluating this repo who wants to see what the agent team actually builds, and who
can then run the deploy runbook themselves when they choose to.

Backend-only: there is no UI, no SPA, no hosted login page. The deliverable is an API.

## Requirements

### Functional

- **F1** `POST /links` accepts `{"url": "<absolute http(s) URL>"}` and returns
  `201 {"code","shortUrl"}` with a newly minted 7-character code — acceptance: unit test
  asserts the 201 body shape and that the code is written with a condition expression that
  rejects an existing code.
- **F2** `GET /{code}` returns `302` with a `Location` header equal to the stored URL and
  `Cache-Control: no-store` — acceptance: unit test asserts status, both headers, and that
  no response body echoes caller input.
- **F3** An unknown or malformed code returns `404` with a machine-readable body
  (`{"error":"NOT_FOUND"}`), never a 5xx and never a redirect to a default page —
  acceptance: unit test over unknown, wrong-length, and illegal-character codes.
- **F4** A URL that is absent, malformed, non-`http(s)`, credential-bearing, over-long, or
  addressed to a loopback / private / link-local **IP literal** is rejected with
  `400 {"error":"INVALID_URL"}` — acceptance: table-driven unit test over the rejection
  corpus in `tests/unit/test_validate.py`.
- **F5** `POST /links` requires a valid Cognito user-pool JWT; an absent or invalid token
  is rejected by the API Gateway authorizer before any handler runs — acceptance: synthesized
  template asserts a JWT authorizer bound to the user pool issuer on the `POST /links` route,
  and asserts the `GET /{code}` route has none.
- **F6** Code minting retries a collision up to 5 times and then returns
  `503 {"error":"CODE_COLLISION"}` rather than overwriting an existing link — acceptance:
  unit test drives 5 consecutive `ConditionalCheckFailedException` responses and asserts
  the 503 plus exactly 5 attempts.

### Non-Functional

- **NF1 (defining constraint)** The agent team does not deploy. No `cdk deploy`, no AWS
  credentials in CI, no `secrets:` reference in the workflow, no live resources, no spend.
  The CDK app is environment-agnostic — no explicit `env`, no context lookups — so
  `cdk synth` succeeds with no AWS account and no credentials. Deployment is a documented
  manual step the repo owner runs from `apps/shorty/README.md`.
- **NF2** Verification is entirely local and offline: `ruff`, `pytest`, `cdk synth`.
- **NF3** Security posture per `.claude/rules/AWS-security-guidelines.md` is asserted **in
  tests against the synthesized template**, not merely described in prose. A regression in
  posture fails the build. See `design.md` → Security Considerations.
- **NF4** Least privilege: each Lambda has its own execution role, scoped to the single
  table ARN and a single DynamoDB action. No wildcard actions, no shared roles.
- **NF5** Readable end to end in one sitting. One language (Python) across infra, handlers,
  and tests. No Powertools, no web framework on the handlers, no ORM.
- **NF6** Cost while the team works: zero, enforced by NF1. Cost once the owner deploys:
  dominated by the customer-managed KMS key (~$1/month); everything else is on-demand and
  effectively free at POC traffic.

## Constraints & Assumptions

1. **`GET /{code}` cannot be authenticated.** A short link is followed by an arbitrary
   browser carrying no credentials. This is the documented exception permitted by
   `AWS-security-guidelines.md` → API Gateway ("no open/unauthenticated endpoints unless
   explicitly documented"). Justification is F2's nature; compensating controls are stage
   throttling, a read-only execution role, and no reflection of caller input into any
   response body.
2. **AWS WAF cannot attach to an HTTP API (API Gateway v2).** WAF supports REST APIs,
   CloudFront, and ALB only. The guidelines rate a missing WAF a *Warning* for public-facing
   APIs. Accepted deliberately rather than switching tier 1 to a REST API; recorded in
   `decisions.md` → D-002.
3. **DNS-based SSRF is not fully preventable at validation time.** Validation rejects
   loopback / private / link-local *IP literals*, but hostnames are not resolved, so a
   public name resolving to `10.0.0.5` still passes. Resolving at validation time would be
   both a TOCTOU race and a network dependency inside a unit-tested pure function. Stated as
   a known limitation, not a defect; recorded in `decisions.md` → D-003.
4. **Cognito MFA is `OPTIONAL`, not required.** Required MFA breaks the scripted
   `admin-initiate-auth` flow that is the only way to obtain a token in a backend-only
   system with no UI. A deliberate deviation from the guidelines' "configure MFA", recorded
   in `decisions.md` → D-004.
5. **Node.js is only available under nvm on this machine** (`~/.nvm/versions/node/
   v22.23.1/bin`) and is not on the default `PATH`. The CDK CLI is Node-based and has no
   pure-Python equivalent, so every `Run:` command that invokes `cdk` must export that path
   first. CI uses `actions/setup-node` and is unaffected.
6. **Python dependencies are isolated to `apps/shorty/.venv`**, separate from the
   repository-root `.venv` that serves the hook test suite. The app carries its own
   `pytest.ini`; the root `pytest.ini` scopes `testpaths` to `tests/` and therefore does not
   collect the app's tests.
7. **Assumption:** `aws-cdk-lib` v2 and `boto3` are the pinned dependency baseline, with
   `aws-cdk-lib`'s `aws_apigatewayv2` L2 constructs used for the HTTP API. Flagged because
   the v2 L2 API surface has moved between minor versions; the pin is exact, not a range.

## Design Decisions

| Decision | Choice | Alternatives Considered | Rationale |
|---|---|---|---|
| Deployment | Authored deploy-ready, deployed by the owner, never by the team | Team deploys to a sandbox; synth-only with no deploy path | Keeps team verification offline and free while still producing something that genuinely runs; the runbook is the handoff |
| Language + IaC | Python 3.13 handlers, CDK in Python | TypeScript + CDK; Python + SAM | One toolchain matching the repo's existing pytest/venv tooling, instead of adding a second language |
| API type | HTTP API (API Gateway v2) | REST API v1 | Cheaper, terser, native JWT authorizer; REST's only advantage here is WAF support, addressed in D-002 |
| Create-route auth | Cognito user pool + JWT authorizer | `AWS_IAM` + SigV4; public + throttling only | An explicit identity on every minted link (`createdBy`); SigV4 needs no user pool but ties callers to IAM principals, and a public create route lets anyone mint links |
| Stack split | Two stacks, split on lifecycle: `ShortyDataStack` / `ShortyAppStack` | One stack; three stacks mirroring the three tiers | The split with an actual operational reason — stateful `RETAIN` resources redeploy on a different cadence than disposable compute. A stack-per-tier split mirrors the diagram but buys nothing |
| Redirect status | `302` + `Cache-Control: no-store` | `301` permanent redirect | `301` is cached indefinitely by browsers and intermediaries, making a link impossible to revoke or repoint |
| Code minting | 7 chars, `secrets.choice` over a 62-symbol alphabet, conditional put | Monotonic counter + base62; UUID prefix | Non-enumerable (a counter leaks volume and lets anyone walk the corpus); the conditional put makes collision handling explicit rather than silent overwrite |
| Table design | Single table, PK `code`, on-demand billing | Provisioned capacity; GSI on `createdBy` | Both access patterns are point operations on `code`; a GSI would exist only for a listing feature that is out of scope |
| Scope | Create + redirect only | Custom aliases; click counting; TTL expiry | YAGNI for a reference POC — each addition brings a second write path and its own failure modes without demonstrating anything the two core routes do not |

## Out of Scope

Custom aliases, click analytics, link expiry/TTL, link deletion or update, listing a user's
links, a hosted signup or login UI, a custom domain, multi-region, and CI-driven deployment.
