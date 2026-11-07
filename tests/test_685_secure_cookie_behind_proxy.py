"""The session cookies keep `Secure` behind the documented TLS proxy (#685).

With HP_COOKIE_SECURE=true, `_cookie_secure` deferred to `request.url.scheme`.
Behind the documented deployment - TLS terminated on a SEPARATE proxy host,
plain HTTP to the backend - the backend always sees `http` (uvicorn only trusts
X-Forwarded-Proto from 127.0.0.1), so `hp_token`, a 30-day admin credential,
was issued without `Secure` and a browser would also send it over plain HTTP.

The rule now:

* HP_COOKIE_SECURE=false is the explicit opt-out: never Secure.
* Otherwise Secure whenever the client's leg is, or may be, TLS: the backend's
  own scheme is https; OR X-Forwarded-Proto says https (from ANY peer - the
  header can only make the cookie stricter, so a forged one gains nothing); OR
  the peer is a trusted proxy (HP_TRUSTED_PROXIES) that did not say http.
* Not Secure only for a DIRECT plain-HTTP connection - the README's own
  install path (`http://<host>:8000/ui` from the LAN). A Secure cookie there
  would never be stored and the operator could not log in; the token it
  answers already crossed that connection in the clear.
"""

from __future__ import annotations

import ipaddress
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from homepilot.auth.router import router as auth_router
from homepilot.auth.tokens import generate_api_token
from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.db.repository import Repository

PROXY = "10.0.0.2"


@pytest.fixture
async def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from homepilot.config import get_settings

    db = Database(str(tmp_path / "hp.db"))
    await db.connect()
    await run_migrations(db)
    repo = Repository(db)
    user_id = await repo.create_user("owner", "api_token")
    token, prefix, token_hash = generate_api_token()
    await repo.create_api_token(
        user_id=str(user_id), token_type="personal", prefix=prefix, hash=token_hash, scope="all"
    )

    def make(peer: str, *, secure: str = "true", trusted: bool = False) -> AsyncClient:
        monkeypatch.setenv("HP_COOKIE_SECURE", secure)
        get_settings.cache_clear()
        app = FastAPI()
        app.include_router(auth_router, prefix="/auth")
        app.state.repo = repo
        app.state.trusted_proxy_networks = [ipaddress.ip_network(f"{PROXY}/32")] if trusted else []
        return AsyncClient(
            transport=ASGITransport(app=app, client=(peer, 5555)), base_url="http://backend"
        )

    yield make, token
    get_settings.cache_clear()
    await db.close()


def _secure_flags(resp) -> dict[str, bool]:
    flags = {}
    for header in resp.headers.get_list("set-cookie"):
        name = header.split("=", 1)[0]
        flags[name] = "; secure" in header.lower()
    return flags


async def _login(client: AsyncClient, token: str, headers: dict[str, str] | None = None):
    async with client:
        return await client.post("/auth/login", json={"token": token}, headers=headers or {})


class TestSecureBehindTheProxy:
    async def test_tls_proxy_forwarding_https_gets_secure_cookies(self, env):
        """The documented deployment: TLS proxy on another host, http to the
        backend, X-Forwarded-Proto: https. The repro from the report."""
        make, token = env
        resp = await _login(make(PROXY), token, {"x-forwarded-proto": "https"})
        assert resp.status_code == 200, resp.text
        assert _secure_flags(resp) == {"hp_token": True, "hp_csrf": True}

    async def test_trusted_proxy_that_names_no_scheme_fails_closed(self, env):
        make, token = env
        resp = await _login(make(PROXY, trusted=True), token)
        assert _secure_flags(resp) == {"hp_token": True, "hp_csrf": True}

    async def test_trusted_proxy_saying_http_is_believed(self, env):
        make, token = env
        resp = await _login(make(PROXY, trusted=True), token, {"x-forwarded-proto": "http"})
        assert _secure_flags(resp) == {"hp_token": False, "hp_csrf": False}

    async def test_logout_clears_the_secure_cookie_it_set(self, env):
        make, _token = env
        async with make(PROXY) as client:
            resp = await client.post("/auth/logout", headers={"x-forwarded-proto": "https"})
        assert _secure_flags(resp) == {"hp_token": True, "hp_csrf": True}


class TestTheInstallPathStillLogsIn:
    async def test_direct_plain_http_from_the_lan_is_not_secure(self, env):
        """README: open http://<host>:8000/ui from another machine and claim it.
        A Secure cookie over that connection is dropped by the browser and the
        operator can never log in, so it must stay usable."""
        make, token = env
        resp = await _login(make("192.168.1.20"), token)
        assert _secure_flags(resp) == {"hp_token": False, "hp_csrf": False}

    async def test_forged_proto_from_an_untrusted_peer_can_only_tighten(self, env):
        make, token = env
        resp = await _login(make("192.168.1.20"), token, {"x-forwarded-proto": "https"})
        assert _secure_flags(resp) == {"hp_token": True, "hp_csrf": True}


class TestTheOptOut:
    async def test_cookie_secure_false_is_never_secure(self, env):
        make, token = env
        resp = await _login(make(PROXY, secure="false"), token, {"x-forwarded-proto": "https"})
        assert _secure_flags(resp) == {"hp_token": False, "hp_csrf": False}
