# Design — Shorty

Slug: `shorty`
Spec: `.claude/specs/shorty/spec.md`

## Architecture

Three runtime tiers. Backend-only, so the tiers are edge / compute / data — not UI / API /
database.

```
              JWT (Cognito user pool)
                      |
  client ──POST /links──►┐
                         │  ┌─────────────────────────┐      ┌──────────────────┐
  browser ─GET /{code}──►├──┤ API Gateway HTTP API v2 │      │  Cognito         │
                         │  │  - JWT authz on POST    │◄─────┤  user pool +     │
                         │  │  - none on GET (F2)     │ iss  │  app client      │
                         │  │  - stage throttling     │      └──────────────────┘
                         │  │  - access logs → CWL    │
                         │  └───────────┬─────────────┘
                         │              │
                         │   ┌──────────┴───────────┐        Tier 1 — edge
                         │   ▼                      ▼
                         │ ┌──────────────┐  ┌──────────────┐
                         │ │ create_fn    │  │ redirect_fn  │  Tier 2 — compute
                         │ │ role: PutItem│  │ role: GetItem│  (one role each)
                         │ └───────┬──────┘  └──────┬───────┘
                         │         └────────┬───────┘
                         │                  ▼
                         │        ┌───────────────────────┐
                         │        │ DynamoDB `links`      │  Tier 3 — data
                         │        │ PK code · CMK · PITR  │
                         │        └───────────────────────┘
```

## Stack Decomposition

Two stacks, split on **lifecycle** rather than on tier:

**`ShortyDataStack`** — the stateful half.
- `kms.Key` — customer-managed, `enableKeyRotation=True`, `RemovalPolicy.RETAIN`
- `dynamodb.TableV2` `links` — PK `code` (String), on-demand billing,
  `encryption=CUSTOMER_MANAGED` with the key above, `pointInTimeRecovery=True`,
  `RemovalPolicy.RETAIN`, tagged `data-classification=internal`
- Exposes `table` and `key` as attributes on the stack object.

**`ShortyAppStack`** — the disposable half. Takes `table` and `key` as **constructor
props** (typed Python parameters), not `Fn::ImportValue` string lookups, so a rename cannot
silently produce a dangling reference.
- `cognito.UserPool` + `UserPoolClient` (`ADMIN_USER_PASSWORD_AUTH` enabled, no hosted UI)
- `lambda_.Function` ×2, Python 3.13, one `iam.Role` each, reserved concurrency set
- `apigatewayv2.HttpApi` + `HttpJwtAuthorizer` on `POST /links` only
- `logs.LogGroup` for API access logs, one-month retention

The app is environment-agnostic: `App()` with no `env=`, no `from_lookup` anywhere, so
`cdk synth` runs with zero credentials (NF1).

## Repo Structure

```
apps/shorty/
  app.py                      # CDK entrypoint: instantiates both stacks, wires props
  cdk.json                    # app = "python3 app.py"
  pytest.ini                  # testpaths = tests  (scoped to this app)
  requirements.txt            # aws-cdk-lib, constructs — exact pins
  requirements-dev.txt        # pytest, ruff, boto3, botocore — exact pins
  README.md                   # deploy runbook: bootstrap → deploy → token → smoke → destroy
  shorty_infra/
    __init__.py
    data_stack.py             # ShortyDataStack
    app_stack.py              # ShortyAppStack
  src/shorty/
    __init__.py
    validate.py               # validate_url(raw) -> str        (pure, no boto3)
    codec.py                  # generate_code() -> str          (pure, no boto3)
    create_handler.py         # handler(event, context)
    redirect_handler.py       # handler(event, context)
  tests/
    unit/  test_validate.py  test_codec.py  test_create_handler.py  test_redirect_handler.py
    infra/ test_data_stack.py  test_app_stack.py  test_security_posture.py
  scripts/smoke.sh            # authored, never run by the team
.github/workflows/shorty.yml  # ruff + pytest + cdk synth. No secrets, no deploy job.
```

## Interface Contracts

