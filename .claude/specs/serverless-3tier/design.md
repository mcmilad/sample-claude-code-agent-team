# Design — Serverless 3-Tier URL Shortener

Architecture, repository structure, and infrastructure design for `serverless-3tier`.
Companion to `spec.md`. The **Security Considerations** section is mandatory and present.

## Architecture Overview

Three tiers, three CDK stacks, one environment-agnostic CDK app.

```
                    ┌──────────────────────────────────────────┐
   browser ────────▶│ CloudFront (OAC, TLS≥1.2, HSTS/CSP)      │
     GET /          └───────────────────┬──────────────────────┘
                                        │ origin (signed, private)
                                        ▼
                         ┌──────────────────────────┐
                         │ S3 spa-bucket (BPA, CMK) │   TIER 1 — presentation
                         └──────────────────────────┘
   browser
     │  1. GetId + GetCredentialsForIdentity (unauthenticated identity)
     ├────────────────▶ Cognito Identity Pool ──▶ unauth IAM role
     │                                            (execute-api:Invoke on POST /links only)
     │  2. SigV4-signed POST /links
     ▼
  ┌─────────────────────────────────────────────────────────┐
  │ API Gateway HTTP API   (stage throttling, access logs)  │   TIER 2 — logic
  │   POST /links   → AWS_IAM authorizer → create-link λ    │
  │   GET  /{code}  → public (documented)  → redirect λ     │
  └──────────────┬──────────────────────────┬───────────────┘
                 │ PutItem (own role)       │ GetItem (own role)
                 ▼                          ▼
       ┌────────────────────────────────────────────────┐
       │ DynamoDB `links` — PK code(S), CMK SSE, PITR   │   TIER 3 — data
       └────────────────────────────────────────────────┘
```

The SPA calls the API's `execute-api` URL **directly**, not through CloudFront. See
Trade-offs — this is forced, not chosen.

## Repository / Module Structure

```
examples/serverless-3tier/
  package.json                  # cdk + jest + handler deps; scripts: typecheck, test, synth
  tsconfig.json
  jest.config.js
  cdk.json                      # no context lookups (NF1)
  bin/
    app.ts                      # instantiates DataStack → ApiStack → WebStack  [Sprint 1 owns]
  lib/
    data-stack.ts               # KMS key, DynamoDB table
    api-stack.ts                # HTTP API, 2 λ, 2 roles, Cognito identity pool
    web-stack.ts                # SPA bucket, log bucket, CloudFront + OAC
  src/
    lib/
      code.ts                   # generateCode(), CODE_PATTERN
      validate.ts               # validateTargetUrl()
      response.ts               # json()/redirect() helpers, shared error shape
    handlers/
      create-link.ts
      redirect.ts
  test/
    lib/{code,validate}.test.ts
    handlers/{create-link,redirect}.test.ts
    infra/{data,api,web}-stack.test.ts     # Template.fromStack assertions (A4)
  web/
    package.json                # vite, react, aws4fetch, vitest
    vite.config.ts
    index.html
    src/{main.tsx,App.tsx,api.ts,config.ts}
    src/api.test.ts
  README.md
.github/workflows/serverless-3tier.yml     # restored; path-filtered, presence-guarded
```

## Components

### DataStack (`lib/data-stack.ts`)
- **Responsibility:** owns the customer-managed KMS key and the `links` table. Nothing
  else may create a key or a table.
- **Interface:** exposes `readonly table: ITable` and `readonly key: IKey`.
- **Dependencies:** none. Synthesizes standalone.

### ApiStack (`lib/api-stack.ts`)
- **Responsibility:** HTTP API, its two routes, the two Lambda functions with one
  execution role each, the access-log group, and the Cognito identity pool plus its
  unauthenticated role.
- **Interface:** consumes `{ table, key }`; exposes `readonly apiUrl: string` and
  `readonly identityPoolId: string`.
- **Dependencies:** DataStack.

### WebStack (`lib/web-stack.ts`)
- **Responsibility:** the private SPA bucket, the access-log bucket, the CloudFront
  distribution with Origin Access Control, and the response-headers policy.
- **Interface:** consumes `{ apiUrl, identityPoolId }` for the SPA's build-time config;
  exposes `readonly distributionDomainName: string`, which ApiStack's CORS allowlist
  needs. That is a cycle if expressed as a CDK reference, so the CORS origin is a stack
  **parameter/context value**, not a cross-stack import — see Trade-offs.
- **Dependencies:** ApiStack (one-way).

### `src/lib/validate.ts`
- **Responsibility:** the single decision point for whether a target URL may be stored.
  Every rejection rule in `spec.md` → Edge Cases lives here and nowhere else.
