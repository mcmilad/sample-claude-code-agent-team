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
