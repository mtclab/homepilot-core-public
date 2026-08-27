"""The fence that actually holds: per-VM firewall rules at provision time (#553).

The vnet firewall is the tidier place to say "a guest may not reach the operator
LAN", and on the legacy iptables stack this estate runs, PVE stores those rules
without applying them to vnet forward traffic. The rules that ARE enforced -
by both stacks - are the per-VM (tap level) ones, so HomePilot writes them while
it is building the guest.

The gates here are journeys, and each asserts an OUTCOME rather than a call:

* a guest provisioned onto the guest vnet ends up with `firewall=1` on its NIC,
  the VM firewall enabled, and the exact rule set in the exact order;
* a guest provisioned onto any other bridge gets ZERO firewall calls - a fence
  on an operator VM would cut it off its own network;
* a fence that cannot be written FAILS the provision loudly AND the half-made
  guest is destroyed, because a machine on the guest wire with the operator's
  LAN in reach is the one outcome this whole slice exists to prevent.

Teeth (each proven by planting the defect and watching the NAMED test fail):
  - drop `,firewall=1` from the NIC -> `test_the_nic_carries_firewall_1` fails
    (PVE stores rules for a NIC without it and applies none of them);
  - move the fence AFTER start_vm -> `test_the_fence_is_written_before_the_guest_boots` fails;
  - swallow the fence error and continue ->
    `test_a_fence_that_cannot_be_written_fails_the_provision` fails;
  - skip the destroy on fence failure ->
    `test_a_failed_fence_destroys_the_half_made_guest` fails;
  - fence regardless of the bridge -> `test_a_guest_on_another_bridge_is_not_fenced` fails.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from homepilot.adapters.proxmox import ProxmoxClient
from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.db.repository import Repository
from homepilot.provision.defaults import ProvisioningDefaults
from homepilot.provision.guest_network import DesiredGuestNetwork
from homepilot.provision.models import ProvisionRequest
from homepilot.provision.service import ProvisionService
from homepilot.tasks.repository import TaskRepository

pytestmark = pytest.mark.asyncio

PUBKEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGwqABCm8wgfVU6mDkhzQAaGT1kEJmpTB/J0UODkf1Xq me@lab"
AGENT_OK = {
    "data": {
        "result": [
            {"name": "lo", "ip-addresses": [{"ip-address": "127.0.0.1"}]},
            {"name": "eth0", "ip-addresses": [{"ip-address": "198.51.100.104"}]},
        ]
    }
}

GUEST_NETWORK = DesiredGuestNetwork(
    zone="guest",
    vnet="innkeep",
    subnet_cidr="198.51.100.0/24",
    gateway="198.51.100.1",
    snat=True,
    dhcp=True,
    dhcp_range="198.51.100.100-198.51.100.199",
    isolate_cidrs=("192.0.2.0/24",),
)


class FakeFirewall:
    """The per-VM firewall half of the PVE gateway, recording what it is told.

    Faked at the gateway boundary (owner mandate): the endpoints belong to the
    estate's proxmox_mcp library, so this test asserts the INTENT HomePilot
    hands it, which is the part HomePilot owns.
    """

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.options: list[dict[str, Any]] = []
        self.rules: list[dict[str, Any]] = []

    async def set_vm_firewall_options(self, node: str, vmid: int, **options: Any) -> str:
        if self.fail:
            raise RuntimeError("403 Permission check failed (/vms/104, VM.Config.Network)")
        self.options.append({"node": node, "vmid": vmid, **options})
        return "ok"

    async def create_vm_firewall_rule(self, node: str, vmid: int, **rule: Any) -> str:
        if self.fail:
            raise RuntimeError("403 Permission check failed (/vms/104, VM.Config.Network)")
        self.rules.append({"node": node, "vmid": vmid, **rule})
        return "ok"


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.connect()
    await run_migrations(database)
    yield database
    await database.close()


@pytest.fixture
def proxmox() -> AsyncMock:
    px = AsyncMock(spec=ProxmoxClient)
    px.next_vmid = AsyncMock(return_value=104)
    px.clone_vm = AsyncMock(return_value="UPID:elizabeth:clone:")
    px.wait_for_task = AsyncMock(return_value={"status": "stopped", "exitstatus": "OK"})
    px.set_vm_config = AsyncMock(return_value={"data": None})
    px.resize_disk = AsyncMock(return_value={"data": None})
    px.start_vm = AsyncMock(return_value="UPID:elizabeth:start:")
    px.delete_vm = AsyncMock(return_value="UPID:elizabeth:destroy:")
    px.get_vm_agent_network = AsyncMock(return_value=AGENT_OK)
    return px


def _service(db, proxmox, firewall, bridge: str, guest_network) -> ProvisionService:
    service = ProvisionService(
        proxmox=proxmox,
        task_repo=TaskRepository(db),
        repo=Repository(db),
        poll_interval=0.01,
        task_timeout_s=2.0,
        ip_wait_s=0.0,
        ip_interval=0.01,
    )
    service.sdn_gateway = firewall

    async def _defaults(_source: Any = None) -> ProvisioningDefaults:
        return ProvisioningDefaults(node="elizabeth", template_vmid=9000, bridge=bridge)

    async def _guest_network(_source: Any = None) -> DesiredGuestNetwork | None:
        return guest_network

    # The two settings reads the fence depends on. Patched on the MODULE, so the
    # service's real resolution path (defaults -> fence decision -> config) runs
    # exactly as it does in production, with only the database lookup replaced.
    import homepilot.provision.service as service_module

    service_module.provisioning_defaults = _defaults  # type: ignore[assignment]
    service_module.desired_from_settings = _guest_network  # type: ignore[assignment]
    return service


@pytest.fixture(autouse=True)
def _restore_module():
    import homepilot.provision.service as service_module

    real_defaults = service_module.provisioning_defaults
    real_network = service_module.desired_from_settings
    yield
    service_module.provisioning_defaults = real_defaults
    service_module.desired_from_settings = real_network


def _request(**overrides: Any) -> ProvisionRequest:
    payload: dict[str, Any] = {
        "name": "friends-box",
        "node": "elizabeth",
        "template_vmid": 9000,
        "ssh_authorized_key": PUBKEY,
        "owner": "a-friend",
    }
    payload.update(overrides)
    return ProvisionRequest(**payload)


async def _run(service: ProvisionService) -> dict[str, Any]:
    task_id = await service.start(_request())
    for _ in range(400):
        row = await service.task_repo.get_task(task_id)
        if row and row["status"] in ("succeeded", "failed", "cancelled"):
            return dict(row)
        import asyncio

        await asyncio.sleep(0.01)
    raise AssertionError("the provision never reached a terminal state")


class TestAGuestOnTheGuestVnetIsFenced:
    async def test_the_nic_carries_firewall_1(self, db, proxmox) -> None:
        firewall = FakeFirewall()
        service = _service(db, proxmox, firewall, "innkeep", GUEST_NETWORK)
        row = await _run(service)
        assert row["status"] == "succeeded", row["error"]
        config = proxmox.set_vm_config.await_args.args[2]
        assert config["net0"] == "virtio,bridge=innkeep,firewall=1", (
            "without firewall=1 on the NIC, PVE stores the rules and applies none of them"
        )

    async def test_the_vm_firewall_is_turned_on_with_an_outbound_accept_policy(
        self, db, proxmox
    ) -> None:
        firewall = FakeFirewall()
        service = _service(db, proxmox, firewall, "innkeep", GUEST_NETWORK)
        await _run(service)
        assert firewall.options == [
            {"node": "elizabeth", "vmid": 104, "enable": 1, "policy_out": "ACCEPT"}
        ], "the guest must still reach the internet; the DROPs are what fence it"

    async def test_the_rules_are_exact_and_in_order(self, db, proxmox) -> None:
        firewall = FakeFirewall()
        service = _service(db, proxmox, firewall, "innkeep", GUEST_NETWORK)
        await _run(service)
        assert [
            (r["action"], r.get("proto"), r.get("dport"), r["dest"]) for r in firewall.rules
        ] == [
            ("ACCEPT", "udp", "67:68", "198.51.100.1"),
            ("ACCEPT", "udp", "53", "198.51.100.1"),
            ("ACCEPT", "tcp", "53", "198.51.100.1"),
            ("DROP", None, None, "192.0.2.0/24"),
            ("DROP", None, None, "198.51.100.1/32"),
        ]
        assert [r["pos"] for r in firewall.rules] == [0, 1, 2, 3, 4]
        assert all(r["type"] == "out" for r in firewall.rules)

    async def test_the_fence_is_written_before_the_guest_boots(self, db, proxmox) -> None:
        """A guest that boots unfenced is on the operator's LAN until somebody
        notices. The order is the property, so the order is what is asserted."""
        order: list[str] = []
        firewall = FakeFirewall()
        real_rule = firewall.create_vm_firewall_rule

        async def record_rule(**kwargs: Any) -> str:
            order.append("rule")
            return await real_rule(**kwargs)

        firewall.create_vm_firewall_rule = record_rule  # type: ignore[assignment]

        async def record_start(node: str, vmid: int) -> str:
            order.append("start")
            return "UPID:elizabeth:start:"

        proxmox.start_vm = AsyncMock(side_effect=record_start)
        service = _service(db, proxmox, firewall, "innkeep", GUEST_NETWORK)
        await _run(service)
        assert order.index("rule") < order.index("start")

    async def test_the_applied_ruleset_is_recorded_on_the_provision(self, db, proxmox) -> None:
        """An operator asking 'is my friend's box walled off' gets the ruleset,
        not a boolean they have to trust."""
        firewall = FakeFirewall()
        service = _service(db, proxmox, firewall, "innkeep", GUEST_NETWORK)
        row = await _run(service)
        result = json.loads(row["result_json"])
        fence = result["guest_network_fence"]
        assert fence["vnet"] == "innkeep"
        assert len(fence["rules"]) == 5
        assert fence["rules"][-1]["dest"] == "198.51.100.1/32"


class TestAGuestElsewhereIsNotFenced:
    async def test_a_guest_on_another_bridge_is_not_fenced(self, db, proxmox) -> None:
        firewall = FakeFirewall()
        service = _service(db, proxmox, firewall, "vmbr0", GUEST_NETWORK)
        row = await _run(service)
        assert row["status"] == "succeeded"
        assert firewall.options == [] and firewall.rules == []
        config = proxmox.set_vm_config.await_args.args[2]
        assert config["net0"] == "virtio,bridge=vmbr0", "an operator VM must not be fenced"
        assert json.loads(row["result_json"])["guest_network_fence"] is None

    async def test_no_guest_network_configured_means_no_fence(self, db, proxmox) -> None:
        firewall = FakeFirewall()
        service = _service(db, proxmox, firewall, "innkeep", None)
        row = await _run(service)
        assert row["status"] == "succeeded"
        assert firewall.rules == []

    async def test_an_empty_isolate_list_refuses_the_guest_vnet(self, db, proxmox) -> None:
        """Empty is what an UNCONFIGURED instance looks like - the code default
        cannot name any operator's LAN (the public-build scrub proved it by
        silently rewriting one), so an empty fence list must refuse to put a
        guest on the guest wire rather than build it unfenced."""
        firewall = FakeFirewall()
        open_network = DesiredGuestNetwork(
            zone="guest",
            vnet="innkeep",
            subnet_cidr="198.51.100.0/24",
            gateway="198.51.100.1",
            dhcp_range="198.51.100.100-198.51.100.199",
            isolate_cidrs=(),
        )
        service = _service(db, proxmox, firewall, "innkeep", open_network)
        row = await _run(service)
        assert row["status"] == "failed"
        assert "guest_network_isolate_cidrs" in row["error"]
        # Refused before anything was built: no firewall writes, no boot.
        assert firewall.rules == []


class TestAFenceThatCannotBeWritten:
    async def test_a_fence_that_cannot_be_written_fails_the_provision(self, db, proxmox) -> None:
        firewall = FakeFirewall(fail=True)
        service = _service(db, proxmox, firewall, "innkeep", GUEST_NETWORK)
        row = await _run(service)
        assert row["status"] == "failed"
        assert "fence" in row["error"]
        assert "VM.Config.Network" in row["error"], "the cluster's own words, verbatim"

    async def test_a_failed_fence_destroys_the_half_made_guest(self, db, proxmox) -> None:
        firewall = FakeFirewall(fail=True)
        service = _service(db, proxmox, firewall, "innkeep", GUEST_NETWORK)
        row = await _run(service)
        proxmox.delete_vm.assert_awaited_once_with("elizabeth", 104)
        assert "destroyed" in row["error"]
        # And it never booted: an unfenced guest must not have been on the wire
        # at all, not even briefly.
        proxmox.start_vm.assert_not_awaited()

    async def test_a_destroy_that_also_fails_says_the_guest_may_remain(self, db, proxmox) -> None:
        firewall = FakeFirewall(fail=True)
        proxmox.delete_vm = AsyncMock(side_effect=RuntimeError("VM is locked"))
        service = _service(db, proxmox, firewall, "innkeep", GUEST_NETWORK)
        row = await _run(service)
        assert row["status"] == "failed"
        assert "may still be on elizabeth" in row["error"]
        assert "unfenced" in row["error"]