- **Interface:** `validateTargetUrl(input: unknown): ValidationResult`. Total function —
  never throws, including on `null`, numbers, and objects.
- **Dependencies:** none (uses the WHATWG `URL` parser from the Node standard library).

### `src/handlers/create-link.ts`
- **Responsibility:** validate → `generateCode()` → conditional `PutItem` → 201. Owns the
  ≤3-attempt retry loop on `ConditionalCheckFailedException`, and only on that exception.
- **Interface:** APIGatewayProxyHandlerV2. Table name from `env.TABLE_NAME`.
- **Dependencies:** `lib/code.ts`, `lib/validate.ts`, `lib/response.ts`, DynamoDB client.

### `src/handlers/redirect.ts`
- **Responsibility:** shape-check the path parameter against `CODE_PATTERN`, `GetItem`,
  return 302 or 404. Never queries with an unvalidated code.
- **Interface:** APIGatewayProxyHandlerV2.
- **Dependencies:** `lib/code.ts`, `lib/response.ts`, DynamoDB client.

### `web/src/api.ts`
- **Responsibility:** obtain unauthenticated credentials from the identity pool, SigV4-sign
  the create request via `aws4fetch`, and map non-2xx bodies onto the error codes in the
  contract. The React components never see an unmapped HTTP status.
- **Interface:** `createLink(url: string): Promise<{code: string; shortUrl: string}>`,
  rejecting with a typed error carrying `INVALID_URL` / `INTERNAL`.
- **Dependencies:** `config.ts` (build-time `VITE_API_URL`, `VITE_IDENTITY_POOL_ID`).

## Data Model

Single table `links`, on-demand billing.

| Attribute | Type | Notes |
|---|---|---|
| `code` (PK) | S | 7 chars, base62, from `crypto.randomBytes` |
| `url` | S | ≤2048 chars, `http`/`https`, validated before write |
| `createdAt` | S | ISO-8601 UTC |

No sort key, no GSI, no TTL (expiry is out of scope). Retention: indefinite — a PoC that
is never deployed stores nothing real. Classification: `data-classification: internal`.
Submitted URLs are user-supplied and can be sensitive (a URL is not a secret but is often
a capability), which is why the table carries a CMK and PITR rather than default
encryption.

## Infrastructure Design

- **IaC:** AWS CDK v2, TypeScript. Three stacks in one app, wired in `bin/app.ts`.
- **Environment-agnostic by requirement (NF1):** no `env` on any stack, no
  `fromLookup`, no `Stack.of(...).account` in any conditional. This is what lets
  `cdk synth` run in CI with no credentials, and it is asserted by A2.
- **Deploy / rollback strategy:** none — nothing is deployed (NF1). The README documents
  what a deploy *would* require (bootstrap, an account, `cdk deploy --all`, then the SPA
  build with the API outputs injected) explicitly as a non-executed path, so no reader
  mistakes the template for something that has been run.
- **Outputs consumed elsewhere:** `apiUrl` and `identityPoolId` feed the SPA's build-time
  config; `distributionDomainName` feeds the API's CORS allowlist.
- **Removal policies:** `DESTROY` on buckets and table, `RETAIN` never used. Appropriate
  for a PoC and stated so no one copies it into production.

## Security Considerations (MANDATORY)

Reconciled against `rules/AWS-security-guidelines.md`. Every Critical control below is
asserted by a test in `test/infra/` (A4), not merely written here.

- **Authentication & Authorization.** `POST /links` uses an `AWS_IAM` authorizer;
  callers present SigV4 credentials from a Cognito identity pool's *unauthenticated*
  identity, whose role permits exactly `execute-api:Invoke` on that one route ARN.
  `GET /{code}` is unauthenticated — the documented exception the rules permit, justified
  by the fact that a short link is followed by an arbitrary credential-less browser.
  Compensating controls: stage throttling, a read-only execution role, and no reflection
  of caller input into the response body.
- **Least privilege.** One execution role per function, never shared. The create role
  holds `dynamodb:PutItem` on the table ARN only; the redirect role holds
  `dynamodb:GetItem` on the table ARN only. Both hold `kms:Decrypt` (plus
  `GenerateDataKey` for create) on the one key ARN. No wildcard actions and no wildcard
  resources anywhere — asserted in `test/infra/api-stack.test.ts`.
- **Encryption at rest.** Customer-managed KMS key with rotation enabled, used by the
  DynamoDB table (`SSESpecification` → KMS, not the default AWS-owned key) and by both S3
  buckets. Critical if absent.
- **Encryption in transit.** Both buckets carry a bucket policy denying every action when
  `aws:SecureTransport` is `false`. CloudFront sets `redirect-to-https` and a minimum
  protocol of TLS 1.2. Critical if absent.
