"""Template assertions for ShortyAppStack (AGENT-80).

Asserts against the synthesized CloudFormation template, not the Python
construct objects. See design.md#security-considerations (Compute, Edge,
Identity) and decisions.md -> D-006 for why the KMS action set is asserted
identical on both roles rather than split by read/write.
"""

import aws_cdk as cdk
from aws_cdk.assertions import Match, Template

from shorty_infra.app_stack import ShortyAppStack
from shorty_infra.data_stack import ShortyDataStack

_KMS_ACTIONS = Match.array_with(
    [
        "kms:Encrypt",
        "kms:Decrypt",
        "kms:ReEncrypt*",
        "kms:GenerateDataKey*",
        "kms:DescribeKey",
    ]
)


def _synth_template() -> Template:
    app = cdk.App()
    data_stack = ShortyDataStack(app, "TestShortyDataStack")
    app_stack = ShortyAppStack(
        app,
        "TestShortyAppStack",
        table=data_stack.table,
        key=data_stack.key,
    )
    return Template.from_stack(app_stack)


def _iam_policies_for_role(template: Template, role_id_prefix: str) -> list[dict]:
    """Return the inline AWS::IAM::Policy documents attached to a role whose
    logical id starts with `role_id_prefix` (CDK preserves the construct id
    as the logical id prefix; the suffix is an address hash)."""
    roles = template.find_resources("AWS::IAM::Role")
    role_logical_ids = [
        logical_id for logical_id in roles if logical_id.startswith(role_id_prefix)
    ]
    assert role_logical_ids, f"no IAM role found with logical id prefix {role_id_prefix!r}"
    (role_logical_id,) = role_logical_ids

    policies = template.find_resources("AWS::IAM::Policy")
    attached = [
        policy["Properties"]["PolicyDocument"]
        for policy in policies.values()
        if {"Ref": role_logical_id} in policy["Properties"].get("Roles", [])
    ]
    assert attached, f"no inline policy attached to role {role_logical_id!r}"
    return attached


def _statements(policy_documents: list[dict]) -> list[dict]:
    statements = []
    for document in policy_documents:
        statements.extend(document["Statement"])
    return statements


def test_two_distinct_lambda_execution_roles_never_shared():
    template = _synth_template()

    roles = template.find_resources("AWS::IAM::Role")
    create_roles = [lid for lid in roles if lid.startswith("CreateFnRole")]
    redirect_roles = [lid for lid in roles if lid.startswith("RedirectFnRole")]
    assert len(create_roles) == 1
    assert len(redirect_roles) == 1
    assert create_roles[0] != redirect_roles[0]

    functions = template.find_resources("AWS::Lambda::Function")
    role_refs = {
        logical_id: fn["Properties"]["Role"]["Fn::GetAtt"][0]
        for logical_id, fn in functions.items()
    }
    assert len(set(role_refs.values())) == 2, (
        f"expected each Lambda to reference a distinct role, got {role_refs!r}"
    )


def test_create_fn_policy_has_putitem_only_no_other_write_or_read_actions():
    template = _synth_template()
    statements = _statements(_iam_policies_for_role(template, "CreateFnRole"))

    dynamodb_actions: set[str] = set()
    for statement in statements:
        actions = statement["Action"]
        actions = actions if isinstance(actions, list) else [actions]
        dynamodb_actions.update(a for a in actions if a.startswith("dynamodb:"))

    assert dynamodb_actions == {"dynamodb:PutItem"}, dynamodb_actions


def test_redirect_fn_policy_has_getitem_only_not_putitem():
    template = _synth_template()
    statements = _statements(_iam_policies_for_role(template, "RedirectFnRole"))

    dynamodb_actions: set[str] = set()
    for statement in statements:
        actions = statement["Action"]
        actions = actions if isinstance(actions, list) else [actions]
        dynamodb_actions.update(a for a in actions if a.startswith("dynamodb:"))

    assert dynamodb_actions == {"dynamodb:GetItem"}, dynamodb_actions


def test_dynamodb_actions_are_scoped_to_the_single_table_arn():
    template = _synth_template()

    for role_prefix in ("CreateFnRole", "RedirectFnRole"):
        statements = _statements(_iam_policies_for_role(template, role_prefix))
        for statement in statements:
            actions = statement["Action"]
            actions = actions if isinstance(actions, list) else [actions]
            if any(a.startswith("dynamodb:") for a in actions):
                resources = statement["Resource"]
                resources = resources if isinstance(resources, list) else [resources]
                assert resources != ["*"], f"{role_prefix} dynamodb statement has Resource *"


def test_both_roles_carry_the_corrected_kms_action_set_with_via_service_condition():
    template = _synth_template()

    for role_prefix in ("CreateFnRole", "RedirectFnRole"):
        statements = _statements(_iam_policies_for_role(template, role_prefix))
        kms_statements = [
            s
            for s in statements
            if any(
                a.startswith("kms:")
                for a in (s["Action"] if isinstance(s["Action"], list) else [s["Action"]])
            )
        ]
        assert kms_statements, f"{role_prefix} has no KMS statement"
        (kms_statement,) = kms_statements

        actions = kms_statement["Action"]
        actions = actions if isinstance(actions, list) else [actions]
        assert set(actions) == {
            "kms:Encrypt",
            "kms:Decrypt",
            "kms:ReEncrypt*",
            "kms:GenerateDataKey*",
            "kms:DescribeKey",
        }, f"{role_prefix} KMS action set: {actions!r}"

        condition = kms_statement.get("Condition", {})
        assert condition == {
            "StringLike": {"kms:ViaService": "dynamodb.*.amazonaws.com"}
        }, f"{role_prefix} KMS condition: {condition!r}"


