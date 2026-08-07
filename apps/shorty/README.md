# Shorty

A backend-only, serverless URL shortener built as a reference example of what this repo's
agent team produces. There is no UI, no SPA, no hosted login page -- the deliverable is an
API: `POST /links` mints a short code for a URL, `GET /{code}` redirects to it.

**The agent team authored, synthesized, and unit-tested this application. The team never
deployed it.** No `cdk deploy`, no `cdk bootstrap`, no execution of `scripts/smoke.sh`, no
AWS credentials, no spend -- see `.claude/specs/shorty/spec.md` NF1 and
`.claude/specs/shorty/decisions.md` D-001. Everything below the "Local verification"
section is a runbook for the repo **owner** to run manually, at their own discretion. The
deploy path is authored and reviewed, **not live-validated** -- do not read anything past
this point as evidence that it has been run.

## Architecture

Three genuinely separate runtime tiers:

| Tier | What | Where |
|---|---|---|
| Edge | Amazon API Gateway HTTP API (v2) with a Cognito JWT authorizer on `POST /links` only; `GET /{code}` is deliberately unauthenticated -- a browser following a short link carries no credentials | `shorty_infra/app_stack.py` |
| Compute | Two AWS Lambda functions (Python 3.13), one execution role each, least-privilege IAM | `src/shorty/`, `shorty_infra/app_stack.py` |
| Data | Amazon DynamoDB table (`code` as partition key), encrypted with a customer-managed KMS key, point-in-time recovery and deletion protection enabled | `shorty_infra/data_stack.py` |

Two CDK stacks, split by lifecycle rather than by tier:

- **`ShortyDataStack`** -- the stateful half (KMS key, DynamoDB table). Both resources are
  `RemovalPolicy.RETAIN` and **survive `cdk destroy`** -- see "Teardown" below, this is not
  optional reading.
- **`ShortyAppStack`** -- the disposable half (Cognito user pool, both Lambda functions, the
  HTTP API). Takes the data stack's `table` and `key` as typed constructor props.

The CDK app (`app.py`) is environment-agnostic: no `env=`, no context lookups, no
account/region literal anywhere. `cdk synth` succeeds with zero AWS credentials, which is
what let the team verify this entirely offline.

## Prerequisites

- **Python 3.13.** `apps/shorty/.python-version` is the single source of truth for the
  version -- tools that honor it (`pyenv`, `uv`) pick it up automatically. A bare `python3`
  on `PATH` is not reliable: on the machine this was built on it silently resolves to a
  different interpreter than the pinned one, so always invoke `python3.13` explicitly when
  creating the virtualenv (see below).
- **Node.js 22**, required only because the AWS CDK CLI is Node-based. On the machine this
  was built on, Node lives under nvm at `~/.nvm/versions/node/v22.23.1/bin` and is **not**
  on the default `PATH` -- export it first:
  ```bash
  export PATH="$HOME/.nvm/versions/node/v22.23.1/bin:$PATH"
  ```
- **AWS CLI v2**, configured with credentials that can deploy CloudFormation stacks, create
  Cognito users, and manage DynamoDB/KMS resources -- needed only for the deploy runbook
  below, not for local verification.

## Local verification (offline, no AWS credentials)

Everything in this section is exactly what the agent team ran and is safe to re-run at any
time -- no AWS account, no credentials, no network access to AWS.

```bash
cd apps/shorty
python3.13 -m venv .venv
./.venv/bin/pip install -q -r requirements.txt -r requirements-dev.txt

# Lint
./.venv/bin/ruff check .

# Unit + infra tests (Node must be on PATH for the CDK-backed infra tests --
# see Prerequisites)
export PATH="$HOME/.nvm/versions/node/v22.23.1/bin:$PATH"
./.venv/bin/python -m pytest

# Synthesize the CloudFormation templates with zero AWS credentials (proves NF1)
npx --yes aws-cdk@2 synth --quiet
```

