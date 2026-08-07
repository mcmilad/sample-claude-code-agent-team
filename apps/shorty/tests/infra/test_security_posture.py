"""Whole-system security posture for Shorty (AGENT-83).

design.md#security-considerations IS this file, expressed as executable
assertions (spec NF3). Per-stack tests (test_data_stack.py, test_app_stack.py)
check that each stack does its own job; this file checks the assembled
system -- both stacks wired exactly as app.py wires them -- against
.claude/rules/AWS-security-guidelines.md. Overlap with the per-stack tests is
expected and intentional: this is the file a security reviewer reads top to
bottom as a checklist.

Synthesizes via the real app.py entrypoint (not a fresh `cdk.App()` built
ad hoc), because the whole point of the tag-inheritance tests below is to
prove `cdk.Tags.of(app).add(...)` in app.py actually reaches every resource
-- a test that builds its own App/stacks and skips the app.py tagging calls
would pass even if that inheritance were broken.
"""

import importlib
import re
from pathlib import Path

from aws_cdk.assertions import Match, Template

# Import app.py itself (module name "app", the file at apps/shorty/app.py) so
# every assertion below runs against the exact object graph the real
# entrypoint builds -- including the App-level tags, which only exist on
# resources reached through `app.py`, not through a fresh Stack built by hand.
_shorty_app = importlib.import_module("app")

_DATA_TEMPLATE = Template.from_stack(_shorty_app.data_stack)
_APP_TEMPLATE = Template.from_stack(_shorty_app.app_stack)

_REQUIRED_TAGS = {
    "service": "shorty",
    "environment": "poc",
    "owner": "shorty-maintainers",
    "data-classification": "internal",
    "cost-center": "shorty-poc",
}

_CORRECTED_KMS_ACTIONS = {
    "kms:Encrypt",
    "kms:Decrypt",
    "kms:ReEncrypt*",
    "kms:GenerateDataKey*",
    "kms:DescribeKey",
}

_WORKFLOW_PATH = Path(__file__).resolve().parents[4] / ".github" / "workflows" / "shorty.yml"

# Matches AWS account ids (12 digits) and AWS region strings (e.g.
# us-east-1) as they would appear if someone had hardcoded one instead of
# using a CFN pseudo parameter / unresolved token.
_ACCOUNT_ID_RE = re.compile(r"\b\d{12}\b")
_REGION_RE = re.compile(r"\b(?:us|eu|ap|ca|sa|me|af|il|cn)-[a-z]+-\d\b")


def _all_policy_statements() -> list[dict]:
    statements = []
    for template in (_DATA_TEMPLATE, _APP_TEMPLATE):
        for policy in template.find_resources("AWS::IAM::Policy").values():
            statements.extend(policy["Properties"]["PolicyDocument"]["Statement"])
    return statements


def _role_policy_statements(role_id_prefix: str) -> list[dict]:
    roles = _APP_TEMPLATE.find_resources("AWS::IAM::Role")
    (role_logical_id,) = [lid for lid in roles if lid.startswith(role_id_prefix)]

    statements = []
    for policy in _APP_TEMPLATE.find_resources("AWS::IAM::Policy").values():
        if {"Ref": role_logical_id} in policy["Properties"].get("Roles", []):
            statements.extend(policy["Properties"]["PolicyDocument"]["Statement"])
    return statements


def _actions(statement: dict) -> list[str]:
    actions = statement["Action"]
    return actions if isinstance(actions, list) else [actions]


# --- Data (tier 3) -- AWS-security-guidelines.md -> DynamoDB, Amazon EBS N/A -----


def test_table_is_encrypted_with_a_customer_managed_key_not_the_aws_owned_default():
    _DATA_TEMPLATE.has_resource_properties(
        "AWS::DynamoDB::Table",
        {
            "SSESpecification": {
                "SSEEnabled": True,
                "SSEType": "KMS",
                "KMSMasterKeyId": Match.any_value(),
            }
        },
    )
    (table,) = _DATA_TEMPLATE.find_resources("AWS::DynamoDB::Table").values()
    kms_master_key_id = table["Properties"]["SSESpecification"]["KMSMasterKeyId"]
    assert "Fn::GetAtt" in kms_master_key_id, (
        "table must reference the CMK resource directly, not an AWS-owned "
        f"default key: {kms_master_key_id!r}"
    )


def test_kms_key_rotation_is_enabled():
    _DATA_TEMPLATE.has_resource_properties(
        "AWS::KMS::Key", {"EnableKeyRotation": True}
    )


def test_table_point_in_time_recovery_is_enabled():
    _DATA_TEMPLATE.has_resource_properties(
        "AWS::DynamoDB::Table",
        {"PointInTimeRecoverySpecification": {"PointInTimeRecoveryEnabled": True}},
    )


