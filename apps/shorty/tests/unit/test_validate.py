"""Tests for shorty.validate -- table-driven over named corpora.

Two named corpora, `REJECTION_CORPUS` and `ACCEPTANCE_CORPUS`, so adding a
case is one line. See src/shorty/validate.py's module docstring and
decisions.md D-003 for why hostnames are not resolved via DNS.
"""

import pytest

from shorty.validate import InvalidUrl, validate_url

REJECTION_CORPUS = [
    ("non_str_int", 12345),
    ("non_str_none", None),
    ("non_str_list", ["https://example.com"]),
    ("empty_string", ""),
    ("whitespace_only", "   "),
    ("too_long", "https://example.com/" + ("a" * 2048)),
    ("scheme_file", "file:///etc/passwd"),
    ("scheme_javascript", "javascript:alert(1)"),
    ("scheme_data", "data:text/html,<script>alert(1)</script>"),
    ("scheme_ftp", "ftp://example.com/file"),
    ("missing_netloc", "https:///path"),
    ("missing_scheme_and_netloc", "example.com/path"),
    ("userinfo_present", "https://user:pass@example.com/"),
    ("control_char_newline", "https://example.com/\npath"),
    ("control_char_null", "https://example.com/\x00path"),
    ("localhost_by_name", "https://localhost/"),
    ("localhost_by_name_with_port", "https://localhost:8080/"),
    ("ipv4_loopback", "https://127.0.0.1/"),
    ("ipv4_private_10", "https://10.0.0.5/"),
    ("ipv4_private_192_168", "https://192.168.1.1/"),
    ("ipv4_private_172_16", "https://172.16.0.1/"),
    ("ipv4_link_local", "https://169.254.169.254/"),
    ("ipv4_unspecified", "https://0.0.0.0/"),
    ("ipv6_loopback", "https://[::1]/"),
    ("ipv6_unique_local", "https://[fd00::1]/"),
]

ACCEPTANCE_CORPUS = [
    ("ordinary_https", "https://example.com/"),
    ("ordinary_http", "http://example.com/"),
    ("with_port", "https://example.com:8443/"),
    ("with_query", "https://example.com/search?q=shorty"),
    ("with_fragment", "https://example.com/docs#section"),
    ("with_trailing_path", "https://example.com/a/b/c"),
    ("punycode_idn_host", "https://xn--n3h.example.com/"),
]


@pytest.mark.parametrize(
    "raw",
    [value for _, value in REJECTION_CORPUS],
    ids=[name for name, _ in REJECTION_CORPUS],
)
def test_validate_url_rejects(raw):
    with pytest.raises(InvalidUrl):
        validate_url(raw)


@pytest.mark.parametrize(
    "raw",
    [value for _, value in ACCEPTANCE_CORPUS],
    ids=[name for name, _ in ACCEPTANCE_CORPUS],
)
def test_validate_url_accepts(raw):
    assert validate_url(raw) == raw


def test_invalid_url_is_a_value_error():
    # create_handler (AGENT-81) is expected to catch this via ValueError
    # handling that maps every failure to a single 400 INVALID_URL.
    assert issubclass(InvalidUrl, ValueError)
