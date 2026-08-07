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
   `sa-review.md`.

**Data classification.** The table is tagged `data-classification: internal`. Stored data is
a caller-supplied URL plus the minting user's Cognito `sub`. No credential, no secret, and
no direct identifier is stored.

## 3. Grants required by each principal

> **This section corrects `design.md` → Security Considerations → Compute (tier 2).**
> The design specifies `kms:GenerateDataKey` for `create_fn` and `kms:Decrypt` for
> `redirect_fn`. Both are **below the documented minimum** and `create_fn`'s is
> functionally broken. Raised as **Finding C-1 (Critical)** in `sa-review.md` and reported
> to the lead; `design.md` is out of this issue's `Files:` scope and is not edited here.

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

### 3.1 Recommended grant — applies identically to both execution roles

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
    "StringEquals": { "kms:ViaService": "dynamodb.<region>.amazonaws.com" }
  }
}
```

`kms:CreateGrant` is required by the principal that **creates or re-keys the table** — the
CloudFormation deployment role, not the Lambda execution roles. DynamoDB uses those grants
for background maintenance and continuous data protection, and each grant is constrained by
the DynamoDB encryption context. Do **not** add `kms:CreateGrant` to the Lambda roles; add
it (or rely on the key's default account-root policy) for the deploying principal.

Least privilege is preserved where it actually pays: the **DynamoDB** action stays split
(`create_fn` → `PutItem` only, `redirect_fn` → `GetItem` only, each on the single table
ARN, per NF4). The redirect function still cannot write a link. What changes is only the
KMS action list, which the service requires as a set.

**Implementation note.** CDK's `table.grant_write_data(role)` / `table.grant_read_data(role)`
wire the corresponding KMS grants automatically when the table has a customer-managed key.
If the implementer prefers hand-written policies (as `design.md` implies), the statement
above is the target. Either way, `tests/infra/test_security_posture.py` should assert the
KMS action set and the `kms:ViaService` condition, not just the DynamoDB action.

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

> **This section corrects `spec.md` NF6 and `design.md` → Trade-offs #2**, both of which
> state the key costs "~$1/month". That is the year-one figure only; it is not the steady
> state for a key with rotation enabled. Raised as **Finding W-4 (Warning)** in
> `sa-review.md`.

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
| Lambda execution-role KMS grants are below the documented minimum | **Finding C-1 (Critical)** — `create_fn` cannot write. Must be fixed before the owner deploys |
| Key policy is not specified in `design.md` | **Finding S-4** — recommend an explicit `kms:ViaService`-constrained policy plus an alias |
| No key alias | Operationally awkward; a raw key ID in the runbook is easy to mistype. Recommend `alias/shorty-links` |
| No CloudWatch alarm on key-disabled / table `Inaccessible` | **Finding S-1** — seven days is the entire window to react |
| Encryption context unused as a policy condition | Acceptable — single table, single key |
| Key is single-Region | Acceptable — multi-Region is out of scope |

---

**Flagged for security review.** This document records BYOK (customer-managed KMS key) usage
for the Shorty workload, per `.claude/rules/AWS-security-guidelines.md` → Amazon S3/DynamoDB
BYOK requirements and Data Security Implementation Order Phase 3 item 8. It contains one
**Critical** finding (C-1, §3) that is a prerequisite for a working deployment, and one
corrected cost figure (§5). Reviewer attention is specifically requested on §3 (grant set)
and §5 (steady-state cost). Full assessment: `.claude/specs/shorty/sa-review.md`.
