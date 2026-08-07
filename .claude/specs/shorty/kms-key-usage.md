# KMS Key Usage — Shorty

Slug: `shorty` · Spec: `.claude/specs/shorty/spec.md` · Design: `.claude/specs/shorty/design.md`
Authored by `sa-1` for AGENT-76. Written against the **design**; no resource exists yet.

Required by `.claude/rules/AWS-security-guidelines.md` → Data Security Implementation Order,
Phase 3 item 8 (BYOK documentation) and the DynamoDB section's BYOK/key-management
requirements.

## 1. The key

| Property | Value | Source |
|---|---|---|
| Logical resource | `kms.Key` in `ShortyDataStack` | `design.md` → Stack Decomposition |
| Type | Symmetric encryption key, AWS-generated key material | DynamoDB supports **only** symmetric KMS keys ([encryption usage notes](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/encryption.usagenotes.html)) |
| Scope | Single-Region, single-account | No multi-Region requirement (`spec.md` → Out of Scope) |
| Rotation | `enableKeyRotation=True` — automatic, annual | `design.md` |
| Removal policy | `RemovalPolicy.RETAIN` | `design.md`; survives `cdk destroy` |
| Alias | **Not currently specified** — see Finding S-4 in `sa-review.md` | — |

## 2. What it encrypts

The key is the table-level encryption key for the DynamoDB `links` table
(`encryption=CUSTOMER_MANAGED`). Per the AWS documentation, selecting a customer-managed
key at the table level means the key also protects, with no further configuration:

- the base table,
- all local and global secondary indexes (Shorty has none — `spec.md` → Table design),
- DynamoDB Streams for the table (Shorty enables none),
- on-demand backups taken while this key is the table key.

**Key hierarchy.** DynamoDB does not encrypt items directly under this key. It uses the key
to generate and encrypt a unique **table key**, which in turn protects the data encryption
keys that encrypt table data. The table key persists for the lifetime of the table.

**Table-key caching — why this matters for both cost and IAM.** DynamoDB caches the
plaintext table key **per caller**, and re-requests a `Decrypt` from KMS after five minutes
of inactivity for that caller. Two consequences:

1. KMS request charges are small and roughly proportional to Lambda cold-start/idle churn,
   not to request volume.
2. The `Decrypt` call is made **in the calling principal's authorization context**. This is
   the mechanism behind the grant requirements in §3 — and behind Finding C-1 in
   `sa-review.md`, now resolved via `decisions.md` → D-006.

**Data classification.** The table is tagged `data-classification: internal`. Stored data is
a caller-supplied URL plus the minting user's Cognito `sub`. No credential, no secret, and
no direct identifier is stored.

## 3. Grants required by each principal

> **RESOLVED — this section documents what shipped.** An earlier draft of `design.md` gave
> `create_fn` only `kms:GenerateDataKey` and `redirect_fn` only `kms:Decrypt`, calling that
> "the minimum for CMK-encrypted table access". It was neither the minimum nor functional:
> `create_fn` would have failed every `PutItem`. Raised as **Finding C-1 (Critical)** in
> `sa-review.md`, corrected before any app-stack code was written, and recorded as
> **`decisions.md` → D-006**. The policy below is the **shipped** configuration, read back
> from the synthesized template — not a proposal.