def test_table_deletion_protection_is_enabled():
    _DATA_TEMPLATE.has_resource_properties(
        "AWS::DynamoDB::Table", {"DeletionProtectionEnabled": True}
    )


def test_table_and_key_are_retained_not_destroyed_on_stack_deletion():
    _DATA_TEMPLATE.has_resource(
        "AWS::DynamoDB::Table",
        {"DeletionPolicy": "Retain", "UpdateReplacePolicy": "Retain"},
    )
    _DATA_TEMPLATE.has_resource(
        "AWS::KMS::Key",
        {"DeletionPolicy": "Retain", "UpdateReplacePolicy": "Retain"},
    )


# --- Tags -- AWS-security-guidelines.md -> General Requirements ------------------


def test_every_taggable_resource_carries_all_five_required_tags():
    checked_any = False
    for template in (_DATA_TEMPLATE, _APP_TEMPLATE):
        cfn_template = template.to_json()
        for logical_id, resource in cfn_template["Resources"].items():
            tags = resource["Properties"].get("Tags")
            if tags is None:
                continue
            checked_any = True
            # Most L1 resource types render Tags as a CFN Tag list
            # ([{"Key": ..., "Value": ...}, ...]); a few (e.g.
            # AWS::ApiGatewayV2::Api/Stage) render it as a plain key/value
            # map instead. Normalize both to a dict before comparing.
            actual_tags = (
                {t["Key"]: t["Value"] for t in tags}
                if isinstance(tags, list)
                else tags
            )
            assert actual_tags == _REQUIRED_TAGS, (
                f"{logical_id} ({resource['Type']}) tags: {actual_tags!r}, "
                f"expected the five App-level tags: {_REQUIRED_TAGS!r}"
            )
    assert checked_any, "no taggable resource found in either template"


# --- Compute (tier 2) -- AWS-security-guidelines.md -> AWS Lambda ----------------


def test_exactly_two_lambda_functions_each_with_a_different_execution_role():
    functions = _APP_TEMPLATE.find_resources("AWS::Lambda::Function")
    assert len(functions) == 2

    role_refs = {
        lid: fn["Properties"]["Role"]["Fn::GetAtt"][0] for lid, fn in functions.items()
    }
    assert len(set(role_refs.values())) == 2, (
        f"functions must never share an execution role, got {role_refs!r}"
    )


def test_both_functions_have_reserved_concurrency_set():
    functions = _APP_TEMPLATE.find_resources("AWS::Lambda::Function")
    for logical_id, fn in functions.items():
        assert fn["Properties"].get("ReservedConcurrentExecutions") is not None, (
            f"{logical_id} has no ReservedConcurrentExecutions"
        )


def test_both_functions_have_an_explicit_log_group_not_a_service_created_one():
    # Finding W-1: a service-created default LogGroup never expires and is
    # orphaned by cdk destroy. Each function must reference its OWN
    # explicit logs.LogGroup resource with retention configured.
    log_groups = _APP_TEMPLATE.find_resources("AWS::Logs::LogGroup")
    fn_log_groups = {
        lid: lg
        for lid, lg in log_groups.items()
        if lid.startswith(("CreateFnLogGroup", "RedirectFnLogGroup"))
    }
    assert len(fn_log_groups) == 2
    for logical_id, log_group in fn_log_groups.items():
        assert log_group["Properties"].get("RetentionInDays") is not None, (
            f"{logical_id} has no retention configured"
        )


def test_neither_function_carries_any_env_var_other_than_table_name():
    functions = _APP_TEMPLATE.find_resources("AWS::Lambda::Function")
    for logical_id, fn in functions.items():
        variables = fn["Properties"]["Environment"]["Variables"]
        assert set(variables.keys()) == {"TABLE_NAME"}, (
            f"{logical_id} environment: {list(variables.keys())!r}"
        )


def test_dynamodb_wildcard_sweep_no_policy_grants_star_or_dynamodb_star():
    for statement in _all_policy_statements():
        actions = _actions(statement)
        assert "*" not in actions, f"wildcard action found: {statement!r}"
        assert "dynamodb:*" not in actions, f"dynamodb:* found: {statement!r}"


def test_resource_wildcard_sweep_no_policy_statement_targets_resource_star():
    for statement in _all_policy_statements():
        resources = statement.get("Resource")
        if resources is None:
            continue
        resources = resources if isinstance(resources, list) else [resources]
        assert "*" not in resources, f"Resource: * found: {statement!r}"


def test_create_role_holds_putitem_only_not_getitem():
    statements = _role_policy_statements("CreateFnRole")
    dynamodb_actions = {
        a for s in statements for a in _actions(s) if a.startswith("dynamodb:")
    }
    assert dynamodb_actions == {"dynamodb:PutItem"}, dynamodb_actions


