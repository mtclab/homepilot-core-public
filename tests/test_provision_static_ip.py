"""HomePilot allocates the guest's address itself (#630).

The defect this gates: prod's SDN guest network has no DHCP server (a `simple`
zone serves DHCP through dnsmasq, the node has no dnsmasq, and installing it is
a node mutation the operator refuses). Provisioning wrote `ipconfig0=ip=dhcp`
anyway, so the first real guest booted with a link-local address while every
surface said success. A silent gap reached a guest, so the gates here are
JOURNEYS - what the cluster was actually asked to build, and what the guest
record ends up saying - never "the allocator returned an address".

The fake is the PVE HTTP API, at the httpx boundary, and it is STATEFUL: a
clone really creates a guest, a config write really lands on it, and the next
provision's scan really reads it back. That is the only way "two provisions get
different addresses" and "a destroyed guest frees its address" can be asserted
at all, because both are claims about what the scan sees.

Teeth (each proven by reverting the allocator and watching the NAMED test fail):
  * return None from `ProvisionService._allocate_address` (i.e. go back to
    writing ip=dhcp) ->
    `test_static_mode_writes_a_concrete_address_gateway_and_nameserver` fails;
  * make `claimed_addresses` return an empty set (the "no bookkeeping needed"
    shortcut) -> `test_two_sequential_provisions_get_different_addresses` and
    `test_an_address_held_by_an_existing_guest_is_skipped` fail;
  * let the service swallow `SubnetExhaustedError` and fall back to ip=dhcp ->
    `test_an_exhausted_subnet_fails_the_provision_before_the_clone` fails;
  * move the allocation to AFTER the clone -> that same test and
    `test_a_config_that_cannot_be_read_refuses_rather_than_guesses` fail, which
    is the promise that a refusal never strands a half-made guest.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from httpx import Request, Response

from homepilot.adapters.proxmox import ProxmoxClient
from homepilot.app_settings import SettingsResolver
from homepilot.config import Settings
from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.db.repository import Repository
from homepilot.portal.models import InviteCaps, RedemptionIdentity, build_provision_request
from homepilot.provision.models import ProvisionRequest
from homepilot.provision.service import ProvisionService
from homepilot.tasks.repository import TaskRepository

pytestmark = pytest.mark.asyncio

PUBKEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGwqABCm8wgfVU6mDkhzQAaGT1kEJmpTB/J0UODkf1Xq me@lab"
NODE = "elizabeth"
VNET = "innkeep"
SUBNET = "198.51.100.0/24"
GATEWAY = "198.51.100.1"


class StatefulPVE:
    """A PVE that remembers what was built on it.

    Stateful on purpose: the allocator's whole design is "ask the cluster who
    holds what", so a fake that cannot be asked back would let the allocator
    hand out one address twice and still pass.
    """

    def __init__(self) -> None:
        self.guests: dict[int, dict[str, Any]] = {}
        self.calls: list[tuple[str, str]] = []
        self.clone_count = 0
        # A template to clone from, on the guest bridge, addressed by DHCP -
        # i.e. holding no address of its own.
        self.add_guest(
            9000, {"net0": f"virtio,bridge={VNET}", "ipconfig0": "ip=dhcp"}, template=True
        )

    def add_guest(
        self, vmid: int, config: dict[str, Any], template: bool = False, guest_type: str = "qemu"
    ) -> None:
        self.guests[vmid] = {"type": guest_type, "template": template, "config": dict(config)}

    def remove_guest(self, vmid: int) -> None:
        del self.guests[vmid]

    def config_of(self, vmid: int) -> dict[str, Any]:
        return self.guests[vmid]["config"]

    def handle(self, request: Request) -> Response:
        path = request.url.path.removeprefix("/api2/json")
        self.calls.append((request.method, path))
        body = json.loads(request.content) if request.content else {}

        if path == "/cluster/resources":
            return Response(
                200,
                json={
                    "data": [
                        {
                            "vmid": vmid,
                            "node": NODE,
                            "type": guest["type"],
                            "template": 1 if guest["template"] else 0,
                        }
                        for vmid, guest in sorted(self.guests.items())
                    ]
                },
            )
        if path == "/cluster/nextid":
            return Response(200, json={"data": max(self.guests) + 1})
        if path.startswith(f"/nodes/{NODE}/tasks/"):
            return Response(200, json={"data": {"status": "stopped", "exitstatus": "OK"}})

        parts = path.strip("/").split("/")
        # /nodes/<node>/<collection>/<vmid>/<rest...>
        if len(parts) >= 4 and parts[0] == "nodes" and parts[2] in ("qemu", "lxc"):
            vmid = int(parts[3])
            tail = "/".join(parts[4:])
            if tail == "clone":
                self.clone_count += 1
                new_vmid = int(body["newid"])
                self.add_guest(new_vmid, dict(self.guests[vmid]["config"]))
                return Response(200, json={"data": f"UPID:{NODE}:clone:{new_vmid}"})
            if tail == "config" and request.method == "GET":
                return Response(200, json={"data": dict(self.guests[vmid]["config"])})
            if tail == "config":
                self.guests[vmid]["config"].update(body)
                return Response(200, json={"data": None})
            if tail == "resize":
                return Response(200, json={"data": None})
            if tail == "status/start":
                return Response(200, json={"data": f"UPID:{NODE}:start:{vmid}"})
            if tail == "agent/network-get-interfaces":
                # No guest agent. THE prod condition: a bare cloud image never
                # answers, which is why the address has to be known without it.
                return Response(500, json={"data": None})
        return Response(501, text=f"unhandled {request.method} {path}")


class FakeFirewall:
    """The per-VM fence boundary (the estate's proxmox_mcp library), recording."""

    def __init__(self) -> None:
        self.rules: list[dict[str, Any]] = []

    async def set_vm_firewall_options(self, node: str, vmid: int, **options: Any) -> str:
        return "ok"

    async def create_vm_firewall_rule(self, node: str, vmid: int, **rule: Any) -> str:
        self.rules.append({"vmid": vmid, **rule})
        return "ok"


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "data_dir": "/tmp/hp-630-test",
        "artifacts_dir": "/tmp/hp-630-test/artifacts",
        "provision_default_node": NODE,
        "provision_default_template_vmid": 9000,
        "provision_default_bridge": VNET,
        "provision_default_ipconfig": "ip=dhcp",
        "guest_network_zone": "guest",
        "guest_network_vnet": VNET,
        "guest_network_subnet": SUBNET,
        "guest_network_gateway": GATEWAY,
        "guest_network_dhcp": 0,
        "guest_network_isolate_cidrs": "192.0.2.0/24",
    }
    values.update(overrides)
    return Settings(**values)


