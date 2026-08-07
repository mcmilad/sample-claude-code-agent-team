# SA Review: Shorty — Backend-Only Serverless 3-Tier URL Shortener

> Authored by `sa-1` (AGENT-76). This is a **design-stage** review: nothing is implemented
> yet, so every finding is against `spec.md` / `design.md` / `decisions.md`, not against code.
> Every service-behaviour, limit, and pricing claim below is cited to an AWS source retrieved
> during this review — none is asserted from memory. Where a figure could not be verified, it
> is marked as such rather than guessed.

## Overview

**Workload.** Two routes. `POST /links` (Cognito JWT authorizer) mints a 7-character code and
writes it to DynamoDB under a conditional put. `GET /{code}` (unauthenticated by necessity)
returns a 302 `Location`. Tier 1 API Gateway HTTP API v2, tier 2 two Python 3.13 Lambdas with
one execution role each, tier 3 a single DynamoDB table encrypted with a customer-managed KMS
key.

**SLA / traffic.** None stated; POC reference workload. No availability target, no RPO/RTO.
Reviewed accordingly — reliability findings are calibrated to "a reference someone may deploy
and leave running", not to a production service.

**Budget.** Spec NF6: zero during the build (no deploy — D-001), and once deployed
"dominated by the customer-managed KMS key (~$1/month)". See W-4.

**Compliance.** No framework named. The rubric is `.claude/rules/AWS-security-guidelines.md`.

**Deliberate architecture facts, not findings.** `GET /{code}` is unauthenticated (spec
Constraint 1 — a browser follows a short link with no credentials); DNS is not resolved during
URL validation (Constraint 3 / D-003). Both are correctly reasoned and are not reported below
as gaps.

### Verdict

The design is unusually strong on the controls it addresses, and its three self-declared
deviations are **all factually correct** — I verified each against AWS documentation rather
than accepting the stated rationale. However, it contains **one Critical error that would make
the application non-functional on first deploy**, and it is silent on several controls the
guidelines require. The Critical is invisible to every gate in the build: `cdk synth` passes,
the handler unit tests stub `boto3`, and D-001 guarantees nobody deploys before handoff.

**Counts as first issued: 1 Critical · 5 Warnings · 7 Suggestions.**

**Status at cycle-1 close: 0 open Critical, 0 open Warnings.** All 5 Warnings are closed —
W-1, W-3 implemented; W-4 corrected across all three documents; W-2 and W-5 accepted as D-007
and D-008. C-1 was corrected before implementation (D-006, verified against the synthesized
template). Of the 7 Suggestions, S-2 and S-6 are done and the remaining 5 are deferred with
stated triggers. Full accounting — all 13 findings — in **Dispositions** below.

---

## Part 1 — Audit of the design's self-declared deviations

The task was to confirm each justification is *actually true* and that the named compensating
controls are sufficient — not to restate them approvingly.

### D-002 — No AWS WAF · **JUSTIFICATION CONFIRMED TRUE**