def test_redirect_role_holds_getitem_only_not_putitem():
    statements = _role_policy_statements("RedirectFnRole")
    dynamodb_actions = {
        a for s in statements for a in _actions(s) if a.startswith("dynamodb:")
    }
    assert dynamodb_actions == {"dynamodb:GetItem"}, dynamodb_actions


def test_dynamodb_actions_are_scoped_to_the_single_table_arn_not_a_wildcard():
    for role_prefix in ("CreateFnRole", "RedirectFnRole"):
        for statement in _role_policy_statements(role_prefix):
            if any(a.startswith("dynamodb:") for a in _actions(statement)):
                resources = statement["Resource"]
                resources = resources if isinstance(resources, list) else [resources]
                assert resources != ["*"], f"{role_prefix}: {statement!r}"


def test_both_roles_hold_the_decisions_d006_corrected_kms_action_set():
    # decisions.md -> D-006: an earlier design split KMS actions read/write,
    # which is broken (see the Decrypt test below for why). Both roles must
    # hold the SAME full AWS-documented set, minus CreateGrant.
    for role_prefix in ("CreateFnRole", "RedirectFnRole"):
        statements = _role_policy_statements(role_prefix)
        kms_statements = [
            s for s in statements if any(a.startswith("kms:") for a in _actions(s))
        ]
        assert len(kms_statements) == 1, f"{role_prefix}: {kms_statements!r}"
        (kms_statement,) = kms_statements
        assert set(_actions(kms_statement)) == _CORRECTED_KMS_ACTIONS, (
            f"{role_prefix} KMS actions: {_actions(kms_statement)!r}"
        )


def test_kms_via_service_condition_is_region_independent_not_pinned():
    # The ViaService Region position must be a literal "*", not the
    # deployment Region -- AWS requires the permission to be
    # Region-independent so DynamoDB can make cross-Region calls. Pinning
    # the Region is the other natural-looking mistake here (decisions.md ->
    # D-006).
    for role_prefix in ("CreateFnRole", "RedirectFnRole"):
        statements = _role_policy_statements(role_prefix)
        (kms_statement,) = [
            s for s in statements if any(a.startswith("kms:") for a in _actions(s))
        ]
        assert kms_statement.get("Condition") == {
            "StringLike": {"kms:ViaService": "dynamodb.*.amazonaws.com"}
        }, f"{role_prefix} condition: {kms_statement.get('Condition')!r}"


def test_neither_role_holds_kms_create_grant():
    # kms:CreateGrant belongs to the deploying principal, never a runtime
    # Lambda role (decisions.md -> D-006).
    for role_prefix in ("CreateFnRole", "RedirectFnRole"):
        for statement in _role_policy_statements(role_prefix):
            assert "kms:CreateGrant" not in _actions(statement), (
                f"{role_prefix}: {statement!r}"
            )


def test_create_role_holds_kms_decrypt_or_every_putitem_fails_after_an_idle_gap():
    # THE assertion that would have caught the original defect
    # (decisions.md -> D-006, finding from AGENT-76). DynamoDB caches the
    # plaintext table key per calling principal and re-requests it with a
    # Decrypt call after roughly five minutes of inactivity -- so the
    # WRITER needs Decrypt too, not just GenerateDataKey. Without it,
    # create_fn's PutItem fails 100% of the time after a cold start or any
    # idle gap past the cache TTL.
    statements = _role_policy_statements("CreateFnRole")
    (kms_statement,) = [
        s for s in statements if any(a.startswith("kms:") for a in _actions(s))
    ]
    assert "kms:Decrypt" in _actions(kms_statement), (
        "create_fn's role is missing kms:Decrypt -- every PutItem will fail "
        "after a cold start or a five-minute idle gap once DynamoDB's "
        "per-principal table-key cache expires (decisions.md D-006)"
    )


# --- Edge (tier 1) -- AWS-security-guidelines.md -> Amazon API Gateway -----------


def test_jwt_authorizer_exists_bound_to_the_user_pool_issuer():
    _APP_TEMPLATE.resource_count_is("AWS::ApiGatewayV2::Authorizer", 1)
    _APP_TEMPLATE.has_resource_properties(
        "AWS::ApiGatewayV2::Authorizer",
        {
            "AuthorizerType": "JWT",
            "JwtConfiguration": Match.object_like(
                {"Issuer": Match.any_value(), "Audience": Match.any_value()}
            ),
        },
    )


