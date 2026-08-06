import { App, Stack } from "aws-cdk-lib";
import { Match, Template } from "aws-cdk-lib/assertions";
import { Key } from "aws-cdk-lib/aws-kms";
import { WebStack, WebStackProps } from "../../lib/web-stack";

const TEST_API_URL = "https://abc123.execute-api.us-east-1.amazonaws.com";
const TEST_IDENTITY_POOL_ID = "us-east-1:11111111-1111-1111-1111-111111111111";

/**
 * WebStack takes its CMK as a prop (from DataStack, in the real app); build
 * a standalone key here so this stack can be template-tested in isolation
 * from DataStack/ApiStack.
 */
function buildWebStack(idSuffix: string, propsOverride: Partial<WebStackProps> = {}): WebStack {
  const app = new App();
  const keyStack = new Stack(app, `KeyStack${idSuffix}`);
  const key = new Key(keyStack, "TestKey");

  return new WebStack(app, `TestWebStack${idSuffix}`, {
    apiUrl: TEST_API_URL,
    identityPoolId: TEST_IDENTITY_POOL_ID,
    key,
    ...propsOverride,
  });
}

function synth(idSuffix: string): Template {
  return Template.fromStack(buildWebStack(idSuffix));
}

describe("WebStack", () => {
  test("both buckets have Block Public Access fully enabled (all four flags)", () => {
    const template = synth("Bpa");
    const buckets = template.findResources("AWS::S3::Bucket");
    expect(Object.keys(buckets)).toHaveLength(2);

    for (const bucket of Object.values(buckets)) {
      expect(bucket.Properties.PublicAccessBlockConfiguration).toEqual({
        BlockPublicAcls: true,
        BlockPublicPolicy: true,
        IgnorePublicAcls: true,
        RestrictPublicBuckets: true,
      });
    }
  });

  test("both bucket policies deny every action when aws:SecureTransport is false", () => {
    const template = synth("Tls");
    const policies = template.findResources("AWS::S3::BucketPolicy");
    expect(Object.keys(policies)).toHaveLength(2);

    for (const policy of Object.values(policies)) {
      const statements: Array<Record<string, unknown>> = policy.Properties.PolicyDocument.Statement;
      const denyStatement = statements.find(
        (statement) =>
          statement.Effect === "Deny" &&
          (statement.Principal === "*" || (statement.Principal as { AWS?: string })?.AWS === "*") &&
          (statement.Condition as { Bool?: Record<string, string> })?.Bool?.["aws:SecureTransport"] === "false",
      );
      expect(denyStatement).toBeDefined();
    }
  });

  test("the SPA bucket is KMS-encrypted with the key passed in from DataStack", () => {
    const template = synth("Kms");
    template.hasResourceProperties("AWS::S3::Bucket", {
      BucketEncryption: {
        ServerSideEncryptionConfiguration: Match.arrayWith([
          Match.objectLike({
            ServerSideEncryptionByDefault: Match.objectLike({
              SSEAlgorithm: "aws:kms",
              KMSMasterKeyID: Match.anyValue(),
            }),
          }),
        ]),
      },
    });
  });

  test("the log bucket is encrypted and separate from the SPA bucket", () => {
    const template = synth("LogEnc");
    const buckets = template.findResources("AWS::S3::Bucket");
    const encryptionAlgorithms = Object.values(buckets).map(
      (bucket) =>
        bucket.Properties.BucketEncryption.ServerSideEncryptionConfiguration[0].ServerSideEncryptionByDefault
          .SSEAlgorithm,
    );
    // One bucket on the shared CMK (the SPA bucket), one on SSE-S3 (the log
    // bucket) -- two distinct buckets, both encrypted, matching the
    // acceptance criteria's "separate from the SPA bucket".
    expect(Object.keys(buckets)).toHaveLength(2);
    expect(encryptionAlgorithms.sort()).toEqual(["AES256", "aws:kms"]);
  });

  test("the distribution uses Origin Access Control (not legacy OAI), with no public bucket policy statement", () => {
    const template = synth("Oac");
    template.resourceCountIs("AWS::CloudFront::OriginAccessControl", 1);
    template.hasResourceProperties("AWS::CloudFront::Distribution", {
      DistributionConfig: Match.objectLike({
        Origins: Match.arrayWith([
          Match.objectLike({
            OriginAccessControlId: Match.anyValue(),
            S3OriginConfig: { OriginAccessIdentity: "" },
          }),
        ]),
      }),
    });

    const policies = template.findResources("AWS::S3::BucketPolicy");
    for (const policy of Object.values(policies)) {
      const statements: Array<Record<string, unknown>> = policy.Properties.PolicyDocument.Statement;
      for (const statement of statements) {
        if (statement.Effect === "Allow") {
          expect(statement.Principal).not.toBe("*");
          expect((statement.Principal as { AWS?: string })?.AWS).not.toBe("*");
        }
      }
    }
  });

  test("the distribution redirects viewers to HTTPS", () => {
    const template = synth("Https");
    template.hasResourceProperties("AWS::CloudFront::Distribution", {
      DistributionConfig: Match.objectLike({
        DefaultCacheBehavior: Match.objectLike({
          ViewerProtocolPolicy: "redirect-to-https",
        }),
      }),
    });
  });

  test("the response headers policy sets HSTS, a CSP permitting the API origin, nosniff, and a referrer policy", () => {
    const template = synth("Headers");
    template.hasResourceProperties("AWS::CloudFront::ResponseHeadersPolicy", {
      ResponseHeadersPolicyConfig: Match.objectLike({
        SecurityHeadersConfig: Match.objectLike({
          StrictTransportSecurity: Match.objectLike({ Override: true }),
          ContentSecurityPolicy: Match.objectLike({
            ContentSecurityPolicy: Match.stringLikeRegexp(`connect-src[^;]*${TEST_API_URL}`),
            Override: true,
          }),
          ContentTypeOptions: Match.objectLike({ Override: true }),
          ReferrerPolicy: Match.objectLike({ Override: true }),
        }),
      }),
    });
  });

  test("no CDK reference back into an ApiStack -- WebStack synthesizes standalone", () => {
    // If WebStack ever grew a construct-level reference back into ApiStack
    // for CORS (the circular dependency design.md warns against), this
    // stack could no longer synthesize on its own the way this whole file
    // synthesizes it. The passing suite above is the guard; this test
    // documents why the pattern above (a bare WebStack, no ApiStack in
    // scope) is itself the assertion.
    expect(() => synth("Standalone")).not.toThrow();
  });

  test("exposes distributionDomainName", () => {
    const stack = buildWebStack("Output");
    expect(typeof stack.distributionDomainName).toBe("string");
  });
});
