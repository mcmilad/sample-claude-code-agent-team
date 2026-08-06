import { Stack, StackProps } from "aws-cdk-lib";
import { Distribution } from "aws-cdk-lib/aws-cloudfront";
import { S3BucketOrigin } from "aws-cdk-lib/aws-cloudfront-origins";
import { Bucket } from "aws-cdk-lib/aws-s3";
import { Construct } from "constructs";

/**
 * Owns the private SPA bucket, the access-log bucket, the CloudFront
 * distribution with Origin Access Control, and the response-headers policy
 * -- see design.md#webstack. Depends on ApiStack one-way (apiUrl,
 * identityPoolId); never the reverse -- see design.md Trade-offs for why
 * the CORS origin flows back as a context value, not a stack reference.
 *
 * This is a scaffold stub: minimal bucket + distribution so `bin/app.ts`
 * wires and `cdk synth` succeeds. Group-2 (AGENT-53) replaces this body
 * with the full posture (BPA, CMK, access logging, response-headers
 * policy) asserted by test/infra/web-stack.test.ts. Do not add that here.
 */
export interface WebStackProps extends StackProps {
  readonly apiUrl: string;
  readonly identityPoolId: string;
}

export class WebStack extends Stack {
  public readonly distributionDomainName: string;

  constructor(scope: Construct, id: string, props: WebStackProps) {
    super(scope, id, props);

    // Consumed by group-2 for the SPA's build-time config; unused in this
    // stub on purpose -- see class doc comment.
    void props.apiUrl;
    void props.identityPoolId;

    const spaBucket = new Bucket(this, "SpaBucket");

    const distribution = new Distribution(this, "SpaDistribution", {
      defaultBehavior: {
        origin: S3BucketOrigin.withOriginAccessControl(spaBucket),
      },
    });

    this.distributionDomainName = distribution.distributionDomainName;
  }
}
