import { Duration, RemovalPolicy, Stack, StackProps } from "aws-cdk-lib";
import {
  Distribution,
  HeadersReferrerPolicy,
  ResponseHeadersPolicy,
  ViewerProtocolPolicy,
} from "aws-cdk-lib/aws-cloudfront";
import { S3BucketOrigin } from "aws-cdk-lib/aws-cloudfront-origins";
import { IKey } from "aws-cdk-lib/aws-kms";
import { BlockPublicAccess, Bucket, BucketEncryption } from "aws-cdk-lib/aws-s3";
import { Construct } from "constructs";

/**
 * Owns the private SPA bucket, the access-log bucket, the CloudFront
 * distribution with Origin Access Control, and the response-headers policy
 * -- see design.md#webstack and design.md#security-considerations-mandatory
 * (AGENT-53). Depends on ApiStack one-way (apiUrl, identityPoolId) and on
 * DataStack for the shared CMK; never a reference back into ApiStack for
 * CORS -- see design.md Trade-offs for why that would be a cycle.
 */
export interface WebStackProps extends StackProps {
  readonly apiUrl: string;
  readonly identityPoolId: string;
  /** The customer-managed key from DataStack, reused to encrypt the SPA bucket. */
  readonly key: IKey;
}

export class WebStack extends Stack {
  public readonly distributionDomainName: string;

  constructor(scope: Construct, id: string, props: WebStackProps) {
    super(scope, id, props);

    // identityPoolId is consumed by the SPA's build-time config (AGENT-54),
    // not by this stack's infrastructure.
    void props.identityPoolId;

    // Separate from the SPA bucket, encrypted, fully private. Receives both
    // CloudFront standard logs and the SPA bucket's own S3 server access
    // logs.
    const logBucket = new Bucket(this, "AccessLogBucket", {
      blockPublicAccess: BlockPublicAccess.BLOCK_ALL,
      encryption: BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      removalPolicy: RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
    });

    const spaBucket = new Bucket(this, "SpaBucket", {
      blockPublicAccess: BlockPublicAccess.BLOCK_ALL,
      encryption: BucketEncryption.KMS,
      encryptionKey: props.key,
      enforceSSL: true,
      removalPolicy: RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
      serverAccessLogsBucket: logBucket,
      serverAccessLogsPrefix: "s3-access-logs/",
    });
    // data-classification / service / environment / owner tags are applied
    // once, app-wide, in bin/app.ts via Tags.of(app) -- every resource in
    // this stack (including both buckets) inherits them from there. See
    // design.md#security-considerations-mandatory.

    // The SPA calls the API's execute-api URL directly (CORS), never
    // through this distribution -- see design.md Trade-offs for why (SigV4
    // binds to the Host header, which a CloudFront custom origin cannot
    // preserve). This policy only has to let the browser make that direct
    // call from pages served here.
    const responseHeadersPolicy = new ResponseHeadersPolicy(this, "SpaResponseHeadersPolicy", {
      securityHeadersBehavior: {
        strictTransportSecurity: {
          accessControlMaxAge: Duration.days(365),
          includeSubdomains: true,
          preload: true,
          override: true,
        },
        contentSecurityPolicy: {
          contentSecurityPolicy:
            "default-src 'self'; " +
            `connect-src 'self' ${props.apiUrl}; ` +
            "img-src 'self' data:; " +
            "style-src 'self' 'unsafe-inline'; " +
            "script-src 'self'; " +
            "object-src 'none'; " +
            "base-uri 'none'",
          override: true,
        },
        contentTypeOptions: { override: true },
        referrerPolicy: {
          referrerPolicy: HeadersReferrerPolicy.STRICT_ORIGIN_WHEN_CROSS_ORIGIN,
          override: true,
        },
      },
    });

    // NOTE on minimum TLS protocol version: CloudFront only lets you raise
    // the viewer-facing minimum protocol version (`ViewerCertificate.
    // MinimumProtocolVersion` in the synthesized template) on a
    // distribution with a custom domain and an ACM certificate -- both are
    // explicitly out of scope for this PoC (spec.md NF1 / Out of Scope,
    // since nothing is deployed). On the default `*.cloudfront.net`
    // certificate this distribution uses instead, CloudFront fixes the
    // floor itself and the CDK `minimumProtocolVersion` prop is a
    // documented no-op with no certificate attached -- it does not appear
    // in the synthesized template. Setting it here anyway would be
    // misleading dead config, so it is deliberately omitted; TLS
    // enforcement for this distribution is `viewerProtocolPolicy:
    // REDIRECT_TO_HTTPS` below, which IS enforced and asserted in the test.
    const distribution = new Distribution(this, "SpaDistribution", {
      defaultRootObject: "index.html",
      logBucket,
      logFilePrefix: "cloudfront-access-logs/",
      defaultBehavior: {
        origin: S3BucketOrigin.withOriginAccessControl(spaBucket),
        viewerProtocolPolicy: ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        responseHeadersPolicy,
      },
      // The SPA is a client-routed app served from a bucket with no server
      // to return a real 404/403 fallback -- both map to index.html so
      // client-side routing can take over.
      errorResponses: [
        { httpStatus: 403, responseHttpStatus: 200, responsePagePath: "/index.html", ttl: Duration.seconds(0) },
        { httpStatus: 404, responseHttpStatus: 200, responsePagePath: "/index.html", ttl: Duration.seconds(0) },
      ],
    });

    this.distributionDomainName = distribution.distributionDomainName;
  }
}
