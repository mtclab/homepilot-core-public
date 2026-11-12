"""THE GATE for #706: a VMID is not an identity.

PVE hands a destroyed guest's number to the next VM created anywhere, and
HomePilot matched inventory rows on the number alone. So when a friend's
provisioned guest (vmid 105) was destroyed and the operator then built their
own VM - which PVE numbered 105 - the refresh folded the operator's machine into
the friend's row: online, listed in the friend's portal, powerable by them,
counted in their quota, and never in the import queue. And a row already
marked gone still sent power actions at vmid 105, whoever held it.

Asserted on outcomes: which rows exist and what they say after a refresh, what
the friend's portal lists, and which power calls reach the hypervisor.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.db.repository import Repository
from homepilot.guest.quota import usage_for
from homepilot.inventory.service import InventoryService
from tests.portal_support import cert_headers, client_for
from tests.test_guest_api import ALICE, stack  # noqa: F401 - the portal fixture


class ListingPVE:
    """What a refresh reads: the node list and each node's guests."""

    def __init__(self, nodes: tuple[str, ...] = ("pve1",)) -> None:
        self.nodes = nodes
        self.guests: dict[str, list[dict[str, Any]]] = {n: [] for n in nodes}

    async def read(self, path: str, query: dict[str, Any] | None = None) -> dict[str, Any]:
        if path == "/nodes":
            return {"data": [{"node": n, "status": "online", "ip": "10.0.0.2"} for n in self.nodes]}
        for node in self.nodes:
            if path == f"/nodes/{node}/qemu":
                return {"data": list(self.guests[node])}
        if path.endswith("/config"):
            return {"data": {}}
        return {"data": []}


@pytest.fixture
async def repo(tmp_path: Path):
    db = Database(str(tmp_path / "inv.db"))
    await db.connect()
    await run_migrations(db)
    try:
        yield Repository(db)
    finally:
        await db.close()


async def _rows_for(repo: Repository, vmid: int) -> list[dict[str, Any]]:
    rows = await repo.db.fetchall(
        "SELECT * FROM hosts WHERE proxmox_id = ? ORDER BY created_at", (vmid,)
    )
    return [dict(r) for r in rows]


class TestAReusedVmidIsANewMachine:
    async def test_the_operators_new_vm_is_not_folded_into_the_friends_row(self, repo):
        alice = await repo.create_host(
            hostname="alice-web",
            host_type="qemu",
            proxmox_id=105,
            node="pve1",
            source="hp_created",
            owner=ALICE,
            managed_by="provisioned",
        )
        pve = ListingPVE()
        inv = InventoryService(repo, pve)
        pve.guests["pve1"] = [{"vmid": 105, "name": "alice-web", "status": "running"}]
        await inv.refresh_inventory()

        # Alice's guest is destroyed and the operator's `operator-db` gets 105.
        pve.guests["pve1"] = [{"vmid": 105, "name": "operator-db", "status": "running"}]
        await inv.refresh_inventory()

        old = await repo.get_host(alice)
        assert old["absent_since"] is not None, "the friend's row still claims vmid 105"
        assert old["pve_status"] != "running"
        new = [r for r in await _rows_for(repo, 105) if r["id"] != alice]
        assert len(new) == 1 and new[0]["hostname"] == "operator-db"
        assert new[0]["owner"] is None and new[0]["source"] == "discovered"
        assert new[0]["import_state"] == "pending", "the operator's VM never reached the queue"
        assert (await usage_for(repo, ALICE)).vms == 0, "the friend is billed for it"

        # And it stays that way: the next cycle updates the new row only - even
        # when both rows carry the same created_at (a second-resolution tie),
        # which is exactly when "newest row for the VMID" picks the old one.
        await repo.db.execute(
            "UPDATE hosts SET created_at = '2026-09-26T00:00:00+00:00' WHERE proxmox_id = 105"
        )
        await repo.db.conn.commit()
        await inv.refresh_inventory()
        assert (await repo.get_host(alice))["absent_since"] is not None
        assert len(await _rows_for(repo, 105)) == 2

    async def test_the_same_guest_is_still_the_same_row(self, repo):
        alice = await repo.create_host(
            hostname="alice-web",
            host_type="qemu",
            proxmox_id=105,
            node="pve1",
            source="hp_created",
            owner=ALICE,
        )
        pve = ListingPVE()
        pve.guests["pve1"] = [{"vmid": 105, "name": "alice-web", "status": "running"}]
        await InventoryService(repo, pve).refresh_inventory()

        assert [r["id"] for r in await _rows_for(repo, 105)] == [alice]
        assert (await repo.get_host(alice))["absent_since"] is None

    async def test_a_migrated_guest_is_recorded_on_its_new_node(self, repo):
        alice = await repo.create_host(
            hostname="alice-web",
            host_type="qemu",
            proxmox_id=105,
            node="pve1",
            source="hp_created",
            owner=ALICE,
        )
        pve = ListingPVE(("pve1", "pve2"))
        pve.guests["pve2"] = [{"vmid": 105, "name": "alice-web", "status": "running"}]
        await InventoryService(repo, pve).refresh_inventory()

        assert (await repo.get_host(alice))["node"] == "pve2"


