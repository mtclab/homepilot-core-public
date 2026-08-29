"""The guest API sees exactly one guest's world (#442 G1).

THE PROPERTY under adversarial test: a cert-holding friend can list, read and
power-cycle THEIR machines - and nothing anyone else owns is visible,
nameable, or actionable, including "does it even exist". The operator's own
hosts (owner IS NULL) are invisible too. The trust gate is the portal's:
without the proxy's three factors, every route refuses.

Real migrated DB, real router, FakePVE at the httpx boundary for power calls.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI

from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.db.repository import Repository
from homepilot.guest.router import router as guest_router

from .portal_support import cert_headers, client_for, portal_settings

pytestmark = pytest.mark.asyncio

ALICE = "alice"
BOB = "bob"


class PowerPVE:
    """Records power calls; fails on demand."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []
        self.fail = False

    async def start_vm(self, node: str, vmid: int) -> str:
        return self._record("start", node, vmid)

    async def stop_vm(self, node: str, vmid: int) -> str:
        return self._record("stop", node, vmid)

    async def reboot_vm(self, node: str, vmid: int) -> str:
        return self._record("reboot", node, vmid)

    def _record(self, action: str, node: str, vmid: int) -> str:
        if self.fail:
            raise RuntimeError("pve exploded: node pve9, storage local-lvm full")
        self.calls.append((action, node, vmid))
        return f"UPID:{node}:{action}:{vmid}:"


@pytest.fixture
async def stack(tmp_path: Path):
    db = Database(str(tmp_path / "guest.db"))
    await db.connect()
    await run_migrations(db)
    repo = Repository(db)

    async def add(hostname: str, owner: str | None, vmid: int | None = None) -> str:
        host_id = await repo.create_host(
            hostname=hostname,
            host_type="vm",
            source="hp_created",
            owner=owner,
            proxmox_id=vmid,
            node="pve1" if vmid else None,
        )
        return host_id

    alice_vm = await add("alice-web", ALICE, vmid=105)
    bob_vm = await add("bob-box", BOB, vmid=106)
    operator_host = await add("core-infra", None)

    pve = PowerPVE()
    app = FastAPI()
    app.include_router(guest_router, prefix="/guest")
    app.state.repo = repo
    app.state.settings = portal_settings()
    app.state.proxmox = pve

    try:
        yield app, pve, {"alice": alice_vm, "bob": bob_vm, "operator": operator_host}
    finally:
        await db.close()


class TestAGuestSeesOnlyTheirWorld:
    async def test_listing_is_scoped_to_the_caller(self, stack):
        app, _pve, _ids = stack
        async with client_for(app) as client:
            res = await client.get("/guest/vms", headers=cert_headers(cn=ALICE))
        assert res.status_code == 200
        items = res.json()["items"]
        assert [h["hostname"] for h in items] == ["alice-web"]

    async def test_a_destroyed_machine_is_shown_as_gone_not_online(self, stack):
        """#613 - the guest page must never show a machine that is not there.

        WHAT THIS FORBIDS: passing the last-seen status through. The row keeps
        whatever it was when the hypervisor stopped reporting it, so a
        destroyed machine went on telling its owner it was "online" - on the
        one page that is their only window onto whether it still exists.
        """
        app, _pve, _ids = stack
        repo = app.state.repo
        await repo.db.execute(
            "UPDATE hosts SET status = 'online', pve_status = 'running' WHERE hostname = ?",
            ("alice-web",),
        )
        await repo.db.conn.commit()

        async with client_for(app) as client:
            before = await client.get("/guest/vms", headers=cert_headers(cn=ALICE))
        assert before.json()["items"][0]["status"] == "online"

        await repo.mark_hosts_absent(seen_ids=set(), sources=("hp_created",))

        async with client_for(app) as client:
            res = await client.get("/guest/vms", headers=cert_headers(cn=ALICE))

        item = res.json()["items"][0]
        assert item["status"] == "gone", "a destroyed machine still reads as online to its owner"
        assert item["gone_since"], "the guest is not told WHEN it went"

    async def test_the_view_leaks_no_topology(self, stack):
        """No node, no template, no proxmox id, no tags: operator vocabulary
        stays out of guest pages."""
        app, _pve, _ids = stack
        async with client_for(app) as client:
            res = await client.get("/guest/vms", headers=cert_headers(cn=ALICE))
        body = res.text
        for word in ("pve1", "proxmox_id", "node", "import_state", "managed_by"):
            assert word not in body, f"guest listing leaked operator field: {word}"

    async def test_another_guests_machine_is_indistinguishable_from_a_typo(self, stack):
        app, _pve, ids = stack
        async with client_for(app) as client:
            other = await client.get(f"/guest/vms/{ids['bob']}", headers=cert_headers(cn=ALICE))
            wrong = await client.get("/guest/vms/no-such-id", headers=cert_headers(cn=ALICE))
        assert other.status_code == wrong.status_code == 404
        assert other.json() == wrong.json(), (
            "the two 404s differ - existence of other guests' machines is probeable"
        )

    async def test_the_operators_hosts_are_invisible(self, stack):
        app, _pve, ids = stack
        async with client_for(app) as client:
            res = await client.get(f"/guest/vms/{ids['operator']}", headers=cert_headers(cn=ALICE))
        assert res.status_code == 404


