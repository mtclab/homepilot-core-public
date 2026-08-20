from __future__ import annotations

import secrets

from ..auth.tokens import PREFIX_LENGTH, TOKEN_BYTES, hash_token, validate_token

# Distinct from the api_tokens 'hp_' prefix so an invite URL is never mistaken
# for — or accepted as — an API credential. Everything else (byte count, prefix
# derivation as the first PREFIX_LENGTH characters of the whole token, sha256
# hashing, constant-time comparison) is the api_tokens scheme unchanged.
PREFIX = "hpi_"

__all__ = ["PREFIX", "PREFIX_LENGTH", "generate_invite_token", "hash_token", "validate_token"]


def generate_invite_token() -> tuple[str, str, str]:
    """Return (full_token, prefix, token_hash). The full token is shown ONCE."""
    raw = secrets.token_hex(TOKEN_BYTES)
    full_token = f"{PREFIX}{raw}"
    return full_token, full_token[:PREFIX_LENGTH], hash_token(full_token)