The [AWS WAF developer guide](https://docs.aws.amazon.com/waf/latest/developerguide/waf-chapter.html)
enumerates protectable resources: CloudFront distribution, **API Gateway REST API**,
Application Load Balancer, AppSync GraphQL API, Cognito user pool, App Runner service, Bedrock
AgentCore Gateway, Verified Access instance, Amplify. HTTP API (v2) is absent. Two re:Post
articles state it flatly: *"AWS WAF supports only API Gateway REST APIs."* The design's claim
is correct and the two named remedies (switch to REST, or front with CloudFront) are the only
two.

**Compensating controls — sufficient for this workload.** Stage throttling, a `GetItem`-only
redirect role, the pre-DynamoDB shape check, and no reflection of caller input. The shape check
is the strongest of these and is genuinely well-placed: it converts a scanner's malformed
probes from billed reads into free 404s.

**Cost of the choice is understated** — see S-6.

### D-003 — Validation does not resolve DNS · **REASONING CONFIRMED SOUND**

The core argument holds: Shorty stores a URL and returns it as a `Location` header, never
fetching it, so the server-side-request primitive that defines SSRF is genuinely absent. The
TOCTOU point is also correct — DNS can be repointed after validation, so resolution buys the
appearance of a guarantee.

**But the decision covers only half the abuse surface** — see W-5.

### D-004 — Cognito MFA `OPTIONAL` · **JUSTIFICATION CONFIRMED TRUE**

Verified against the
[AdminInitiateAuth examples](https://docs.aws.amazon.com/cognito/latest/developerguide/cognito-identity-provider_example_cognito-identity-provider_AdminInitiateAuth_section.html):
an `ADMIN_USER_PASSWORD_AUTH` call for an MFA-configured user returns
`"ChallengeName": "SOFTWARE_TOKEN_MFA"` and a `Session` instead of tokens. The
[SDK reference](https://docs.aws.amazon.com/sdk-for-ruby/v2/api/Aws/CognitoIdentityProvider/Types/AdminInitiateAuthResponse.html)
adds the worse case: *"`MFA_SETUP`: If MFA is required, users who do not have at least one of
the MFA methods set up are presented with an `MFA_SETUP` challenge."* So with MFA required, a
freshly `admin-create-user`'d account cannot obtain a token at all without an enrolment round
trip. The runbook would be unusable exactly as D-004 claims.

**Compensating controls confirmed real and adequate**: `self_sign_up_enabled=False`,
administrative user creation only, 12-character minimum across four classes, email-only
recovery. With no self-service signup, the population of accounts is exactly what the owner
created by hand — the residual risk is small and correctly scoped.

### D-005 — No KMS on Lambda environment variables · **CONFIRMED CORRECT**

`TABLE_NAME` is not sensitive and is already visible in the synthesized template. Lambda
encrypts environment variables at rest with an AWS-managed key regardless; a CMK would only
add control over console plaintext display. Recording the absence as a decision was the right
call, and the stated fallback (Secrets Manager at runtime, not an encrypted env var) is the
correct guidance. **No action.**

---

## Part 2 — Control-by-control assessment against AWS-security-guidelines.md

### Amazon DynamoDB

| Control | Status | Note |
|---|---|---|
| Encryption at rest with KMS key (not AWS-owned) | **Met** | `encryption=CUSTOMER_MANAGED`, rotation enabled |
| Point-in-time recovery | **Met** | `pointInTimeRecovery=True` |
| Fine-grained IAM conditions (`dynamodb:LeadingKeys`, `Attributes`) | **Not-applicable** | Keys are cryptographically random codes, not tenant-scoped identifiers; there is no principal-to-partition relationship for `LeadingKeys` to express. Recording this explicitly so a reviewer does not read the absence as an oversight |
| Never grant `dynamodb:*` on production tables | **Met** | One action per role, single table ARN (NF4) |
| Data classification tags | **Met** | `data-classification: internal` |
| Encryption in transit (HTTPS only) | **Met** | AWS SDK default; AWS docs confirm all DynamoDB traffic is TLS |
| KMS grants sufficient for CMK table access | **DEVIATION — Critical** | **C-1** |
| Data-operation audit trail | **DEVIATION — Warning** | **W-2** |
| Deletion protection | **Suggestion** | **S-2** |

### AWS Lambda

| Control | Status | Note |
|---|---|---|
| Least-privilege execution role, one per function | **Met** (DynamoDB actions) / **see C-1** (KMS actions) | The DynamoDB half is exemplary; the KMS half is broken |
| Environment variable encryption | **Deviation — accepted** | D-005, confirmed correct |
| VPC configuration | **Not-applicable** | Neither function reaches a private resource; the design's cost reasoning (ENI cold start + NAT bill for zero gain) is correct |
| Resource-based policy — no wildcard principals | **Suggestion** | **S-7** — not asserted in the posture test |
| Reserved concurrency | **Met** | Set on both; the stated rationale (cross-route starvation) is sound |
| Log retention / log group management | **DEVIATION — Warning** | **W-1** |

### Amazon API Gateway

| Control | Status | Note |
|---|---|---|
| Authorization on all routes | **Met** on `POST /links` (JWT authorizer bound to pool issuer); **documented exception** on `GET /{code}` per Constraint 1 | The negative assertion — authorizer *absent* on GET — is a genuinely good design choice: it stops a later change from silently breaking F2 |
| Throttling | **Met** (stage-level); **Suggestion** for route-level | **S-3** |
| WAF integration | **Deviation — accepted** | D-002, confirmed true |
| Mutual TLS | **Not-applicable** | Clients are arbitrary browsers following a link; they cannot present client certificates. mTLS is meaningful only for the API-to-API case, which Shorty does not have |
| Access logging | **Met** | Dedicated log group, explicit JSON format. Retention: **S-5** |
| Resource policies | **Not-applicable** | [REST vs HTTP API comparison](https://docs.aws.amazon.com/apigateway/latest/developerguide/http-api-vs-rest.html) — resource policies: REST **Yes**, HTTP **No**. Structurally unavailable, not declined |
| Request validation | **Not-applicable** | Request validators are a REST API feature. Compensated in-handler by `validate_url` with a table-driven rejection corpus, which is stronger than a schema validator for this input |

### General Requirements

| Control | Status | Note |
|---|---|---|
| Secrets via Secrets Manager / Parameter Store | **Met** (vacuously) | There are no secrets. The design's phrasing — "not used, rather than used badly" — is the right disposition |
| Tag: service, environment, owner, data-classification | **Met** | Applied at `App` level so all resources inherit |
| Tag: **cost-center** | **DEVIATION — Warning** | **W-3** |
| Key management strategy documented | **Met** | `kms-key-usage.md`, this issue |
| BYOK documented and flagged for security review | **Met** | `kms-key-usage.md` §7 |
| Access logging for data operations (CloudTrail) | **DEVIATION — Warning** | **W-2** |

**Not applicable to this workload:** Amazon S3 (no bucket — the guidelines' entire Phase 1–3
S3 ladder, TLS-deny policy, Block Public Access, versioning, MFA Delete), Amazon RDS, Amazon
EBS, Aurora DSQL, AWS Amplify Gen 2. Listed explicitly so their absence is legible as scope,
not omission.

---

## Findings by Pillar

### Security

**C-1 · [Critical — RESOLVED] The Lambda execution roles' KMS grants are below the documented
minimum; `create_fn` cannot write to the table at all.**

> **RESOLVED via `decisions.md` → D-006**, before any app-stack code was written. Both roles
> now carry `kms:Encrypt`, `kms:Decrypt`, `kms:ReEncrypt*`, `kms:GenerateDataKey*`,
> `kms:DescribeKey` on the single key ARN under
> `{"StringLike": {"kms:ViaService": "dynamodb.*.amazonaws.com"}}`; neither carries
> `kms:CreateGrant`. The DynamoDB action split (`PutItem` / `GetItem`) is untouched.
> **Verified against the synthesized template** — `cdk synth` run and both
> `*RoleDefaultPolicy` resources read back from `cdk.out/*.template.json` — not against the
> design prose. `tests/infra/test_security_posture.py` asserts the corrected set.
>
> One correction to the fix I originally proposed: my suggested policy pinned the Region in
> `kms:ViaService`. The shipped form wildcards it, which is what AWS's own customer-managed-key
> example for DynamoDB uses and what keeps the grant valid for DynamoDB-initiated calls that
> do not originate from the key's Region. See `kms-key-usage.md` §3.1 for why narrowing it
> breaks the deployment. The finding below is retained as the original analysis.

`design.md` → Security Considerations → Compute specifies: *"Each role also gets
`kms:GenerateDataKey` (create) / `kms:Decrypt` (redirect) on the one key ARN — the minimum for
CMK-encrypted table access."* That is not the minimum, and the split is backwards for the
create path.

AWS documents the required set in
[DynamoDB encryption at rest usage notes](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/encryption.usagenotes.html)
→ "Key policy for a customer managed key": *"That principal, a user or role, must have the
permissions on the KMS key that DynamoDB requires. At a minimum, DynamoDB requires…"* —
`kms:Encrypt`, `kms:Decrypt`, `kms:ReEncrypt*`, `kms:GenerateDataKey*`, `kms:DescribeKey`,
`kms:CreateGrant`. The same page's worked key-policy example lists exactly those six.
[re:Post on DynamoDB `AccessDeniedException`](https://repost.aws/knowledge-center/dynamodb-access-denied-exception)
repeats it as the fix for this precise symptom.

The mechanism is the table-key cache, documented on the same page: *"To avoid calling AWS KMS
for every DynamoDB operation, DynamoDB caches the plaintext table keys **for each caller** in
memory. If DynamoDB gets a request for the cached table key after five minutes of inactivity,
it sends a new request to AWS KMS to **decrypt** the table key."* Every caller — reader **and
writer** — needs `kms:Decrypt`. `kms:GenerateDataKey` is consumed when the *table key is first
created*, by DynamoDB under a grant, not on each `PutItem`.

So `create_fn`, holding `kms:GenerateDataKey` and nothing else, fails on its first `PutItem`
after each cold start or five-minute idle gap. `redirect_fn` has the right primary action but
lacks `kms:DescribeKey`, which DynamoDB uses to confirm the key exists.

**Why nothing in the build would catch this.** `cdk synth` validates structure, not runtime
authorization. The handler tests replace the `boto3` client with a stub (`design.md` →
Interface Contracts), so no real KMS or DynamoDB call is ever made. The infra tests assert the
policy statements *the design specifies* — they would assert the wrong thing and pass. And
D-001 means the first real invocation happens on the owner's machine, after handoff, with the
team gone. This is the failure mode the issue exists to catch.

**Severity rationale.** Availability, not confidentiality: 100% failure of `POST /links`, and
`GET /{code}` failing on `DescribeKey`. No data is exposed. Critical because the deliverable is
a *deploy-ready reference* and it does not work.

**Recommendation.** Grant both roles the DynamoDB-required KMS action set on the single key
ARN, constrained by `kms:ViaService: dynamodb.<region>.amazonaws.com` so neither role can use
the key for anything but reaching `links`. Keep the DynamoDB action split (`PutItem` /
`GetItem`) exactly as-is — that is where least privilege actually pays here. Exact policy in
`kms-key-usage.md` §3.1. `kms:CreateGrant` belongs to the deploying principal, **not** the
Lambda roles. Alternatively use CDK's `table.grant_write_data()` / `grant_read_data()`, which
wire the KMS grants for a customer-managed key automatically. Then extend
`tests/infra/test_security_posture.py` to assert the KMS action set **and** the `ViaService`
condition — otherwise the test encodes the bug.

**W-1 · [Warning] Lambda log groups are unmanaged: never-expire retention, no CDK control, and
orphaned by `cdk destroy`.**

`design.md` creates a `logs.LogGroup` for API access logs with one-month retention, but says
nothing about the functions' own logs. The
[CDK Lambda construct docs](https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_lambda/README.html)
are explicit: *"By default, Lambda functions automatically create a log group with the name
`/aws/lambda/<function-name>` upon first execution with log data set to **never expire**. This
is convenient, but **prevents you from changing any of the properties of this auto-created log
group using the AWS CDK**. For example you cannot set log retention or assign a data protection
policy."*

Three consequences: unbounded retention and therefore unbounded storage cost on a stack whose
whole cost story is "effectively free"; no data-protection policy possible on logs that, per
`design.md` → Error Handling, carry exception detail and request ids; and because the groups
are service-created rather than stack-managed, `cdk destroy` leaves them behind — a second
silent residue alongside the `RETAIN`ed table and key, which the runbook currently does not
mention.

**Recommendation.** Create two `logs.LogGroup`s in `ShortyAppStack` and pass them via the
`log_group=` property (available in commercial Regions since 2023-11-16 per the same doc). Set
`retention=logs.RetentionDays.ONE_MONTH` to match the access log group and
`removal_policy=RemovalPolicy.DESTROY` so teardown is complete. Assert retention is set in
`test_security_posture.py`.

**W-2 · [Warning] No audit trail of data-plane access to the links table.**

`AWS-security-guidelines.md` → General Requirements: *"Enable access logging for data
operations: CloudTrail, S3 access logs, database audit logs."* DynamoDB item-level operations
appear in CloudTrail **only as data events**, which
[the DynamoDB CloudTrail docs](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/logging-using-cloudtrail.html)
confirm require explicit opt-in: *"To enable logging of the following API actions in CloudTrail
files, you'll need to enable logging of data plane API activity"* — `GetItem` and `PutItem` are
both on that list — and
[Choose log events](https://docs.aws.amazon.com/help-panel/awscloudtrail/latest/console/create-trail-events.html):
*"By default, trails do not log data events."*

As designed there is no record of who read or wrote which link. API access logs cover the edge
but not the data tier, and they cannot attribute a read on `GET /{code}` to anything but an IP.

**Recommendation.** Either (a) add a trail with an advanced event selector scoped to
`AWS::DynamoDB::Table` for this table only — per-table selection is supported and keeps cost
proportional — or (b) if the POC deliberately accepts no data-tier audit trail, record that as
a D-006 in `decisions.md` with the same rigour as D-002/3/4/5. Option (b) is defensible; the
current silence is not, because it is indistinguishable from an oversight.

**W-3 · [Warning] The `cost-center` tag is missing from the tagging scheme.**

`AWS-security-guidelines.md` → General Requirements: *"Tag all resources: service, environment,
owner, cost-center, data-classification."* `design.md` → Security Considerations → Data applies
`service`, `environment`, `owner` at the `App` level plus `data-classification` on the table —
four of the five. `cost-center` is absent.

Minor in isolation, but it is the tag that makes W-4's recurring KMS charge attributable in
Cost Explorer once the owner has left the stack running, which is precisely the scenario the
`RETAIN` policy creates.

**Recommendation.** Add `cost-center` to the `App`-level `Tags.of(app).add(...)` calls so every
resource inherits it, and extend the posture test's tag assertion to cover all five keys rather
than the four currently planned.

**W-5 · [Warning] Open-redirect abuse for phishing is unaddressed, and D-003 does not cover it.**

D-003 reasons carefully about one abuse of the missing DNS resolution — redirecting toward an
internal address — and correctly concludes the risk is bounded because the victim must already
be inside the network. It says nothing about the far more common abuse: a URL shortener is a
reputation-laundering tool. An authenticated user can mint an unlimited number of links
pointing at attacker-controlled sites, and recipients see only the Shorty domain.

Nothing in the design constrains this. Stage-level throttling is a global rate limit, not a
per-identity quota, so a single account can mint at the stage ceiling indefinitely. The design
stores `createdBy` — the attribution exists — but nothing consumes it. There is no blocklist,
no abuse contact, and no interstitial.

This sits outside all five existing decisions, which is why it is a new finding rather than a
restatement.

**Recommendation.** For a POC, the proportionate response is to (a) record it as a decision
with an explicit accepted-risk statement, noting that Shorty is not on a custom domain and the
`execute-api` hostname carries no reputation to launder — which materially reduces the
incentive; and (b) note the concrete control to add before any real exposure: a per-`createdBy`
write quota. Since `createdBy` is already on every item, that is a counter, not a schema
change. Pair it with the CloudFront path from D-002's "revisit if", which is where a real
domain would arrive anyway.

**S-7 · [Suggestion] Assert the Lambda resource-based policy is scoped to this API.**

The guidelines require *"no wildcard principals"* on Lambda resource policies. CDK's HTTP API
Lambda integration emits an `AWS::Lambda::Permission` for `apigateway.amazonaws.com` with a
`SourceArn`, which is correct by default — but `design.md`'s posture checklist does not include
it, and the whole point of NF3 is that posture is asserted rather than assumed. A future
refactor could widen it silently.

**Recommendation.** Add a `test_security_posture.py` assertion that each function's
`AWS::Lambda::Permission` carries a `SourceArn` referencing this API, and no wildcard principal.

**S-4 · [Suggestion] The KMS key policy is unspecified, and the key has no alias.**

`design.md` claims the CMK buys "explicit key policy, rotation, and an auditable grant list",
but no key policy is stated anywhere. CDK's default gives the account root full access and
defers to IAM — workable, but it is not the "explicit key policy" the trade-off claims.

**Recommendation.** State the intended key policy: account-root administration plus a
`kms:ViaService`-constrained use statement (template in `kms-key-usage.md` §3.1). Add
`alias="alias/shorty-links"` — the runbook currently asks the owner to handle a raw key ID.

**S-6 · [Suggestion] D-002 understates what choosing an HTTP API costs.**

D-002 reads as though WAF is the sole forfeit. Per the
[REST vs HTTP comparison](https://docs.aws.amazon.com/apigateway/latest/developerguide/http-api-vs-rest.html),
HTTP APIs also lack **resource policies** (REST: Yes / HTTP: No) and request validators.
Neither matters for Shorty — there is no source-IP or VPC-endpoint restriction to express, and
`validate_url` is a better fit than a schema validator — but the decision record should be
complete so a future reader re-evaluating the tier choice sees the whole ledger.

**Recommendation.** Append one line to D-002 listing resource policies and request validators
as also forfeited, each with its one-line reason for being immaterial here.

### Reliability

**S-1 · [Suggestion] The design specifies no alarms or monitoring of any kind.**

No CloudWatch alarm appears anywhere in `design.md`. For a reference someone deploys and leaves
running, at minimum: API Gateway `5xx`, Lambda `Errors` and `Throttles` (the latter especially,
since reserved concurrency is set and will produce 429s under load), DynamoDB `SystemErrors` /
`UserErrors`, and the 503 `CODE_COLLISION` path — five collisions in a row is a strong signal
something is wrong with `generate_code`, and it currently surfaces only as a client-visible 503.

Highest-value alarm: **KMS key disabled / table `Inaccessible`.** Per the DynamoDB docs, if the
key is inaccessible for more than seven days the table is *archived and can no longer be
accessed*. Seven days is the entire remediation window, and nothing currently notifies anyone.

**Recommendation.** Add the five alarms above to `ShortyAppStack` (and the key-availability one
to `ShortyDataStack`) with an SNS topic the runbook tells the owner to subscribe to.

**S-2 · [Suggestion] `RemovalPolicy.RETAIN` does not prevent an API-level `DeleteTable`.**

`RETAIN` governs CloudFormation only. A direct `DeleteTable` call still succeeds, and with the
CMK still live the data is unrecoverable beyond PITR's window.

**Recommendation.** Set `deletion_protection=True` on the `TableV2`. It composes with `RETAIN`
rather than duplicating it, and costs nothing.

**S-3 · [Suggestion] Add route-level throttling, not just stage-level.**

`design.md` specifies stage-level throttling as *"the primary compensating control for the
unauthenticated redirect route"* — but a stage-level limit is shared, so a flood on the
unauthenticated `GET` consumes the same budget as authenticated `POST`s. HTTP APIs support
per-route limits:
[Throttling](https://docs.aws.amazon.com/help-panel/apigateway/latest/console/http-throttling.html)
— *"To configure a unique throttling limit for a route, use the AWS CLI or an SDK"*, via
`update-stage --route-settings '{"GET /pets":{"ThrottlingBurstLimit":100,"ThrottlingRateLimit":2000}}'`.

Note the CLI/SDK caveat: this is set through stage route settings, which in CDK means the
`CfnStage` `routeSettings` property rather than an L2 convenience prop.

**Recommendation.** Give `GET /{code}` and `POST /links` independent rate/burst limits so
neither route can starve the other. This strengthens D-002's compensating-controls argument at
essentially zero cost.

**S-5 · [Suggestion] One-month API access log retention is short for the only audit trail on
the unauthenticated route.**

With W-2 open, the access log is currently the *sole* record of redirect traffic. One month is
below a typical incident-investigation window.

**Recommendation.** Three months, or state one month as a deliberate cost choice. Resolve
alongside W-2 — if data events are enabled, one month here is more defensible.

### Cost Optimization

**W-4 · [Warning] The KMS cost figure is understated threefold at steady state.**

Spec NF6 and `design.md` trade-off #2 both say the key costs "~$1/month". Per
[AWS KMS pricing](https://aws.amazon.com/kms/pricing/): *"Each AWS KMS key that you create in
AWS KMS costs $1/month (prorated hourly). […] For KMS keys that you rotate automatically or on
demand, the first and second rotation of the key adds $1/month (prorated hourly) in cost. This
price increase is capped at the second rotation."*

The design enables rotation. So: **$1/month in year 1, $2/month after the first rotation,
$3/month after the second — capped there.** "~$1/month" is the year-one figure only.

This matters more than a small absolute number suggests, for three reasons. The key is the
*only* meaningful recurring cost in the workload — everything else is on-demand and near-free
at POC traffic — so the error is on 100% of the steady-state bill, not a rounding line. The key
is `RETAIN`, so the charge continues indefinitely after `cdk destroy`. And the runbook's
"lingering KMS charge" warning (trade-off #3) would understate what the owner is left paying by
3x, in the exact document meant to prevent that surprise.

**Recommendation.** Correct NF6, trade-off #2, and the runbook to state $1 → $3/month over the
first two rotation cycles, capped at $3. `kms-key-usage.md` §5 carries the full table and the
citation. Keep the CMK — the decision is right, only the number is wrong.

**Cost check on the tier-1 choice (no finding).** The HTTP API decision is well-founded on cost
as well as terseness: HTTP APIs are **$1.00 per million** requests for the first 300M in
us-east-1 versus **$3.50 per million** for REST
([API Gateway pricing](https://aws.amazon.com/api-gateway/pricing/) and the
[private-APIs whitepaper](https://docs.aws.amazon.com/whitepapers/latest/best-practices-api-gateway-private-apis-integration/cost-optimization.html)),
a 3.5x difference, plus a 1M-call/month free tier for 12 months on each. Switching to REST
purely to gain WAF would more than triple per-request cost for every route to protect one.
D-002's cost reasoning is sound.

### Operational Excellence

Covered by S-1 (no alarms) and W-1 (log management). One further note, not a finding: the
decision to assert posture as `Template` tests (NF3) rather than prose is the strongest
operational choice in this design, and C-1 is precisely the case for it — provided the tests
assert the *corrected* grant set, since a posture test that encodes the design's error passes
while the system fails.

### Performance Efficiency

No findings. Both routes are single-digit-millisecond point operations on a partition key.
On-demand billing removes capacity planning. Module-level `boto3` client reuse across warm
invocations is the right pattern. The pre-DynamoDB shape check on the redirect path is both a
security and a latency win.

Note (not a finding): reserved concurrency is a ceiling as well as a floor — once set, the
function cannot burst past it and excess requests get 429s. That is the intended trade (design
says so), but the chosen values should be recorded with a one-line rationale so a future reader
knows whether they were reasoned or arbitrary.

### Sustainability

No findings. Serverless on-demand across all three tiers means no idle capacity. Arm64/Graviton
for both functions would reduce energy per invocation and cost, but at POC volume the effect is
immeasurable and it is not worth a finding.

---

## Priority Actions

Highest impact, lowest effort first.

| # | Action | Finding | Impact | Effort |
|---|---|---|---|---|
| 1 | ~~Fix the KMS grants on both execution roles; add `ViaService`; assert both in the posture test~~ — **DONE** (D-006, template-verified) | **C-1** | Application does not work without it | Low — one policy statement per role |
| 2 | ~~Correct the KMS cost figure to $1 → $3/month in NF6, trade-off #2, and the runbook~~ — **DONE** (verified in all three) | **W-4** | Owner is left paying 3x the documented amount, indefinitely | Trivial |
| 3 | ~~Add explicit `logs.LogGroup`s for both functions with retention + `DESTROY`~~ — **DONE** | **W-1** | Unbounded cost, no data-protection policy, incomplete teardown | Low |
| 4 | ~~Add the `cost-center` tag at `App` level~~ — **DONE** (`app.py`, asserted in posture suite) | **W-3** | Guidelines-required; one line | Trivial |
| 5 | ~~Decide DynamoDB CloudTrail data events — enable, or record a decision accepting the gap~~ — **DONE** (accepted, **D-007**) | **W-2** | No data-tier audit trail | Low either way |
| 6 | ~~Record open-redirect abuse as a decision~~ — **DONE** (accepted, **D-008**) | **W-5** | Closes the one uncovered abuse surface | Low |
| 7 | S-2 and S-6 **done**; S-1, S-3, S-4, S-5, S-7 deferred with triggers — see **Dispositions** | S-1…S-7 | Hardening and completeness | Low each |

> **This table is the original cycle-1 plan, struck as items landed.** The live status of
> every finding is the **Dispositions** section above — if the two ever disagree, Dispositions
> wins. Row 2 in particular must not be re-actioned: the $3/month figure is the correct one,
> and "correcting" it back toward ~$1/month would reintroduce the error W-4 exists to refute.

**Sequencing note.** Items 1 and 3 change `ShortyDataStack` / `ShortyAppStack` and their
posture tests, so they should land before the group's review cycle rather than after. Items 2,
5, and 6 are edits to `spec.md` / `design.md` / `decisions.md`, which are outside this issue's
`Files:` — they are proposed to the lead, not made here.

## Dispositions

Recorded at cycle-1 close (AGENT-91). Verified against the shipped tree, not assumed. The
governing context is D-001: **this PoC is authored deploy-ready but never deployed by the
team**, so "deferred with a stated trigger" is the honest disposition for controls whose value
only exists once something is running. Nothing is silently dropped.

**Resolved / implemented — all 13 findings are accounted for here or under "Deferred" below.**

| Finding | Disposition |
|---|---|
| **C-1** (KMS grant split) | **Resolved** — D-006; verified against the synthesized template |
| **W-1** (Lambda log groups) | **Accepted and implemented** — explicit `logs.LogGroup` per function, `ONE_MONTH` retention, passed via `log_group=` |
| **W-2** (CloudTrail data events) | **Accepted as a documented deviation** — D-007 |
| **W-3** (`cost-center` tag) | **Accepted and implemented** — `cdk.Tags.of(app).add("cost-center", "shorty-poc")` in `app.py`, and asserted in `tests/infra/test_security_posture.py`, so the full five-tag set is now a build-breaking guarantee rather than a convention |
| **W-4** (KMS cost understated 3x) | **Accepted and corrected** — `$3/month steady state` now stated in `spec.md` NF6, `design.md` trade-off #2, and `apps/shorty/README.md`. **This figure is final; do not "correct" it back toward ~$1/month** — that is the error the finding exists to refute. `kms-key-usage.md` §5 carries the derivation and citation |
| **W-5** (open-redirect / phishing abuse) | **Accepted as a documented deviation** — **D-008**. The mitigation recorded is stronger than the per-`createdBy` quota I proposed: minting requires a Cognito JWT and `self_sign_up_enabled=False`, so an anonymous attacker cannot mint at all and every link is attributable to an administratively-created identity. Revisit trigger is signup being opened up |
| **S-2** (DynamoDB deletion protection) | **Accepted and implemented** — `deletion_protection=True` in `data_stack.py` |
| **S-6** (D-002 completeness) | **Accepted and applied by the lead** — D-002 now carries the full REST-vs-HTTP ledger, including resource policies and request validators as explicit **No** rows |

**Deferred, with triggers:**

- **S-1 · Alarms — deferred; the one I would un-defer first.** No CloudWatch alarm exists
  anywhere in the tree (confirmed: no `Alarm` in `shorty_infra/` or `tests/`). Alarms measure a
  running system, and under D-001 nothing runs, so they buy nothing during the build. **What
  makes them necessary:** the owner's first `cdk deploy` — not "real traffic". The
  key-disabled / table-`Inaccessible` alarm specifically should not wait, because that failure
  has a hard **seven-day** window: if KMS access is not restored within it the table is
  archived and can no longer be accessed, and DynamoDB's only notification is an email nobody
  may be watching. An alarm on it is the difference between a recoverable mistake and permanent
  data loss. The rest (API Gateway `5xx`, Lambda `Errors` and `Throttles` — the latter matters
  because reserved concurrency is set and will produce 429s — DynamoDB `SystemErrors`, and the
  503 `CODE_COLLISION` path) become necessary the moment the service serves anyone but the
  owner.
- **S-3 · Route-level throttling — deferred.** Confirmed still stage-only: a single
  `ThrottleSettings(rate_limit=50, burst_limit=100)`. At those numbers a shared bucket is not a
  realistic starvation risk for two routes with one user. **Trigger:** any public exposure of
  `GET /{code}`, at which point the unauthenticated route needs its own ceiling so a flood on
  it cannot consume the mint route's budget — this is also what would strengthen D-002's
  compensating-controls argument.
- **S-4 · Key policy and alias — deferred.** Confirmed neither shipped. CDK's default key
  policy (root administers, IAM governs use) is safe, and the identity-side grant in
  `kms-key-usage.md` §3.1 is what actually constrains access, so the explicit policy is
  belt-and-braces here. **Trigger for the policy:** a second principal or any cross-account
  consumer. **Trigger for the alias:** first deploy — the runbook currently hands the owner a
  raw key ID to retype, and `alias/shorty-links` is one line.
- **S-5 · Access-log retention — deferred.** Confirmed still `ONE_MONTH`. D-007 accepted no
  DynamoDB data events, which makes the access log the *sole* record of redirect traffic and
  argues for longer — but retaining logs for a system that never serves a request is spend
  without a reader. **Trigger:** first deploy that serves non-owner traffic; three months is
  the number, and it should be revisited together with D-007 rather than separately.
- **S-7 · Lambda resource-policy assertion — deferred.** Confirmed absent: no
  `AWS::Lambda::Permission` or `SourceArn` assertion anywhere in `tests/`. CDK's HTTP API
  integration emits a correctly scoped permission by default, so this is a *regression* guard,
  not a live gap — and D-011 is the precedent for why that distinction is thin (a guard that
  checks one shape is dead for its neighbours). **Trigger:** bundle it with the next change to
  the posture suite; it is a few lines and the suite is already the right home for it.

**Rejected:** none. Every suggestion above remains valid; none was found wrong on re-check.

## Cost Impact

Monthly, us-east-1, POC traffic (well under 1M requests/month). "Current" = as the design
currently documents it; "Corrected" = what the owner will actually be billed.

| Item | Current (documented) | Corrected | Delta | Source |
|---|---|---|---|---|
| KMS customer-managed key — year 1 | $1.00 | $1.00 | — | [KMS pricing](https://aws.amazon.com/kms/pricing/) |
| KMS customer-managed key — after 1st rotation | $1.00 | $2.00 | +$1.00 | ibid. — first rotation adds $1/mo |
| KMS customer-managed key — after 2nd rotation (steady state) | $1.00 | $3.00 | **+$2.00** | ibid. — capped at the second rotation |
| KMS requests | not stated | negligible | — | 5-min per-caller table-key cache ([DynamoDB usage notes](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/encryption.usagenotes.html)); per-request rate not verified in this review |
| API Gateway HTTP API | ~$0 | ~$0 | — | $1.00/million after a 1M/month 12-month free tier ([pricing](https://aws.amazon.com/api-gateway/pricing/)) |
| Lambda (2 functions) | ~$0 | ~$0 | — | Free tier at POC volume |
| DynamoDB on-demand | ~$0 | ~$0 | — | Point ops only; per-RRU/WRU rate not verified in this review |
| CloudWatch Logs — API access, 1-month retention | ~$0 | ~$0 | — | Low volume |
| CloudWatch Logs — Lambda, **never-expire** | not accounted | grows without bound | unbounded | **W-1** — [CDK Lambda docs](https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_lambda/README.html) |
| CloudTrail data events (if W-2 → enable) | $0 | small, per-event | + | Per-table selector keeps it proportional |

**Steady-state total after teardown** (`cdk destroy` run, `RETAIN`ed resources left): **$3/month
for the key**, plus whatever the orphaned never-expire Lambda log groups have accumulated —
not ~$1/month as currently documented.

---

## Verification Notes

- Every AWS behaviour, limit, and price above is cited to a document retrieved during this
  review. Nothing is asserted from memory.
- **Not verified, and marked as such in place:** the KMS per-request rate and DynamoDB
  on-demand per-RRU/WRU rates. The pricing MCP (`deploy-on-aws:awspricing`) is not available in
  this session and the AWS Pricing API needs credentials, which NF1 forbids. Both are
  immaterial at POC volume; the owner should confirm current rates for their Region before
  budgeting.
- This review assesses the **design**. C-1 in particular is a static reading of AWS's
  documented minimum permission set and should be confirmed by the owner's first live deploy —
  which, per D-001, is the first time any of this executes against real AWS. That is exactly
  why it was worth catching now.
- Consistent with D-001, nothing here implies a `deploy → smoke → teardown` was performed. It
  was not.