If all four commands succeed, the app is in the same state the team last verified it in.

## Deploy runbook (owner-run only)

Everything below requires real AWS credentials and creates billable resources. Set your
target region once and reuse it explicitly on every call, per
`.claude/rules/execution-hygiene.md` -- no command in this runbook relies on an ambient
region or account.

```bash
export AWS_REGION=us-east-1   # substitute your deployment region
export PATH="$HOME/.nvm/versions/node/v22.23.1/bin:$PATH"
cd apps/shorty
```

### 1. Bootstrap and deploy

```bash
npx --yes aws-cdk@2 bootstrap --region "$AWS_REGION"

npx --yes aws-cdk@2 deploy --all \
  --require-approval never \
  --region "$AWS_REGION"
```

### 2. Look up the deployed resource identifiers

The stacks do not export `CfnOutput`s, so pull physical resource IDs from CloudFormation
directly:

```bash
USER_POOL_ID=$(aws cloudformation describe-stack-resources \
  --stack-name ShortyAppStack --region "$AWS_REGION" --no-cli-pager \
  --query "StackResources[?ResourceType=='AWS::Cognito::UserPool'].PhysicalResourceId" \
  --output text)

USER_POOL_CLIENT_ID=$(aws cloudformation describe-stack-resources \
  --stack-name ShortyAppStack --region "$AWS_REGION" --no-cli-pager \
  --query "StackResources[?ResourceType=='AWS::Cognito::UserPoolClient'].PhysicalResourceId" \
  --output text)

TABLE_NAME=$(aws cloudformation describe-stack-resources \
  --stack-name ShortyDataStack --region "$AWS_REGION" --no-cli-pager \
  --query "StackResources[?ResourceType=='AWS::DynamoDB::Table'].PhysicalResourceId" \
  --output text)

KMS_KEY_ID=$(aws cloudformation describe-stack-resources \
  --stack-name ShortyDataStack --region "$AWS_REGION" --no-cli-pager \
  --query "StackResources[?ResourceType=='AWS::KMS::Key'].PhysicalResourceId" \
  --output text)
KMS_KEY_ARN=$(aws kms describe-key \
  --key-id "$KMS_KEY_ID" --region "$AWS_REGION" --no-cli-pager \
  --query 'KeyMetadata.Arn' --output text)

API_ID=$(aws cloudformation describe-stack-resources \
  --stack-name ShortyAppStack --region "$AWS_REGION" --no-cli-pager \
  --query "StackResources[?ResourceType=='AWS::ApiGatewayV2::Api'].PhysicalResourceId" \
  --output text)
API_BASE_URL="https://${API_ID}.execute-api.${AWS_REGION}.amazonaws.com"

echo "API_BASE_URL=$API_BASE_URL"
```

### 3. Create a test user

There is no self-service signup (`self_sign_up_enabled=False`) -- users are created
administratively, which is the whole user-management story for this PoC.

```bash
aws cognito-idp admin-create-user \
  --user-pool-id "$USER_POOL_ID" \
  --username smoke-test-user \
  --user-attributes Name=email,Value=you@example.com Name=email_verified,Value=true \
  --message-action SUPPRESS \
  --region "$AWS_REGION" --no-cli-pager

aws cognito-idp admin-set-user-password \
  --user-pool-id "$USER_POOL_ID" \
  --username smoke-test-user \
  --password 'Sup3r$ecureP4ssw0rd!' \
  --permanent \
  --region "$AWS_REGION" --no-cli-pager
```