AWS documents the minimum permissions a principal needs on a customer-managed key in order
to access a CMK-encrypted DynamoDB table
([DynamoDB encryption at rest usage notes](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/encryption.usagenotes.html)
→ "Key policy for a customer managed key"; corroborated by
[re:Post: AccessDeniedException accessing a DynamoDB table](https://repost.aws/knowledge-center/dynamodb-access-denied-exception)):

```
kms:Encrypt
kms:Decrypt
kms:ReEncrypt*
kms:GenerateDataKey*
kms:DescribeKey
kms:CreateGrant
```

### 3.1 The shipped grant — identical on both execution roles

Scoped to the one key ARN and constrained so the key is usable **only** when the request
reaches KMS via DynamoDB. The `kms:ViaService` condition is what keeps this from being a
general-purpose encryption grant, and it is the reason both roles can safely carry the same
action list: neither can use the key for anything except reaching the `links` table.

```json
{
  "Effect": "Allow",
  "Action": [
    "kms:Encrypt",
    "kms:Decrypt",
    "kms:ReEncrypt*",
    "kms:GenerateDataKey*",
    "kms:DescribeKey"
  ],
  "Resource": "arn:aws:kms:<region>:<account-id>:key/<key-id>",
  "Condition": {
    "StringLike": { "kms:ViaService": "dynamodb.*.amazonaws.com" }
  }
}
```

> **Why the Region position is a literal `*`, and why not to "tighten" it.** The wildcard
> keeps the grant Region-independent, which is the form AWS documents for a customer-managed
> key protecting a DynamoDB table — its own worked key-policy example in
> [DynamoDB encryption at rest usage notes](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/encryption.usagenotes.html)
> → "Key policy for a customer managed key" uses exactly `StringLike` with
> `dynamodb.*.amazonaws.com`. DynamoDB-initiated calls need not originate from the key's own
> Region (cross-Region restore and export today; global-table replication if the table is
> ever converted), and a Region-pinned grant does not authorize those.
>
> **The trap — operator semantics, nothing subtler.** Only the `Like` family of condition
> operators performs wildcard expansion; the exact-match operator treats `*` as a literal
> asterisk. So swapping the operator while keeping this value yields a condition that matches
> **no** real `kms:ViaService` value, silently denying both roles all KMS access and taking
> down both routes. `decisions.md` → D-006 calls this out as "a second, subtler way to break
> this".
>
> Two guards make that mistake self-announcing rather than something you discover in
> production:
> - `tests/infra/test_security_posture.py` asserts the shipped form, so a change in either
>   direction fails the build before it reaches a deploy.
> - IAM Access Analyzer's policy validation raises
>   **`WILDCARD_WITHOUT_LIKE_OPERATOR`** (a `GENERAL_WARNING`) on exactly this pattern:
>   *"Your condition value includes a `*` or `?` character. If you meant to use a wildcard
>   (`*`, `?`), update the condition operator to include `Like`."*
>   ([IAM policy validation check reference](https://docs.aws.amazon.com/IAM/latest/UserGuide/access-analyzer-reference-policy-checks.html))
>   Worth wiring into any future policy linting — it turns this paragraph into an automated
>   check.
>
> *(Aside, to prevent a different wrong inference: `kms:ViaService` is a **single-valued
> context key** — that describes the value AWS supplies at evaluation time, not what your
> policy may contain. A policy may still supply a **list** of endpoints to match against, and
> AWS's own examples do. The trap above follows from operator semantics alone and does not
> depend on this.)*

`kms:CreateGrant` is required by the principal that **creates or re-keys the table** — the
CloudFormation deployment role, not the Lambda execution roles. DynamoDB uses those grants
for background maintenance and continuous data protection, and each grant is constrained by
the DynamoDB encryption context. Do **not** add `kms:CreateGrant` to the Lambda roles; add
it (or rely on the key's default account-root policy) for the deploying principal.

Least privilege is preserved where it actually pays: the **DynamoDB** action stays split
(`create_fn` → `PutItem` only, `redirect_fn` → `GetItem` only, each on the single table
ARN, per NF4). The redirect function still cannot write a link. What changes is only the
KMS action list, which the service requires as a set.

**Verified against the synthesized template.** `cdk synth` was run and both role policies
read back directly from `cdk.out/*.template.json`. `CreateFnRoleDefaultPolicy` and
`RedirectFnRoleDefaultPolicy` each carry exactly
`kms:Decrypt, kms:DescribeKey, kms:Encrypt, kms:GenerateDataKey*, kms:ReEncrypt*` under
`{"StringLike": {"kms:ViaService": "dynamodb.*.amazonaws.com"}}`, and neither carries
`kms:CreateGrant`. This document is written from that template, not from the design prose it
supersedes.

### 3.2 Encryption context

DynamoDB passes a fixed encryption context on every cryptographic call:

```json
{ "aws:dynamodb:tableName": "<table-name>", "aws:dynamodb:subscriberId": "<account-id>" }
```

It appears in plaintext in CloudTrail and CloudWatch Logs and can be used as an additional
policy/grant condition. Not currently used as a condition in Shorty; noted as an available
tightening if the account ever hosts a second table under the same key.

## 4. Rotation schedule

- **Automatic, annual.** `enableKeyRotation=True` rotates the key material once per year.
- **Rotation is transparent.** KMS retains all prior key material; data encrypted under an
  older version stays readable with no re-encryption and no downtime. The key ARN and ID do
  not change, so no IAM policy, table configuration, or application code references break.
- **On-demand rotation** is available if an incident requires it, and is subject to the same
  billing cap described in §5.
- **No manual step is required of the owner.** There is nothing to schedule, and no runbook
  action on the rotation anniversary.

## 5. Cost

> **RESOLVED.** `spec.md` NF6 and `design.md` → Trade-offs #2 originally stated the key
> costs "~$1/month". That is the year-one figure only, not the steady state for a key with
> rotation enabled. Raised as **Finding W-4 (Warning)** in `sa-review.md` and **since
> corrected** — NF6, Trade-offs #2 and `apps/shorty/README.md` all now state $3/month steady
> state, verified independently by the lead and by review-1.
>
> Retained as a record of why the figure is what it is. **Do not "correct" $3/month back
> toward $1/month** — that is the error this section exists to prevent.

Per [AWS KMS pricing](https://aws.amazon.com/kms/pricing/):

> "Each AWS KMS key that you create in AWS KMS costs $1/month (prorated hourly). […] For
> KMS keys that you rotate automatically or on demand, the first and second rotation of the
> key adds $1/month (prorated hourly) in cost. This price increase is capped at the second
> rotation, and any subsequent rotations will not be billed."

With annual automatic rotation enabled, key **storage** therefore costs:

| Period | Monthly storage cost |
|---|---|
| Deployment → first rotation (~year 1) | $1.00 |
| After 1st rotation (~year 2) | $2.00 |
| After 2nd rotation (~year 3 onward) | $3.00 — **steady state, capped** |

Plus KMS **request** charges, which are billed per API call and are negligible here: the
five-minute per-caller table-key cache means DynamoDB issues a `Decrypt` roughly on cold
start and after idle gaps, not per `GetItem`/`PutItem`. (Per-request rate not quoted — the
pricing page section retrieved for this review covers key storage; the owner should confirm
the current request rate on the pricing page for their Region before budgeting.)

**Because the key is `RETAIN`, this charge continues after `cdk destroy` until the owner
deletes the key deliberately.** Left in place indefinitely it settles at $3/month, not $1.
This is the single largest recurring line item in the whole application at POC traffic —
API Gateway HTTP API, Lambda, and on-demand DynamoDB are all effectively free at that
volume.

## 6. Deliberate removal by the owner

`cdk destroy` deletes `ShortyAppStack` and leaves the table and key behind by design. To
remove them, in this order:

1. **Confirm the data is expendable.** Deleting the key makes the table permanently
   unreadable — this is irreversible.
2. **Delete the table first**, while the key is still enabled and accessible. A table whose
   key is unavailable cannot be restored, and DynamoDB retires its grants on table deletion.
   ```bash
   aws dynamodb delete-table --table-name <table-name> --no-cli-pager
   ```
3. **Schedule key deletion.** The waiting period is 7–30 days; 30 is the default and is the
   safer choice.
   ```bash
   aws kms schedule-key-deletion --key-id <key-arn> --pending-window-in-days 30 --no-cli-pager
   ```
   Billing stops as soon as deletion is scheduled. Cancelling deletion during the waiting
   period re-bills the key as though it had never been scheduled.
4. **To abort**, before the window elapses:
   ```bash
   aws kms cancel-key-deletion --key-id <key-arn> --no-cli-pager
   ```

**Failure mode the owner must know about.** If the key is disabled or scheduled for deletion
while the table still exists, the table's status becomes `Inaccessible` and all reads and
writes fail. DynamoDB emails a notification. If access is not restored within **seven days**,
the table is archived and can no longer be accessed; DynamoDB takes a billed on-demand backup,
and restoring it requires re-enabling that same key. This is why step 2 precedes step 3.

## 7. Residual risks and open items

| Item | Disposition |
|---|---|
| Lambda execution-role KMS grants below the documented minimum | **RESOLVED** — Finding C-1, corrected per `decisions.md` → D-006 and verified against the synthesized template (§3.1). Both roles now carry the full action set under the Region-independent `ViaService` condition |
| No explicit key policy; CDK's account-root default is in force | **Finding S-4 — deferred.** The default (root administers, IAM policies govern use) is safe; the identity-side grant in §3.1 is what actually constrains access. **Trigger:** any second principal or cross-account consumer of this key |
| No key alias | **Finding S-4 — deferred.** Operationally awkward — the runbook asks the owner to handle a raw key ID. **Trigger:** first real deploy; `alias/shorty-links` is a one-line addition |
| No CloudWatch alarm on key-disabled / table `Inaccessible` | **Finding S-1 — deferred.** The highest-value alarm in the workload: seven days is the *entire* window before the table is archived and unrecoverable. **Trigger:** the owner's first deploy — this one should not wait for real traffic |
| Encryption context unused as a policy condition | Acceptable — single table, single key |
| Key is single-Region | Acceptable — multi-Region is out of scope. Note this does **not** make the `ViaService` wildcard unnecessary; see §3.1 |

---

**Flagged for security review.** This document records BYOK (customer-managed KMS key) usage
for the Shorty workload, per `.claude/rules/AWS-security-guidelines.md` → Amazon S3/DynamoDB
BYOK requirements and Data Security Implementation Order Phase 3 item 8. **No open Critical
remains:** C-1 was corrected before implementation and is recorded as D-006; §3.1 documents
the shipped grant, read back from the synthesized template. Reviewer attention is requested
on §3.1 (the `ViaService` form and why it must not be narrowed), §5 (steady-state cost —
$3/month, not $1), and the deferred items in §7. Full assessment:
`.claude/specs/shorty/sa-review.md`.
