# Decisions — Shorty

Mid-flight decision log. Entries added by any teammate that makes a call not already
settled in `spec.md` or `design.md`. Append; never rewrite history.

---

## D-001 — Deploy-ready, but the team does not deploy

**Date:** 2026-08-07 · **By:** lead (with the user) · **Status:** Accepted

The team authors everything needed for a real deployment — env-agnostic CDK app, runbook,
smoke script — and verifies entirely offline. The owner runs `cdk deploy` when they choose.

**Why:** Keeps team verification free, credential-free, and reproducible, while still
producing an artifact that genuinely runs. A synth-only build would have been cheaper but
would never have forced the deploy path to be thought through; a team deploy would have
required credentials, spend, and a teardown step for no additional demonstration.

**Consequence:** The live-validation gate in `spec-workflow` is **not** satisfied by this
work. No reviewer may report a PASS that implies a deploy → smoke → teardown was executed.
The correct disposition is *authored-and-statically-validated only, live gate outstanding
and deliberately deferred to the owner*.

---

## D-002 — No AWS WAF, because HTTP API cannot take one

**Date:** 2026-08-07 · **By:** lead · **Status:** Accepted (deviation)

`AWS-security-guidelines.md` → API Gateway rates a missing WAF web ACL a **Warning** for
public-facing APIs. `GET /{code}` is public by necessity (spec Constraint 1), so the
warning applies.

AWS WAF attaches to REST APIs (v1), CloudFront, and ALB — **not** to HTTP APIs (v2). The
only ways to comply would be to switch tier 1 to a REST API, or to front the HTTP API with
CloudFront purely to host the web ACL.

**Choice:** keep the HTTP API and accept the warning.

**Compensating controls:** stage-level rate and burst throttling; a redirect execution role
holding `dynamodb:GetItem` and nothing else; a code shape-check before any DynamoDB call so
malformed probes cost nothing; no reflection of caller input into any response body.

**Revisit if:** the service is ever exposed to real traffic, at which point CloudFront in
front of the API is the cheaper of the two compliance paths and also buys caching on the
redirect route.

**The full ledger, added after review (finding S-6).** WAF is not the only thing HTTP API
gives up versus REST API. Choosing v2 also forfeits:

| Capability | REST (v1) | HTTP (v2) |
|---|---|---|
| AWS WAF web ACL | Yes | **No** |
| Resource policies (source-IP / VPC-endpoint restriction) | Yes | **No** |
| Request validators (body / query / path) | Yes | **No** |

Neither of the additional two matters for Shorty — there is no private-API or source-IP
requirement, and both handlers validate their own input explicitly rather than delegating to
a gateway validator. Recorded because D-002 as originally written implied WAF was the sole
cost, and anyone re-evaluating the tier-1 choice later needs the whole ledger rather than the
one item that happened to trip a guideline check.

---

## D-003 — URL validation does not resolve DNS

**Date:** 2026-08-07 · **By:** lead · **Status:** Accepted (known limitation)

`validate_url` rejects loopback / private / link-local / reserved **IP literals**, but does
not resolve hostnames. A public name whose A record points at `169.254.169.254` or `10.0.0.5`
passes validation.

**Why not resolve:** it makes a pure, offline-testable function depend on the network; and
it is a TOCTOU race regardless — DNS can be repointed between validation and the moment a
victim follows the link, so resolution buys the appearance of a guarantee rather than the
guarantee.

**Why the residual risk is acceptable here:** Shorty stores a URL and hands it back as a
`Location` header. It never fetches the URL itself, so there is no server-side request to
forge — the classic SSRF primitive is absent. The risk is that Shorty becomes an open
redirector toward an internal address, which requires the victim to already be inside the
network.

**Revisit if:** any feature is added that makes the service fetch a stored URL
(link preview, title scraping, health checking). At that point resolution-time validation
plus an egress-restricted VPC becomes mandatory, not optional.

---

## D-004 — Cognito MFA is OPTIONAL, not required

**Date:** 2026-08-07 · **By:** lead · **Status:** Accepted (deviation)

The guidelines say to configure MFA. Shorty is backend-only: the sole way to obtain a token
is `admin-initiate-auth` with `ADMIN_USER_PASSWORD_AUTH` from the runbook. Required MFA
turns that into a `SOFTWARE_TOKEN_MFA` challenge loop with no UI to complete it in, which
would make the documented deploy path unusable.

**Choice:** `mfa=OPTIONAL` with software tokens enabled, so MFA is available and can be
switched to required the moment a real client exists.

**Compensating controls:** self-service signup disabled, users created administratively
only, 12-character minimum password with all four character classes, email-only account
recovery.

**Revisit if:** any interactive client is added.

---

## D-005 — No KMS encryption on Lambda environment variables

**Date:** 2026-08-07 · **By:** lead · **Status:** Accepted

