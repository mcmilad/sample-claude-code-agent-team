import { Stack, StackProps } from "aws-cdk-lib";
import { HttpApi } from "aws-cdk-lib/aws-apigatewayv2";
import { CfnIdentityPool } from "aws-cdk-lib/aws-cognito";
import { ITable } from "aws-cdk-lib/aws-dynamodb";
import { IKey } from "aws-cdk-lib/aws-kms";
import { Construct } from "constructs";

/**
 * Owns the HTTP API, its two routes, the two Lambda functions (one
 * execution role each), the access-log group, and the Cognito identity pool
 * plus its unauthenticated role -- see design.md#apistack.
 *
 * This is a scaffold stub: it accepts the DataStack outputs and exposes the
 * two values WebStack needs, but does not yet wire the routes, handlers, or
 * IAM policies. Group-2 (AGENT-51) replaces this body with the full
 * implementation asserted by test/infra/api-stack.test.ts (AWS_IAM
 * authorizer on POST /links, two distinct least-privilege execution roles,
 * no wildcard actions). Do not add that here.
 */
export interface ApiStackProps extends StackProps {
  readonly table: ITable;
  readonly key: IKey;
}

export class ApiStack extends Stack {
  public readonly apiUrl: string;
  public readonly identityPoolId: string;

  constructor(scope: Construct, id: string, props: ApiStackProps) {
    super(scope, id, props);

    // Referenced by group-2's handlers/routes; unused in this stub on
    // purpose -- see class doc comment.
    void props.table;
    void props.key;

    const httpApi = new HttpApi(this, "LinksApi");

    const identityPool = new CfnIdentityPool(this, "LinksIdentityPool", {
      allowUnauthenticatedIdentities: true,
    });

    this.apiUrl = httpApi.apiEndpoint;
    this.identityPoolId = identityPool.ref;
  }
}
