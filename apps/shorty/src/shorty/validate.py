"""URL validation for `POST /links`.

Validates and normalizes a candidate URL before it is persisted. Rejects
malformed input, non-http(s) schemes, credential-bearing URLs, control
characters, and hosts that are loopback / private / link-local / reserved IP
literals -- for both IPv4 and IPv6, parsed with the stdlib `ipaddress`
module rather than hand-rolled CIDR string matching (`172.16.0.1` slips past
a naive `startswith("192.168.")` check; `ipaddress.is_private` does not).

Does NOT resolve hostnames via DNS. This is deliberate, not an oversight:
resolving would turn a pure, offline-testable function into a
network-dependent one, and the guarantee it would buy is illusory anyway --
DNS can be repointed between validation and the moment a caller follows the
link (TOCTOU). See `.claude/specs/shorty/decisions.md` D-003 for the full
rationale. Do not "fix" this by adding resolution.
"""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit

MAX_URL_LENGTH = 2048

_ALLOWED_SCHEMES = frozenset({"http", "https"})
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")


class InvalidUrl(ValueError):
    """Raised when a candidate URL fails validation.

    The message is diagnostic only, for logs -- it must never reach a caller.
    Every failure maps to the same `400 INVALID_URL` response so a prober
    cannot use per-rule detail to map the validation ruleset.
    """


def validate_url(raw: object) -> str:
    """Return the normalized URL, or raise InvalidUrl.

    Rejects: non-str, empty, > 2048 chars, non-http(s) scheme, missing
    netloc, userinfo present (`user:pass@host`), control characters, the
    literal host "localhost", and hosts that are loopback / private /
    link-local / reserved IP literals (v4 and v6). Hostnames are NOT
    resolved -- see the module docstring and spec Constraint 3.
    """
    if not isinstance(raw, str):
        raise InvalidUrl("not a string")

    if _CONTROL_CHAR_RE.search(raw):
        raise InvalidUrl("control character present")

    candidate = raw.strip()
    if not candidate:
        raise InvalidUrl("empty")

    if len(candidate) > MAX_URL_LENGTH:
        raise InvalidUrl("too long")

    try:
        parts = urlsplit(candidate)
    except ValueError as exc:
        raise InvalidUrl("unparsable URL") from exc

    if parts.scheme not in _ALLOWED_SCHEMES:
        raise InvalidUrl("scheme not http(s)")

    if not parts.netloc:
        raise InvalidUrl("missing netloc")

    if "@" in parts.netloc:
        raise InvalidUrl("userinfo present")

    try:
        hostname = parts.hostname
    except ValueError as exc:
        raise InvalidUrl("unparsable host") from exc

    if not hostname:
        raise InvalidUrl("missing host")

    if hostname.lower() == "localhost":
        raise InvalidUrl("localhost rejected by name")

    if _is_disallowed_ip_literal(hostname):
        raise InvalidUrl("host is a disallowed IP literal")

    return candidate


def _is_disallowed_ip_literal(hostname: str) -> bool:
    """Return True if hostname is an IP literal in a disallowed range.

    Hostnames that are not IP literals -- ordinary DNS names, including
    punycode/IDN hosts -- return False here; they are accepted without
    resolution, per the module docstring.
    """
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return False

    return (
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_reserved
        or address.is_unspecified
    )
