"""Lambda handler for `GET /{code}` -- resolve a short code and redirect.

Unauthenticated by necessity (spec Constraint 1): a short link is followed by
an arbitrary browser carrying no credentials, so every input here is treated
as hostile. See design.md#data-flow (Resolve) and design.md#error-handling.

Module-level `boto3.client("dynamodb")`, reused across warm invocations.
Tests replace this module attribute with a stub via monkeypatch -- no moto,
no network (design.md#interface-contracts).

TABLE_NAME must be set before this module is first imported (it is read at
module level, so a misconfigured deployment fails loudly at cold start with
a KeyError naming the missing variable, matching create_handler -- see
design.md D-005 and the reopened-finding note on AGENT-82).
"""

from __future__ import annotations

import json
import logging
import os

import boto3

from shorty.codec import CODE_PATTERN

# The only *application* environment variable Shorty declares (design.md
# D-005); required, so a misconfigured deployment fails loudly at cold start
# rather than silently reading GetItem(TableName="") on every request.
TABLE_NAME = os.environ["TABLE_NAME"]

logger = logging.getLogger(__name__)

# AWS_REGION is a platform-reserved variable Lambda always sets at runtime --
# never something the CDK stack configures -- so this is not a second
# application env var. The "us-east-1" fallback only matters when this
# module is imported outside Lambda (unit tests, CI): boto3 refuses to
# construct a client with no resolvable region at all, and the fallback
# value is irrelevant there because the client is replaced with a stub
# before any test calls handler().
dynamodb = boto3.client("dynamodb", region_name=os.environ.get("AWS_REGION", "us-east-1"))

# Both branches return this exact object so a malformed code and an unminted
# one are byte-identical responses -- a prober must not be able to tell
# "wrong shape" from "not minted" apart (spec F3, design.md#error-handling).
_NOT_FOUND = {
    "statusCode": 404,
    "headers": {"Content-Type": "application/json"},
    "body": json.dumps({"error": "NOT_FOUND"}),
}

_INTERNAL_ERROR = {
    "statusCode": 500,
    "headers": {"Content-Type": "application/json"},
    "body": json.dumps({"error": "INTERNAL"}),
}


def handler(event: dict, context: object) -> dict:
    """Resolve `code` from the path to its stored URL, or return 404/500.

    Never echoes caller-supplied input into the response, and never issues a
    DynamoDB call for a code that doesn't shape-match `codec.CODE_PATTERN` --
    that keeps a scanner from turning junk path segments into billed reads.
    """
    path_params = event.get("pathParameters") or {}
    code = path_params.get("code") or ""

    if not CODE_PATTERN.fullmatch(code):
        return _NOT_FOUND

    try:
        response = dynamodb.get_item(
            TableName=TABLE_NAME,
            Key={"code": {"S": code}},
        )
        item = response.get("Item")
        if item is None:
            return _NOT_FOUND

        # "url" is an implicit contract with create_handler (AGENT-81): the
        # DynamoDB item schema isn't pinned in design.md beyond the `code`
        # PK, so this attribute name must match what create_handler writes.
        url = item["url"]["S"]
        if not url:
            # A present-but-empty `url` is a data-integrity problem, not a
            # caller-input problem -- surfacing it as a 302 would redirect
            # to an empty Location instead of the 500 a malformed stored
            # item should produce.
            raise ValueError("stored item has an empty url")
    except Exception:
        request_id = event.get("requestContext", {}).get("requestId", "unknown")
        logger.exception("redirect_handler failed, request_id=%s", request_id)
        return _INTERNAL_ERROR

    return {
        "statusCode": 302,
        "headers": {
            "Location": url,
            "Cache-Control": "no-store",
        },
        "body": "",
    }
