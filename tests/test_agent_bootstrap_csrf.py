"""Minting a bootstrap token is a MUTATION, so it must not be reachable by GET.

``POST /agents/bootstrap`` creates a one-time credential that enrols a host into
the fleet. It used to be a ``GET``. The CSRF gate in ``auth/deps.py`` deliberately
skips safe methods (a GET is not a state change, by contract), so a
cookie-authenticated admin browsing any attacker-controlled page could be made to
mint a fleet-enrolment token cross-origin - the exact shape CSRF protection
exists to stop.

Teeth: flip the decorator back to ``@router.get("/bootstrap", ...)`` and
``test_get_is_not_a_way_to_mint`` fails on the 405 assertion while
``test_cookie_auth_without_csrf_headers_cannot_mint`` fails on the 403 - the GET
sails past the CSRF gate and returns a live token.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.db.repository import Repository

pytestmark = pytest.mark.asyncio

MINTED = "hpbat_REDACTED_TOKEN"


class _TokenStore:
    def __init__(self) -> None:
        self.mints = 0

    async def create(self) -> str:
        self.mints += 1
        return MINTED


class _Registry:
    def __init__(self) -> None:
        self.hub_server = MagicMock()
        self.hub_server.host = "hub.example"
        self.hub_server.port = 8443
        self.hub_server.tls = False
        self.hub_server.cert_sha256 = ""
        self.hub_server._token_store = _TokenStore()

    def get(self, agent_id: str):
        # With /bootstrap no longer a GET route, a GET falls through to the
        # catch-all GET /agents/{agent_id} - which must simply not find an agent
        # called "bootstrap".
        return None


@pytest.fixture
async def api(tmp_path: Path, monkeypatch):
    import homepilot.app_state as app_state
    from homepilot.agent_hub.router import router as agents_router
    from homepilot.auth.router import router as auth_router
    from homepilot.auth.tokens import generate_api_token
    from homepilot.config import get_settings

    monkeypatch.setenv("HP_COOKIE_SECURE", "false")
    get_settings.cache_clear()

    db = Database(str(tmp_path / "homepilot.db"))
    await db.connect()
    await run_migrations(db)
    repo = Repository(db)

    user_id = await repo.create_user("admin", "admin@example.com")
    full_token, prefix, token_hash = generate_api_token()
    await repo.create_api_token(
        user_id=user_id, token_type="api", prefix=prefix, hash=token_hash, scope="*"
    )

    registry = _Registry()
    monkeypatch.setattr(app_state, "_agent_registry", registry, raising=False)

    app = FastAPI()
    app.include_router(auth_router, prefix="/auth")
    app.include_router(agents_router)
    app.state.repo = repo
    app.state.agent_registry = registry

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, full_token, registry
    await db.close()
    get_settings.cache_clear()


async def test_get_is_not_a_way_to_mint(api):
    client, token, registry = api

    resp = await client.get("/agents/bootstrap", headers={"Authorization": f"Bearer {token}"})

    # Not a mint by any route: /bootstrap is no longer a GET, so this lands on
    # the catch-all GET /agents/{agent_id} and 404s.
    assert resp.status_code in (404, 405), resp.text
    assert "bootstrap_token" not in resp.text
    assert registry.hub_server._token_store.mints == 0, (
        "a GET minted a fleet-enrolment token - the CSRF gate skips safe methods, "
        "so this is cross-origin mintable"
    )


async def test_post_still_mints_for_a_legitimate_admin(api):
    client, token, registry = api

    resp = await client.post("/agents/bootstrap", headers={"Authorization": f"Bearer {token}"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["bootstrap_token"] == MINTED
    assert registry.hub_server._token_store.mints == 1


async def test_cookie_auth_without_csrf_headers_cannot_mint(api):
    """The point of moving to POST: the CSRF gate now covers this route."""
    client, token, registry = api

    login = await client.post("/auth/login", json={"token": token})
    assert login.status_code == 200, login.text

    forged = await client.post("/agents/bootstrap")

    assert forged.status_code == 403, forged.text
    assert registry.hub_server._token_store.mints == 0

    csrf = client.cookies.get("hp_csrf")
    allowed = await client.post(
        "/agents/bootstrap",
        headers={"X-CSRF-Token": csrf or "", "X-Requested-With": "XMLHttpRequest"},
    )
    assert allowed.status_code == 200, allowed.text
    assert registry.hub_server._token_store.mints == 1
