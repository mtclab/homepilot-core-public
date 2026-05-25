from __future__ import annotations

import logging
import os
import secrets
import time
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from ..config import get_settings
from ..db.repository import Repository
from .deps import get_db, require_scope, require_token
from .tokens import PREFIX_LENGTH, generate_api_token, validate_token

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
_token_create_attempts: dict[str, list[float]] = defaultdict(list)


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
                return val
            for v in secret_data.values():
                if isinstance(v, str) and v:
                    return v
        except (VaultError, OSError):
            logger.debug("Vault 'admin-secret' unavailable at runtime", exc_info=True)

    env_secret = os.environ.get("HP_ADMIN_SECRET", "")
    if env_secret:
        return env_secret

    return ""


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
    return {
        "authenticated": True,
        "token_label": label,
        "scope": row.get("scope"),
        "role": row.get("role"),
    }


@router.get("/tokens")
async def list_tokens(
    token: dict[str, Any] = _require_admin_dep,
    db: Repository = _get_db_dep,
) -> dict[str, Any]:
    user_id = token.get("user_id")
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
        full_token, prefix, token_hash = generate_api_token()
        await db.create_api_token(
            user_id=row["user_id"],
            token_type="personal",
            prefix=prefix,
            hash=token_hash,
            scope=row.get("scope", "full"),
            label=row.get("label", "rotated"),
            fingerprint=current_fingerprint,
        )
        await db.delete_token(row["id"])
        await db.db.conn.commit()
        raw_token = full_token
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
async def logout(
    request: Request, response: Response, db: Repository = _get_db_dep
) -> dict[str, str]:
    raw_token: str | None = None
    authorization = request.headers.get("authorization")
    if authorization:
        parts = authorization.split(" ", 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            raw_token = parts[1].strip()
    if not raw_token:
        raw_token = request.cookies.get(_COOKIE_HP_TOKEN)

    if raw_token:
        try:
            row = await _validate_bearer(raw_token, db)
            await db.delete_token(row["id"])
            await db.db.conn.commit()
        except HTTPException:
            pass

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
    now = time.time()
    window = _token_create_attempts[client_ip]
    _token_create_attempts[client_ip] = [
        ts for ts in window if now - ts < _TOKEN_CREATE_RATE_WINDOW
    ]
    if len(_token_create_attempts[client_ip]) >= _TOKEN_CREATE_RATE_LIMIT:
        raise HTTPException(status_code=429, detail="Rate limit exceeded for token creation")
    _token_create_attempts[client_ip].append(now)

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
    token: dict[str, Any] = _require_admin_dep,
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
    logger.info("Token revoked: prefix=%s, by=%s", prefix, token.get("prefix", "unknown"))
    await db.db.conn.commit()
    return Response(status_code=204)
