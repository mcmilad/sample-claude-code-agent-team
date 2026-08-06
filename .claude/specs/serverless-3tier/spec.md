# Spec — Serverless 3-Tier URL Shortener (Proof of Concept)

Slug: `serverless-3tier`
Created: 2026-08-07
Status: Approved

## Problem / Goal

This repository demonstrates a multi-agent, spec-driven workflow but ships no worked
example of the thing that workflow is supposed to produce: a real AWS application. A CI
workflow for `examples/serverless-3tier/` was added in commit `1605c9e` and has been
sitting in the tree guarding a directory that does not exist.

Build that example — a URL shortener with three genuinely separate tiers (static SPA,
serverless API, managed data store) — as a **reference template that is read, synthesized,
and tested, but never deployed**. The audience is someone evaluating this repo who wants
to see what the agent team actually builds, not a production link service.

## Requirements

### Functional

- **F1** `POST /links` accepts `{"url": "<absolute http(s) URL>"}` and returns
  `201 {"code","shortUrl"}` with a newly minted 7-character code — acceptance: unit test
  asserts a 201 body shape and that the code is unique against a mocked conditional put.
- **F2** `GET /{code}` returns `302` with a `Location` header equal to the stored URL —
  acceptance: unit test asserts status, header, and `Cache-Control: no-store`.
- **F3** An unknown code returns `404` with a machine-readable body, never a 5xx and never
  a redirect to a default page — acceptance: unit test on the redirect handler.
- **F4** A URL that is absent, malformed, non-`http(s)`, credential-bearing, or pointed at
  a non-public host is rejected with `400 {"error":"INVALID_URL"}` — acceptance:
  table-driven unit test over the rejection corpus in `validate.test.ts`.
- **F5** The SPA lets a user paste a URL, submit it, and copy the resulting short link,
  and renders the API's error code on failure — acceptance: `vite build` succeeds and the
  API-client module's unit test covers the success and error branches.

### Non-Functional

- **NF1 (defining constraint)** Nothing is ever deployed. No `cdk deploy`, no AWS
  credentials in CI, no `secrets:` reference in the workflow, no live resources, no spend.
  The CDK app is environment-agnostic — no explicit `env`, no context lookups — so
  `cdk synth` succeeds with no AWS account. Any change that breaks this breaks the spec.
- **NF2** Verification is entirely local and offline: `tsc --noEmit`, `jest`, `cdk synth`,
  `vite build`.
- **NF3** Security posture per `rules/AWS-security-guidelines.md` is asserted **in tests
  against the synthesized template**, not merely described in prose. See A4.
- **NF4** Least privilege: each Lambda has its own execution role, scoped to a single
  table ARN and a single action. No wildcard actions, no shared roles.
- **NF5** Readable end to end in one sitting. One language (TypeScript) across infra,
  handlers, and SPA. No Powertools, no framework on the handlers.
- **NF6** Cost ceiling: zero. Enforced by NF1.

## Constraints & Assumptions

1. **SigV4 binds the signature to the `Host` header.** API Gateway validates a signed
   request against the `execute-api` hostname it receives. A CloudFront distribution in
   front of the API cannot preserve that hostname, so signed requests routed through the
   distribution fail authentication. This constraint, not preference, is why CloudFront
   fronts only the SPA. Recorded in `design.md` → Trade-offs.
