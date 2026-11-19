"""The first-run path from an empty install to a managed change (#445 A7).

A fresh install landed on a dashboard showing 0% coverage, three empty donuts and
no indication of what to do next. The steps existed - get a host into inventory,
adopt it, install the agent, apply a change - but only in the README.

The gates below assert the property that makes a checklist worth having: every
step's state is READ FROM THE ESTATE. A checklist that ticks itself off from a
stored "setup done" flag is a tutorial that can lie about what happened, which is
worse than no checklist on a control plane.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from homepilot.auth.deps import require_token
from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.db.repository import Repository

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def api(tmp_path: Path):
    from homepilot.dashboard.router import router as dashboard_router

    db = Database(str(tmp_path / "homepilot.db"))
    await db.connect()
    await run_migrations(db)
    repo = Repository(db)

    app = FastAPI()
    app.include_router(dashboard_router)
    app.state.repo = repo
    app.dependency_overrides[require_token] = lambda: {"scope": "*", "role": "admin"}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, repo
    await db.close()


async def _steps(client: AsyncClient) -> dict[str, bool]:
    body = (await client.get("/dashboard/summary")).json()
    return {s["key"]: s["done"] for s in body["onboarding"]["steps"]}


class TestTheFirstRunPath:
    async def test_an_empty_install_has_nothing_done(self, api):
        client, _repo = api

        body = (await client.get("/dashboard/summary")).json()

        assert body["onboarding"]["complete"] is False
        assert not any(s["done"] for s in body["onboarding"]["steps"])

    async def test_every_step_names_where_to_do_it(self, api):
        """A step an operator cannot act on is decoration."""
        client, _repo = api

        body = (await client.get("/dashboard/summary")).json()

        for step in body["onboarding"]["steps"]:
            assert step["href"].startswith("/"), step
            assert step["title"] and step["detail"], step

    async def test_a_host_in_inventory_ticks_the_first_step_only(self, api):
        client, repo = api

        await repo.create_host(hostname="web01", host_type="qemu", role="guest")

        steps = await _steps(client)
        assert steps["inventory"] is True
        assert steps["adopt"] is False, (
            "a discovered host is not an adopted one - adopting is the operator "
            "saying HomePilot may act on it"
        )

    async def test_adopting_ticks_the_second_step(self, api):
        client, repo = api

        await repo.create_host(hostname="web01", host_type="qemu", role="guest", managed=True)

        steps = await _steps(client)
        assert steps["adopt"] is True

    async def test_an_applied_artifact_ticks_the_last_step(self, api):
        client, repo = api

        await repo.create_artifact(
            id="2026-08-21-first-change",
            kind="shell-command",
            intent="the first managed change",
            status="applied",
            mutating=True,
            hash="deadbeef",
            target_json=None,
            idempotence=None,
            produced_by_json="{}",
            file_path="2026/08/2026-08-21-first-change.md",
        )

        steps = await _steps(client)
        assert steps["artifact"] is True

    async def test_a_proposed_artifact_does_not_tick_it(self, api):
        """Proposing is not applying. The step is "a change reached a host"."""
        client, repo = api

        await repo.create_artifact(
            id="2026-08-21-not-yet",
            kind="shell-command",
            intent="proposed only",
            status="proposed",
            mutating=True,
            hash="deadbeef",
            target_json=None,
            idempotence=None,
            produced_by_json="{}",
            file_path="2026/08/2026-08-21-not-yet.md",
        )

        steps = await _steps(client)
        assert steps["artifact"] is False

    async def test_it_is_complete_only_when_the_whole_path_is_walked(self, api):
        client, repo = api
        await repo.create_host(hostname="web01", host_type="qemu", role="guest", managed=True)
        await repo.create_artifact(
            id="2026-08-21-first-change",
            kind="shell-command",
            intent="the first managed change",
            status="applied",
            mutating=True,
            hash="deadbeef",
            target_json=None,
            idempotence=None,
            produced_by_json="{}",
            file_path="2026/08/2026-08-21-first-change.md",
        )

        body = (await client.get("/dashboard/summary")).json()

        # Three of four: no agent has ever connected, so a change cannot actually
        # reach a host, and the path is not walked.
        assert body["onboarding"]["complete"] is False, (
            "the checklist called itself complete with no agent ever connected"
        )


class TestGetAlertsOnYourPhone:
    """#822: the checklist OFFERS phone alerts and never requires them (owner
    decision 2026-11-18, opt-in). Done means a channel has DELIVERED, read from
    its own record - not that one exists.

    Teeth: count the step towards `complete` and the walked-path test fails;
    tick it for any channel row and the never-delivered test fails."""

    async def test_it_is_offered_and_optional(self, api):
        client, _repo = api
        body = (await client.get("/dashboard/summary")).json()
        step = next(s for s in body["onboarding"]["steps"] if s["key"] == "phone")
        assert step["optional"] is True
        assert step["href"] == "/monitoring/channels"
        assert step["done"] is False

    async def test_a_channel_that_never_delivered_does_not_tick_it(self, api):
        client, repo = api
        await repo.db.execute(
            "INSERT INTO notification_channels (kind, name, endpoint, secret_ref, has_token, "
            "enabled, created_at) VALUES ('ntfy', 'p', 'https://ntfy.example.com', 'x', 0, 1, "
            "'2026-11-18T00:00:00Z')"
        )
        await repo.db.conn.commit()
        assert (await _steps(client))["phone"] is False

        await repo.db.execute(
            "UPDATE notification_channels SET last_ok_at = '2026-11-18T00:01:00Z', "
            "last_result = 'ok'"
        )
        await repo.db.conn.commit()
        assert (await _steps(client))["phone"] is True

    async def test_the_path_completes_without_it(self, api, monkeypatch):
        """Every required step walked, no phone: the checklist is complete."""
        from types import SimpleNamespace

        import homepilot.app_state as app_state

        client, repo = api
        live = SimpleNamespace(list_connected=lambda: [{"agent_id": "a1"}], started_at=None)
        monkeypatch.setattr(app_state, "get_agent_registry", lambda: live)
        await repo.create_host(hostname="web01", host_type="qemu", role="guest", managed=True)
        await repo.create_artifact(
            id="2026-11-18-first-change",
            kind="shell-command",
            intent="the first managed change",
            status="applied",
            mutating=True,
            hash="deadbeef",
            target_json=None,
            idempotence=None,
            produced_by_json="{}",
            file_path="2026/11/2026-11-18-first-change.md",
        )

        body = (await client.get("/dashboard/summary")).json()

        steps = {s["key"]: s["done"] for s in body["onboarding"]["steps"]}
        assert steps == {
            "inventory": True,
            "adopt": True,
            "agent": True,
            "phone": False,
            "artifact": True,
        }
        assert body["onboarding"]["complete"] is True, "an optional step held the checklist open"