class World:
    def __init__(self, db: Database, pve: StatefulPVE, settings: Settings) -> None:
        self.pve = pve
        self.settings = settings
        self.repo = Repository(db)
        self.firewall = FakeFirewall()
        proxmox = ProxmoxClient(base_url="https://pve.example:8006", token="root@pam!t=uuid")
        client = httpx.AsyncClient(
            base_url="https://pve.example:8006/api2/json",
            transport=httpx.MockTransport(pve.handle),
        )
        proxmox._client = client
        proxmox._write_client = client
        self.proxmox = proxmox
        state = SimpleNamespace(
            repo=self.repo,
            settings=settings,
            settings_resolver=SettingsResolver(self.repo, settings),
        )
        self.service = ProvisionService(
            proxmox=proxmox,
            task_repo=TaskRepository(db),
            repo=self.repo,
            poll_interval=0.01,
            task_timeout_s=2.0,
            ip_wait_s=0.0,
            ip_interval=0.01,
            defaults_source=state,
        )
        self.service.sdn_gateway = self.firewall

    async def provision(self, name: str = "friends-box", **overrides: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": name,
            "node": NODE,
            "template_vmid": 9000,
            "ssh_authorized_key": PUBKEY,
            "owner": "a-friend",
        }
        payload.update(overrides)
        return await self.run(ProvisionRequest(**payload))

    async def run(self, request: ProvisionRequest) -> dict[str, Any]:
        task_id = await self.service.start(request)
        for _ in range(600):
            row = await self.service.task_repo.get_task(task_id)
            if row and row["status"] in ("succeeded", "failed", "cancelled"):
                return dict(row)
            await asyncio.sleep(0.01)
        raise AssertionError("the provision never reached a terminal state")