class TestPowerIsScopedAndHonest:
    async def test_a_guest_can_power_cycle_their_own_machine(self, stack):
        app, pve, ids = stack
        async with client_for(app) as client:
            res = await client.post(
                f"/guest/vms/{ids['alice']}/power",
                json={"action": "reboot"},
                headers=cert_headers(cn=ALICE),
            )
        assert res.status_code == 200
        assert pve.calls == [("reboot", "pve1", 105)]

    async def test_a_guest_cannot_power_anyone_elses_machine(self, stack):
        """THE adversarial gate: Alice sends Bob's host id. Nothing reaches
        Proxmox, and the answer is the uniform 404."""
        app, pve, ids = stack
        async with client_for(app) as client:
            res = await client.post(
                f"/guest/vms/{ids['bob']}/power",
                json={"action": "stop"},
                headers=cert_headers(cn=ALICE),
            )
        assert res.status_code == 404
        assert pve.calls == [], "an unauthorized power action reached the hypervisor"

    async def test_only_the_three_actions_exist(self, stack):
        app, pve, ids = stack
        async with client_for(app) as client:
            res = await client.post(
                f"/guest/vms/{ids['alice']}/power",
                json={"action": "destroy"},
                headers=cert_headers(cn=ALICE),
            )
        assert res.status_code == 400
        assert pve.calls == []

    async def test_a_pve_failure_never_leaks_its_text_to_the_guest(self, stack):
        app, pve, ids = stack
        pve.fail = True
        async with client_for(app) as client:
            res = await client.post(
                f"/guest/vms/{ids['alice']}/power",
                json={"action": "start"},
                headers=cert_headers(cn=ALICE),
            )
        assert res.status_code == 502
        assert "pve9" not in res.text and "local-lvm" not in res.text, (
            "the hypervisor's error text leaked into a guest response"
        )


class TestTheTrustGateHoldsHere:
    async def test_no_certificate_no_answers(self, stack):
        app, _pve, _ids = stack
        async with client_for(app) as client:
            res = await client.get("/guest/vms")
        assert res.status_code == 403

    async def test_wrong_proxy_secret_is_refused(self, stack):
        app, _pve, _ids = stack
        async with client_for(app) as client:
            res = await client.get(
                "/guest/vms", headers=cert_headers(cn=ALICE, secret="stolen-guess")
            )
        assert res.status_code == 403

    async def test_untrusted_source_is_refused_even_with_perfect_headers(self, stack):
        app, _pve, _ids = stack
        async with client_for(app, peer="203.0.113.99") as client:
            res = await client.get("/guest/vms", headers=cert_headers(cn=ALICE))
        assert res.status_code == 403


class TestThePortalPageShipsInTheBackend:
    async def test_guest_root_serves_the_portal_shell(self, stack):
        """The front nginx adds ONE proxy location and copies nothing: the
        page itself comes from the backend. A data-free shell, so no
        certificate is needed to receive it - the APIs behind it stay gated."""
        app, _pve, _ids = stack
        async with client_for(app) as client:
            res = await client.get("/guest/")
        assert res.status_code == 200
        assert "Your machines" in res.text
        # And it is the SHELL - no data baked in, everything fetched.
        assert "alice-web" not in res.text