class TestPowerIsNeverSentAtAMachineThatIsNotTheirs:
    async def test_a_machine_shown_as_gone_cannot_be_powered(self, stack):  # noqa: F811
        app, pve, ids = stack
        await app.state.repo.mark_hosts_absent(seen_ids=set(), sources=("hp_created",))
        async with client_for(app) as client:
            res = await client.post(
                f"/guest/vms/{ids['alice']}/power",
                json={"action": "stop"},
                headers=cert_headers(cn=ALICE),
            )
        assert res.status_code == 409
        assert pve.calls == [], f"a hard-stop reached vmid 105 for a gone machine: {pve.calls}"

    async def test_a_vmid_reused_before_the_next_refresh_is_not_powered(self, stack):  # noqa: F811
        """The row still reads alive - no refresh has run yet - but the machine
        at 105 is now somebody else's. The name is checked before the stop."""
        app, pve, ids = stack
        pve.guests[105]["name"] = "operator-db"
        async with client_for(app) as client:
            res = await client.post(
                f"/guest/vms/{ids['alice']}/power",
                json={"action": "stop"},
                headers=cert_headers(cn=ALICE),
            )
        assert res.status_code == 409
        assert pve.calls == []
        assert "operator-db" not in res.text, "the guest was told whose machine it is"

    async def test_a_machine_that_cannot_be_found_is_not_acted_on(self, stack):  # noqa: F811
        app, pve, ids = stack
        del pve.guests[105]
        async with client_for(app) as client:
            res = await client.post(
                f"/guest/vms/{ids['alice']}/power",
                json={"action": "start"},
                headers=cert_headers(cn=ALICE),
            )
        assert res.status_code == 409
        assert pve.calls == []


class TestARetiredOccupantIsJournalledAsAbsent:
    """A row retired by a reused VMID went absent THIS cycle, and the journal says so.

    #712 made the journal name only the hosts the absent sweep actually marked.
    But a row retired by #706 is stamped absent BEFORE the sweep runs, so the
    sweep (which only marks rows with no absent_since) never returned it - the
    friend's machine vanished from the inventory and no journal entry said so.

    Teeth: drop ``retired_ids`` from ``absent_host_ids`` in
    ``refresh_inventory`` -> both tests fail.
    """

    async def _reused(self, repo: Repository) -> tuple[str, InventoryService]:
        alice = await repo.create_host(
            hostname="alice-web",
            host_type="qemu",
            proxmox_id=105,
            node="pve1",
            source="hp_created",
            owner=ALICE,
        )
        pve = ListingPVE()
        inv = InventoryService(repo, pve)
        pve.guests["pve1"] = [{"vmid": 105, "name": "alice-web", "status": "running"}]
        await inv.refresh_inventory()
        pve.guests["pve1"] = [{"vmid": 105, "name": "operator-db", "status": "running"}]
        return alice, inv

    async def test_the_sweep_reports_the_retired_row(self, repo):
        alice, inv = await self._reused(repo)
        result = await inv.refresh_inventory()

        assert result["complete"] is True
        assert alice in result["absent_host_ids"], "the retired occupant was not reported"
        assert result["absent"] == len(result["absent_host_ids"])

    async def test_the_reconciler_journal_names_it(self, repo, monkeypatch):
        import json

        from homepilot.reconciler.inventory import InventoryReconciler

        alice, inv = await self._reused(repo)

        async def no_enrichment(*_a: Any, **_kw: Any) -> dict[str, int]:
            return {"enriched": 0, "unchanged": 0}

        monkeypatch.setattr(inv, "enrich_inventory", no_enrichment)
        result = await InventoryReconciler(inventory_service=inv, repo=repo).run()

        assert result.success is True
        assert result.details["absent_hosts"] == 1
        assert result.details["absent_host_ids"] == [alice]
        rows = await repo.db.fetchall(
            "SELECT details_json FROM audit_log WHERE action = 'refresh' "
            "AND source = 'reconciler:inventory'"
        )
        assert any(alice in json.loads(r["details_json"]).get("absent_host_ids", []) for r in rows)
