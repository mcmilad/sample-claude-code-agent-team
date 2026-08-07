#!/usr/bin/env bash
#
# Smoke test for a deployed Shorty stack (AGENT-84).
#
# AUTHORED, NEVER RUN BY THE AGENT TEAM -- see spec.md NF1 and decisions.md
# D-001. The repo owner runs this manually after `cdk deploy` and fetching a
# Cognito IdToken, per apps/shorty/README.md. Executing it here would be a
# spec violation: it requires a live deployment, which the team does not have.
#
# Usage:
#   API_BASE_URL=https://<api-id>.execute-api.<region>.amazonaws.com \
#   ID_TOKEN=<cognito-id-token> \
#   ./scripts/smoke.sh
# or positionally:
#   ./scripts/smoke.sh <api-base-url> <id-token>
#
# Exercises all three routes end to end:
#   1. POST /links with a bearer token -> 201, extract the minted code
#   2. GET /{code} (curl WITHOUT -L) -> 302 + Location matching the submitted URL
#   3. GET /{unknown-code} -> 404
#
# Fails loudly (non-zero exit, message on stderr) on any missing input or
# unexpected response. Never prompts, never reads stdin.

set -euo pipefail

API_BASE_URL="${1:-${API_BASE_URL:-}}"
ID_TOKEN="${2:-${ID_TOKEN:-}}"

if [[ -z "$API_BASE_URL" ]]; then
  echo "FAIL: API_BASE_URL is required (positional arg 1, or the API_BASE_URL env var)." >&2
  echo "Example: API_BASE_URL=https://abc123.execute-api.us-east-1.amazonaws.com ID_TOKEN=... $0" >&2
  exit 1
fi

if [[ -z "$ID_TOKEN" ]]; then
  echo "FAIL: ID_TOKEN is required (positional arg 2, or the ID_TOKEN env var)." >&2
  echo "Fetch one with 'aws cognito-idp admin-initiate-auth' -- see README.md." >&2
  exit 1
fi

# Unique per run so repeated smoke-test invocations never collide on a
# previously-minted code for the same URL.
TARGET_URL="https://example.com/shorty-smoke-test-$$"

echo "== Shorty smoke test =="
echo "API base: $API_BASE_URL"
echo

create_response="$(mktemp)"
trap 'rm -f "$create_response"' EXIT

# --- 1. POST /links --------------------------------------------------------
echo "-- POST /links"
create_status="$(
  curl -sS -o "$create_response" -w '%{http_code}' \
    -X POST "$API_BASE_URL/links" \
    -H "Authorization: Bearer $ID_TOKEN" \
    -H 'Content-Type: application/json' \
    -d "{\"url\": \"$TARGET_URL\"}"
)"

if [[ "$create_status" != "201" ]]; then
  echo "FAIL: POST /links returned $create_status, expected 201" >&2
  echo "Body: $(cat "$create_response")" >&2
  exit 1
fi

code="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["code"])' < "$create_response")"

if [[ -z "$code" ]]; then
  echo "FAIL: could not extract \"code\" from the POST /links response" >&2
  echo "Body: $(cat "$create_response")" >&2
  exit 1
fi

echo "PASS: 201, minted code=$code"
echo

# --- 2. GET /{code} ----------------------------------------------------------
echo "-- GET /$code (expect 302, no redirect followed)"
redirect_result="$(curl -sS -o /dev/null -w '%{http_code} %{redirect_url}' "$API_BASE_URL/$code")"
redirect_status="${redirect_result%% *}"
redirect_location="${redirect_result#* }"

if [[ "$redirect_status" != "302" ]]; then
  echo "FAIL: GET /$code returned $redirect_status, expected 302" >&2
  exit 1
fi

if [[ "$redirect_location" != "$TARGET_URL" ]]; then
  echo "FAIL: GET /$code Location was '$redirect_location', expected '$TARGET_URL'" >&2
  exit 1
fi

echo "PASS: 302, Location=$redirect_location"
echo

# --- 3. GET /{unknown-code} --------------------------------------------------
# A syntactically valid 7-char code (matches CODE_PATTERN) that is
# astronomically unlikely to have actually been minted.
unknown_code="ZZZZZZZ"
echo "-- GET /$unknown_code (expect 404)"
unknown_status="$(curl -sS -o /dev/null -w '%{http_code}' "$API_BASE_URL/$unknown_code")"

if [[ "$unknown_status" != "404" ]]; then
  echo "FAIL: GET /$unknown_code returned $unknown_status, expected 404" >&2
  exit 1
fi

echo "PASS: 404 for an unknown code"
echo

echo "== ALL CHECKS PASSED =="