The guidelines call for a KMS key on environment variables *containing sensitive data*.
The only variable either function carries is `TABLE_NAME`, which is not sensitive and is
already visible in the synthesized template and the console.

Recorded so the absence reads as a decision rather than an oversight during review. If any
function ever carries a secret, the correct fix is Secrets Manager at runtime, not an
encrypted environment variable.

---

## D-006 — Both Lambda roles get the same full KMS action set, not a read/write split

**Date:** 2026-08-08 · **By:** sa-1 (finding C-1), confirmed by the lead against AWS docs ·
**Status:** Accepted — corrects an earlier error in `design.md`

**The original design was wrong.** It gave `create_fn` only `kms:GenerateDataKey` and
`redirect_fn` only `kms:Decrypt`, describing that as "the minimum for CMK-encrypted table
access". It is not the minimum; it is a broken configuration.

AWS documents the minimum permissions on a customer-managed key for DynamoDB as
`kms:Encrypt`, `kms:Decrypt`, `kms:ReEncrypt*`, `kms:GenerateDataKey*`, `kms:DescribeKey`,
and `kms:CreateGrant`. The mechanism that makes the split fail is **table-key caching**:
DynamoDB caches the plaintext table key per calling principal and re-requests it with a
`Decrypt` call after roughly five minutes of inactivity. Every caller therefore needs
`Decrypt` — the writer included. `GenerateDataKey` is consumed when the table key is first
created, by DynamoDB under a grant, not on each `PutItem`.

**Impact had it shipped:** `create_fn` would fail every `PutItem` after each cold start or
idle gap — a 100% failure rate on `POST /links`, not an intermittent one.

**Why no gate would have caught it.** This is the part worth remembering. `cdk synth`
validates structure, not authorization. The handler tests stub `boto3` per the interface
contract, so no real KMS call is ever made. The infra tests would have asserted the policy
the design specified — asserting the bug and passing. And under D-001 the first real
invocation happens on the owner's machine, after the team is gone. A design-stage review was
the only thing positioned to catch it, which is why AGENT-76 ran in group 1 rather than
alongside the code.

**Correction:** both roles get `kms:Encrypt`, `kms:Decrypt`, `kms:ReEncrypt*`,
`kms:GenerateDataKey*`, `kms:DescribeKey` on the single key ARN, constrained by
`kms:ViaService = dynamodb.*.amazonaws.com`. `kms:CreateGrant` goes to the deploying
principal, not the Lambda roles. The **DynamoDB** action split (`PutItem` vs `GetItem`) is
untouched — that is where least privilege actually pays here, and it remains asserted.

The `ViaService` Region position is a literal `*`, not the deployment Region: AWS requires
the permission to be Region-independent so DynamoDB can make cross-Region calls. Pinning the
Region is a second, subtler way to break this.

**Consequence for AGENT-83:** the posture test must assert the *corrected* set plus the
`ViaService` condition. A test written against the old design would encode the defect.

---

## D-007 — No CloudTrail data events on the DynamoDB table

**Date:** 2026-08-08 · **By:** lead (from sa-1 finding W-2) · **Status:** Accepted

DynamoDB data events are off by default and are a per-table opt-in. Shorty enables neither
them nor a trail.

**Why:** data events are billed per event and would be the second recurring charge in a PoC
whose whole cost story is one KMS key. Management events — table created, key policy
changed — are captured by the account's default trail regardless, and those are the events
that matter for a reference build.

Recorded rather than left silent, because an absent audit trail on a data store reads as an
oversight in review. **Revisit if:** the service ever stores real user links, at which point
per-item read auditing becomes a compliance question rather than a cost one.

---

## D-008 — Open-redirect abuse is accepted, and mitigated by authenticated minting

**Date:** 2026-08-08 · **By:** lead (from sa-1 finding W-5) · **Status:** Accepted

Any URL shortener is an open redirector by construction: it takes an arbitrary URL and
bounces a browser to it, which is exactly what makes shorteners useful for phishing. D-003
addresses only the internal-address angle, not this one.

**Why the residual risk is acceptable here:** minting requires a valid Cognito JWT and
self-service signup is disabled (`self_sign_up_enabled=False`), so every link is
attributable to an administratively-created identity recorded in `createdBy`. An anonymous
attacker cannot mint at all. That is a stronger control than the domain-blocklist most
public shorteners rely on.

**Not doing:** blocklist/reputation checks on submitted URLs, or an interstitial warning
page. Both need an external reputation feed and a UI — the second is explicitly out of scope
for a backend-only build.

**Revisit if:** signup is ever opened up, which converts this from a low risk to the
service's primary abuse vector.

---

## D-009 — `dynamodb.Table` (v1 L2), not `TableV2`

**Date:** 2026-08-08 · **By:** devops-1, during AGENT-79 · **Status:** Accepted

`design.md` originally specified `dynamodb.TableV2`, the global-table L2. It cannot be used
here.

