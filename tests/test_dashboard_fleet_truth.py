"""The Overview page must not overstate the fleet (#469).

The bug these gates forbid: `/dashboard/summary` counted the persisted
`agents.connected` column while `/agents/` overlaid the LIVE hub registry, so
the two endpoints disagreed. The column is written best-effort on register and
unregister, which means it stays `1` for every agent that vanished without a
clean goodbye - a killed process, a dropped link, or a backend restart.

That made the number most wrong exactly when it matters most: an operator
loading the dashboard after an upgrade to check the fleet came back sees a
healthy count over stranded agents. It would have masked the #417 lockout, and
it did mask #468 until the two endpoints were compared by hand on the dev box.

These assert the OUTCOME an operator reads - the number on the page - and that
the two endpoints answer the same question the same way, rather than asserting
that some counting function was called.

Teeth: restore
``SELECT COUNT(*) FROM agents WHERE connected = 1`` in
``dashboard/router.py`` and ``test_stale_connected_column_is_not_believed``
plus ``test_dashboard_and_agents_list_agree_when_stale`` both fail.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from homepilot.auth.deps import require_token
from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.db.repository import Repository

pytestmark = pytest.mark.asyncio


class _FakeRegistry:
    """Stands in for the live hub registry, holding only what is dialled in."""

    def __init__(self, connected: list[dict[str, Any]] | None = None) -> None:
        self._connected = connected or []

    def list_connected(self) -> list[dict[str, Any]]:
        return list(self._connected)


@pytest.fixture
async def fleet_db(tmp_path: Path):
    db = Database(str(tmp_path / "homepilot.db"))
    await db.connect()
    await run_migrations(db)
    yield db
    await db.close()


@pytest.fixture
def fleet_app(fleet_db):
    from homepilot.agent_hub.router import router as agents_router
    from homepilot.dashboard.router import router as dashboard_router

    application = FastAPI()
    application.include_router(dashboard_router)
    application.include_router(agents_router)
    application.state.repo = Repository(fleet_db)
    application.dependency_overrides[require_token] = lambda: {"scope": "*", "role": "admin"}
    return application


async def _persist_agent(db: Database, agent_id: str, *, connected: int) -> None:
    """Write the row a real enrolment leaves behind, with the connected flag the
    hub last managed to persist."""
    await db.execute(
        "INSERT INTO agents (agent_id, hostname, connected) VALUES (?, ?, ?)",
        (agent_id, "hp-test-server", connected),
    )
    await db.conn.commit()


async def _get(app: FastAPI, path: str) -> Any:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(path)
    assert response.status_code == 200, response.text
    return response.json()


async def test_stale_connected_column_is_not_believed(fleet_db, fleet_app, monkeypatch):
    """Two agents persisted as connected, none actually dialled in.

    This is the state after any backend restart, and the state the dev box was
    in while the dashboard reported a healthy fleet.
    """
    await _persist_agent(fleet_db, "agent-a", connected=1)
    await _persist_agent(fleet_db, "agent-b", connected=1)
    monkeypatch.setattr("homepilot.app_state.get_agent_registry", lambda: _FakeRegistry([]))

    summary = await _get(fleet_app, "/dashboard/summary")

    assert summary["agents"]["known"] == 2
    assert summary["agents"]["connected"] == 0, (
        "no agent is dialled in, so the page must not report one as connected"
    )


async def test_dashboard_and_agents_list_agree_when_stale(fleet_db, fleet_app, monkeypatch):
    """One source of truth: the two endpoints must not disagree.

    Comparing them is the assertion that survives a future refactor of either
    one - it forbids the class, not just today's arithmetic.
    """
    await _persist_agent(fleet_db, "agent-a", connected=1)
    await _persist_agent(fleet_db, "agent-b", connected=1)
    live = _FakeRegistry([{"agent_id": "agent-a", "hostname": "hp-test-server"}])
    monkeypatch.setattr("homepilot.app_state.get_agent_registry", lambda: live)

    summary = await _get(fleet_app, "/dashboard/summary")
    agents = await _get(fleet_app, "/agents/")

    assert summary["agents"]["connected"] == sum(1 for a in agents if a["connected"])
    assert summary["agents"]["known"] == len(agents)
    assert summary["agents"]["connected"] == 1


async def test_a_live_agent_missing_from_the_table_still_counts(fleet_db, fleet_app, monkeypatch):
    """The reverse skew: persistence is best-effort, so an agent can be dialled
    in before its row lands. It is connected, and it is known."""
    live = _FakeRegistry([{"agent_id": "agent-new", "hostname": "hp-test-server"}])
    monkeypatch.setattr("homepilot.app_state.get_agent_registry", lambda: live)

    summary = await _get(fleet_app, "/dashboard/summary")

    assert summary["agents"]["connected"] == 1
    assert summary["agents"]["known"] == 1


async def test_no_hub_means_no_connected_agents(fleet_db, fleet_app, monkeypatch):
    """With the hub disabled - including the #468 refusal path - nothing can be
    connected, and the page must say so rather than reporting the last flag."""
    await _persist_agent(fleet_db, "agent-a", connected=1)
    monkeypatch.setattr("homepilot.app_state.get_agent_registry", lambda: None)

    summary = await _get(fleet_app, "/dashboard/summary")

    assert summary["agents"]["connected"] == 0
    assert summary["agents"]["known"] == 1
