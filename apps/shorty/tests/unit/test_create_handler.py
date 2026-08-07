"""Tests for shorty.create_handler -- POST /links (F1, F4, F6).

TABLE_NAME must be set before shorty.create_handler is first imported (it is
read at module level, so a misconfigured deployment fails at cold start --
see the module docstring). Collection happens before any fixture runs, so
the env var is set here at true module level, ahead of the import.
"""

import base64
import json
import os

os.environ.setdefault("TABLE_NAME", "shorty-links-test")

import pytest

from shorty import create_handler
from shorty.codec import CODE_PATTERN

TABLE_NAME = os.environ["TABLE_NAME"]
DOMAIN = "abc123.execute-api.us-east-1.amazonaws.com"
SUB = "user-0001"


class _ConditionalCheckFailedException(Exception):
    """Stand-in for the real client's ConditionalCheckFailedException."""


class _StubExceptions:
    ConditionalCheckFailedException = _ConditionalCheckFailedException


class StubDynamoClient:
    """Records put_item calls; fails the first `failures` of them.

    No moto, no network -- this replaces shorty.create_handler's
    module-level `dynamodb_client` attribute entirely, per
    design.md#interface-contracts ("tests inject a stub by monkeypatching
    the module attribute").
    """

    exceptions = _StubExceptions()

    def __init__(self, failures: int = 0):
        self.calls: list[dict] = []
        self.failures = failures

    def put_item(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) <= self.failures:
            raise self.exceptions.ConditionalCheckFailedException("stub collision")


class BoomDynamoClient(StubDynamoClient):
    """Raises an exception that is NOT ConditionalCheckFailedException."""

    def put_item(self, **kwargs):
        self.calls.append(kwargs)
        raise RuntimeError("simulated unexpected AWS failure")


@pytest.fixture
def stub_client(monkeypatch):
    stub = StubDynamoClient()
    monkeypatch.setattr(create_handler, "dynamodb_client", stub)
    return stub


def _event(
    body: dict | None = None,
    *,
    raw_body: str | None = None,
    is_base64: bool = False,
    sub: str | None = SUB,
    domain: str | None = DOMAIN,
) -> dict:
    """Build an API Gateway HTTP API payload-format-2.0 event.

    `body` is JSON-encoded for the common case; pass `raw_body` directly to
    exercise malformed-body cases that a dict can't represent.
    """
    if raw_body is not None:
        body_str = raw_body
    elif body is not None:
        body_str = json.dumps(body)
    else:
        body_str = None

    if is_base64 and body_str is not None:
        body_str = base64.b64encode(body_str.encode()).decode()

    request_context: dict = {}
    if domain is not None:
        request_context["domainName"] = domain
    if sub is not None:
        request_context["authorizer"] = {"jwt": {"claims": {"sub": sub}}}

    return {
        "body": body_str,
        "isBase64Encoded": is_base64,
        "requestContext": request_context,
    }


# -- Happy path ---------------------------------------------------------


def test_happy_path_returns_201_with_code_and_short_url(stub_client):
    response = create_handler.handler(_event({"url": "https://example.com/"}), None)

    assert response["statusCode"] == 201
    assert response["headers"]["Content-Type"] == "application/json"
    body = json.loads(response["body"])
    assert set(body.keys()) == {"code", "shortUrl"}
    assert CODE_PATTERN.match(body["code"])
    assert body["shortUrl"] == f"https://{DOMAIN}/{body['code']}"


def test_put_item_carries_condition_expression_and_exact_item_shape(stub_client):
    response = create_handler.handler(_event({"url": "https://example.com/"}), None)
    code = json.loads(response["body"])["code"]

    assert len(stub_client.calls) == 1
    call = stub_client.calls[0]
    assert call["TableName"] == TABLE_NAME
    assert call["ConditionExpression"] == "attribute_not_exists(code)"
    assert call["Item"] == {
        "code": {"S": code},
        "url": {"S": "https://example.com/"},
        "createdAt": {"S": call["Item"]["createdAt"]["S"]},
        "createdBy": {"S": SUB},
    }
    assert call["Item"]["createdAt"]["S"].endswith("Z")


def test_base64_encoded_body_is_decoded(stub_client):
    raw = json.dumps({"url": "https://example.com/"})
    response = create_handler.handler(
        _event(raw_body=raw, is_base64=True), None
    )

    assert response["statusCode"] == 201
    assert stub_client.calls[0]["Item"]["url"] == {"S": "https://example.com/"}