@pytest.fixture
async def world(tmp_path: Path):
    db = Database(str(tmp_path / "hp.db"))
    await db.connect()
    await run_migrations(db)
    try:
        yield lambda **overrides: World(db, StatefulPVE(), _settings(**overrides))
    finally:
        await db.close()


def _configured(pve: StatefulPVE, vmid: int) -> dict[str, Any]:
    return pve.config_of(vmid)


class TestStaticModeAddressesTheGuest:
    async def test_static_mode_writes_a_concrete_address_gateway_and_nameserver(self, world):
        """The prod failure, inverted: the guest leaves with an address."""
        w = world()
        row = await w.provision()
        assert row["status"] == "succeeded", row["error"]

        config = _configured(w.pve, 9001)
        assert config["ipconfig0"] == "ip=198.51.100.10/24,gw=198.51.100.1", (
            "a guest on a subnet with no DHCP server must be handed its address, "
            "not told to go and ask for one"
        )
        assert config["nameserver"] == "1.1.1.1", (
            "nothing hands out a resolver on a subnet with no DHCP either"
        )
        result = json.loads(row["result_json"])
        assert result["ipconfig0"] == "ip=198.51.100.10/24,gw=198.51.100.1"
        assert result["ip"] == "198.51.100.10"

    async def test_the_lowest_free_address_is_at_or_above_the_infra_floor(self, world):
        """.1-.9 belong to infrastructure - the gateway is only the first of them."""
        w = world()
        await w.provision()
        address = ipaddress.IPv4Address(
            _configured(w.pve, 9001)["ipconfig0"].split("=")[1].split("/")[0]
        )
        assert int(address) - int(ipaddress.IPv4Address("198.51.100.0")) >= 10

    async def test_two_sequential_provisions_get_different_addresses(self, world):
        """The whole point of scanning: the second guest must not be handed the
        first one's address."""
        w = world()
        await w.provision("first")
        await w.provision("second")

        first = _configured(w.pve, 9001)["ipconfig0"]
        second = _configured(w.pve, 9002)["ipconfig0"]
        assert first == "ip=198.51.100.10/24,gw=198.51.100.1"
        assert second == "ip=198.51.100.11/24,gw=198.51.100.1"
        assert first != second

    async def test_an_address_held_by_an_existing_guest_is_skipped(self, world):
        """An address HomePilot did not hand out is still an address in use."""
        w = world()
        w.pve.add_guest(
            120,
            {"net0": f"virtio,bridge={VNET}", "ipconfig0": "ip=198.51.100.10/24,gw=198.51.100.1"},
        )
        await w.provision()
        assert _configured(w.pve, 9001)["ipconfig0"] == "ip=198.51.100.11/24,gw=198.51.100.1", (
            "the .10 an existing guest holds must be skipped, not re-issued"
        )

    async def test_a_container_on_the_guest_bridge_holds_its_address_too(self, world):
        """An LXC states its address on the NIC line rather than on a cloud-init
        one. It is still a machine on the wire holding that address."""
        w = world()
        w.pve.add_guest(
            121,
            {"net0": f"name=eth0,bridge={VNET},ip=198.51.100.10/24,gw={GATEWAY}"},
            guest_type="lxc",
        )
        await w.provision()
        assert _configured(w.pve, 9001)["ipconfig0"] == "ip=198.51.100.11/24,gw=198.51.100.1"

    async def test_an_address_on_another_bridge_is_not_treated_as_taken(self, world):
        """A guest on the operator LAN holding 198.51.100.10 there says nothing
        about the guest wire - refusing it would burn addresses for nothing."""
        w = world()
        w.pve.add_guest(
            122,
            {"net0": "virtio,bridge=vmbr0", "ipconfig0": "ip=198.51.100.10/24,gw=198.51.100.1"},
        )
        await w.provision()
        assert _configured(w.pve, 9001)["ipconfig0"] == "ip=198.51.100.10/24,gw=198.51.100.1"

    async def test_destroying_a_guest_frees_its_address_for_the_next_one(self, world):
        """The reason there is no allocation table: the cluster IS the record,
        so a guest destroyed by any means at all returns its address."""
        w = world()
        await w.provision("first")
        assert _configured(w.pve, 9001)["ipconfig0"] == "ip=198.51.100.10/24,gw=198.51.100.1"

        w.pve.remove_guest(9001)

        await w.provision("second")
        assert _configured(w.pve, 9001)["ipconfig0"] == "ip=198.51.100.10/24,gw=198.51.100.1", (
            "the destroyed guest's address must come back into supply - there is "
            "no table to forget to update, so nothing can leak an address"
        )

    async def test_the_guest_record_carries_the_address_without_a_guest_agent(self, world):
        """What the friend sees on the portal. The fake PVE's agent endpoint
        fails on purpose: a bare cloud image never answers, and before #630 such
        a guest showed 'no address yet' forever."""
        w = world()
        await w.provision()
        host = await w.repo.get_host_by_proxmox_id(9001)
        assert host is not None
        assert host["ip_address"] == "198.51.100.10"
        assert host["status"] == "online"
        assert ("GET", f"/nodes/{NODE}/qemu/9001/agent/network-get-interfaces") not in w.pve.calls


