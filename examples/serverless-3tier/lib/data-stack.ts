import { Stack, StackProps } from "aws-cdk-lib";
import { AttributeType, BillingMode, ITable, Table } from "aws-cdk-lib/aws-dynamodb";
import { IKey, Key } from "aws-cdk-lib/aws-kms";
import { Construct } from "constructs";

/**
 * Owns the customer-managed KMS key and the `links` table. Nothing else in
 * the app may create a key or a table -- see design.md#datastack.
 *
 * This is a scaffold stub: minimal resources so `bin/app.ts` wires and
 * `cdk synth` succeeds. Group-2 (AGENT-51) replaces this body with the full
 * security posture (CMK encryption, PITR, removal policy, tags) asserted by
 * test/infra/data-stack.test.ts. Do not add that hardening here.
 */
export interface DataStackProps extends StackProps {}

export class DataStack extends Stack {
  public readonly table: ITable;
  public readonly key: IKey;

  constructor(scope: Construct, id: string, props?: DataStackProps) {
    super(scope, id, props);

    this.key = new Key(this, "LinksKey");

    this.table = new Table(this, "LinksTable", {
      partitionKey: { name: "code", type: AttributeType.STRING },
      billingMode: BillingMode.PAY_PER_REQUEST,
    });
  }
}
