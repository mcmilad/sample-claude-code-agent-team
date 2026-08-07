"""Template assertions for ShortyDataStack (AGENT-79).

Asserts against the synthesized CloudFormation template, not the Python
construct objects -- a construct property that fails to reach CloudFormation
is exactly the bug these tests exist to catch. See
design.md#security-considerations (Data tier).
"""

import aws_cdk as cdk
from aws_cdk.assertions import Match, Template

from shorty_infra.data_stack import ShortyDataStack


def _synth_template() -> Template:
    app = cdk.App()
    stack = ShortyDataStack(app, "TestShortyDataStack")
    return Template.from_stack(stack)


def test_table_is_encrypted_with_the_customer_managed_key_not_an_aws_owned_key():
    template = _synth_template()

    template.has_resource_properties(
        "AWS::DynamoDB::Table",
        {
            "SSESpecification": {
                "SSEEnabled": True,
                "SSEType": "KMS",
                # A concrete reference to the KMS key resource, not an
                # AWS-owned/default key (which would omit KMSMasterKeyId).
                "KMSMasterKeyId": Match.any_value(),
            },
        },
    )

    resources = template.find_resources("AWS::DynamoDB::Table")
    (table,) = resources.values()
    kms_master_key_id = table["Properties"]["SSESpecification"]["KMSMasterKeyId"]
    assert "Fn::GetAtt" in kms_master_key_id, (
        "expected the table's KMSMasterKeyId to reference the KMS key resource "
        f"via Fn::GetAtt, got: {kms_master_key_id!r}"
    )


def test_table_has_point_in_time_recovery_enabled():
    template = _synth_template()

    template.has_resource_properties(
        "AWS::DynamoDB::Table",
        {
            "PointInTimeRecoverySpecification": {
                "PointInTimeRecoveryEnabled": True,
            },
        },
    )


def test_table_has_deletion_protection_enabled():
    # AGENT-76 design review finding S-2 / decisions.md D-010.
    template = _synth_template()

    template.has_resource_properties(
        "AWS::DynamoDB::Table",
        {
            "DeletionProtectionEnabled": True,
        },
    )


def test_table_has_expected_key_schema_and_billing_mode():
    template = _synth_template()

    template.has_resource_properties(
        "AWS::DynamoDB::Table",
        {
            "KeySchema": [{"AttributeName": "code", "KeyType": "HASH"}],
            "AttributeDefinitions": Match.array_with(
                [{"AttributeName": "code", "AttributeType": "S"}]
            ),
            "BillingMode": "PAY_PER_REQUEST",
        },
    )


def test_kms_key_has_rotation_enabled():
    template = _synth_template()

    template.has_resource_properties(
        "AWS::KMS::Key",
        {
            "EnableKeyRotation": True,
        },
    )


def test_table_and_key_are_retained_on_stack_deletion():
    template = _synth_template()

    template.has_resource(
        "AWS::DynamoDB::Table",
        {
            "DeletionPolicy": "Retain",
            "UpdateReplacePolicy": "Retain",
        },
    )
    template.has_resource(
        "AWS::KMS::Key",
        {
            "DeletionPolicy": "Retain",
            "UpdateReplacePolicy": "Retain",
        },
    )


def test_stack_exposes_table_and_key_attributes():
    app = cdk.App()
    stack = ShortyDataStack(app, "TestShortyDataStack")

    assert stack.table is not None
    assert stack.key is not None
