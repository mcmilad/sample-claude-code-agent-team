"""CDK entrypoint for Shorty.

Deliberately environment-agnostic: `App()` takes no `env=` and this file makes no
context lookups, so `cdk synth` succeeds with zero AWS credentials (spec NF1).
Deployment is a manual step the repo owner runs from `apps/shorty/README.md`.
"""

import aws_cdk as cdk

from shorty_infra.app_stack import ShortyAppStack
from shorty_infra.data_stack import ShortyDataStack

app = cdk.App()

data_stack = ShortyDataStack(app, "ShortyDataStack")
app_stack = ShortyAppStack(
    app,
    "ShortyAppStack",
    table=data_stack.table,
    key=data_stack.key,
)

cdk.Tags.of(app).add("service", "shorty")
cdk.Tags.of(app).add("environment", "poc")
cdk.Tags.of(app).add("owner", "shorty-maintainers")
cdk.Tags.of(app).add("data-classification", "internal")
cdk.Tags.of(app).add("cost-center", "shorty-poc")

app.synth()
