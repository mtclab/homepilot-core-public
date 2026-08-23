"""Inventory lifecycle: add a host by hand, notice one that is gone, forget it (#445 A5).

Three holes, all of them about what inventory can REPRESENT:

* Inventory could only ever be filled by a Proxmox sync, so a homelab that is not
  entirely Proxmox guests - the NAS, the router, the Pi, the old tower - was
  literally unrepresentable, and everything downstream (docs, adoption, agent
  install, an artifact targeting a host) was closed to those machines.
* A guest destroyed in Proxmox kept its row and its last-known status forever. It
  simply stopped being updated, which from the inventory is indistinguishable
  from a machine that is merely powered off. The reconciler already computed the
  absent set and spent it on an audit counter.
* There was no way to remove a host at all.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
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


class TestAddingAHostByHand:
    async def test_a_non_proxmox_machine_can_enter_inventory(self, api):
        client, repo = api

        resp = await client.post(
            "/inventory",
            json={"hostname": "nas01", "ip_address": "10.0.0.4", "role": "service"},
        )

        assert resp.status_code == 201, resp.text
        rows = await repo.list_hosts()
        assert [h["hostname"] for h in rows] == ["nas01"]

    async def test_it_is_adopted_not_left_pending_triage(self, api):
        """A machine an operator typed in by hand is not a discovery awaiting
        review - leaving it 'pending' would put it in the adopt queue it can
        never legitimately be in."""
        client, repo = api

        await client.post("/inventory", json={"hostname": "nas01"})

        host = (await repo.list_hosts())[0]
        assert host["source"] == "manual"
        assert host["import_state"] == "adopted"

    async def test_the_operators_values_are_not_marked_as_guesses(self, api):
        """#416: an enrich pass overwrites anything marked "inferred". A role a
        person chose must not be demoted by the next enrichment cycle."""
        client, repo = api

        await client.post("/inventory", json={"hostname": "nas01", "role": "database"})

        host = (await repo.list_hosts())[0]
        assert host["role"] == "database"
        assert host["role_source"] == "user"

    async def test_a_duplicate_hostname_is_refused(self, api):
        client, _repo = api
        await client.post("/inventory", json={"hostname": "nas01"})

        resp = await client.post("/inventory", json={"hostname": "nas01"})

        assert resp.status_code == 409
        assert "already in inventory" in resp.json()["detail"]

    async def test_a_hostname_that_is_not_a_hostname_is_refused(self, api):
        client, repo = api

        resp = await client.post("/inventory", json={"hostname": "http://nas01/;rm -rf /"})

        assert resp.status_code == 422
        assert await repo.list_hosts() == []


class TestNoticingAHostIsGone:
    async def _proxmox_host(self, repo: Repository, hostname: str) -> str:
        return await repo.create_host(
            hostname=hostname, host_type="qemu", role="guest", source="discovered"
        )

    async def test_a_host_the_hypervisor_stopped_reporting_is_stamped(self, api):
        _client, repo = api
        still_there = await self._proxmox_host(repo, "web01")
        destroyed = await self._proxmox_host(repo, "old-vm")

        await repo.mark_hosts_absent({still_there}, ("discovered",))

        rows = {h["id"]: h for h in await repo.list_hosts()}
        assert rows[destroyed]["absent_since"], (
            "a destroyed guest is still indistinguishable from a powered-off one"
        )
        assert rows[still_there]["absent_since"] is None

    async def test_a_manually_added_host_is_never_declared_gone(self, api):
        """Proxmox never looked for it, so a Proxmox sync has no standing to say
        it has vanished."""
        client, repo = api
        await client.post("/inventory", json={"hostname": "nas01"})

        await repo.mark_hosts_absent(set(), ("discovered", "hp_created", "imported"))

        host = (await repo.list_hosts())[0]
        assert host["absent_since"] is None

    async def test_the_date_it_went_missing_is_not_restamped(self, api):
        """ "Gone since Tuesday" is what tells a deletion apart from a hypervisor
        that was briefly unreachable. Restamping every cycle destroys that."""
        _client, repo = api
        gone = await self._proxmox_host(repo, "old-vm")
        await repo.mark_hosts_absent(set(), ("discovered",))
        first = (await repo.get_host(gone))["absent_since"]

        newly = await repo.mark_hosts_absent(set(), ("discovered",))

        assert newly == 0, "an already-absent host was counted as newly absent"
        assert (await repo.get_host(gone))["absent_since"] == first

    async def test_a_host_that_comes_back_is_cleared(self, api):
        _client, repo = api
        back = await self._proxmox_host(repo, "web01")
        await repo.mark_hosts_absent(set(), ("discovered",))
        assert (await repo.get_host(back))["absent_since"]

        await repo.mark_hosts_absent({back}, ("discovered",))

        assert (await repo.get_host(back))["absent_since"] is None


class TestThePagerTellsTheTruth:
    async def test_total_is_a_count_not_the_page_size(self, api):
        """`len(hosts)` was returned as `total`, so the UI capped at 100 with no
        way to reach page 2 and told the operator their estate was smaller than
        it is (#428)."""
        client, repo = api
        for n in range(120):
            await repo.create_host(hostname=f"h{n:03d}", host_type="qemu", role="guest")

        body = (await client.get("/inventory", params={"limit": 50})).json()

        assert len(body["items"]) == 50
        assert body["total"] == 120, "total reported the page size, not the count"

    async def test_the_count_respects_the_same_filters_as_the_page(self, api):
        """Two queries, one filter set: a count for a different set of rows than
        the page shows is a pager that offers empty pages."""
        client, repo = api
        for n in range(30):
            await repo.create_host(hostname=f"vm{n:03d}", host_type="qemu", role="guest")
        for n in range(5):
            await repo.create_host(hostname=f"db{n:03d}", host_type="qemu", role="database")

        body = (await client.get("/inventory", params={"role": "database"})).json()

        assert body["total"] == 5

    async def test_offset_reaches_the_second_page(self, api):
        client, repo = api
        for n in range(120):
            await repo.create_host(hostname=f"h{n:03d}", host_type="qemu", role="guest")

        page_two = (await client.get("/inventory", params={"limit": 100, "offset": 100})).json()

        assert len(page_two["items"]) == 20
        assert page_two["total"] == 120


class TestTheReconcilerSeesEveryHost:
    async def test_the_snapshot_is_not_truncated_at_a_hundred(self, tmp_path: Path):
        """The absent/changed sets were computed from `list_hosts()`, whose
        default limit is 100 - so on a larger estate they were computed from an
        arbitrary first page (#428)."""
        from homepilot.reconciler.inventory import InventoryReconciler

        db = Database(str(tmp_path / "recon.db"))
        await db.connect()
        await run_migrations(db)
        repo = Repository(db)
        try:
            for n in range(150):
                await repo.create_host(hostname=f"h{n:03d}", host_type="qemu", role="guest")

            service = AsyncMock()
            service.refresh_inventory = AsyncMock(
                return_value={"hosts": 0, "services": 0, "proxmox_host_ids": []}
            )
            service.enrich_inventory = AsyncMock(return_value={"enriched": 0})
            reconciler = InventoryReconciler(inventory_service=service, repo=repo)

            result = await reconciler.run()

            assert result.details["absent_hosts"] == 150, (
                "the reconciler saw only the first page of the estate"
            )
        finally:
            await db.close()


class TestForgettingAHost:
    async def test_a_manual_host_can_be_removed(self, api):
        client, repo = api
        created = (await client.post("/inventory", json={"hostname": "nas01"})).json()

        resp = await client.delete(f"/inventory/{created['id']}")

        assert resp.status_code == 200, resp.text
        assert await repo.list_hosts() == []

    async def test_a_host_the_hypervisor_still_reports_is_refused(self, api):
        """Deleting it would be undone by the next sync, and the operator would
        believe it was gone."""
        client, repo = api
        host_id = await repo.create_host(
            hostname="web01", host_type="qemu", role="guest", source="discovered"
        )

        resp = await client.delete(f"/inventory/{host_id}")

        assert resp.status_code == 409
        assert "would bring it straight back" in resp.json()["detail"]
        assert len(await repo.list_hosts()) == 1

    async def test_an_absent_host_can_be_removed(self, api):
        client, repo = api
        host_id = await repo.create_host(
            hostname="old-vm", host_type="qemu", role="guest", source="discovered"
        )
        await repo.mark_hosts_absent(set(), ("discovered",))

        resp = await client.delete(f"/inventory/{host_id}")

        assert resp.status_code == 200, resp.text
        assert await repo.list_hosts() == []

    async def test_its_services_go_with_it(self, api):
        """A service row pointing at an id nothing resolves is how a "forgotten"
        machine keeps showing up in service listings and coverage counts."""
        client, repo = api
        created = (await client.post("/inventory", json={"hostname": "nas01"})).json()
        await repo.create_service(host_id=created["id"], name="smbd", runtime="systemd")
        assert await repo.list_services(host_id=created["id"])

        await client.delete(f"/inventory/{created['id']}")

        assert await repo.list_services(host_id=created["id"]) == []

    async def test_removing_an_unknown_host_is_a_404(self, api):
        client, _repo = api
        resp = await client.delete("/inventory/never-existed")
        assert resp.status_code == 404


class TestTheRefreshRecordsAbsence:
    async def test_a_full_refresh_marks_what_proxmox_no_longer_reports(self, tmp_path: Path):
        """The reconciler computed the absent set and spent it on an audit
        counter; nothing ever reached the host row or the UI."""
        from homepilot.inventory.service import InventoryService

        db = Database(str(tmp_path / "homepilot.db"))
        await db.connect()
        await run_migrations(db)
        repo = Repository(db)
        try:
            gone = await repo.create_host(
                hostname="old-vm", host_type="qemu", role="guest", source="discovered"
            )

            proxmox = AsyncMock()

            async def _read(path: str) -> Any:
                if path == "/nodes":
                    return {"data": [{"node": "pve1", "status": "online", "ip": "10.0.0.1"}]}
                if path.endswith("/qemu"):
                    return {"data": [{"vmid": 101, "name": "web01", "status": "running"}]}
                return {"data": []}

            proxmox.read.side_effect = _read
            svc = InventoryService(repo=repo, proxmox=proxmox)

            result = await svc.refresh_inventory()

            assert result.get("absent") == 1
            assert (await repo.get_host(gone))["absent_since"], (
                "the refresh saw the host was gone and recorded nothing on it"
            )
        finally:
            await db.close()

    async def test_a_scoped_refresh_does_not_declare_other_nodes_gone(self, tmp_path: Path):
        """A scoped sync looks at ONE node, so every guest on every other node
        would look absent to it."""
        from homepilot.inventory.service import InventoryService

        db = Database(str(tmp_path / "homepilot.db"))
        await db.connect()
        await run_migrations(db)
        repo = Repository(db)
        try:
            elsewhere = await repo.create_host(
                hostname="db01",
                host_type="qemu",
                role="guest",
                source="discovered",
                node="pve2",
            )

            proxmox = AsyncMock()

            async def _read(path: str) -> Any:
                if path == "/nodes":
                    return {"data": [{"node": "pve1", "status": "online", "ip": "10.0.0.1"}]}
                return {"data": []}

            proxmox.read.side_effect = _read
            svc = InventoryService(repo=repo, proxmox=proxmox)

            await svc.refresh_inventory(scope="pve1")

            assert (await repo.get_host(elsewhere))["absent_since"] is None, (
                "a one-node sync declared a host on another node gone"
            )
        finally:
            await db.close()
