"""A decommissioned agent can be forgotten, credential and all (#415, #445 A6).

There was no way to remove an agent. `AgentRegistry.unregister()` only drops the
in-memory entry for a LIVE connection, and since #343 agents are persisted and
listed overlaid - so a scrapped host stayed in the list forever, counted in
"N known", with no operator path to get rid of it.

The part that matters more than tidiness: the `agents` table doubles as the
per-agent credential store (#362 slice 2). A decommissioned box's row IS a
credential that still authenticates. These gates assert the OUTCOME an operator
is actually buying - the agent is gone AND its credential can no longer be used -
rather than that a DELETE returned 200.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from homepilot.auth.deps import require_token
from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.db.repository import Repository

pytestmark = pytest.mark.asyncio

GONE = "agent-decommissioned"
LIVE = "agent-still-running"


class _Registry:
    """Stands in for the live hub registry."""

    def __init__(self, connected: list[str] | None = None) -> None:
        self._connected = connected or []
        self.unregistered: list[str] = []
        self.hub_server = MagicMock()

    def list_connected(self):
        return [{"agent_id": a, "hostname": "host"} for a in self._connected]

    def unregister(self, agent_id: str, conn_id: str | None = None) -> None:
        self.unregistered.append(agent_id)


@pytest.fixture
async def api(tmp_path: Path):
    from homepilot.agent_hub.router import router as agents_router

    db = Database(str(tmp_path / "homepilot.db"))
    await db.connect()
    await run_migrations(db)
    repo = Repository(db)

    # Two agents with real per-agent credentials: one scrapped, one still up.
    await repo.set_agent_credential(GONE, "old-box", "hash-of-old-credential")
    await repo.set_agent_credential(LIVE, "live-box", "hash-of-live-credential")

    app = FastAPI()
    app.include_router(agents_router)
    app.state.repo = repo
    app.state.agent_registry = _Registry(connected=[LIVE])
    app.dependency_overrides[require_token] = lambda: {"scope": "*", "role": "admin"}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, repo, app
    await db.close()


class TestForgettingAnAgentRemovesItsCredential:
    async def test_the_agent_is_gone_from_the_record(self, api):
        client, repo, _app = api

        resp = await client.delete(f"/agents/{GONE}")

        assert resp.status_code == 200, resp.text
        remaining = {a["agent_id"] for a in await repo.list_agents()}
        assert GONE not in remaining, "the decommissioned agent is still listed"
        assert LIVE in remaining, "forgetting one agent removed another"

    async def test_its_credential_can_no_longer_be_used(self, api):
        """The security point of #415: the row IS the credential store, so a
        removal that left it behind would leave a scrapped box able to
        authenticate."""
        client, repo, _app = api
        assert await repo.get_agent_credential(GONE) is not None, "fixture precondition"

        await client.delete(f"/agents/{GONE}")

        assert await repo.get_agent_credential(GONE) is None, (
            "the credential of a forgotten agent survived - a decommissioned host "
            "can still authenticate"
        )

    async def test_the_hostname_cannot_be_used_to_rebind(self, api):
        """#418 lets a credential rebind to a new agent_id when it matches a
        non-revoked credential for the SAME hostname. A forgotten agent must not
        leave that door open."""
        client, repo, _app = api

        await client.delete(f"/agents/{GONE}")

        assert await repo.get_agent_credentials_by_hostname("old-box") == []

    async def test_a_stale_in_memory_record_is_dropped_too(self, api):
        client, _repo, app = api

        await client.delete(f"/agents/{GONE}")

        assert GONE in app.state.agent_registry.unregistered


class TestItRefusesTheDangerousCases:
    async def test_a_connected_agent_is_refused(self, api):
        """Removing a live agent would delete the credential out from under an
        open channel and leave it reconnecting into a hub that has forgotten
        it. Stop or revoke it first, deliberately."""
        client, repo, _app = api

        resp = await client.delete(f"/agents/{LIVE}")

        assert resp.status_code == 409
        assert "connected right now" in resp.json()["detail"]
        assert await repo.get_agent_credential(LIVE) is not None, (
            "a refused removal still destroyed the credential"
        )

    async def test_an_unknown_agent_is_a_404(self, api):
        client, _repo, _app = api
        resp = await client.delete("/agents/never-existed")
        assert resp.status_code == 404
