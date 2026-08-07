"""Stateful half of Shorty: customer-managed KMS key + DynamoDB table.

Fills in the AGENT-75 stub (AGENT-79). Class name and the `self.table` /
`self.key` attribute names are fixed -- ShortyAppStack (AGENT-80) and app.py
both depend on them.

DEVIATION from design.md#stack-decomposition: design.md names `dynamodb.TableV2`
(the Global Table L2). aws-cdk-lib 2.263.0's `TableEncryptionV2.customer_managed_key`
unconditionally throws `ReplicaSpecificationCannotRenderedRegion` when the stack's
region is an unresolved token -- i.e. it cannot render even the *deployment*
region's replica SSE spec in a region-agnostic stack (verified against the
compiled construct source; there is no parameter that avoids it). That is a hard
conflict with spec NF1, "the defining constraint": no `env=`, no context lookups,
`cdk synth` succeeds with zero AWS credentials. NF1 wins. `dynamodb.Table` (the v1,
single-region L2) has no such restriction and satisfies every acceptance bullet
here (CMK encryption, PITR, RemovalPolicy.RETAIN) region-agnostically. The only
externally-visible effect is `self.table`'s CDK interface type: `ITable`, not
`ITableV2` -- ShortyAppStack's stub (AGENT-80, not this issue's Files:) needs its
type hint updated accordingly. Flagged to the lead; not silently absorbed.
"""

from aws_cdk import RemovalPolicy, Stack
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_kms as kms
from constructs import Construct


class ShortyDataStack(Stack):
    """The stateful half of Shorty: `table` (DynamoDB) and `key` (KMS).

    No `env=`, no `from_lookup`, no account/region literals anywhere -- this
    stack must synth with zero AWS credentials (spec NF1).
    """

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.key = kms.Key(
            self,
            "TableKey",
            description="Customer-managed key for the Shorty links table",
            enable_key_rotation=True,
            removal_policy=RemovalPolicy.RETAIN,
        )

        self.table = dynamodb.Table(
            self,
            "LinksTable",
            partition_key=dynamodb.Attribute(
                name="code", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            encryption=dynamodb.TableEncryption.CUSTOMER_MANAGED,
            encryption_key=self.key,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
            removal_policy=RemovalPolicy.RETAIN,
            deletion_protection=True,
        )
