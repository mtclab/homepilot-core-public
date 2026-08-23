"""The console must not rate-limit its own operator (#518 follow-up).

THE STARVATION BUG, found by the public mirror's live-browser e2e and NOT by
the first fix (raising the authenticated limit did nothing): every request
counted into ONE per-IP window, but /auth/login is always held to the
anonymous limit - so 60 authenticated UI calls in a minute made login
impossible from that IP. An operator whose console was busy could not log in
from a second browser, and a fleet of API clients behind one NAT could lock
the UI out entirely.

Two lanes now: requests carrying credentials compete only with each other for
the authenticated limit; login and anonymous traffic only with each other for
the anonymous one. Junk credentials fall back to the anonymous lane once
verification starts (past the anonymous count), so a cookie-stuffing flood
cannot buy the higher limit.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest
from fastapi.responses import JSONResponse

from homepilot.main import (
    _AUTH_RATE_LIMIT,
    _RATE_LIMIT,
    _RATE_WINDOW,
    _RATE_WINDOW_AUTH,
    rate_limit_middleware,
)

pytestmark = pytest.mark.asyncio

IP = "9.9.9.9"


def _request(path: str, *, cookie_token: str | None = None) -> MagicMock:
    req = MagicMock()
    req.url.path = path
    req.client.host = IP
    req.headers = {}
    req.cookies = {"hp_token": cookie_token} if cookie_token else {}
    # A repo that fails every lookup: any credential presented is junk.
    req.app.state.repo = None
    return req


async def _ok(req: MagicMock) -> JSONResponse:
    return JSONResponse({"ok": True})


@pytest.fixture(autouse=True)
def _clean_windows():
    _RATE_WINDOW.clear()
    _RATE_WINDOW_AUTH.clear()
    yield
    _RATE_WINDOW.clear()
    _RATE_WINDOW_AUTH.clear()


class TestLoginIsNeverStarvedByUiTraffic:
    async def test_a_busy_console_does_not_block_login(self):
        """THE bug: fill the authenticated lane to the brim - login must still
        work, because it competes in the anonymous lane."""
        _RATE_WINDOW_AUTH[IP] = [time.time()] * (_AUTH_RATE_LIMIT - 1)

        resp = await rate_limit_middleware(_request("/auth/login"), _ok)

        assert resp.status_code == 200, (
            "login was starved by the console's own authenticated traffic"
        )

    async def test_credentialed_traffic_does_not_consume_the_anonymous_lane(self):
        """The mirror image: UI calls with a cookie must not fill the window
        that anonymous requests (and login) are limited against."""
        for _ in range(10):
            resp = await rate_limit_middleware(
                _request("/hosts", cookie_token="hp_sessioncookievalue"), _ok
            )
            assert resp.status_code == 200
        assert len(_RATE_WINDOW.get(IP, [])) == 0, (
            "credentialed requests were counted into the anonymous window"
        )
        assert len(_RATE_WINDOW_AUTH.get(IP, [])) == 10


class TestJunkCredentialsBuyNothing:
    async def test_a_cookie_stuffing_flood_falls_back_to_the_anonymous_limit(self):
        """Past the anonymous count, credentials are VERIFIED; junk ones drop
        to the anonymous lane - presence of a cookie must not rent the higher
        limit. (repo is None here, so every lookup fails.)"""
        # The credentialed lane already saw more than the anonymous limit.
        _RATE_WINDOW_AUTH[IP] = [time.time()] * (_RATE_LIMIT + 5)
        # And the anonymous lane is full.
        _RATE_WINDOW[IP] = [time.time()] * _RATE_LIMIT

        resp = await rate_limit_middleware(_request("/hosts", cookie_token="junk-cookie"), _ok)

        assert resp.status_code == 429, "an unverifiable cookie bought the authenticated rate limit"

    async def test_anonymous_flood_still_capped(self):
        _RATE_WINDOW[IP] = [time.time()] * _RATE_LIMIT
        resp = await rate_limit_middleware(_request("/auth/login"), _ok)
        assert resp.status_code == 429
