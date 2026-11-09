"""THE GATE for the guest portal's power route (#781 follow-up, 3.10.0).

3.9.0 gave the operator's power button, and an approved ``guest-power`` change,
a full set of guards (``inventory/power.py``). The friend's portal kept its own
shorter copy - gone, and the name at the VMID - so a friend could still start
their machine into an address a RUNNING guest holds (the 2026-11-07 incident,
from the other side) or onto a passthrough disk a running guest has open.

The portal now runs the SAME ``PowerService``. Asserted on outcomes, through the
real portal router and the real trust gate, against the one cluster fake:

* every guard refuses on the portal path and NOTHING reaches the hypervisor:
  an address clash, a shared passthrough disk, a reboot whose pending address
  clashes, a locked machine, an action its state does not allow, and an off
  action on a machine the operator protected;
* the refusal names nothing outside the friend's own machine - no holder, no
  VMID, no node - while the operator's audit trail keeps the full reason;
* the portal calls the operator's guard function itself, not a copy of it;
* a start that goes through reports the SETTLED state, and the friend's own
  listing shows it without waiting for the next sync.

Teeth (evidence in the PR): restore the 3.9.0 portal route (its own name check,
then ``start_vm``/``stop_vm``/``reboot_vm``) and every test in
``TestEveryGuardHoldsOnThePortal`` plus ``test_the_portal_runs_the_operators_guard``
fails - the clash, disk and protected cases with the action SENT.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI

from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.db.repository import Repository
from homepilot.guest.router import router as guest_router
from homepilot.inventory.power import PowerService
from homepilot.inventory.service import InventoryService

from .portal_support import cert_headers, client_for, portal_settings
from .test_power_controls import DISK, FakeCluster, qemu_net

pytestmark = pytest.mark.asyncio

ALICE = "alice"
# The operator's machines: their names, VMIDs and node must never reach Alice.
HOLDER = "vpn-box"
HOLDER_VMID = "101"
NODE = "pve1"


@pytest.fixture
async def portal(tmp_path: Path):
    db = Database(str(tmp_path / "portal-power.db"))
    await db.connect()
    await run_migrations(db)
    repo = Repository(db)
    pve = FakeCluster()
    app = FastAPI()
    app.include_router(guest_router, prefix="/guest")
    app.state.repo = repo
    app.state.settings = portal_settings()
    app.state.proxmox = pve

    async def sync_and_give(name: str) -> str:
        """Sync the cluster, then hand ``name`` to Alice the way provisioning does."""
        result = await InventoryService(repo, pve).refresh_inventory()
        assert result["complete"], result
        row = await repo.get_host_by_hostname(name)
        assert row is not None
        await repo.update_host(str(row["id"]), owner=ALICE)
        return str(row["id"])

    async def post(host_id: str, action: str) -> Any:
        async with client_for(app) as client:
            return await client.post(
                f"/guest/vms/{host_id}/power",
                json={"action": action},
                headers=cert_headers(cn=ALICE),
            )

    try:
        yield repo, pve, sync_and_give, post
    finally:
        await db.close()


def _leaks_nothing(text: str) -> None:
    for secret in (HOLDER, HOLDER_VMID, NODE, DISK):
        assert secret not in text, f"the portal told a guest {secret!r}: {text}"


async def _refusals(repo: Repository) -> list[dict[str, Any]]:
    rows = await repo.db.fetchall(
        "SELECT * FROM audit_log WHERE action = 'guest_power_refused' ORDER BY id"
    )
    return [dict(r) for r in rows]


class TestEveryGuardHoldsOnThePortal:
    async def test_a_start_into_an_address_a_running_guest_holds_is_refused(self, portal):
        repo, pve, give, post = portal
        pve.add(101, HOLDER, "running", qemu_net("192.0.2.10", "BC:24:11:00:00:01"))
        pve.add(105, "alice-web", "stopped", qemu_net("192.0.2.10", "BC:24:11:00:00:05"))
        mine = await give("alice-web")

        res = await post(mine, "start")

        assert res.status_code == 409, res.text
        assert res.json()["code"] == "address_clash"
        assert "network address" in res.json()["detail"]
        _leaks_nothing(res.text)
        assert pve.power_posts == [], "the start reached the hypervisor"
        assert pve.guests[105]["status"] == "stopped"
        # The operator's trail has the whole story, holder named.
        (row,) = await _refusals(repo)
        assert row["user_id"] == f"guest:{ALICE}"
        assert row["source"] == "guest-portal"
        assert HOLDER in json.loads(row["details_json"])["reason"]

    async def test_a_start_onto_a_disk_a_running_guest_has_open_is_refused(self, portal):
        _repo, pve, give, post = portal
        pve.add(
            101,
            HOLDER,
            "running",
            {**qemu_net("192.0.2.10", "BC:24:11:00:00:01"), "scsi1": f"{DISK},size=1T"},
        )
        pve.add(
            105,
            "alice-web",
            "stopped",
            {**qemu_net("192.0.2.15", "BC:24:11:00:00:05"), "scsi1": DISK},
        )
        mine = await give("alice-web")

        res = await post(mine, "start")

        assert res.status_code == 409, res.text
        assert res.json()["code"] == "disk_shared"
        _leaks_nothing(res.text)
        assert pve.power_posts == []
        assert pve.guests[105]["status"] == "stopped"

    async def test_a_reboot_that_would_apply_a_clashing_address_is_refused(self, portal):
        _repo, pve, give, post = portal
        pve.add(101, HOLDER, "running", qemu_net("192.0.2.10", "BC:24:11:00:00:01"))
        pve.add(
            105,
            "alice-web",
            "running",
            qemu_net("192.0.2.15", "BC:24:11:00:00:05"),
            pending={"ipconfig0": "ip=192.0.2.10/24,gw=192.0.2.1"},
        )
        mine = await give("alice-web")

        res = await post(mine, "reboot")

        assert res.status_code == 409, res.text
        assert res.json()["code"] == "address_clash"
        _leaks_nothing(res.text)
        assert pve.power_posts == []

    async def test_a_locked_machine_is_refused(self, portal):
        _repo, pve, give, post = portal
        pve.add(105, "alice-web", "running", qemu_net("192.0.2.15", "BC:24:11:00:00:05"))
        mine = await give("alice-web")
        pve.guests[105]["lock"] = "backup"

        res = await post(mine, "stop")

        assert res.status_code == 409, res.text
        assert res.json()["code"] == "locked"
        assert "busy" in res.json()["detail"]
        assert pve.power_posts == []
        assert pve.guests[105]["status"] == "running"

    async def test_an_action_the_state_does_not_allow_is_refused(self, portal):
        _repo, pve, give, post = portal
        pve.add(105, "alice-web", "running", qemu_net("192.0.2.15", "BC:24:11:00:00:05"))
        mine = await give("alice-web")

        res = await post(mine, "start")

        assert res.status_code == 409, res.text
        assert res.json()["code"] == "state"
        assert res.json()["detail"] == "your machine is already running"
        assert pve.power_posts == []

    async def test_a_machine_the_operator_protected_is_not_switched_off(self, portal):
        repo, pve, give, post = portal
        pve.add(105, "alice-web", "running", qemu_net("192.0.2.15", "BC:24:11:00:00:05"))
        mine = await give("alice-web")
        await repo.update_host(mine, protected=1)

        for action in ("stop", "reboot"):
            res = await post(mine, action)
            assert res.status_code == 409, res.text
            assert res.json()["code"] == "confirmation_required"
            assert "protected" in res.json()["detail"]
        assert pve.power_posts == []
        assert pve.guests[105]["status"] == "running"


class TestTheGuardIsTheOperatorsOwn:
    async def test_the_portal_runs_the_operators_guard(self, portal, monkeypatch):
        """One guard function, not a copy: the portal goes through PowerService._guard."""
        _repo, pve, give, post = portal
        pve.add(105, "alice-web", "stopped", qemu_net("192.0.2.15", "BC:24:11:00:00:05"))
        mine = await give("alice-web")
        seen: list[tuple[str, str]] = []
        real = PowerService._guard

        async def spy(self: PowerService, host: dict[str, Any], action: str, **kw: Any) -> Any:
            seen.append((str(host["id"]), action))
            return await real(self, host, action, **kw)

        monkeypatch.setattr(PowerService, "_guard", spy)

        res = await post(mine, "start")

        assert res.status_code == 200, res.text
        assert seen == [(mine, "start")]

    async def test_a_start_reports_the_settled_state_and_the_listing_shows_it(self, portal):
        repo, pve, give, post = portal
        pve.add(105, "alice-web", "stopped", qemu_net("192.0.2.15", "BC:24:11:00:00:05"))
        mine = await give("alice-web")
        # Provisioning records the address it gave the guest.
        await repo.update_host(mine, ip_address="192.0.2.15")
        assert (await repo.get_host(mine))["pve_status"] == "stopped"

        res = await post(mine, "start")

        assert res.status_code == 200, res.text
        body = res.json()
        assert body["outcome"] == "ok"
        assert body["status"] == "running"
        assert body["message"] == "your machine is running"
        _leaks_nothing(res.text)
        assert pve.guests[105]["status"] == "running"
        # No sync has run, yet the row - and so Alice's own listing - says so.
        assert (await repo.get_host(mine))["pve_status"] == "running"
        async with client_for(portal_app(repo, pve)) as client:
            listing = await client.get("/guest/vms", headers=cert_headers(cn=ALICE))
        (vm,) = listing.json()["items"]
        assert vm["status"] == "online", vm


def portal_app(repo: Repository, pve: FakeCluster) -> FastAPI:
    app = FastAPI()
    app.include_router(guest_router, prefix="/guest")
    app.state.repo = repo
    app.state.settings = portal_settings()
    app.state.proxmox = pve
    return app