def test_post_links_route_references_the_jwt_authorizer():
    (authorizer_logical_id,) = _APP_TEMPLATE.find_resources(
        "AWS::ApiGatewayV2::Authorizer"
    ).keys()
    routes = _APP_TEMPLATE.find_resources("AWS::ApiGatewayV2::Route")
    (post_route,) = [
        r for r in routes.values() if r["Properties"]["RouteKey"] == "POST /links"
    ]
    assert post_route["Properties"]["AuthorizerId"] == {"Ref": authorizer_logical_id}


def test_get_code_route_authorization_type_is_none_not_merely_absent():
    # Required NEGATIVE assertion (spec Constraint 1, F5): a browser
    # following a short link carries no credentials. Asserting NONE
    # explicitly is what stops a later change from quietly authenticating
    # this route and breaking F2.
    routes = _APP_TEMPLATE.find_resources("AWS::ApiGatewayV2::Route")
    (get_route,) = [
        r for r in routes.values() if r["Properties"]["RouteKey"] == "GET /{code}"
    ]
    assert get_route["Properties"].get("AuthorizationType", "NONE") == "NONE"
    assert "AuthorizerId" not in get_route["Properties"]


def test_default_stage_has_explicit_throttling_limits():
    _APP_TEMPLATE.has_resource_properties(
        "AWS::ApiGatewayV2::Stage",
        {
            "DefaultRouteSettings": Match.object_like(
                {
                    "ThrottlingRateLimit": Match.any_value(),
                    "ThrottlingBurstLimit": Match.any_value(),
                }
            )
        },
    )


def test_default_stage_has_access_log_destination_and_format_configured():
    _APP_TEMPLATE.has_resource_properties(
        "AWS::ApiGatewayV2::Stage",
        {
            "AccessLogSettings": Match.object_like(
                {"DestinationArn": Match.any_value(), "Format": Match.any_value()}
            )
        },
    )


# --- Identity -- AWS-security-guidelines.md -> AWS Amplify Gen 2 / Cognito -------


def test_cognito_self_signup_is_disabled():
    # decisions.md -> D-008: no self-service signup means every link is
    # attributable to an administratively-created identity.
    _APP_TEMPLATE.has_resource_properties(
        "AWS::Cognito::UserPool",
        {"AdminCreateUserConfig": {"AllowAdminCreateUserOnly": True}},
    )


def test_cognito_password_policy_meets_the_minimum_bar():
    _APP_TEMPLATE.has_resource_properties(
        "AWS::Cognito::UserPool",
        {
            "Policies": {
                "PasswordPolicy": {
                    "MinimumLength": Match.any_value(),
                    "RequireLowercase": True,
                    "RequireUppercase": True,
                    "RequireNumbers": True,
                    "RequireSymbols": True,
                }
            }
        },
    )
    (pool,) = _APP_TEMPLATE.find_resources("AWS::Cognito::UserPool").values()
    min_length = pool["Properties"]["Policies"]["PasswordPolicy"]["MinimumLength"]
    assert min_length >= 12, f"password MinimumLength: {min_length!r}"


def test_cognito_account_recovery_is_email_only():
    _APP_TEMPLATE.has_resource_properties(
        "AWS::Cognito::UserPool",
        {
            "AccountRecoverySetting": {
                "RecoveryMechanisms": [{"Name": "verified_email", "Priority": 1}]
            }
        },
    )


# --- NF1 guard -- the never-deploy constraint is the easiest one to break -------


def test_neither_template_contains_a_hardcoded_account_id_or_region():
    for template in (_DATA_TEMPLATE, _APP_TEMPLATE):
        rendered = str(template.to_json())
        assert not _ACCOUNT_ID_RE.search(rendered), (
            "template contains what looks like a hardcoded 12-digit "
            "account id -- NF1 requires zero account/region literals"
        )
        assert not _REGION_RE.search(rendered), (
            "template contains what looks like a hardcoded AWS region "
            "literal -- NF1 requires the app to stay region-agnostic"
        )


def test_ci_workflow_has_no_secrets_reference():
    workflow_text = _WORKFLOW_PATH.read_text()
    assert "secrets." not in workflow_text, (
        "shorty.yml must never reference a GitHub secret -- the agent team "
        "does not deploy (NF1)"
    )


def test_ci_workflow_has_no_configure_aws_credentials_step():
    workflow_text = _WORKFLOW_PATH.read_text()
    assert "configure-aws-credentials" not in workflow_text, (
        "shorty.yml must never assume an AWS credentials role -- the agent "
        "team does not deploy (NF1)"
    )


def test_ci_workflow_has_no_deploy_job():
    workflow_text = _WORKFLOW_PATH.read_text()
    assert "cdk deploy" not in workflow_text, (
        "shorty.yml must never run cdk deploy -- the agent team does not "
        "deploy (NF1)"
    )