(Password satisfies the pool's policy: 12+ characters, upper, lower, digit, symbol.)

### 4. Fetch an IdToken

```bash
AUTH_RESULT=$(aws cognito-idp admin-initiate-auth \
  --user-pool-id "$USER_POOL_ID" \
  --client-id "$USER_POOL_CLIENT_ID" \
  --auth-flow ADMIN_USER_PASSWORD_AUTH \
  --auth-parameters USERNAME=smoke-test-user,PASSWORD='Sup3r$ecureP4ssw0rd!' \
  --region "$AWS_REGION" --no-cli-pager)

ID_TOKEN=$(echo "$AUTH_RESULT" | \
  python3 -c 'import json,sys; print(json.load(sys.stdin)["AuthenticationResult"]["IdToken"])')
```

### 5. Run the smoke test

```bash
API_BASE_URL="$API_BASE_URL" ID_TOKEN="$ID_TOKEN" ./scripts/smoke.sh
```

Mints a link, follows it (without auto-redirect) to confirm the `302` and `Location`, and
confirms an unknown code returns `404`. See `scripts/smoke.sh` for exactly what it checks.

### 6. Teardown

```bash
npx --yes aws-cdk@2 destroy --all --force --region "$AWS_REGION"
```

**This does not fully delete the deployment.** `cdk destroy` removes `ShortyAppStack`
(Cognito, Lambdas, the HTTP API) but **`ShortyDataStack`'s table and KMS key are
`RemovalPolicy.RETAIN` and survive it by design** -- this is a deliberate footgun-avoidance
so a slipped `cdk destroy` can never silently take production data with it. If you stop
here, the table and key are still live and **still billing**.

**Cost while they remain, steady state: $3/month.** The key costs $1/month to hold, plus
$1/month for each of the first two annual rotations -- capped after the second rotation, so
it settles at $3/month indefinitely if left in place. Everything else in this app (API
Gateway HTTP API, Lambda, on-demand DynamoDB) is effectively free at PoC traffic; the KMS
key is the only recurring line item. Full detail:
`.claude/specs/shorty/kms-key-usage.md` section 5.

**To actually delete the table and key**, in this exact order -- deleting the key first
makes the table permanently unreadable, so the table has to go first while the key can still
decrypt it:

```bash
# 1. Confirm the data is expendable -- this is irreversible past this point.

# 2. The table has deletion_protection=True, so it must be disabled before it can
#    be deleted at all.
aws dynamodb update-table \
  --table-name "$TABLE_NAME" \
  --no-deletion-protection-enabled \
  --region "$AWS_REGION" --no-cli-pager

# 3. Delete the table while the key is still enabled and accessible.
aws dynamodb delete-table \
  --table-name "$TABLE_NAME" \
  --region "$AWS_REGION" --no-cli-pager

# 4. Schedule key deletion. 7-30 day waiting window; 30 is the default and the
#    safer choice. Billing stops as soon as deletion is scheduled.
aws kms schedule-key-deletion \
  --key-id "$KMS_KEY_ARN" \
  --pending-window-in-days 30 \
  --region "$AWS_REGION" --no-cli-pager
```

To abort key deletion during the waiting window:

```bash
aws kms cancel-key-deletion --key-id "$KMS_KEY_ARN" --region "$AWS_REGION" --no-cli-pager
```

**Do not disable or delete the key while the table still exists.** If the key becomes
unavailable, the table's status becomes `Inaccessible` and every read/write fails; DynamoDB
archives the table if access is not restored within seven days, and restoring from that
archived state requires re-enabling that same key. Step 2 above (disable protection, delete
table) must happen before step 4 (schedule key deletion) for exactly this reason.

## Security posture

Encryption at rest via a customer-managed KMS key (not AWS-owned), key rotation enabled,
point-in-time recovery and deletion protection on the table, least-privilege per-function
IAM roles, a JWT authorizer on the only route that mutates state, and stage-level
throttling as the compensating control for the necessarily-unauthenticated redirect route.
Full detail and the corrected KMS grant set (`decisions.md` D-006) are in
`.claude/specs/shorty/design.md` and `.claude/specs/shorty/kms-key-usage.md`; asserted
directly against the synthesized template in `tests/infra/test_security_posture.py`.
