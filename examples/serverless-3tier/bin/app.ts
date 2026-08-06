import { App, Tags } from "aws-cdk-lib";
import { ApiStack } from "../lib/api-stack";
import { DataStack } from "../lib/data-stack";
import { WebStack } from "../lib/web-stack";

// Environment-agnostic on purpose (NF1): no `env` on the App or any stack,
// no `Stack.of(...).account`/`.region` in a conditional, no context
// lookups. This is what lets `cdk synth` succeed with no AWS account.
const app = new App();

const dataStack = new DataStack(app, "ServerlessThreeTierDataStack");

const apiStack = new ApiStack(app, "ServerlessThreeTierApiStack", {
  table: dataStack.table,
  key: dataStack.key,
});

const webStack = new WebStack(app, "ServerlessThreeTierWebStack", {
  apiUrl: apiStack.apiUrl,
  identityPoolId: apiStack.identityPoolId,
});
webStack.addStackDependency(apiStack);

Tags.of(app).add("service", "serverless-3tier");
Tags.of(app).add("environment", "poc");
Tags.of(app).add("owner", "agent-team");
Tags.of(app).add("data-classification", "internal");
