from __future__ import annotations

import secrets

from ..auth.tokens import PREFIX_LENGTH, hash_token, validate_token

# Distinct from the api_tokens 'hp_' and the invite 'hpi_' prefixes so a claim
# code is never mistaken for - or accepted as - either. Hashing (sha256 over the
# whole code), the constant-time compare, and the prefix-as-first-PREFIX_LENGTH-
# characters convention are the api_tokens scheme unchanged.
PREFIX = "hpc_"

# 16 bytes = 128 bits, half the api_tokens length. A claim code is TRANSCRIBED
# by a person out of `docker compose logs` into a browser field, unlike an API
# token (pasted by a machine) or an invite URL (clicked); 128 bits behind a rate
# limiter is far past any brute-force reach, and the shorter code is the part an
# operator actually has to handle.
CODE_BYTES = 16

__all__ = [
    "CODE_BYTES",
    "PREFIX",
    "PREFIX_LENGTH",
    "generate_claim_code",
    "hash_token",
    "validate_token",
]


def generate_claim_code() -> tuple[str, str, str]:
    """Return (code, prefix, code_hash). The code is shown ONCE per generation."""
    raw = secrets.token_hex(CODE_BYTES)
    code = f"{PREFIX}{raw}"
    return code, code[:PREFIX_LENGTH], hash_token(code)
