"""Unit tests for shorty.redirect_handler (AGENT-82).

Real payload-format-2.0 event fixtures; the module-level boto3 client is
replaced with a stub that records calls and can be told to raise -- no moto,
no network (design.md#testing-strategy). This handler is the unauthenticated
route (spec Constraint 1), so every test here treats input as hostile.

TABLE_NAME must be set before shorty.redirect_handler is first imported (it
is read at module level, so a misconfigured deployment fails at cold start
-- see the module docstring). Collection happens before any fixture runs, so
the env var is set here at true module level, ahead of the import, matching
the pattern in test_create_handler.py.
"""

import inspect
import json
import os

os.environ.setdefault("TABLE_NAME", "shorty-links-test")

import pytest

from shorty import redirect_handler
from shorty.redirect_handler import handler

VALID_CODE = "abc123X"  # matches codec.CODE_PATTERN: 7 alphanumeric chars
STORED_URL = "https://example.com/some/page"


class _StubDynamoClient:
    """Records every `get_item` call; never implements a write method."""

    def __init__(self, *, item=None, raise_exc=None):
        self.item = item
        self.raise_exc = raise_exc
        self.get_item_calls = []

    def get_item(self, **kwargs):
        self.get_item_calls.append(kwargs)
        if self.raise_exc is not None:
            raise self.raise_exc
        if self.item is None:
            return {}
        return {"Item": self.item}


def _event(code, request_id="test-request-id"):
    """A payload-format-2.0 event for `GET /{code}`."""
    return {
        "version": "2.0",
        "routeKey": "GET /{code}",
        "rawPath": f"/{code}",
        "pathParameters": {"code": code},
        "requestContext": {"requestId": request_id},
    }


def _install_stub(monkeypatch, **kwargs):
    stub = _StubDynamoClient(**kwargs)
    monkeypatch.setattr(redirect_handler, "dynamodb", stub)
    return stub


@pytest.mark.parametrize(
    "malformed_code",
    [
        "short",  # 6 chars
        "toolongcode",  # 8+ chars
        "abc-123",  # illegal character
        "",  # empty
        "abcdefg\n",  # trailing newline
    ],
)
def test_malformed_code_returns_404_without_calling_dynamodb(monkeypatch, malformed_code):
    stub = _install_stub(monkeypatch)

    response = handler(_event(malformed_code), None)

    assert response["statusCode"] == 404
    assert stub.get_item_calls == [], (
        "a malformed code must never reach DynamoDB -- that is what keeps a "
        "scanner from turning junk input into billed reads"
    )


def test_missing_path_parameters_returns_404_without_calling_dynamodb(monkeypatch):
    stub = _install_stub(monkeypatch)
    event = {"version": "2.0", "routeKey": "GET /{code}"}  # no pathParameters

    response = handler(event, None)

    assert response["statusCode"] == 404
    assert stub.get_item_calls == []


def test_hit_returns_302_with_location_and_no_store_cache_control(monkeypatch):
    _install_stub(monkeypatch, item={"code": {"S": VALID_CODE}, "url": {"S": STORED_URL}})

    response = handler(_event(VALID_CODE), None)

    assert response["statusCode"] == 302
    assert response["headers"]["Location"] == STORED_URL
    assert response["headers"]["Cache-Control"] == "no-store"


def test_hit_calls_get_item_with_expected_key_shape(monkeypatch):
    stub = _install_stub(monkeypatch, item={"code": {"S": VALID_CODE}, "url": {"S": STORED_URL}})

    handler(_event(VALID_CODE), None)

    assert stub.get_item_calls == [
        {"TableName": redirect_handler.TABLE_NAME, "Key": {"code": {"S": VALID_CODE}}}
    ]


def test_miss_returns_404_not_found(monkeypatch):
    _install_stub(monkeypatch, item=None)

    response = handler(_event(VALID_CODE), None)

    assert response["statusCode"] == 404
    assert json.loads(response["body"]) == {"error": "NOT_FOUND"}


def test_miss_and_malformed_responses_are_byte_identical(monkeypatch):
    _install_stub(monkeypatch, item=None)
    miss_response = handler(_event(VALID_CODE), None)

    _install_stub(monkeypatch)
    malformed_response = handler(_event("bad"), None)

    assert miss_response == malformed_response, (
        "a prober must not be able to distinguish 'wrong shape' from "
        "'not minted' -- both must be the exact same response"
    )


def test_404_body_never_echoes_the_requested_code(monkeypatch):
    _install_stub(monkeypatch, item=None)
    weird_code = "zZ9-!@#"

    response = handler(_event(weird_code), None)

    assert weird_code not in response["body"]
    assert weird_code not in json.dumps(response["headers"])


def test_dynamodb_exception_returns_500_without_leaking_detail(monkeypatch):
    _install_stub(monkeypatch, raise_exc=RuntimeError("table is on fire"))

    response = handler(_event(VALID_CODE), None)

    assert response["statusCode"] == 500
    assert json.loads(response["body"]) == {"error": "INTERNAL"}
    assert "table is on fire" not in response["body"]


def test_item_missing_url_attribute_returns_500_not_a_5xx_leak(monkeypatch):
    # A found item with no `url` attribute is a data-integrity problem, not
    # caller-supplied bad input -- it must not surface as an open redirect
    # to an empty Location, nor leak the malformed item back to the caller.
    _install_stub(monkeypatch, item={"code": {"S": VALID_CODE}})

    response = handler(_event(VALID_CODE), None)

    assert response["statusCode"] == 500
    assert json.loads(response["body"]) == {"error": "INTERNAL"}


def test_item_with_empty_url_returns_500_instead_of_redirecting_nowhere(monkeypatch):
    # A present-but-empty `url` must not become a 302 to an empty Location --
    # that is a broken redirect masquerading as success. Same data-integrity
    # class as a missing attribute.
    _install_stub(monkeypatch, item={"code": {"S": VALID_CODE}, "url": {"S": ""}})

    response = handler(_event(VALID_CODE), None)

    assert response["statusCode"] == 500
    assert json.loads(response["body"]) == {"error": "INTERNAL"}


def test_handler_never_calls_a_dynamodb_write_method():
    # Source-inspection guard against a degenerate implementation that
    # accidentally invokes a write method on the shared client -- this
    # handler must be GetItem-only (design.md#security-considerations,
    # Compute tier: "redirect_fn gets dynamodb:GetItem").
    source = inspect.getsource(redirect_handler)
    assert "get_item" in source
    for write_method in ("put_item", "update_item", "delete_item"):
        assert write_method not in source
