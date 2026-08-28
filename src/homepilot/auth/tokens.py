from __future__ import annotations

import hashlib
import hmac
import secrets

from .scopes import (
    API_SCOPE_TO_MCP_TIER,
    ROLE_SCOPES,
    SCOPE_ADMIN,
    SCOPE_READ,
    SCOPE_WRITE,
)

PREFIX = "hp_"
TOKEN_BYTES = 32
PREFIX_LENGTH = 16

SCOPE_ALL = "all"
SCOPE_FULL_LEGACY = "full"  # accepted forever; never advertised (#579)
SCOPE_READ_ONLY = "read_only"

_READ_ONLY_SCOPES = {"read"}
_FULL_SCOPES = {"read", "write"}
_MUTATING_ACTIONS = {"write"}


def normalize_scope(scope: str | None, role: str | None = None) -> list[str]:
    """Normalize a scope string to the stored scope list.

    The superuser scope (= "*", everything - what `hp init` mints) is
    ADVERTISED as "all". The word "full" used to name it and collided with
    the MCP tool tier "full" (= the WRITE tier, API scope "write") - #579.
    "full" is still accepted here forever so no existing token or script
    breaks, but nothing operator-facing says it any more; the only "full"
    an operator now reads is the MCP write tier.
    """
    if role and role in ROLE_SCOPES:
        return list(ROLE_SCOPES[role])
    if not scope:
        return []
    stripped = scope.strip()
    if stripped in (SCOPE_ALL, SCOPE_FULL_LEGACY, "*"):
        return ["*"]
    if stripped == SCOPE_READ_ONLY:
        return ["read"]
    parts = [s.strip() for s in stripped.split(",") if s.strip()]
    if SCOPE_ADMIN in parts:
        parts = sorted({*parts, "read", "write", SCOPE_ADMIN})
    return parts


def scope_allows(scope: str | None, action: str, role: str | None = None) -> bool:
    normalized = normalize_scope(scope, role)
    if not normalized:
        return False
    if "*" in normalized:
        return True
    return action in normalized


def mcp_tier_for_token(scope: str | None, role: str | None = None) -> str | None:
    """The MCP tool tier an API token's scope grants, or None for no access.

    The strongest capability the token holds wins, resolved through
    API_SCOPE_TO_MCP_TIER - the same map the MCP tier<->API scope parity gate
    reads - so an admin token gets the admin tier, a write token the full tier,
    a read token read_only, and a token with no usable capability nothing at all.
    """
    normalized = normalize_scope(scope, role)
    if not normalized:
        return None
    if "*" in normalized:
        return API_SCOPE_TO_MCP_TIER[SCOPE_ADMIN]
    for api_scope in (SCOPE_ADMIN, SCOPE_WRITE, SCOPE_READ):
        if api_scope in normalized:
            return API_SCOPE_TO_MCP_TIER[api_scope]
    return None


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def validate_token(token: str, token_hash: str) -> bool:
    computed = hash_token(token)
    return hmac.compare_digest(computed, token_hash)


def generate_api_token() -> tuple[str, str, str]:
    raw = secrets.token_hex(TOKEN_BYTES)
    full_token = f"{PREFIX}{raw}"
    prefix = full_token[:PREFIX_LENGTH]
    token_hash = hash_token(full_token)
    return full_token, prefix, token_hash
