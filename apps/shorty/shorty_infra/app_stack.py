"""Disposable half of Shorty: Cognito user pool, Lambdas, HTTP API.

Fills in the AGENT-75 stub (AGENT-80). Owns Cognito, the two Lambda functions,
the HTTP API, and the least-privilege IAM roles that point at the data stack's
`table` / `key`. The handler CODE under src/shorty/ is group 3's; this stack
only wires infrastructure at it.

`table`'s type hint is `dynamodb.ITable`, not `dynamodb.ITableV2` -- AGENT-79
switched ShortyDataStack from `TableV2` to the classic `dynamodb.Table` L2 to
satisfy NF1 (region-agnostic synth); see data_stack.py's module docstring and
decisions.md.

IAM policy shape follows decisions.md -> D-006: both Lambda roles get the SAME
KMS action set (including Decrypt on the writer) because DynamoDB caches the
table key per calling principal and re-requests it with Decrypt after an idle
gap -- splitting read/write here breaks every PutItem after a cold start.
"""

import json
import typing
from pathlib import Path

from aws_cdk import RemovalPolicy, Stack
from aws_cdk import aws_apigatewayv2 as apigwv2
from aws_cdk import aws_apigatewayv2_authorizers as apigwv2_authorizers
from aws_cdk import aws_apigatewayv2_integrations as apigwv2_integrations
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from constructs import Construct

# src/shorty relative to this file, not the process cwd -- robust regardless
# of where `cdk`/pytest is invoked from.
_SRC_DIR = Path(__file__).resolve().parent.parent / "src" / "shorty"

# The full AWS-documented CMK action set for DynamoDB access, minus
# kms:CreateGrant (that belongs to the deploying principal, not the runtime
# role). Identical for both functions -- see decisions.md -> D-006.
_KMS_TABLE_KEY_ACTIONS = [
    "kms:Encrypt",
    "kms:Decrypt",
    "kms:ReEncrypt*",
    "kms:GenerateDataKey*",
    "kms:DescribeKey",
]

# A literal wildcard in the Region position, not the deployment Region -- AWS
# requires this permission to stay Region-independent so DynamoDB can make
# cross-Region calls. StringLike (not StringEquals) is required for the "*"
# to be treated as a wildcard rather than a literal character.
_KMS_VIA_DYNAMODB_CONDITION = {"StringLike": {"kms:ViaService": "dynamodb.*.amazonaws.com"}}

_RESERVED_CONCURRENCY = 5


