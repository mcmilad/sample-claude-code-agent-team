"""`create_handler` for `POST /links` (spec F1, F4, F6).

Validates the submitted URL, mints a unique 7-character code, and writes the
link to DynamoDB with a conditional `PutItem` so an existing code is never
silently overwritten. Consumes `validate_url`/`InvalidUrl` from
`shorty.validate` and `generate_code` from `shorty.codec` exactly as
published in design.md#interface-contracts -- neither is reimplemented here.

Every validation failure collapses to a single `400 INVALID_URL`, and every
unexpected failure to a single `500 INTERNAL` -- see design.md#error-handling.
No response body ever contains caller-supplied input.
"""

import json
import logging
import os
from base64 import b64decode
from datetime import UTC, datetime

import boto3

from shorty.codec import generate_code
from shorty.validate import InvalidUrl, validate_url

logger = logging.getLogger(__name__)

# The only *application* environment variable Shorty declares (design.md
# D-005); required, so a misconfigured deployment fails loudly at cold start
# rather than on the first request.
TABLE_NAME = os.environ["TABLE_NAME"]

# AWS_REGION is a platform-reserved variable Lambda always sets at runtime --
# never something the CDK stack configures -- so this is not a second
# application env var. The "us-east-1" fallback only matters when this
# module is imported outside Lambda (unit tests, CI): boto3 refuses to
# construct a client with no resolvable region at all, and the fallback
# value is irrelevant there because the client is replaced with a stub
# before any test calls handler().
dynamodb_client = boto3.client(
    "dynamodb", region_name=os.environ.get("AWS_REGION", "us-east-1")
)

MAX_MINT_ATTEMPTS = 5


def _json_response(status_code: int, body: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


def _error_response(status_code: int, error_code: str) -> dict:
    return _json_response(status_code, {"error": error_code})


def _read_candidate_url(event: dict) -> object:
    """Extract the raw `url` field from the request body.

    Returns whatever the caller sent under "url" -- possibly not even a
    string -- and leaves rejecting it to `validate_url`. Raises `InvalidUrl`
    for every way the body itself can be malformed (absent, undecodable,
    non-JSON, not an object, missing the key), so the handler always maps
    it to a single 400 INVALID_URL rather than an unhandled exception.
    """
    raw_body = event.get("body")
    if raw_body is None:
        raise InvalidUrl("missing body")

    if event.get("isBase64Encoded"):
        try:
            raw_body = b64decode(raw_body).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise InvalidUrl("undecodable body") from exc

    try:
        payload = json.loads(raw_body)
    except (json.JSONDecodeError, TypeError) as exc:
        raise InvalidUrl("body is not valid JSON") from exc

    if not isinstance(payload, dict):
        raise InvalidUrl("body is not a JSON object")

    if "url" not in payload:
        raise InvalidUrl("missing url field")

    return payload["url"]


def handler(event: dict, context: object) -> dict:
    """Handle `POST /links`. See design.md#data-flow and #error-handling."""
    try:
        url = validate_url(_read_candidate_url(event))
    except InvalidUrl:
        return _error_response(400, "INVALID_URL")

    try:
        created_by = event["requestContext"]["authorizer"]["jwt"]["claims"]["sub"]
        # Direct indexing, not .get() with a fallback: a missing domainName
        # would otherwise silently mint a 201 with a broken shortUrl
        # ("https:///<code>") and no log trace. Absence here is exactly as
        # unexpected as a missing authorizer claim above, so it takes the
        # same path -- caught below and logged as 500 INTERNAL.
        domain_name = event["requestContext"]["domainName"]
        created_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

        for _attempt in range(MAX_MINT_ATTEMPTS):
            code = generate_code()
            try:
                dynamodb_client.put_item(
                    TableName=TABLE_NAME,
                    Item={
                        "code": {"S": code},
                        "url": {"S": url},
                        "createdAt": {"S": created_at},
                        "createdBy": {"S": created_by},
                    },
                    ConditionExpression="attribute_not_exists(code)",
                )
            except dynamodb_client.exceptions.ConditionalCheckFailedException:
                continue
            return _json_response(
                201, {"code": code, "shortUrl": f"https://{domain_name}/{code}"}
            )

        return _error_response(503, "CODE_COLLISION")
    except Exception:
        # Detail goes to CloudWatch Logs only -- never into the response.
        logger.exception("create_handler: unexpected error")
        return _error_response(500, "INTERNAL")