Fixed at spec time so producers and consumers can be built in parallel by different
teammates. Any change to these signatures is a spec change, not an implementation detail.

```python
# src/shorty/validate.py
class InvalidUrl(ValueError):
    """Raised when a candidate URL fails validation."""

def validate_url(raw: object) -> str:
    """Return the normalized URL, or raise InvalidUrl.

    Rejects: non-str, empty, > 2048 chars, non-http(s) scheme, missing netloc,
    userinfo present (`user:pass@host`), control characters, and hosts that are
    loopback / private / link-local / reserved IP literals (v4 and v6).
    Hostnames are NOT resolved — see spec Constraint 3.
    """

# src/shorty/codec.py
ALPHABET: str  # 62 symbols: A-Z a-z 0-9
CODE_LENGTH: int = 7
CODE_PATTERN: re.Pattern  # ^[A-Za-z0-9]{7}$

def generate_code() -> str:
    """Return a cryptographically-random 7-character code."""
```

Both handlers read `TABLE_NAME` from the environment and build their own module-level
`boto3.client("dynamodb")` so the client is reused across warm invocations. Tests inject a
stub by monkeypatching the module attribute — no moto, no network.

**Handler I/O.** API Gateway HTTP API **payload format 2.0**. `create_handler` reads
`event["body"]` (honouring `isBase64Encoded`) and the caller's subject from
`event["requestContext"]["authorizer"]["jwt"]["claims"]["sub"]`. `redirect_handler` reads
`event["pathParameters"]["code"]`. Both return
`{"statusCode": int, "headers": {...}, "body": str}`.

## Data Flow

**Mint.** `POST /links` → authorizer validates the JWT against the pool issuer → `create_fn`
→ `validate_url` (400 on failure) → `generate_code` → `PutItem` with
`ConditionExpression="attribute_not_exists(code)"` → on `ConditionalCheckFailedException`
regenerate and retry, ≤5 attempts → 201 `{code, shortUrl}`, or 503 after exhaustion.

**Resolve.** `GET /{code}` → no authorizer → `redirect_fn` → shape-check the code against
`CODE_PATTERN` (404 immediately if it fails, before any DynamoDB call — this keeps a
scanner from turning malformed input into billed reads) → `GetItem` → 302 with `Location`
and `Cache-Control: no-store`, or 404.

## Error Handling

| Condition | Status | Body | Note |
|---|---|---|---|
| Body absent / not JSON / `url` missing / invalid | 400 | `{"error":"INVALID_URL"}` | One code for every validation failure — a granular reason would tell a prober which rule it tripped |
| Missing or invalid JWT | 401 | authorizer-generated | Never reaches the handler |
| Code shape invalid, or no such code | 404 | `{"error":"NOT_FOUND"}` | Same response for both, so a prober cannot distinguish "wrong shape" from "not minted" |
| 5 mint collisions | 503 | `{"error":"CODE_COLLISION"}` | Retryable by the caller |
| Unexpected exception | 500 | `{"error":"INTERNAL"}` | Detail goes to CloudWatch Logs, never into the response |

No response body ever contains caller-supplied input. Handlers log the exception type and
the request id, never the raw URL alongside the caller's identity.

## Security Considerations

Every item below is asserted as a `Template` assertion in
`tests/infra/test_security_posture.py`, so a posture regression fails `pytest` rather than
being caught in review.

**Data (tier 3).**
- Encryption at rest with a **customer-managed** KMS key (not an AWS-owned key), rotation
  enabled — `AWS-security-guidelines.md` → DynamoDB.
- Point-in-time recovery `ENABLED`.
- `data-classification: internal` tag, plus `service`, `environment`, `owner` tags applied
  at the `App` level so every resource inherits them.
- Encryption in transit is inherent: the AWS SDK reaches DynamoDB over HTTPS only.
- Key usage, rotation schedule, and grant policy are documented in `kms-key-usage.md` and
  flagged for security review, per the guidelines' BYOK requirement.