class ShortyAppStack(Stack):
    """The disposable half of Shorty: compute + edge, built on the data stack's outputs."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        table: dynamodb.ITable,
        key: kms.IKey,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.table = table
        self.key = key

        user_pool, user_pool_client = self._build_user_pool()
        create_fn = self._build_function(
            "CreateFn",
            handler="create_handler.handler",
            dynamodb_action="dynamodb:PutItem",
        )
        redirect_fn = self._build_function(
            "RedirectFn",
            handler="redirect_handler.handler",
            dynamodb_action="dynamodb:GetItem",
        )
        self._build_http_api(user_pool, user_pool_client, create_fn, redirect_fn)

    def _build_user_pool(self) -> tuple[cognito.UserPool, cognito.UserPoolClient]:
        # No self-service signup: users are created administratively by the
        # runbook, which is the whole user-management story for this POC
        # (decisions.md -> D-004, D-008).
        user_pool = cognito.UserPool(
            self,
            "UserPool",
            self_sign_up_enabled=False,
            mfa=cognito.Mfa.OPTIONAL,
            mfa_second_factor=cognito.MfaSecondFactor(sms=False, otp=True),
            password_policy=cognito.PasswordPolicy(
                min_length=12,
                require_lowercase=True,
                require_uppercase=True,
                require_digits=True,
                require_symbols=True,
            ),
            account_recovery=cognito.AccountRecovery.EMAIL_ONLY,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # ADMIN_USER_PASSWORD_AUTH only -- the runbook's admin-initiate-auth
        # flow is the sole way to obtain a token in a backend-only system
        # with no UI. No hosted UI, no OAuth flows.
        user_pool_client = user_pool.add_client(
            "UserPoolClient",
            auth_flows=cognito.AuthFlow(admin_user_password=True),
            disable_o_auth=True,
            generate_secret=False,
        )
        return user_pool, user_pool_client

    def _build_function(
        self, construct_id: str, *, handler: str, dynamodb_action: str
    ) -> lambda_.Function:
        role = iam.Role(
            self,
            f"{construct_id}Role",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
        )
        # DynamoDB: exactly one action, scoped to the single table ARN --
        # never grant_write_data()/grant_read_data(), which pull in
        # BatchWriteItem/UpdateItem/DeleteItem or a broader read set than
        # either function needs.
        role.add_to_policy(
            iam.PolicyStatement(
                actions=[dynamodb_action],
                resources=[self.table.table_arn],
            )
        )
        # KMS: same action set on both roles, region-independent ViaService
        # condition. See decisions.md -> D-006 for why a read/write split
        # here is a bug, not an optimization.
        role.add_to_policy(
            iam.PolicyStatement(
                actions=_KMS_TABLE_KEY_ACTIONS,
                resources=[self.key.key_arn],
                conditions=_KMS_VIA_DYNAMODB_CONDITION,
            )
        )

        # Explicit, scoped log group (finding W-1) -- the service-created
        # default never expires and is orphaned by cdk destroy.
        log_group = logs.LogGroup(
            self,
            f"{construct_id}LogGroup",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )

        return lambda_.Function(
            self,
            construct_id,
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler=handler,
            code=lambda_.Code.from_asset(str(_SRC_DIR)),
            role=role,
            log_group=log_group,
            environment={"TABLE_NAME": self.table.table_name},
            reserved_concurrent_executions=_RESERVED_CONCURRENCY,
        )

    def _build_http_api(
        self,
        user_pool: cognito.UserPool,
        user_pool_client: cognito.UserPoolClient,
        create_fn: lambda_.Function,
        redirect_fn: lambda_.Function,
    ) -> None:
        jwt_authorizer = apigwv2_authorizers.HttpJwtAuthorizer(
            "JwtAuthorizer",
            jwt_issuer=user_pool.user_pool_provider_url,
            jwt_audience=[user_pool_client.user_pool_client_id],
        )

        # No create_default_stage: the default stage is built explicitly
        # below so it can carry throttling and access logging.
        http_api = apigwv2.HttpApi(self, "HttpApi", create_default_stage=False)

        # POST /links -- JWT-authorized, so every minted link is
        # attributable to an administratively-created identity.
        http_api.add_routes(
            path="/links",
            methods=[apigwv2.HttpMethod.POST],
            integration=apigwv2_integrations.HttpLambdaIntegration(
                "CreateIntegration", create_fn
            ),
            authorizer=jwt_authorizer,
        )
        # GET /{code} -- deliberately unauthenticated (spec Constraint 1): a
        # browser following a short link carries no credentials.
        http_api.add_routes(
            path="/{code}",
            methods=[apigwv2.HttpMethod.GET],
            integration=apigwv2_integrations.HttpLambdaIntegration(
                "RedirectIntegration", redirect_fn
            ),
        )

        access_log_group = logs.LogGroup(
            self,
            "AccessLogGroup",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )
        access_log_format = json.dumps(
            {
                "requestId": "$context.requestId",
                "ip": "$context.identity.sourceIp",
                "requestTime": "$context.requestTime",
                "httpMethod": "$context.httpMethod",
                "routeKey": "$context.routeKey",
                "status": "$context.status",
                "protocol": "$context.protocol",
                "responseLength": "$context.responseLength",
            }
        )

        # Stage-level throttling is the primary compensating control for the
        # unauthenticated redirect route (decisions.md -> D-002).
        #
        # access_log_settings is deliberately NOT passed to the HttpStage L2
        # constructor: `IAccessLogSettings` is a jsii behavioral interface,
        # not a data struct, and the Python jsii kernel in aws-cdk-lib
        # 2.263.0 rejects a plain dict for it ("does not have the
        # $jsii.byref key") despite the construct's own docstring examples
        # showing that exact shape. Setting it on the underlying L1 CfnStage
        # is the documented escape hatch for this class of gap and produces
        # an identical synthesized template.
        stage = apigwv2.HttpStage(
            self,
            "DefaultStage",
            http_api=http_api,
            stage_name="$default",
            auto_deploy=True,
            throttle=apigwv2.ThrottleSettings(rate_limit=50, burst_limit=100),
        )
        cfn_stage = typing.cast(apigwv2.CfnStage, stage.node.default_child)
        cfn_stage.access_log_settings = apigwv2.CfnStage.AccessLogSettingsProperty(
            destination_arn=access_log_group.log_group_arn,
            format=access_log_format,
        )
        # Mirrors what LogGroupLogDestination.bind() would have wired had
        # the L2 accepted it: API Gateway needs write access to the
        # destination log group.
        access_log_group.grant_write(iam.ServicePrincipal("apigateway.amazonaws.com"))