class TestWhatMustNotChange:
    async def test_dhcp_mode_restores_the_previous_behaviour_exactly(self, world):
        w = world(provision_ip_mode="dhcp")
        row = await w.provision()
        assert row["status"] == "succeeded", row["error"]
        config = _configured(w.pve, 9001)
        assert config["ipconfig0"] == "ip=dhcp"
        assert "nameserver" not in config
        assert json.loads(row["result_json"])["ipconfig0"] == "ip=dhcp"

    async def test_an_explicit_static_request_passes_through_untouched(self, world):
        """An operator who wrote an address means it."""
        w = world()
        row = await w.provision(ipconfig0="ip=198.51.100.77/24,gw=198.51.100.1")
        assert row["status"] == "succeeded", row["error"]
        config = _configured(w.pve, 9001)
        assert config["ipconfig0"] == "ip=198.51.100.77/24,gw=198.51.100.1"
        assert "nameserver" not in config, (
            "the allocator did not choose this address, so it does not get to "
            "decide the guest's resolver either"
        )

    async def test_a_guest_on_another_bridge_gets_no_guest_subnet_address(self, world):
        """The same condition the fence uses: an operator VM on vmbr0 must
        never be handed an address out of the friends' subnet."""
        w = world(provision_default_bridge="vmbr0")
        row = await w.provision()
        assert row["status"] == "succeeded", row["error"]
        assert _configured(w.pve, 9001)["ipconfig0"] == "ip=dhcp"

    async def test_an_instance_with_no_guest_network_still_provisions(self, world):
        """A fresh install describes no guest subnet. There is nothing to
        allocate out of, and that must not fail a provision."""
        w = world(guest_network_subnet="", guest_network_gateway="")
        row = await w.provision()
        assert row["status"] == "succeeded", row["error"]
        assert _configured(w.pve, 9001)["ipconfig0"] == "ip=dhcp"


class TestRefusalsHappenBeforeAnythingIsBuilt:
    async def test_an_exhausted_subnet_fails_the_provision_before_the_clone(self, world):
        """A refusal that arrives after the clone leaves a guest nobody asked
        for. This one arrives before there is anything to leave."""
        w = world(guest_network_subnet="198.51.100.0/28", guest_network_gateway="198.51.100.1")
        # /28 = .1-.14 usable; .1-.9 are infra, so .10-.14 are the whole supply.
        for offset, vmid in enumerate(range(130, 135)):
            w.pve.add_guest(
                vmid,
                {
                    "net0": f"virtio,bridge={VNET}",
                    "ipconfig0": f"ip=198.51.100.{10 + offset}/28,gw=198.51.100.1",
                },
            )

        row = await w.provision()

        assert row["status"] == "failed"
        assert "no free address left" in row["error"]
        assert "failed at allocate_ip" in row["error"], (
            "the step must name WHERE it died, or an operator cannot act on it"
        )
        assert w.pve.clone_count == 0, "nothing may be cloned once the address is refused"
        assert list(w.pve.guests) == [9000, 130, 131, 132, 133, 134], (
            "a refused provision must leave the cluster exactly as it found it"
        )

    async def test_a_config_that_cannot_be_read_refuses_rather_than_guesses(self, world):
        """A partial scan is how two guests end up on one address. The provision
        stops instead - still before the clone."""
        w = world()
        w.pve.add_guest(140, {"net0": f"virtio,bridge={VNET}"})
        real_handle = w.pve.handle

        def handle(request: Request) -> Response:
            if request.url.path.endswith("/qemu/140/config") and request.method == "GET":
                return Response(500, text="hookscript error")
            return real_handle(request)

        w.pve.handle = handle  # type: ignore[method-assign]
        client = httpx.AsyncClient(
            base_url="https://pve.example:8006/api2/json",
            transport=httpx.MockTransport(handle),
        )
        w.proxmox._client = client
        w.proxmox._write_client = client

        row = await w.provision()

        assert row["status"] == "failed"
        assert "addresses already in use is not known" in row["error"]
        assert w.pve.clone_count == 0


