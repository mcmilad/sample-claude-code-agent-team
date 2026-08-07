"""Short-code minting.

Pure module: no boto3, no network. See design.md#interface-contracts -- this
is the shared contract both create_handler and redirect_handler depend on, so
the signatures here are fixed at spec time, not implementation details.
"""

import re
import secrets
import string

# 62 symbols: A-Z a-z 0-9. Non-enumerable by construction (see
# spec.md -> Design Decisions -> Code minting: rejected a monotonic counter
# because it leaks volume and lets anyone walk the corpus).
ALPHABET: str = string.ascii_letters + string.digits

CODE_LENGTH: int = 7

# Fully anchored with \A/\Z rather than ^/$ -- in Python, $ also matches
# immediately before a trailing newline, so ^[A-Za-z0-9]{7}$ would wrongly
# accept "abcdefg\n". \A/\Z admit no such exception.
CODE_PATTERN: re.Pattern = re.compile(rf"\A[A-Za-z0-9]{{{CODE_LENGTH}}}\Z")


def generate_code() -> str:
    """Return a cryptographically-random 7-character code.

    Uses `secrets.choice`, never `random`: `random` is seeded
    deterministically, which would make codes guessable and defeat the whole
    reason minting is not a monotonic counter.

    Not guaranteed unique -- callers that persist the result (create_handler,
    per spec F6) are responsible for detecting a collision (e.g. a
    conditional put) and retrying with a fresh call.
    """
    return "".join(secrets.choice(ALPHABET) for _ in range(CODE_LENGTH))