def test_neither_role_holds_kms_create_grant():
    template = _synth_template()

    for role_prefix in ("CreateFnRole", "RedirectFnRole"):
        statements = _statements(_iam_policies_for_role(template, role_prefix))
        for statement in statements:
            actions = statement["Action"]
            actions = actions if isinstance(actions, list) else [actions]
            assert "kms:CreateGrant" not in actions, (
                f"{role_prefix} must not hold kms:CreateGrant "
                "(that belongs to the deploying principal)"
            )


def test_no_policy_statement_uses_a_wildcard_action_or_resource():
    template = _synth_template()

    policies = template.find_resources("AWS::IAM::Policy")
    for policy in policies.values():
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            actions = statement["Action"]
            actions = actions if isinstance(actions, list) else [actions]
            assert "*" not in actions, f"wildcard action found: {statement!r}"

            resources = statement.get("Resource")
            if resources is not None:
                resources = resources if isinstance(resources, list) else [resources]
                assert "*" not in resources, f"wildcard resource found: {statement!r}"


def test_each_lambda_has_an_explicit_log_group_with_one_month_retention():
    template = _synth_template()

    log_groups = template.find_resources("AWS::Logs::LogGroup")
    create_log_groups = [lid for lid in log_groups if lid.startswith("CreateFnLogGroup")]
    redirect_log_groups = [
        lid for lid in log_groups if lid.startswith("RedirectFnLogGroup")
    ]
    assert len(create_log_groups) == 1
    assert len(redirect_log_groups) == 1

    for logical_id in (*create_log_groups, *redirect_log_groups):
        retention = log_groups[logical_id]["Properties"]["RetentionInDays"]
        assert retention == 30, f"{logical_id} retention: {retention!r}"


def test_reserved_concurrency_set_on_both_functions():
    template = _synth_template()

    functions = template.find_resources("AWS::Lambda::Function")
    assert len(functions) == 2
    for props in functions.values():
        assert props["Properties"]["ReservedConcurrentExecutions"] is not None


def test_jwt_authorizer_bound_to_user_pool_is_attached_to_post_links_only():
    template = _synth_template()

    template.resource_count_is("AWS::ApiGatewayV2::Authorizer", 1)
    template.has_resource_properties(
        "AWS::ApiGatewayV2::Authorizer",
        {
            "AuthorizerType": "JWT",
            "JwtConfiguration": Match.object_like({"Audience": Match.any_value()}),
        },
    )

    authorizers = template.find_resources("AWS::ApiGatewayV2::Authorizer")
    (authorizer_logical_id,) = authorizers.keys()

    routes = template.find_resources("AWS::ApiGatewayV2::Route")
    post_routes = [r for r in routes.values() if r["Properties"]["RouteKey"] == "POST /links"]
    assert len(post_routes) == 1
    assert post_routes[0]["Properties"]["AuthorizerId"] == {"Ref": authorizer_logical_id}


def test_get_code_route_has_no_authorizer():
    template = _synth_template()

    routes = template.find_resources("AWS::ApiGatewayV2::Route")
    get_routes = [r for r in routes.values() if r["Properties"]["RouteKey"] == "GET /{code}"]
    assert len(get_routes) == 1
    route_props = get_routes[0]["Properties"]

    # A route with no authorizer either omits AuthorizationType or sets it to
    # NONE -- either way, no AuthorizerId may be present.
    assert route_props.get("AuthorizationType", "NONE") == "NONE"
    assert "AuthorizerId" not in route_props


def test_default_stage_has_throttling_and_access_logging():
    template = _synth_template()

    template.has_resource_properties(
        "AWS::ApiGatewayV2::Stage",
        {
            "StageName": "$default",
            "DefaultRouteSettings": Match.object_like(
                {
                    "ThrottlingRateLimit": Match.any_value(),
                    "ThrottlingBurstLimit": Match.any_value(),
                }
            ),
            "AccessLogSettings": Match.object_like(
                {"DestinationArn": Match.any_value(), "Format": Match.any_value()}
            ),
        },
    )


def test_no_env_or_account_region_context_used_by_the_stack():
    # NF1: the app stack must synth with zero AWS credentials -- Environment
    # must be left unresolved (CDK::Environment token), not a literal.
    app = cdk.App()
    data_stack = ShortyDataStack(app, "TestShortyDataStack")
    app_stack = ShortyAppStack(
        app,
        "TestShortyAppStack",
        table=data_stack.table,
        key=data_stack.key,
    )
    assert cdk.Token.is_unresolved(app_stack.account)
    assert cdk.Token.is_unresolved(app_stack.region)


def test_stack_exposes_table_and_key_attributes():
    app = cdk.App()
    data_stack = ShortyDataStack(app, "TestShortyDataStack")
    app_stack = ShortyAppStack(
        app,
        "TestShortyAppStack",
        table=data_stack.table,
        key=data_stack.key,
    )

    assert app_stack.table is data_stack.table
    assert app_stack.key is data_stack.key