class TestAnInviteIsAllocatedWhenItIsREDEEMED:
    async def test_a_frozen_invite_carrying_ip_dhcp_gets_its_address_at_build_time(self, world):
        """Two outstanding invites must not be able to claim one address, so an
        invite freezes 'ip=dhcp' and the address is chosen when the guest is
        actually built."""
        w = world()
        caps = InviteCaps(template_vmid=9000, node=NODE, ipconfig0="ip=dhcp")
        identity = RedemptionIdentity(ciuser="friend", ssh_authorized_key=PUBKEY)

        first = build_provision_request(caps, identity, "first-friend", "cn-a")
        row = await w.run(first)
        assert row["status"] == "succeeded", row["error"]

        second = build_provision_request(caps, identity, "second-friend", "cn-b")
        row = await w.run(second)
        assert row["status"] == "succeeded", row["error"]

        assert _configured(w.pve, 9001)["ipconfig0"] == "ip=198.51.100.10/24,gw=198.51.100.1"
        assert _configured(w.pve, 9002)["ipconfig0"] == "ip=198.51.100.11/24,gw=198.51.100.1"


class TestTheSettingsRefuseWhatTheyCannotHonour:
    """The two new settings are ordinary registry entries, and the registry-wide
    gates in test_app_settings.py already cover shape, env precedence and the
    report. What is asserted here is the part specific to #630: a mode nobody
    can act on is refused, and 'dhcp' is checked against the thing it depends
    on rather than waved through."""

    async def test_a_mode_that_is_neither_static_nor_dhcp_is_refused(self):
        from homepilot.app_settings import REGISTRY, SettingError

        with pytest.raises(SettingError):
            REGISTRY["provision_ip_mode"].parse("auto")
        assert REGISTRY["provision_ip_mode"].parse("STATIC") == "static"
        # An emptied field falls back to static, never to dhcp: an install that
        # cannot say what it wants must not end up depending on a DHCP server.
        assert REGISTRY["provision_ip_mode"].parse("") == "static"

    async def test_a_nameserver_that_is_not_an_ipv4_address_is_refused(self):
        from homepilot.app_settings import REGISTRY, SettingError

        with pytest.raises(SettingError):
            REGISTRY["provision_default_nameserver"].parse("dns.example.com")
        assert REGISTRY["provision_default_nameserver"].parse(" 9.9.9.9 ") == "9.9.9.9"

    async def test_dhcp_mode_is_refused_on_a_cluster_where_no_zone_serves_dhcp(self):
        """The prod condition, caught at the setting instead of at the guest."""
        from homepilot.provision.probes import PROBES, ProbeContext

        class Cluster:
            async def read(self, path: str, query: Any = None) -> dict[str, Any]:
                assert path == "/cluster/sdn/zones"
                return {"data": [{"zone": "guest", "type": "simple"}]}

        result = await PROBES["provision_ip_mode"]("dhcp", ProbeContext(proxmox=Cluster()))
        assert result.ok is False
        assert result.reachable is True
        assert "no SDN zone" in result.detail

        # Static depends on nothing, so it needs no cluster at all.
        static = await PROBES["provision_ip_mode"]("static", ProbeContext(proxmox=None))
        assert static.ok is True
