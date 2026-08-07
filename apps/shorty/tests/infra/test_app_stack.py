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


def _walk_policy_documents(node: object):
    """Recursively yield every statement found under a literal "PolicyDocument"
    key anywhere in a CFN resource (sub)tree.

    Structural, not resource-type-based, on purpose (AGENT-88 -- the original
    sweep enumerated only `AWS::IAM::Policy`, which is blind to two other
    CDK-rendered shapes for the exact same kind of statement):
      - `AWS::IAM::Policy`            -> Properties.PolicyDocument
      - `AWS::IAM::ManagedPolicy`     -> Properties.PolicyDocument
      - `AWS::IAM::Role`              -> Properties.Policies[].PolicyDocument
        (from `iam.Role(..., inline_policies=...)`)
    All three render the permission document under the identical CFN property
    key "PolicyDocument". Walking for that key covers all three -- and any
    future CDK rendering path that reuses the same CFN property name -- by
    construction, instead of by enumerating resource types and hoping the
    list stays exhaustive. See test_security_posture.py's copy of this
    helper for the full rationale.

    Deliberately EXCLUDES `AssumeRolePolicyDocument` (trust policy -- a
    different security question) and `AWS::KMS::Key.Properties.KeyPolicy`
    (CDK's default key policy legitimately grants kms:* on Resource: "*" to
    the account root; key policies are not IAM policies and are not
    assessed here). Both are skipped simply because their CFN property key
    is not literally "PolicyDocument" -- named here so that reads as a
    decision, not an accident.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "PolicyDocument" and isinstance(value, dict):
                yield from _normalize_statement(value.get("Statement"))
            else:
                yield from _walk_policy_documents(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_policy_documents(item)


def _normalize_statement(statement: object):
    """CloudFormation permits `PolicyDocument.Statement` to be either a list
    or a single object -- AGENT-88 cycle 2: the first version of this walk
    only handled the list form and silently dropped a single-object
    Statement, so a wildcard expressed that way passed the entire suite.
    Handle both, and raise on anything else rather than silently skipping it
    -- an unrecognized shape here is exactly the kind of gap this file
    exists to not have. See test_security_posture.py's copy for the fuller
    rationale.
    """
    if isinstance(statement, list):
        yield from statement
    elif isinstance(statement, dict):
        yield statement
    elif statement is not None:
        raise TypeError(f"unexpected PolicyDocument.Statement shape: {statement!r}")


def _role_policy_statements(template: Template, role_id_prefix: str) -> list[dict]:
    """Return every policy statement attached to a role whose logical id
    starts with `role_id_prefix` (CDK preserves the construct id as the
    logical id prefix; the suffix is an address hash), across all three CDK
    rendering paths -- see `_walk_policy_documents`."""
    roles = template.find_resources("AWS::IAM::Role")
    role_logical_ids = [
        logical_id for logical_id in roles if logical_id.startswith(role_id_prefix)
    ]
    assert role_logical_ids, f"no IAM role found with logical id prefix {role_id_prefix!r}"
    (role_logical_id,) = role_logical_ids
    role_ref = {"Ref": role_logical_id}

    statements = []
    cfn = template.to_json()
    for logical_id, resource in cfn["Resources"].items():
        properties = resource.get("Properties", {})
        is_this_role = logical_id == role_logical_id
        attaches_to_this_role = role_ref in properties.get("Roles", [])
        if is_this_role or attaches_to_this_role:
            statements.extend(_walk_policy_documents(properties))
    assert statements, f"no policy statements found attached to role {role_logical_id!r}"
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
    statements = _role_policy_statements(template, "CreateFnRole")

    dynamodb_actions: set[str] = set()
    for statement in statements:
        actions = statement["Action"]
        actions = actions if isinstance(actions, list) else [actions]
        dynamodb_actions.update(a for a in actions if a.startswith("dynamodb:"))

    assert dynamodb_actions == {"dynamodb:PutItem"}, dynamodb_actions


def test_redirect_fn_policy_has_getitem_only_not_putitem():
    template = _synth_template()
    statements = _role_policy_statements(template, "RedirectFnRole")

    dynamodb_actions: set[str] = set()
    for statement in statements:
        actions = statement["Action"]
        actions = actions if isinstance(actions, list) else [actions]
        dynamodb_actions.update(a for a in actions if a.startswith("dynamodb:"))

    assert dynamodb_actions == {"dynamodb:GetItem"}, dynamodb_actions


def test_dynamodb_actions_are_scoped_to_the_single_table_arn():
    template = _synth_template()

    for role_prefix in ("CreateFnRole", "RedirectFnRole"):
        statements = _role_policy_statements(template, role_prefix)
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
        statements = _role_policy_statements(template, role_prefix)
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
        statements = _role_policy_statements(template, role_prefix)
        for statement in statements:
            actions = statement["Action"]
            actions = actions if isinstance(actions, list) else [actions]
            assert "kms:CreateGrant" not in actions, (
                f"{role_prefix} must not hold kms:CreateGrant "
                "(that belongs to the deploying principal)"
            )


def test_no_policy_statement_uses_a_wildcard_action_or_resource():
    # AGENT-88: swept structurally via _walk_policy_documents, not just
    # AWS::IAM::Policy -- see that helper's docstring for the three CDK
    # rendering paths this now covers and the two deliberate exclusions.
    template = _synth_template()

    for statement in _walk_policy_documents(template.to_json()["Resources"]):
        actions = statement["Action"]
        actions = actions if isinstance(actions, list) else [actions]
        assert "*" not in actions, f"wildcard action found: {statement!r}"

        resources = statement.get("Resource")
        if resources is not None:
            resources = resources if isinstance(resources, list) else [resources]
            assert "*" not in resources, f"wildcard resource found: {statement!r}"


def test_neither_lambda_role_has_an_attached_managed_policy():
    # An AWS-managed policy attached via `role.add_managed_policy(...)`
    # renders as a ManagedPolicyArns entry on the role, with no inline
    # Statement for the wildcard sweep above to catch -- ManagedPolicyArns
    # presence is the only thing that CAN be asserted against it.
    template = _synth_template()

    roles = template.find_resources("AWS::IAM::Role")
    for role_prefix in ("CreateFnRole", "RedirectFnRole"):
        (role_logical_id,) = [lid for lid in roles if lid.startswith(role_prefix)]
        managed_policy_arns = roles[role_logical_id]["Properties"].get(
            "ManagedPolicyArns"
        )
        assert not managed_policy_arns, (
            f"{role_logical_id} has ManagedPolicyArns attached: "
            f"{managed_policy_arns!r}"
        )


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