def test_collision_regenerates_and_succeeds_on_a_later_attempt(monkeypatch):
    stub = StubDynamoClient(failures=3)
    monkeypatch.setattr(create_handler, "dynamodb_client", stub)

    response = create_handler.handler(_event({"url": "https://example.com/"}), None)

    assert response["statusCode"] == 201
    assert len(stub.calls) == 4
    # Each attempt regenerates the code -- not a fixed retry of the same one.
    codes = {call["Item"]["code"]["S"] for call in stub.calls}
    assert len(codes) == 4


# -- Collision exhaustion (F6) -------------------------------------------


def test_collision_exhausted_returns_503_after_exactly_5_attempts(monkeypatch):
    stub = StubDynamoClient(failures=5)
    monkeypatch.setattr(create_handler, "dynamodb_client", stub)

    response = create_handler.handler(_event({"url": "https://example.com/"}), None)

    assert response["statusCode"] == 503
    assert json.loads(response["body"]) == {"error": "CODE_COLLISION"}
    assert len(stub.calls) == 5


# -- Validation failures (F4) --------------------------------------------

REJECTION_BODIES = [
    ("invalid_url_value", {"url": "not a url"}),
    ("missing_url_key", {"foo": "bar"}),
    ("url_not_a_string", {"url": 12345}),
    ("private_ip_url", {"url": "https://10.0.0.5/"}),
    ("javascript_scheme", {"url": "javascript:alert(1)"}),
]


@pytest.mark.parametrize(
    "body", [body for _, body in REJECTION_BODIES], ids=[n for n, _ in REJECTION_BODIES]
)
def test_handler_rejects_invalid_url_with_400(stub_client, body):
    response = create_handler.handler(_event(body), None)

    assert response["statusCode"] == 400
    assert json.loads(response["body"]) == {"error": "INVALID_URL"}
    assert stub_client.calls == []  # never reached DynamoDB


MALFORMED_RAW_BODIES = [
    ("absent_body", None),
    ("not_json", "not-json-at-all"),
    ("json_array_not_object", "[1, 2, 3]"),
    ("json_string_not_object", '"just a string"'),
    ("empty_string", ""),
]


@pytest.mark.parametrize(
    "raw_body",
    [raw for _, raw in MALFORMED_RAW_BODIES],
    ids=[n for n, _ in MALFORMED_RAW_BODIES],
)
def test_handler_rejects_malformed_body_with_400(stub_client, raw_body):
    response = create_handler.handler(_event(raw_body=raw_body), None)

    assert response["statusCode"] == 400
    assert json.loads(response["body"]) == {"error": "INVALID_URL"}
    assert stub_client.calls == []


def test_no_response_body_echoes_the_submitted_url(stub_client):
    # A private-IP literal so this is guaranteed rejected (F4), with a
    # distinctive marker to prove it isn't echoed back on the 400 path.
    submitted = "https://10.0.0.5/evil-marker-xyz"
    response = create_handler.handler(_event({"url": submitted}), None)

    assert response["statusCode"] == 400
    assert "evil-marker-xyz" not in response["body"]
    assert "10.0.0.5" not in response["body"]


# -- Unexpected failures --------------------------------------------------


def test_unexpected_put_item_error_returns_500_internal(monkeypatch):
    stub = BoomDynamoClient()
    monkeypatch.setattr(create_handler, "dynamodb_client", stub)

    response = create_handler.handler(_event({"url": "https://example.com/"}), None)

    assert response["statusCode"] == 500
    assert json.loads(response["body"]) == {"error": "INTERNAL"}


def test_missing_authorizer_claims_returns_500_not_a_crash(stub_client):
    event = _event({"url": "https://example.com/"}, sub=None)

    response = create_handler.handler(event, None)

    assert response["statusCode"] == 500
    assert json.loads(response["body"]) == {"error": "INTERNAL"}
    assert stub_client.calls == []


def test_missing_domain_name_returns_500_not_a_broken_201(stub_client):
    # Regression test: domain_name used to default silently to "" via
    # .get(..., ""), which would have written the item and returned 201
    # with a structurally broken shortUrl ("https:///<code>") -- a link
    # persisted and reported as success with no log trace. Absence must
    # fail the same way a missing authorizer claim does: 500, and nothing
    # written.
    event = _event({"url": "https://example.com/"}, domain=None)

    response = create_handler.handler(event, None)

    assert response["statusCode"] == 500
    assert json.loads(response["body"]) == {"error": "INTERNAL"}
    assert stub_client.calls == []
    assert "https:///" not in response["body"]
