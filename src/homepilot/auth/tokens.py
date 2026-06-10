from __future__ import annotations

import hashlib
import hmac
import secrets

from .scopes import ROLE_SCOPES, SCOPE_ADMIN

PREFIX = "hp_"
TOKEN_BYTES = 32
PREFIX_LENGTH = 16

SCOPE_FULL = "full"
SCOPE_READ_ONLY = "read_only"

_READ_ONLY_SCOPES = {"read"}
_FULL_SCOPES = {"read", "write"}
_MUTATING_ACTIONS = {"write"}


def normalize_scope(scope: str | None, role: str | None = None) -> list[str]:
    if role and role in ROLE_SCOPES:
        return list(ROLE_SCOPES[role])
    if not scope:
        return []
    stripped = scope.strip()
    if stripped in (SCOPE_FULL, "*"):
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
