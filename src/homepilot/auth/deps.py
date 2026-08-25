from __future__ import annotations

from datetime import UTC
from typing import Any

from fastapi import Cookie, Depends, Header, HTTPException, Request, status

from ..db.repository import Repository
from .tokens import PREFIX_LENGTH, normalize_scope, validate_token

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Marker attribute set on any dependency callable that enforces a scope (or an
# explicit admin/secret gate). The startup route-scope guard (see main.py) walks
# each route's dependency tree looking for this marker, so a new non-public route
# that ships without a scope dependency fails fast at app construction.
SCOPE_ENFORCER_ATTR = "_hp_enforces_scope"

# The scope string a require_scope() dependency enforces, recorded on the
# callable so a route's required scope can be resolved exactly (not just "some
# scope is enforced"). The MCP tier<->API scope anti-escalation gate reads it to
# prove every MCP tool sits at exactly its route's scope tier.
REQUIRED_SCOPE_ATTR = "_hp_required_scope"


def get_db(request: Request) -> Repository:
    repo: Repository = request.app.state.repo
    return repo


async def require_token(
    request: Request,
    authorization: str | None = Header(None),
    hp_token: str | None = Cookie(None),
    hp_csrf: str | None = Cookie(None),
    db: Repository = Depends(get_db),  # noqa: B008
) -> dict[str, Any]:
    # --- resolve raw token and auth method ---
    raw_token: str | None = None
    using_cookie = False

    if authorization:
        parts = authorization.split(" ", 1)
        if len(parts) != 2 or parts[0].lower() != "bearer":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid Authorization header format",
            )
        raw_token = parts[1]
    elif hp_token:
        raw_token = hp_token
        using_cookie = True

    if not raw_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing credentials",
        )

    # --- validate token against DB ---
    prefix = raw_token[:PREFIX_LENGTH]
    row = await db.get_token_by_prefix(prefix)
    if row is None or not validate_token(raw_token, row["hash"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
        )
    if row.get("expires_at"):
        from datetime import datetime

        expires = datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00"))
        if expires <= datetime.now(UTC):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token expired",
            )

    # --- CSRF check for cookie-authenticated mutations ---
    # Bearer token auth (CLI / MCP) is not a browser flow; no CSRF risk.
    if using_cookie and request.method not in _SAFE_METHODS:
        csrf_header = request.headers.get("x-csrf-token")
        if not hp_csrf or csrf_header != hp_csrf:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="CSRF token missing or invalid",
            )
        x_requested_with = request.headers.get("x-requested-with")
        if not x_requested_with:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Missing X-Requested-With header (CSRF protection)",
            )

    await db.touch_token_last_used(row["id"])
    user = await db.get_user_by_id(row["user_id"])
    return {
        "user_id": row["user_id"],
        "token_id": row["id"],
        "display_name": user["display_name"] if user else None,
        "scope": row.get("scope"),
        "role": row.get("role"),
    }


_require_token_dep = Depends(require_token)


def require_scope(scope: str) -> Any:
    async def _check(token: dict[str, Any] = _require_token_dep) -> dict[str, Any]:
        token_scope = token.get("scope")
        token_role = token.get("role")
        normalized = normalize_scope(token_scope, token_role)
        if not normalized:
            raise HTTPException(
                status_code=403,
                detail=f"Token has no scope assigned — requires '{scope}'",
            )
        if "*" in normalized or scope in normalized:
            return token
        raise HTTPException(
            status_code=403,
            detail=f"Insufficient scope: requires '{scope}', has '{token_scope}'",
        )

    setattr(_check, SCOPE_ENFORCER_ATTR, True)
    setattr(_check, REQUIRED_SCOPE_ATTR, scope)
    return _check
