"""Automation does not overwrite what an operator set (#424).

`role_source` / `ip_source` were the only provenance guards, and they covered two
fields out of the several an operator can write:

* the node refresh overwrote an operator's `role` and `ip_address` AND stamped
  `role_source="user"` over its own guess - a forgery that defeated the #416 fix
  and hid the UI's "inferred" badge;
* every refresh clobbered an operator's `description` with the PVE blurb, and no
  provenance existed for that field at all;
* enrich re-derived `status` over anything set with `PATCH /inventory/{id}`,
  which made that PATCH field a lie.

Fixed as a CLASS: one `pinned_fields` list per host, written by the PATCH that an
operator uses, and one `update_host_from_automation()` door that every sync and
enrich pass goes through.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

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
    from homepilot.inventory.router import router as inventory_router

    db = Database(str(tmp_path / "homepilot.db"))
    await db.connect()
    await run_migrations(db)
    repo = Repository(db)

    app = FastAPI()
    app.include_router(inventory_router, prefix="/inventory")
    app.state.repo = repo
    app.state.inventory_service = AsyncMock()
    app.dependency_overrides[require_token] = lambda: {"scope": "*", "role": "admin"}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, repo
    await db.close()


async def _host(repo: Repository, **overrides) -> str:
    kwargs = {
        "hostname": "web01",
        "host_type": "qemu",
        "role": "guest",
        "source": "discovered",
    }
    kwargs.update(overrides)
    return await repo.create_host(**kwargs)


class TestAPatchIsAnOperatorDeciding:
    async def test_the_fields_it_writes_are_pinned(self, api):
        client, repo = api
        host_id = await _host(repo)

        await client.patch(f"/inventory/{host_id}", json={"description": "the mail relay"})

        host = await repo.get_host(host_id)
        assert "description" in Repository._pinned_fields(host)

    async def test_a_later_sync_leaves_them_alone(self, api):
        """Every refresh cycle used to clobber an operator's description with the
        PVE blurb. No provenance column existed for it at all."""
        client, repo = api
        host_id = await _host(repo)
        await client.patch(f"/inventory/{host_id}", json={"description": "the mail relay"})

        await repo.update_host_from_automation(host_id, description="Proxmox VM 101")

        host = await repo.get_host(host_id)
        assert host["description"] == "the mail relay"

    async def test_enrich_cannot_re_derive_a_status_the_operator_set(self, api):
        """`PATCH status` was a lie: enrich overwrote it on the next cycle."""
        client, repo = api
        host_id = await _host(repo)
        await client.patch(f"/inventory/{host_id}", json={"status": "offline"})

        await repo.update_host_from_automation(host_id, status="online")

        host = await repo.get_host(host_id)
        assert host["status"] == "offline"

    async def test_an_unpinned_field_is_still_updated(self, api):
        """The guard must protect operator intent WITHOUT freezing the host: a
        sync that can no longer update anything is not a fix."""
        client, repo = api
        host_id = await _host(repo)
        await client.patch(f"/inventory/{host_id}", json={"description": "mine"})

        await repo.update_host_from_automation(host_id, pve_status="running")

        host = await repo.get_host(host_id)
        assert host["pve_status"] == "running"

    async def test_it_reports_what_it_skipped(self, api):
        client, repo = api
        host_id = await _host(repo)
        await client.patch(f"/inventory/{host_id}", json={"role": "database"})

        skipped = await repo.update_host_from_automation(
            host_id, role="guest", pve_status="running"
        )

        assert skipped == ["role"]


class TestAutomationNeverForgesOperatorProvenance:
    async def test_role_source_user_from_automation_is_rewritten(self, api):
        """The worst part of #424: the node refresh stamped `role_source="user"`
        over its own guess, which defeated the #416 fix and hid the UI badge that
        tells an operator a value was inferred."""
        _client, repo = api
        host_id = await _host(repo)

        await repo.update_host_from_automation(host_id, role="node", role_source="user")

        host = await repo.get_host(host_id)
        assert host["role_source"] == "inferred", "a sync claimed an operator had chosen this role"

    async def test_ip_source_user_from_automation_is_rewritten(self, api):
        _client, repo = api
        host_id = await _host(repo)

        await repo.update_host_from_automation(host_id, ip_address="10.0.0.9", ip_source="user")

        host = await repo.get_host(host_id)
        assert host["ip_source"] == "pve"


class TestTheNodeRefreshRespectsIntent:
    async def test_it_does_not_overwrite_an_operator_role_or_ip(self, tmp_path: Path):
        """The end-to-end version: a real refresh over a node an operator has
        already edited."""
        from homepilot.inventory.service import InventoryService

        db = Database(str(tmp_path / "refresh.db"))
        await db.connect()
        await run_migrations(db)
        repo = Repository(db)
        try:
            host_id = await repo.create_host(
                hostname="pve1", host_type="node", role="node", source="hp_created"
            )
            await repo.update_host(host_id, role="control-plane", ip_address="10.9.9.9")
            await repo.pin_host_fields(host_id, {"role", "ip_address"})

            proxmox = AsyncMock()

            async def _read(path: str):
                if path == "/nodes":
                    return {"data": [{"node": "pve1", "status": "online", "ip": "10.0.0.1"}]}
                return {"data": []}

            proxmox.read.side_effect = _read
            svc = InventoryService(repo=repo, proxmox=proxmox)

            await svc.refresh_inventory()

            host = await repo.get_host(host_id)
            assert host["role"] == "control-plane", "the sync overwrote an operator's role"
            assert host["ip_address"] == "10.9.9.9", "the sync overwrote an operator's IP"
            assert host["role_source"] != "user" or "role" in Repository._pinned_fields(host)
        finally:
            await db.close()

    async def test_it_still_updates_liveness(self, tmp_path: Path):
        from homepilot.inventory.service import InventoryService

        db = Database(str(tmp_path / "refresh2.db"))
        await db.connect()
        await run_migrations(db)
        repo = Repository(db)
        try:
            host_id = await repo.create_host(
                hostname="pve1", host_type="node", role="node", source="hp_created"
            )
            await repo.update_host(host_id, role="control-plane")
            await repo.pin_host_fields(host_id, {"role"})

            proxmox = AsyncMock()

            async def _read(path: str):
                if path == "/nodes":
                    return {"data": [{"node": "pve1", "status": "online", "ip": "10.0.0.1"}]}
                return {"data": []}

            proxmox.read.side_effect = _read
            svc = InventoryService(repo=repo, proxmox=proxmox)

            await svc.refresh_inventory()

            host = await repo.get_host(host_id)
            assert host["pve_status"] == "running", (
                "pinning a role stopped the sync reporting whether the node is up"
            )
        finally:
            await db.close()