In `aws-cdk-lib` 2.263.0, `TableEncryptionV2.customer_managed_key` throws
`ReplicaSpecificationCannotRenderedRegion` whenever the stack's Region is an unresolved
token. It cannot render even the deployment Region's replica SSE specification in a
Region-agnostic stack, and there is no parameter that avoids it (verified by devops-1
against the compiled construct source).

That is a direct collision with **NF1**, the spec's defining constraint: no `env=`, no
context lookups, `cdk synth` succeeding with zero AWS credentials. NF1 wins — it is the
constraint the entire verification strategy rests on, whereas `TableV2` was a default, not a
requirement. Shorty is single-region and needs no global tables.

`dynamodb.Table` satisfies every acceptance criterion Region-agnostically: customer-managed
key encryption, PITR, deletion protection, `RemovalPolicy.RETAIN`.

**Externally-visible consequence:** `ShortyDataStack.table` is typed `ITable`, not
`ITableV2`, so `ShortyAppStack`'s constructor prop is typed accordingly. Recorded because a
future reader who "upgrades" the construct to `TableV2` will break `cdk synth` for a reason
that is not obvious from the error message.

---

## D-010 — Group barriers are enforced by issue links, not by spawn timing

**Date:** 2026-08-08 · **By:** lead · **Status:** Accepted (process correction)

A process error worth recording, because it cost real rework.

I sequenced the four parallel groups by *when I spawned teammates*, assuming a teammate
works the issue it was handed and then stops. It does not. Teammates self-claim from the
queue, and the `TeammateIdle` hook actively pushes an idle teammate to claim the next
unclaimed issue carrying its role label. So devops-1 finished AGENT-75 (group 1), then
immediately claimed and completed AGENT-79 and claimed AGENT-80 — both group 2 — while I
still believed group 2 had not started.

Two concrete consequences: AGENT-79 was completed ~2 minutes before I added
`deletion_protection` to its acceptance criteria, and AGENT-80 was claimed ~20 seconds after
I posted the D-006 KMS correction, which came far too close to shipping the Critical defect
that review had just caught.

**The rule:** a group barrier only exists if it is expressed as a `Blocks` issue link, in
place *before* the blocking issue can reach `In Review`. Spawn timing enforces nothing.
Equivalently — an issue's acceptance criteria must be final before it is claimable, because
editing a claimed or completed issue's criteria does not retroactively change the work.

---

## D-011 — A guard must be tested against the shapes it does *not* cover

**Date:** 2026-08-08 · **By:** review-1 (finding C-1), confirmed by the lead ·
**Status:** Accepted (process correction)

The IAM least-privilege sweep in `test_security_posture.py` and `test_app_stack.py`
enumerated `template.find_resources("AWS::IAM::Policy")` and nothing else. CDK renders
policies three ways; the guard checked one. `role.add_managed_policy(...)` (rendering to
`Role.ManagedPolicyArns`) and `iam.Role(..., inline_policies={...})` (rendering to
`Role.Policies`) both escaped it entirely.

Demonstrated, not theorised: attaching **`AdministratorAccess` to both Lambda execution
roles passed 130 of 130 tests** — administrator rights on the unauthenticated redirect
function, with the posture suite green. The same wildcard added via `role.add_to_policy(...)`
— the path the authors happened to use — correctly fired 6 tests.

**The lesson, which generalises past this repo:** the guard worked for the shape the code was
written in and was dead for two neighbouring shapes. Enumerating resource *types by name* is
what created the hole, so the fix walks `to_json()` for `PolicyDocument` shapes structurally
— covering a fourth rendering path by construction rather than by the next patch. Deliberate
exclusions (`AssumeRolePolicyDocument`, and `KeyPolicy`, which trips CDK's legitimate default
root statement of `kms:*` on `*`) are named in comments rather than left as omissions.

**Two second-order lessons worth more than the fix:**

1. **Mutation-testing one control proves that control, not the guard.** The lead had already
   mutation-tested this suite by deleting `kms:Decrypt` and watching two tests fire — a real
   check, but of a shape the guard *covers*. The question that found C-1 was not "does this
   assertion fire?" but "what could I add that this would not see?" Only the second question
   finds coverage holes.
2. **An author cannot reliably audit their own guard.** devops-2 wrote both `app_stack.py`
   and its posture suite; devops-1 objected at the time and was overruled for cost reasons,
   with an independent audit promised as compensation. The audit found exactly the predicted
   class of defect. The suite was otherwise sound — it derived from the rules rather than
   rationalising its author's code — which is the point: the failure was not bias but
   blindness, and blindness is not fixable by trying harder.

**Consequence:** any test whose job is to *prevent* a class of defect must itself be shown to
fail against that class, including against variants the current code does not use. A fix to a
guard that is not itself mutation-tested repeats the original error one level up.
