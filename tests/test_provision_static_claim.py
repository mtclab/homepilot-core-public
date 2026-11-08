"""An operator-written static address is checked before anything is built (lane B2).

The allocator (#630) scans the guest vnet - but only when it is the one choosing
the address. An explicit ``ipconfig0=ip=<addr>`` from the operator, or from the
provisioning defaults, was written into the new guest unexamined, so provisioning
could build a second machine onto an address a running guest already held: the
same hijack as starting a stopped duplicate, done by HomePilot itself.

Journeys over the stateful PVE fake from test_provision_static_ip: what the
cluster was asked to build (nothing, on a refusal) and what the task says.

Teeth (proven by removing the call to ``_refuse_claimed_static`` in
``ProvisionService._run`` and watching these fail):
  * ``test_an_address_a_running_guest_holds_is_refused_before_the_clone``
  * ``test_an_address_a_stopped_guest_is_configured_with_is_refused_too``
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from httpx import Request, Response

from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from tests.test_provision_static_ip import NODE, VNET, StatefulPVE, World, _settings

pytestmark = pytest.mark.asyncio

STATIC = "ip=198.51.100.50/24,gw=198.51.100.1"


class ClaimingPVE(StatefulPVE):
    """StatefulPVE whose cluster listing says which guests are running."""

    def __init__(self) -> None:
        super().__init__()
        self.running: set[int] = set()
        # Guests whose config read fails, as a node going away mid-scan does.
        self.unreadable: set[int] = set()

    def handle(self, request: Request) -> Response:
        path = request.url.path.removeprefix("/api2/json")
        if any(path == f"/nodes/{NODE}/qemu/{v}/config" for v in self.unreadable):
            return Response(500, text="node went away")
        if path == "/cluster/resources":
            self.calls.append((request.method, path))
            return Response(
                200,
                json={
                    "data": [
                        {
                            "vmid": vmid,
                            "node": NODE,
                            "type": g["type"],
                            "name": g["config"].get("name", f"guest-{vmid}"),
                            "status": "running" if vmid in self.running else "stopped",
                            "template": 1 if g["template"] else 0,
                        }
                        for vmid, g in sorted(self.guests.items())
                    ]
                },
            )
        return super().handle(request)


@pytest.fixture
async def world(tmp_path: Path):
    db = Database(str(tmp_path / "hp.db"))
    await db.connect()
    await run_migrations(db)
    try:
        yield lambda: World(db, ClaimingPVE(), _settings())
    finally:
        await db.close()


def _holder(pve: ClaimingPVE, vmid: int, bridge: str = VNET, running: bool = True) -> None:
    pve.add_guest(
        vmid,
        {
            "name": f"holder-{vmid}",
            "net0": f"virtio=BC:24:11:00:00:{vmid % 100:02d},bridge={bridge}",
            "ipconfig0": STATIC,
        },
    )
    if running:
        pve.running.add(vmid)


class TestAStaticAddressSomebodyHoldsIsRefused:
    async def test_an_address_a_running_guest_holds_is_refused_before_the_clone(self, world):
        w = world()
        _holder(w.pve, 120)
        row = await w.provision(ipconfig0=STATIC)
        assert row["status"] == "failed"
        assert "198.51.100.50 is already claimed by holder-120 (VM 120, running)" in row["error"]
        assert w.pve.clone_count == 0, "a refusal must never leave a half-built guest"

    async def test_an_address_a_stopped_guest_is_configured_with_is_refused_too(self, world):
        """The new guest starts at the end of the provision; the stopped one would
        take the address back the moment anybody starts it."""
        w = world()
        _holder(w.pve, 121, running=False)
        row = await w.provision(ipconfig0=STATIC)
        assert row["status"] == "failed"
        assert "holder-121 (VM 121, stopped)" in row["error"]
        assert w.pve.clone_count == 0

    async def test_a_free_address_is_built_as_asked(self, world):
        w = world()
        row = await w.provision(ipconfig0=STATIC)
        assert row["status"] == "succeeded", row["error"]
        assert w.pve.config_of(9001)["ipconfig0"] == STATIC
        assert json.loads(row["result_json"])["ipconfig0"] == STATIC

    async def test_the_same_address_on_another_bridge_is_not_a_claim(self, world):
        w = world()
        _holder(w.pve, 122, bridge="vmbr0")
        row = await w.provision(ipconfig0=STATIC)
        assert row["status"] == "succeeded", row["error"]

    async def test_dhcp_and_allocation_are_untouched(self, world):
        """The allocator path keeps its own scan; this check adds nothing to it."""
        w = world()
        _holder(w.pve, 123)
        row = await w.provision()
        assert row["status"] == "succeeded", row["error"]
        assert w.pve.config_of(9001)["ipconfig0"] != STATIC

    async def test_a_scan_that_cannot_complete_refuses(self, world):
        """A guest whose config cannot be read might hold the address; not
        knowing is a refusal, never a pass. (Guest 125 holds a DIFFERENT
        address, so only the unanswered read can be what refuses.)"""
        w = world()
        w.pve.add_guest(125, {"name": "mute", "net0": f"virtio,bridge={VNET}"})
        w.pve.unreadable.add(125)
        row = await w.provision(ipconfig0=STATIC)
        assert row["status"] == "failed"
        assert "could not read the config of qemu 125" in row["error"]
        assert w.pve.clone_count == 0

    async def test_two_provisions_in_flight_cannot_share_a_static_address(self, world):
        w = world()
        w.service.reservations.reserve_address("198.51.100.50")
        row = await w.provision(ipconfig0=STATIC)
        assert row["status"] == "failed"
        assert "provisioning right now" in row["error"]
        assert w.pve.clone_count == 0
