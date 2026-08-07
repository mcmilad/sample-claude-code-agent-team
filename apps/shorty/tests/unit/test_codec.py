"""Unit tests for shorty.codec (AGENT-78).

Pure-function tests, no AWS, no mocks. codec.py is the shared interface both
create_handler (AGENT-81) and redirect_handler (AGENT-82) depend on, so these
tests double as a contract check on ALPHABET, CODE_LENGTH, CODE_PATTERN, and
generate_code.
"""

import inspect
import re

from shorty.codec import ALPHABET, CODE_LENGTH, CODE_PATTERN, generate_code

ITERATIONS = 1000


def test_alphabet_is_exactly_62_unique_characters():
    assert len(ALPHABET) == 62
    assert len(set(ALPHABET)) == 62


def test_code_length_is_seven():
    assert CODE_LENGTH == 7


def test_generate_code_uses_secrets_not_random():
    # `random` is deterministically seeded and would make codes guessable --
    # the entire reason minting is not a monotonic counter (spec.md -> Design
    # Decisions -> Code minting). Assert the implementation, not just the
    # observable output, so a swap to `random.choice` fails this test even
    # though it would still "look" random over a small sample.
    source = inspect.getsource(generate_code)
    assert "secrets." in source
    assert "random." not in source


def test_generate_code_always_matches_code_pattern():
    for _ in range(ITERATIONS):
        code = generate_code()
        assert CODE_PATTERN.fullmatch(code), f"{code!r} does not match CODE_PATTERN"


def test_generate_code_output_has_expected_length_and_alphabet():
    for _ in range(ITERATIONS):
        code = generate_code()
        assert len(code) == CODE_LENGTH
        assert all(char in ALPHABET for char in code)


def test_many_generated_codes_are_distinct():
    # Smoke check against a degenerate implementation that returns a
    # constant -- that would pass every other assertion in this file.
    codes = {generate_code() for _ in range(ITERATIONS)}
    assert len(codes) == ITERATIONS


def test_code_pattern_rejects_wrong_length():
    assert CODE_PATTERN.fullmatch("abcdef") is None  # 6 chars
    assert CODE_PATTERN.fullmatch("abcdefgh") is None  # 8 chars


def test_code_pattern_rejects_non_alphanumeric_characters():
    assert CODE_PATTERN.fullmatch("abc-efg") is None
    assert CODE_PATTERN.fullmatch("abc_efg") is None
    assert CODE_PATTERN.fullmatch("abc efg") is None
    assert CODE_PATTERN.fullmatch("abc.efg") is None


def test_code_pattern_rejects_empty_string():
    assert CODE_PATTERN.fullmatch("") is None


def test_code_pattern_rejects_trailing_newline():
    # In Python, `$` also matches immediately before a trailing newline, so
    # a pattern anchored with `^...$` would wrongly accept "abcdefg\n". This
    # is the test that proves CODE_PATTERN is anchored with \A/\Z (or
    # equivalent fullmatch semantics) instead.
    assert CODE_PATTERN.fullmatch("abcdefg\n") is None
    assert re.match(r"\A[A-Za-z0-9]{7}\Z", "abcdefg\n") is None


def test_code_pattern_rejects_leading_newline():
    assert CODE_PATTERN.fullmatch("\nabcdefg") is None
