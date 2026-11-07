"""A browser session gets the authenticated rate limit (#689).

WHAT THIS FORBIDS: the rate-limit middleware's token check (`_is_authenticated`)
demanding `X-CSRF-Token` on EVERY cookie request, GETs included. The UI never
sends the CSRF header on a GET (nor should it - `require_token` checks CSRF only
on unsafe methods), so once a session passed the anonymous count every console
GET was judged unauthenticated and dropped to the anonymous lane: measured, a
cookie session got 120 of the 300 requests a minute #514 promised, while a
bearer token got all 300.

The journey drives the real middleware against a real token store as the
browser does: cookie + no CSRF header on GETs. The other arm keeps CSRF where
it belongs: an unsafe cookie request without it is still not authenticated.

Teeth: restore the unconditional CSRF check and the GET journey stops at the
anonymous limit.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.responses import JSONResponse

from homepilot.auth.tokens import generate_api_token
from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.db.repository import Repository
from homepilot.main import (
    _AUTH_RATE_LIMIT,
    _RATE_LIMIT,
    _RATE_WINDOW,
    _RATE_WINDOW_AUTH,
    _is_authenticated,
    rate_limit_middleware,
)

IP = "203.0.113.50"


@pytest.fixture(autouse=True)
def _clean_windows():
    _RATE_WINDOW.clear()
    _RATE_WINDOW_AUTH.clear()
    yield
    _RATE_WINDOW.clear()
    _RATE_WINDOW_AUTH.clear()


@pytest.fixture
async def session(tmp_path: Path):
    db = Database(str(tmp_path / "rl.db"))
    await db.connect()
    await run_migrations(db)
    repo = Repository(db)
    uid = await repo.create_user("operator")
    token, prefix, digest = generate_api_token()
    await repo.create_api_token(uid, "session", prefix, digest, scope="*", label="browser")
    await db.conn.commit()
    yield repo, token
    await db.close()


def _browser(repo: Repository, token: str, method: str = "GET", headers=None) -> MagicMock:
    req = MagicMock()
    req.method = method
    req.url.path = "/inventory"
    req.client.host = IP
    req.headers = dict(headers or {})
    req.cookies = {"hp_token": token, "hp_csrf": "csrf-value"}
    req.app.state.repo = repo
    return req


async def _ok(_req: MagicMock) -> JSONResponse:
    return JSONResponse({"ok": True})


async def test_a_cookie_session_gets_the_authenticated_limit(session):
    repo, token = session
    allowed = 0
    for _ in range(_AUTH_RATE_LIMIT + 5):
        resp = await rate_limit_middleware(_browser(repo, token), _ok)
        if resp.status_code == 200:
            allowed += 1
    assert allowed == _AUTH_RATE_LIMIT, (
        f"a cookie session got {allowed} requests, the authenticated limit is "
        f"{_AUTH_RATE_LIMIT} (anonymous is {_RATE_LIMIT})"
    )


async def test_a_cookie_get_without_csrf_is_authenticated(session):
    repo, token = session
    assert await _is_authenticated(_browser(repo, token, "GET"))
    assert await _is_authenticated(_browser(repo, token, "HEAD"))


async def test_an_unsafe_cookie_request_still_needs_csrf(session):
    repo, token = session
    assert not await _is_authenticated(_browser(repo, token, "POST"))
    assert not await _is_authenticated(
        _browser(repo, token, "POST", {"x-csrf-token": "wrong", "x-requested-with": "x"})
    )
    assert not await _is_authenticated(
        _browser(repo, token, "DELETE", {"x-csrf-token": "csrf-value"})
    )
    assert await _is_authenticated(
        _browser(repo, token, "POST", {"x-csrf-token": "csrf-value", "x-requested-with": "x"})
    )
