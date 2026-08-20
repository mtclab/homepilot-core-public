from __future__ import annotations

import logging
import os
import secrets
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from ..common import SlidingWindowLimiter
from ..config import get_settings
from ..db.repository import Repository
from .deps import SCOPE_ENFORCER_ATTR, get_db, require_scope, require_token
from .tokens import PREFIX_LENGTH, generate_api_token, normalize_scope, validate_token

logger = logging.getLogger(__name__)

router = APIRouter(tags=["auth"])

_get_db_dep = Depends(get_db)
_require_admin_dep = Depends(require_scope("admin"))
_require_token_dep = Depends(require_token)

_COOKIE_HP_TOKEN = "hp_token"
_COOKIE_HP_CSRF = "hp_csrf"

_same_site_lax: Literal["lax"] = "lax"

_TOKEN_CREATE_RATE_LIMIT = 5
_TOKEN_CREATE_RATE_WINDOW = 60
_token_create_attempts = SlidingWindowLimiter(
    limit=_TOKEN_CREATE_RATE_LIMIT, window_seconds=_TOKEN_CREATE_RATE_WINDOW
)


def _cookie_secure(request: Request | None = None) -> bool:
    if not get_settings().cookie_secure:
        return False
    return not (request is not None and request.url.scheme == "http")


async def resolve_admin_secret(request: Request) -> str:
    """Resolve admin secret from settings, then vault at runtime, then env."""
    settings = get_settings()
    admin_secret = settings.admin_secret
    if admin_secret:
        return admin_secret

    vault = getattr(request.app.state, "vault", None)
    if vault is not None:
        from ..vault import VaultError

        try:
            secret_data = await vault.get_secret("admin-secret")
            val = secret_data.get("secret", "") or secret_data.get("value", "")
            if val:
                return str(val)
            for v in secret_data.values():
                if isinstance(v, str) and v:
                    return v
        except (VaultError, OSError):
            logger.debug("Vault 'admin-secret' unavailable at runtime", exc_info=True)

    env_secret = os.environ.get("HP_ADMIN_SECRET", "")
    if env_secret:
        return env_secret

    return ""


async def require_admin_or_secret(
    request: Request,
    authorization: str | None = Header(None),
    hp_token: str | None = Cookie(None),
    hp_csrf: str | None = Cookie(None),
    db: Repository = _get_db_dep,
) -> dict[str, Any]:
    """Authorize token-management (list/revoke) by EITHER an admin-scope token
    OR the admin secret.

    The sibling POST /tokens (create) already accepts the admin secret, but the
    list/revoke endpoints only honored an admin-scope bearer — so the CLI, which
    holds the admin secret (vault) rather than a token, got 401 on `hp token
    list`/`revoke`. Accept the admin secret here too. When the secret is used
    there is no user context, so the caller sees the fleet-wide token list.
    """
    header_secret = request.headers.get("x-hp-admin-secret") or request.headers.get(
        "x-admin-secret", ""
    )
    if header_secret:
        admin_secret = await resolve_admin_secret(request)
        if admin_secret and secrets.compare_digest(header_secret.encode(), admin_secret.encode()):
            return {"auth": "admin-secret", "user_id": None}

    token = await require_token(request, authorization, hp_token, hp_csrf, db)
    normalized = normalize_scope(token.get("scope"), token.get("role"))
    if "*" in normalized or "admin" in normalized:
        return token
    raise HTTPException(
        status_code=403,
        detail=f"Insufficient scope: requires 'admin', has '{token.get('scope')}'",
    )


# This is an explicit admin/secret gate; mark it so the startup route-scope
# guard (main.py) counts it as satisfying a route's scope requirement.
setattr(require_admin_or_secret, SCOPE_ENFORCER_ATTR, True)

_require_admin_or_secret_dep = Depends(require_admin_or_secret)


class LoginRequest(BaseModel):
    token: str = Field(..., max_length=256)