**Compute (tier 2).**
- One execution role per function, never shared. `create_fn` gets `dynamodb:PutItem`;
  `redirect_fn` gets `dynamodb:GetItem`. Each is scoped to the single table ARN. No
  wildcard actions, no `dynamodb:*`, no `Resource: "*"`.
- Each role also gets `kms:GenerateDataKey` (create) / `kms:Decrypt` (redirect) on the one
  key ARN — the minimum for CMK-encrypted table access.
- Reserved concurrency set on both functions, so a flood on one route cannot exhaust the
  account's concurrency and starve the other.
- No KMS on environment variables: the only variable is `TABLE_NAME`, which is not
  sensitive. Recorded in `decisions.md` → D-005 so its absence reads as a decision rather
  than an oversight.
- No VPC. Neither function reaches a private resource; a VPC would add ENI cold-start cost
  and a NAT gateway bill for zero security gain.

**Edge (tier 1).**
- JWT authorizer bound to the Cognito user-pool issuer on `POST /links`. Asserted present
  on that route and **asserted absent** on `GET /{code}` — the negative assertion is what
  stops a later change from quietly authenticating the redirect route and breaking F2.
- Stage-level throttling (rate and burst) — the primary compensating control for the
  unauthenticated redirect route.
- Access logging to a dedicated CloudWatch log group with an explicit JSON format.
- No WAF: not attachable to an HTTP API. See spec Constraint 2 and `decisions.md` → D-002.

**Identity.**
- Password policy: minimum 12 characters, all four character classes.
- Account recovery via verified email only.
- MFA `OPTIONAL` — deviation, justified in spec Constraint 4 / `decisions.md` → D-004.
- No hosted UI, no self-service signup: `self_sign_up_enabled=False`. Users are created
  administratively by the runbook, which is the whole user-management story for a POC.

**Secrets.** There are none. No credential is stored, inlined, or passed as an environment
variable anywhere in the app — so Secrets Manager is not used, rather than being used
badly.

## Testing Strategy

| Layer | Approach |
|---|---|
| `validate.py`, `codec.py` | Pure-function unit tests; table-driven rejection corpus. No AWS, no mocks |
| Handlers | Real payload-format-2.0 event fixtures; `boto3` client replaced with a stub that records calls and raises injected exceptions. Asserts status, headers, body, **and the exact DynamoDB call shape** including the condition expression |
| Infra | `aws_cdk.assertions.Template` over each stack — resource properties, IAM policy statements, authorizer bindings |
| Posture | A dedicated `test_security_posture.py` that reads as the checklist from Security Considerations, one test per control |
| Synth | `cdk synth` with no credentials — the executable proof of NF1 |

## Deployment (owner-run, never team-run)

`apps/shorty/README.md` carries the full runbook: `cdk bootstrap`, `cdk deploy --require-approval never`,
`admin-create-user` + `admin-set-user-password`, `admin-initiate-auth` to fetch an
`IdToken`, `scripts/smoke.sh` to mint and follow a link end to end, then `cdk destroy`
(with an explicit note that the table and key are `RETAIN` and survive it, and how to
remove them deliberately).

CI runs `ruff` + `pytest` + `cdk synth` only. It references no secrets and contains no
deploy job — wiring one would mean adding an OIDC role and credentials the repository does
not have.

## Trade-offs

1. **HTTP API over REST API** costs WAF. Accepted: throttling plus a read-only redirect
   role covers the realistic POC threat, and REST API would raise cost and verbosity for
   every route to protect one.
2. **CMK over an AWS-owned key** costs ~$1/month once deployed and buys explicit key
   policy, rotation, and an auditable grant list. The guidelines require it for anything
   above `public`.
3. **`RETAIN` on the data stack** means `cdk destroy` leaves the table and key behind — a
   deliberate footgun-avoidance that the runbook must call out, or the owner will be
   surprised by a lingering KMS charge.
4. **No listing/lookup by user** keeps the table free of a GSI. If a "my links" feature is
   ever wanted, that is a schema change, not an addition — worth knowing before someone
   assumes it is a small feature.