2. **`GET /{code}` cannot be authenticated.** A short link is followed by an arbitrary
   browser with no credentials. This is the documented exception permitted by
   `AWS-security-guidelines.md` → API Gateway ("no open/unauthenticated endpoints unless
   explicitly documented"). The justification is F2's nature; the compensating controls
   are stage throttling, a read-only execution role, and no reflection of caller input
   into the response body.
3. **Node.js is only available under nvm on the dev machine** (`~/.nvm/versions/node/
   v22.23.1/bin`); it is not on the default `PATH`. Any `Run:` command touching npm must
   export it first. CI uses `actions/setup-node` and is unaffected.
4. **Assumption:** `aws-cdk-lib` v2 and `@aws-sdk/client-dynamodb` v3 are the pinned
   dependency baseline. Flagged because the SPA's SigV4 signing adds `aws4fetch`, a small
   third dependency, and that choice should be revisited if it grows.

## Design Decisions

| Decision | Choice | Alternatives Considered | Rationale |
|---|---|---|---|
| Deployment | Synth + test only, never deployed | Deploy to a sandbox account | PoC value is in the readable artifact; deploying adds credentials, spend, and teardown for no added demonstration |
| IaC + runtime | CDK TypeScript, Lambda on Node 22 TypeScript | CDK TS + Python handlers; SAM + Python | One language across all three tiers; the pre-existing CI was already written around `cdk synth` + npm |
| API type | HTTP API (API Gateway v2) | REST API | Cheaper and terser; `AWS_IAM` authorizer is all the auth model needs, and REST's API keys are not an authentication mechanism |
| Create-endpoint auth | Cognito identity pool, unauthenticated identities, `AWS_IAM` authorizer | Cognito user pool + JWT; public + throttling only | Satisfies the authorizer requirement with no login screen and no user-management scope; a public create route would let anyone mint links |
| Redirect status | `302` + `Cache-Control: no-store` | `301` | A permanent redirect is cached by browsers indefinitely and is undebuggable in a PoC; 301 is also unrevocable per client |
| Code generation | 7 chars base62 from `crypto.randomBytes` | Incrementing counter; hash of URL | Random resists enumeration of the whole link space; a counter leaks volume and a URL hash leaks equality |
| Collision handling | `PutItem` with `attribute_not_exists(code)`, ≤3 retries | Read-then-write; ignore | Read-then-write is a race; the conditional put is the only correct single-round-trip form |
| CloudFront scope | SPA origin only; SPA calls the API URL directly with CORS | Single distribution fronting S3 and the API | Forced by constraint 1 — SigV4 through CloudFront cannot authenticate |
| Stack split | Three stacks: Data, Api, Web | One stack | Makes the three tiers legible as separate units and forces the cross-stack contract to be explicit |
| WAF | Deferred, documented | Attach a web ACL to the distribution | Warning-level in the rules, not Critical; nothing is deployed, so it would be untestable ceremony. Named in Out of Scope rather than silently skipped |

## Interfaces & Contracts

Pinned so the handler, SPA, and stack tasks can run in parallel without blocking.

**HTTP**

```
POST /links
  Authorization: AWS4-HMAC-SHA256 ...        (SigV4, from Cognito identity pool creds)
  Content-Type: application/json
  → { "url": "https://example.com/a/b?c=d" }
  ← 201 { "code": "aB3xY7z", "shortUrl": "https://<api-host>/aB3xY7z" }
  ← 400 { "error": "INVALID_URL", "message": "<safe, caller-facing text>" }
  ← 500 { "error": "INTERNAL" }

GET /{code}
  ← 302  Location: <stored url>;  Cache-Control: no-store
  ← 400 { "error": "INVALID_CODE" }          (code fails /^[0-9A-Za-z]{7}$/)
  ← 404 { "error": "NOT_FOUND" }
```

**DynamoDB item** — table `links`, partition key `code` (S), no sort key:

```
{ code: S(7 chars, base62), url: S, createdAt: S(ISO-8601 UTC) }
```

**Module signatures**

```ts
// src/lib/code.ts
export function generateCode(): string;                    // 7 chars, [0-9A-Za-z]
export const CODE_PATTERN: RegExp;                         // /^[0-9A-Za-z]{7}$/

// src/lib/validate.ts
export type ValidationResult = { ok: true; url: string } | { ok: false; reason: string };
export function validateTargetUrl(input: unknown): ValidationResult;
```

**Cross-stack outputs** — `DataStack` exports `table: ITable` and `key: IKey`;
`ApiStack` consumes both and exports `apiUrl: string`; `WebStack` consumes `apiUrl` plus
the identity pool id for the SPA's build-time config. Stacks are wired in `bin/app.ts`,
which Sprint 1 owns exclusively.

## Edge Cases & Risks

- Code collision on insert → conditional put fails, regenerate, retry up to 3 times, then
  `500 INTERNAL`. At 62^7 (~3.5e12) keys the retry path is effectively dead code, and it
  is tested anyway because "effectively dead" is not "unreachable".
- `url` present but not a string (number, object, `null`) → `400 INVALID_URL`, not a crash.
- Extremely long URL (> 2048 chars) → `400 INVALID_URL`. Bounds the item size and the
  `Location` header.
- **Open-redirect / SSRF laundering (the real risk here).** An unrestricted shortener
  masks phishing targets and can be used to reach link-following internal services.
  Mitigation: scheme allowlist (`http`, `https` only — rejecting `javascript:`, `data:`,
  `file:`), rejection of embedded credentials, and rejection of `localhost`, RFC1918
  ranges, and `169.254.169.254`. This mitigates but does not eliminate: DNS names
  resolving to private space at follow time are not caught, and that residual risk is
  accepted for a non-deployed PoC.
- Stored URL is echoed into a `Location` header → header-injection risk if it contained
  CR/LF. `new URL()` parsing in validation rejects those before storage.
- **Risk: the security posture drifts from the spec.** Prose in a design doc does not fail
  a build. Mitigation is A4 — assertions against the synthesized CloudFormation template.

## Acceptance criteria

| # | Criterion | Verification |
|---|-----------|--------------|
| A1 | Handlers meet the contract above, including every rejection case | `npm test` (jest, `test/handlers/`, `test/lib/`) |
| A2 | The CDK app synthesizes with no AWS account or credentials | `npx cdk synth` |
| A3 | Types are sound across infra, handlers, and SPA | `npm run typecheck` (`tsc --noEmit`, both packages) |
| A4 | The synthesized template asserts the security posture: Block Public Access on both buckets, `aws:SecureTransport:false` deny statements, CMK encryption on table and buckets, PITR enabled, `AWS_IAM` authorizer on `POST /links`, two distinct execution roles, and no wildcard IAM actions | `npm test` (`test/infra/`) |
| A5 | The SPA builds and its API client handles success and error branches | `npm run build --prefix web` and its unit test |
| A6 | CI is restored, passes on the built tree, and contains no deploy job, no AWS credentials, and no `secrets:` reference | `grep -L secrets .github/workflows/serverless-3tier.yml` + a green run |

## Out of Scope

Deliberately excluded; each is a real feature that a production shortener would need.

- **Deployment of any kind**, and therefore also: custom domains, ACM certificates,
  teardown procedures, and live smoke tests. (NF1.)
- Click analytics / counters, custom vanity aliases, link expiry (TTL), link deletion or
  editing, and per-user link ownership.
- WAF web ACL on the distribution (Warning-level in the rules; see Design Decisions).
- Authenticated end users. The identity pool issues *unauthenticated* identities only;
  there is no sign-up, sign-in, or per-user authorization.
- Multi-region, DR, backup restore drills, and CloudWatch alarms/dashboards.
- Rate limiting beyond API Gateway's default stage throttling.

## Open Questions

None. All four opened during brainstorming (deploy target, presentation tier, IaC and
runtime, feature scope, create-endpoint auth) were resolved by the user before this spec
was written; the resolutions are the Design Decisions table above.