- **Secrets management.** There are none. No credentials, tokens, or keys exist in this
  design — the identity pool issues short-lived credentials at runtime and CI holds no AWS
  secrets (NF1). Nothing is inlined because there is nothing to inline.
- **Network.** No VPC. Both Lambdas are public-subnet-free managed functions talking to
  DynamoDB over the AWS network; no private resources are reached, so VPC attachment would
  add cold-start cost and ENI management for no isolation benefit. Reserved concurrency is
  set on both functions to bound runaway invocation.
- **Public exposure.** Exactly two public surfaces: the CloudFront distribution and the
  HTTP API. The S3 buckets have Block Public Access fully on and are reachable only via
  the distribution's Origin Access Control.
- **Logging & audit.** API Gateway access logging to a CloudWatch log group with a
  retention period; CloudFront standard logging to the dedicated log bucket; S3 server
  access logging on the SPA bucket. Handlers emit structured JSON to stdout and never log
  the full target URL at info level.
- **Data classification & tagging.** `data-classification: internal` plus `service`,
  `environment`, and `owner` tags applied at the app level in `bin/app.ts` so every
  resource inherits them.
- **Threat model notes.** The dominant abuse case is not compromise of the stack but
  **abuse of the product**: an open redirector is a phishing amplifier and an SSRF
  laundering hop. Mitigation is the scheme allowlist, credential rejection, and
  private-address rejection in `validate.ts`, plus the `AWS_IAM` authorizer raising the
  cost of bulk minting. Residual, accepted for a non-deployed PoC: a public DNS name that
  resolves into private space at follow time is not caught by pre-storage validation.
  Secondary: header injection via CR/LF in a stored URL, eliminated because `new URL()`
  rejects those at validation time.

## Trade-offs & Alternatives

- **CloudFront does not front the API.** Preferred design was a single distribution with
  two origins — one domain, no CORS. It does not work: SigV4 binds the signature to the
  `Host` header, and API Gateway validates it against the `execute-api` hostname, which a
  CloudFront custom origin cannot preserve. Signed requests through the distribution fail
  authentication. Chosen instead: CloudFront serves the SPA, the SPA calls the API URL
  directly, and the API's CORS `allowOrigins` is pinned to the distribution domain. Cost:
  short links live on the `execute-api` hostname, which is ugly; the fix is a custom
  domain on the API, which is out of scope because nothing is deployed.
- **CORS origin is a context value, not a cross-stack reference.** WebStack depends on
  ApiStack for `apiUrl`; ApiStack needs the distribution domain for CORS. Expressed as CDK
  references that is a circular dependency and synth fails. Resolved by passing the origin
  as a context value (defaulting to a placeholder that synth accepts), which keeps the
  dependency one-way. Alternative rejected: merging Api and Web into one stack — it would
  work, and it would also collapse two of the three tiers the example exists to show.
- **Three stacks instead of one.** More ceremony and explicit cross-stack plumbing, chosen
  because the artifact's purpose is to make the tier boundaries legible.
- **`AWS_IAM` over a Cognito user pool.** A user pool with a JWT authorizer is the
  stronger posture and the more realistic one. Rejected because it adds sign-up, sign-in,
  and token refresh to a PoC scoped to "create and redirect" — an entire auth tier to
  protect one route. The identity pool gets an authorizer on the route with no user-facing
  surface. If this example ever grows per-user link ownership, this decision reverses.
- **No Powertools, no middleware.** Structured logging is four lines of `JSON.stringify`.
  A reader should not have to learn a logging framework to read a 40-line handler.
- **Handlers in TypeScript over Python.** Python would have reused this repo's existing
  pytest and `.venv` setup. Rejected: two languages in one example costs more in reader
  attention than it saves in toolchain reuse, and the pre-existing CI already assumed npm.

## Delivery Plan (sprints)

`bin/app.ts` is the one file every stack task would contend on, so it is claimed alone
first rather than merged three ways.

- **Sprint 1 — foundation (1 issue, `devops`).** Scaffold, toolchain, `cdk.json`, and
  `bin/app.ts` instantiating all three stacks against stub stack classes; restore the CI
  workflow. Barrier: everything downstream imports this.
- **Sprint 2 — fan-out (4 issues, file-disjoint, parallel).**
  `[coding]` DataStack + ApiStack + their infra tests · `[coding]` handlers and `src/lib`
  plus their unit tests · `[devops]` WebStack + its infra test · `[coding]` the SPA under
  `web/`.
- **Sprint 3 — close (2 issues).** `[devops]` README; `[review]` single verdict over the
  whole example.

## Open Design Questions

None outstanding. The two that would normally be open — how the SPA authenticates and how
CORS avoids a stack cycle — are resolved above under Trade-offs.
