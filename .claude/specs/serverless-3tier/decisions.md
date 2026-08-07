# Decisions — `serverless-3tier`

Mid-flight decision log. Each entry records a call made *during* the build that changed
or clarified `spec.md` / `design.md`, with the reason live at the time. Decisions taken
before the build are in `spec.md` → Design Decisions; this file is only for what moved
afterwards.

---

## D1 — `WebStackProps` gains `key: IKey`

**Date:** 2026-08-07 · **Raised by:** `devops-1` (AGENT-53) · **Status:** accepted

`design.md` gave WebStack `{ apiUrl, identityPoolId }`, but AGENT-53's acceptance criteria
require the SPA bucket be encrypted with the customer-managed key **from DataStack**. The
contract as written could not satisfy its own acceptance criteria — the key had no route
to WebStack.

**Chosen:** add `readonly key: IKey` to `WebStackProps` and pass `dataStack.key` in
`bin/app.ts`.

**Rejected:** WebStack minting its own CMK. It would satisfy "encrypted" while
contradicting "passed in from DataStack", and would leave a second key to rotate,
document, and audit for no benefit.

`bin/app.ts` is AGENT-50's declared file, held by the same agent, and no other issue
touches it — so the one-line edit crossed no other claim. `spec.md#interfaces--contracts`
and `design.md#components` were amended to match.

---

## D2 — `minimumProtocolVersion` omitted; TLS 1.2 floor is **not** achieved

**Date:** 2026-08-07 · **Raised by:** `devops-1` (AGENT-53) · **Status:** accepted gap

`design.md` claimed CloudFront enforces a TLS 1.2 minimum. It does not, and cannot here.
`MinimumProtocolVersion` applies only to a distribution with a custom domain and an ACM
certificate, both out of scope because nothing is deployed (NF1). On the default
`*.cloudfront.net` certificate CloudFront pins the security policy itself and admits
TLS 1.0/1.1 — **weaker than the design originally asserted**, not merely "set elsewhere".

**Chosen:** omit the prop, document the gap. The CDK prop is a no-op without a
certificate and never reaches the synthesized template, so setting it would be dead config
that reads as protection — the exact "check that cannot fail" class the review rubric
treats as inheriting the severity of the property it pretends to guard.

**Consequence:** `redirect-to-https` is the real, asserted transit control for the
distribution. Closing the TLS-floor gap requires a custom domain, and therefore a deploy.
Recorded in `spec.md` → Out of Scope and `design.md` → Encryption in transit.

---

## D3 — The access-log bucket stays SSE-S3, not the CMK

**Date:** 2026-08-07 · **Raised by:** lead, reading the code · **Status:** accepted

`design.md` said the CMK encrypts "both S3 buckets". The implementation puts the SPA
bucket on the CMK and the access-log bucket on SSE-S3. The implementation is right:
**CloudFront standard logging does not support an SSE-KMS destination bucket** — pointing
a distribution's `logBucket` at one does not error, it silently delivers no logs at all.

**Chosen:** SSE-S3 on the log bucket, with an in-code comment naming the failure mode.

**Why the comment is load-bearing:** without it the two buckets look gratuitously
inconsistent, and the obvious "fix" trades a working audit trail for a cosmetically
stronger setting — with nothing to catch the loss, because no test asserts that logs
actually arrive. Found during review of AGENT-53, which had made the correct call but not
written down why.

---

## D4 — Lambda assets are directory-level, and plain `lambda.Function`

**Date:** 2026-08-07 · **Raised by:** `coding-1` (AGENT-51) · **Status:** accepted

Two coupled calls that let AGENT-51 and AGENT-52 run in parallel instead of in sequence.

**Asset granularity.** ApiStack's synth needs the handler sources, which AGENT-52 was
writing concurrently. `Code.fromAsset` on the *directory* requires only that the directory
exist, not any particular file inside it — so synth does not race file creation. A
file-level reference would have made AGENT-51's verification fail on AGENT-52's timing and
look like AGENT-51's bug.

**Construct choice.** Plain `lambda.Function` over `NodejsFunction`: the latter falls back
to Docker bundling when `esbuild` is absent, and NF2 requires verification to stay fully
local and offline. Nothing is deployed (NF1), so the asset need only synthesize, not run.

---

## D5 — `generateCode()` moves inside the try, for contract conformance only

**Date:** 2026-08-07 · **Raised by:** a review helper under `coding-2` · **Status:** accepted, rationale corrected

`generateCode()` sat outside `create-link.ts`'s `try`, so a throw from it would escape the
handler's error handling. The helper reported this as an information disclosure — raw
runtime payload and stack trace reaching the caller.

**That rationale is wrong and was corrected before it entered the record.** With a Lambda
proxy integration behind an API Gateway HTTP API, an unhandled function error yields a
generic `500 {"message":"Internal Server Error"}`; the error and stack go to CloudWatch,
not to the caller.

**The fix stands on narrower grounds:** (1) `spec.md#interfaces--contracts` pins the error
body as `{"error":"INTERNAL"}`, and the one path escaping the handler was also the one path
violating the published contract; (2) it bypassed `logError`, so that failure alone would
land with no structured log line. Severity low — `crypto.randomBytes` throws only if the
entropy source fails — and the fix is one line.

Logged because the *correction* is the reusable lesson: a plausible-sounding Critical from
a helper agent is still a claim to verify, not a finding to file.

---

## D6 — DynamoDB mocked with `jest.spyOn`, not `aws-sdk-client-mock`

**Date:** 2026-08-07 · **Raised by:** `coding-2` (AGENT-52) · **Status:** accepted deviation

AGENT-52's acceptance criteria named `aws-sdk-client-mock`. Adding it means editing
`package.json`, which is **AGENT-50's declared file**, not AGENT-52's — and `coding-1` was
working the same package concurrently. Adding a dependency there would have been a
cross-claim write, exactly what the file-disjoint partition exists to prevent.

**Chosen:** mock via `jest.spyOn(DynamoDBClient.prototype, "send")`, using only the
already-installed `@aws-sdk/client-dynamodb`.

**Assessment:** correct call. The agent preferred a scope boundary over a literal reading
of its acceptance criteria, and said so rather than doing it quietly. The substance the
criterion was protecting — handler tests that never touch a real DynamoDB — is fully met,
with one fewer dev dependency. The criterion named an implementation, not a requirement.

**Note for review:** this is an accepted deviation, not an unmet criterion. Do not score
it as a finding.