class TokenCreateRequest(BaseModel):
    label: str = "admin"
    scope: str = "full"


async def _validate_bearer(token: str, db: Repository) -> dict[str, Any]:
    prefix = token[:PREFIX_LENGTH]
    row = await db.get_token_by_prefix(prefix)
    if row is None or not validate_token(token, row["hash"]):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    if row.get("expires_at"):
        expires = datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00"))
        if expires <= datetime.now(UTC):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expired")
    return row


@router.get("/me")
async def me(
    request: Request,
    authorization: str | None = Header(None),
    db: Repository = _get_db_dep,
) -> dict[str, Any]:
    raw_token: str | None = None

    if authorization:
        parts = authorization.split(" ", 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            raw_token = parts[1].strip()

    if not raw_token:
        raw_token = request.cookies.get(_COOKIE_HP_TOKEN)

    if not raw_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    row = await _validate_bearer(raw_token, db)
    label = row.get("label") or row.get("prefix", "")
    # Expose the token prefix and a normalized capability list so the UI stops
    # re-deriving scope logic (which it did incorrectly). normalize_scope maps
    # scope+role to base capabilities and expands "*"/"full" to the full set.
    normalized = normalize_scope(row.get("scope"), row.get("role"))
    if "*" in normalized:
        capabilities = ["read", "write", "admin"]
    else:
        order = {"read": 0, "write": 1, "admin": 2}
        capabilities = sorted({c for c in normalized if c in order}, key=lambda c: order[c])
    return {
        "authenticated": True,
        "token_label": label,
        "prefix": row.get("prefix"),
        "scope": row.get("scope"),
        "role": row.get("role"),
        "capabilities": capabilities,
    }


@router.get("/tokens")
async def list_tokens(
    auth: dict[str, Any] = _require_admin_or_secret_dep,
    db: Repository = _get_db_dep,
) -> dict[str, Any]:
    user_id = auth.get("user_id")
    # Admin-secret callers (the CLI) have no user context — show the fleet-wide
    # token list. A user-scoped admin token sees only its own tokens.
    if user_id is None:
        rows = await db.list_all_tokens()
    else:
        rows = await db.list_tokens_for_user(str(user_id))
    return {"items": rows, "total": len(rows)}


@router.post("/login")
async def login(
    body: LoginRequest, request: Request, response: Response, db: Repository = _get_db_dep
) -> dict[str, str]:
    raw_token = body.token.strip()
    row = await _validate_bearer(raw_token, db)

    # Security note: fingerprint is IP:User-Agent — both are spoofable by an
    # attacker with access to the token+cookie pair. This provides defense in
    # depth only; the primary protection is the HMAC-secure token comparison
    # and the cookie's HttpOnly/SameSite flags.
    stored_fingerprint = row.get("fingerprint")
    current_fingerprint = request.headers.get(
        "x-hp-session-fingerprint",
        f"{(request.client.host if request.client else 'unknown')}"
        f":{request.headers.get('user-agent', '')}",
    )

    if stored_fingerprint and stored_fingerprint != current_fingerprint:
        # A personal API token is shared across the UI, the CLI, and any API
        # integration. Rotating-and-deleting it on a fingerprint mismatch
        # silently killed the token for every OTHER holder the moment a second
        # client logged in from a different IP/User-Agent — the cause of
        # "tokens die within minutes" (#323). Fingerprint is advisory only
        # (the security note above says as much: the real protection is the
        # HMAC token compare + the cookie flags), so log the mismatch and keep
        # the token alive instead of destroying it.
        logger.info(
            "token fingerprint mismatch for prefix=%s — proceeding (advisory)",
            str(row.get("prefix", ""))[:16],
        )
    elif not stored_fingerprint:
        await db.update_token_fingerprint(row["id"], current_fingerprint)
        await db.db.conn.commit()

    csrf_token = secrets.token_hex(32)
    response.set_cookie(
        _COOKIE_HP_TOKEN,
        raw_token,
        httponly=True,
        samesite=_same_site_lax,
        secure=_cookie_secure(request),
        path="/",
        max_age=60 * 60 * 24 * 30,
    )
    response.set_cookie(
        _COOKIE_HP_CSRF,
        csrf_token,
        httponly=False,
        samesite=_same_site_lax,
        secure=_cookie_secure(request),
        path="/",
        max_age=60 * 60 * 24 * 30,
    )
    return {"status": "ok"}


@router.post("/logout")
async def logout(request: Request, response: Response) -> dict[str, str]:
    # Logout clears the session COOKIE only — it must NOT delete the API token
    # (#389). A personal token is shared across the UI, the CLI, and any MCP/API
    # client; hard-deleting it here logged the browser out AND silently killed
    # every other holder of the same token. Token destruction stays behind the
    # explicit Tokens -> Revoke path (DELETE /auth/tokens/{prefix}) only.
    response.delete_cookie(
        _COOKIE_HP_TOKEN, path="/", secure=_cookie_secure(request), samesite=_same_site_lax
    )
    response.delete_cookie(
        _COOKIE_HP_CSRF, path="/", secure=_cookie_secure(request), samesite=_same_site_lax
    )
    return {"status": "ok"}


@router.post("/tokens", status_code=201)
async def admin_create_token(
    body: TokenCreateRequest,
    request: Request,
    db: Repository = _get_db_dep,
) -> dict[str, str]:
    """Create an API token via admin secret. Checks vault at runtime if not in settings."""
    client_ip = request.client.host if request.client else "unknown"
    if not _token_create_attempts.allow(client_ip):
        raise HTTPException(status_code=429, detail="Rate limit exceeded for token creation")

    admin_secret = await resolve_admin_secret(request)
    if not admin_secret:
        logger.warning("Admin token creation attempted but HP_ADMIN_SECRET is not configured")
        raise HTTPException(
            status_code=403,
            detail="HP_ADMIN_SECRET must be configured to use admin token creation",
        )
    # Accept both X-Hp-Admin-Secret (canonical) and X-Admin-Secret (common convention)
    header_secret = request.headers.get("x-hp-admin-secret") or request.headers.get(
        "x-admin-secret", ""
    )
    if not secrets.compare_digest(header_secret.encode(), admin_secret.encode()):
        raise HTTPException(status_code=403, detail="Invalid admin secret")

    full_token, prefix, token_hash = generate_api_token()
    existing_users = await db.db.fetchall("SELECT id FROM users LIMIT 1")
    if existing_users:
        user_id = str(existing_users[0]["id"])
    else:
        user_id = str(await db.create_user(display_name=body.label, auth_source="api_token"))

    await db.create_api_token(
        user_id=user_id,
        token_type="personal",
        prefix=prefix,
        hash=token_hash,
        scope=body.scope,
        label=body.label,
        expires_at=None,
    )
    await db.db.conn.commit()
    return {"token": full_token, "scope": body.scope}


@router.delete("/tokens/{prefix}", status_code=204)
async def revoke_token(
    prefix: str,
    auth: dict[str, Any] = _require_admin_or_secret_dep,
    db: Repository = _get_db_dep,
) -> Response:
    """Revoke an API token by its prefix.

    Admin-only endpoint. The prefix-based lookup means the prefix
    namespace is exposed; rate limiting should be applied in production
    to mitigate token-prefix enumeration risk.

    Migration note (#218): prefix length increased from 8 to 16 chars
    (hp_ + 13 hex = 52 bits entropy). Existing 8-char prefixes in the DB
    will continue to work for lookup but new tokens use 16-char prefixes.
    """
    row = await db.get_token_by_prefix(prefix)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Token not found")
    await db.delete_token(row["id"])
    logger.info("Token revoked: prefix=%s, by=%s", prefix, auth.get("prefix", "admin-secret"))
    await db.db.conn.commit()
    return Response(status_code=204)
