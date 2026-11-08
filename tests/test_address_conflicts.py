"""THE GATE for lane B2: every address a host holds, and who else holds it.

Two incidents this exists for (no environment detail here, only their shape):

* a stopped VM was started whose cloud-init ``ipconfig0`` named an address a
  RUNNING VM already used, and it silently took that address over on the LAN;
* another VM shared an address with a VPN box for days.

HomePilot knew both machines' configs and said nothing. These tests assert the
operator-visible outcomes, through the real refresh over a fake hypervisor, the
real repository, the real dashboard summary and the real HTTP routes:

* two guests configured with one address -> ONE critical attention item that
  names BOTH guests (and the remedy);
* a stopped guest whose address a running guest holds ->
  ``address_conflicts_for_start`` returns that running holder;
* the things that must NOT clash (other bridge, gone host, template, DHCP, a
  stopped holder at start) stay quiet.

Teeth (each proven by reverting the named code and watching the named test
fail; evidence in the PR):
  * drop the configured-vs-configured loop in ``find_conflicts`` ->
    ``test_two_guests_on_one_address_raise_one_critical_line_naming_both`` fails;
  * drop the ``absent_since`` filter -> ``test_a_gone_guest_holds_nothing`` fails;
  * make ``same_wire`` always True ->
    ``test_the_same_address_on_another_bridge_is_another_network`` fails;
  * read ``/config`` instead of ``/pending`` in ``AddressGuard.target_addresses``
    -> ``test_a_reboot_applying_a_pending_ipconfig_is_checked_against_it`` fails;
  * make ``AddressGuard.holders`` swallow a failed config read ->
    ``test_a_scan_that_cannot_complete_refuses_rather_than_answers`` fails.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from homepilot.auth.deps import require_token
from homepilot.dashboard.service import build_summary
from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.db.repository import Repository
from homepilot.inventory.addresses import (
    AddressCheckError,
    NeighbourAnswer,
    NicAddress,
    find_conflicts,
    parse_agent_interfaces,
    parse_guest_config,
    record_probe_answers,
)
from homepilot.inventory.service import InventoryService
from homepilot.reconciler.inventory import InventoryReconciler

pytestmark = pytest.mark.asyncio

NODE = "pve1"
LAN = "vmbr0"


class FakePVE:
    """The reads a refresh, an enrich and the address guard make, from one state.

    ``guests[vmid] = {"type", "name", "status", "config", "pending", "agent"}``.
    Stateful, so a test can stop or start a guest and ask again.
    """

    def __init__(self) -> None:
        self.guests: dict[int, dict[str, Any]] = {}
        self.fail_config: set[int] = set()
        self.node_network: list[dict[str, Any]] = [
            {"iface": LAN, "type": "bridge", "address": "192.0.2.2", "cidr": "192.0.2.2/24"}
        ]

    def add(
        self,
        vmid: int,
        name: str,
        status: str,
        config: dict[str, Any],
        guest_type: str = "qemu",
        agent: list[dict[str, Any]] | None = None,
        pending: dict[str, Any] | None = None,
        template: bool = False,
    ) -> None:
        self.guests[vmid] = {
            "type": guest_type,
            "name": name,
            "status": status,
            "config": {"name": name, **config, **({"template": 1} if template else {})},
            "pending": pending or {},
            "agent": agent,
            "template": template,
        }

    async def read(self, path: str, query: dict[str, Any] | None = None) -> dict[str, Any]:
        if path == "/nodes":
            return {"data": [{"node": NODE, "status": "online"}]}
        if path == f"/nodes/{NODE}/network":
            return {"data": self.node_network}
        if path == "/cluster/resources":
            return {
                "data": [
                    {
                        "vmid": vmid,
                        "node": NODE,
                        "type": g["type"],
                        "name": g["name"],
                        "status": g["status"],
                        "template": 1 if g["template"] else 0,
                    }
                    for vmid, g in sorted(self.guests.items())
                ]
            }
        for guest_type in ("qemu", "lxc"):
            if path == f"/nodes/{NODE}/{guest_type}":
                return {
                    "data": [
                        {"vmid": vmid, "name": g["name"], "status": g["status"]}
                        for vmid, g in sorted(self.guests.items())
                        if g["type"] == guest_type
                    ]
                }
        parts = path.strip("/").split("/")
        if len(parts) >= 5 and parts[0] == "nodes" and parts[2] in ("qemu", "lxc"):
            vmid = int(parts[3])
            guest = self.guests[vmid]
            tail = "/".join(parts[4:])
            if tail == "config":
                if vmid in self.fail_config:
                    raise ConnectionError("node went away mid-read")
                return {"data": dict(guest["config"])}
            if tail == "pending":
                rows = []
                for key, value in guest["config"].items():
                    row: dict[str, Any] = {"key": key, "value": value}
                    if key in guest["pending"]:
                        row["pending"] = guest["pending"][key]
                    rows.append(row)
                for key, value in guest["pending"].items():
                    if key not in guest["config"]:
                        rows.append({"key": key, "pending": value})
                return {"data": rows}
            if tail == "agent/network-get-interfaces":
                if guest["status"] != "running" or guest["agent"] is None:
                    import httpx

                    request = httpx.Request("GET", "https://pve.example/x")
                    raise httpx.HTTPStatusError(
                        "agent not running",
                        request=request,
                        response=httpx.Response(500, request=request),
                    )
                return {"data": {"result": guest["agent"]}}
        raise AssertionError(f"unexpected read {path}")


def qemu_static(
    address: str, mac: str, bridge: str = LAN, tag: int | None = None
) -> dict[str, Any]:
    net0 = f"virtio={mac},bridge={bridge}" + (f",tag={tag}" if tag else "")
    return {"net0": net0, "ipconfig0": f"ip={address}/24,gw=192.0.2.1"}


@pytest.fixture
async def world(tmp_path: Path):
    db = Database(str(tmp_path / "homepilot.db"))
    await db.connect()
    await run_migrations(db)
    repo = Repository(db)
    pve = FakePVE()
    svc = InventoryService(repo, pve)
    try:
        yield repo, pve, svc
    finally:
        await db.close()


async def _sync(svc: InventoryService) -> None:
    result = await svc.refresh_inventory()
    assert result["complete"], result
    await svc.enrich_inventory()


async def _host(repo: Repository, name: str) -> dict[str, Any]:
    row = await repo.get_host_by_hostname(name)
    assert row is not None, name
    return row


class TestConfiguredClashesRaiseOneCriticalLine:
    async def test_two_guests_on_one_address_raise_one_critical_line_naming_both(self, world):
        """The incident, configured: running or not, both claim 192.0.2.10."""
        repo, pve, svc = world
        pve.add(101, "web-01", "running", qemu_static("192.0.2.10", "BC:24:11:00:00:01"))
        pve.add(102, "db-01", "stopped", qemu_static("192.0.2.10", "BC:24:11:00:00:02"))
        pve.add(103, "quiet-03", "running", qemu_static("192.0.2.11", "BC:24:11:00:00:03"))
        await _sync(svc)

        summary = await build_summary(repo)
        conflicts = summary["addresses"]["conflicts"]
        assert len(conflicts) == 1, conflicts
        line = conflicts[0]
        assert line["severity"] == "critical"
        assert line["address"] == "192.0.2.10"
        assert "web-01 (VM 101, running)" in line["summary"]
        assert "db-01 (VM 102, stopped)" in line["summary"]
        assert "quiet-03" not in line["summary"]
        assert "takes the address over" in line["remedy"]
        web, db = await _host(repo, "web-01"), await _host(repo, "db-01")
        assert sorted(line["host_ids"]) == sorted([web["id"], db["id"]])

    async def test_a_container_and_a_vm_clash_too(self, world):
        repo, pve, svc = world
        pve.add(101, "web-01", "running", qemu_static("192.0.2.10", "BC:24:11:00:00:01"))
        pve.add(
            201,
            "dns-ct",
            "running",
            {"net0": f"name=eth0,bridge={LAN},hwaddr=BC:24:11:00:00:09,ip=192.0.2.10/24"},
            guest_type="lxc",
        )
        await _sync(svc)
        (conflict,) = (await build_summary(repo))["addresses"]["conflicts"]
        assert "dns-ct (CT 201, running)" in conflict["summary"]

    async def test_a_guest_on_the_nodes_own_address_clashes_with_the_node(self, world):
        repo, pve, svc = world
        pve.add(101, "bad-copy", "stopped", qemu_static("192.0.2.2", "BC:24:11:00:00:01"))
        await _sync(svc)
        (conflict,) = (await build_summary(repo))["addresses"]["conflicts"]
        assert "pve1 (Proxmox node, running)" in conflict["summary"]
        assert "bad-copy (VM 101, stopped)" in conflict["summary"]

    async def test_a_manual_device_on_a_guests_address_clashes(self, world):
        """The VPN-box shape: a LAN device typed in by hand, on a guest's address."""
        repo, pve, svc = world
        pve.add(101, "llm", "running", qemu_static("192.0.2.10", "BC:24:11:00:00:01"))
        await _sync(svc)
        device = await repo.create_host(
            hostname="vpn-box",
            host_type="baremetal",
            ip_address="192.0.2.10",
            source="manual",
            ip_source="user",
        )
        await repo.sync_operator_address(device)
        (conflict,) = (await build_summary(repo))["addresses"]["conflicts"]
        assert "vpn-box (device)" in conflict["summary"]
        assert "llm (VM 101, running)" in conflict["summary"]

        # Forgetting the device takes its claim with it.
        await repo.delete_host(device)
        assert (await build_summary(repo))["addresses"]["conflicts"] == []


class TestWhatMustNotClash:
    async def test_the_same_address_on_another_bridge_is_another_network(self, world):
        """The guest vnet and the operator LAN can both have a .10."""
        repo, pve, svc = world
        pve.add(101, "lan-10", "running", qemu_static("192.0.2.10", "BC:24:11:00:00:01"))
        pve.add(
            102, "vnet-10", "running", qemu_static("192.0.2.10", "BC:24:11:00:00:02", "innkeep")
        )
        pve.add(103, "vlan-10", "running", qemu_static("192.0.2.10", "BC:24:11:00:00:03", LAN, 20))
        await _sync(svc)
        assert (await build_summary(repo))["addresses"]["conflicts"] == []

    async def test_a_gone_guest_holds_nothing(self, world):
        """Dev carried four destroyed guests on one address. None of them clash."""
        repo, pve, svc = world
        pve.add(101, "old-101", "running", qemu_static("192.0.2.10", "BC:24:11:00:00:01"))
        await _sync(svc)
        del pve.guests[101]
        pve.add(101, "new-101", "stopped", qemu_static("192.0.2.10", "BC:24:11:00:00:02"))
        await _sync(svc)
        old = await _host(repo, "old-101")
        assert old["absent_since"], "the old occupant of VMID 101 is retired"
        assert (await build_summary(repo))["addresses"]["conflicts"] == []

    async def test_a_template_and_dhcp_claim_nothing(self, world):
        repo, pve, svc = world
        pve.add(101, "web-01", "running", qemu_static("192.0.2.10", "BC:24:11:00:00:01"))
        pve.add(
            9000, "tmpl", "stopped", qemu_static("192.0.2.10", "BC:24:11:00:00:02"), template=True
        )
        pve.add(
            102, "dhcp-guest", "running", {"net0": f"virtio,bridge={LAN}", "ipconfig0": "ip=dhcp"}
        )
        await _sync(svc)
        assert (await build_summary(repo))["addresses"]["conflicts"] == []
        assert await repo.list_host_addresses((await _host(repo, "tmpl"))["id"]) == []

    async def test_a_config_read_that_fails_keeps_the_last_known_addresses(self, world):
        repo, pve, svc = world
        pve.add(101, "web-01", "running", qemu_static("192.0.2.10", "BC:24:11:00:00:01"))
        await _sync(svc)
        pve.fail_config.add(101)
        await svc.refresh_inventory()
        rows = await repo.list_host_addresses((await _host(repo, "web-01"))["id"])
        assert [r["address"] for r in rows] == ["192.0.2.10"]


class TestStartPrecondition:
    async def test_a_stopped_guest_whose_address_a_running_guest_holds_names_the_holder(
        self, world
    ):
        """THE incident: Start would take a running guest's address over."""
        repo, pve, svc = world
        pve.add(101, "holder", "running", qemu_static("192.0.2.10", "BC:24:11:00:00:01"))
        pve.add(102, "sleeper", "stopped", qemu_static("192.0.2.10", "BC:24:11:00:00:02"))
        pve.add(103, "bystander", "running", qemu_static("192.0.2.11", "BC:24:11:00:00:03"))
        await _sync(svc)

        holders = await svc.address_conflicts_for_start(await _host(repo, "sleeper"))
        assert [(h.hostname, h.proxmox_id, h.pve_status, h.address) for h in holders] == [
            ("holder", 101, "running", "192.0.2.10")
        ]
        assert holders[0].host_id == (await _host(repo, "holder"))["id"]
        # By id works too - what a route handler has in hand.
        by_id = await svc.address_conflicts_for_start((await _host(repo, "sleeper"))["id"])
        assert [h.hostname for h in by_id] == ["holder"]

    async def test_a_holder_started_after_the_last_sync_is_still_found(self, world):
        """The table is up to a cycle old; the guard reads the cluster live."""
        repo, pve, svc = world
        pve.add(101, "holder", "stopped", qemu_static("192.0.2.10", "BC:24:11:00:00:01"))
        pve.add(102, "sleeper", "stopped", qemu_static("192.0.2.10", "BC:24:11:00:00:02"))
        await _sync(svc)
        pve.guests[101]["status"] = "running"  # started in the PVE UI a minute ago
        holders = await svc.address_conflicts_for_start(await _host(repo, "sleeper"))
        assert [h.hostname for h in holders] == ["holder"]

    async def test_a_stopped_holder_does_not_block_a_start(self, world):
        """Two stopped guests on one address: a config fault (the attention line
        says so), but starting one hijacks nothing right now."""
        repo, pve, svc = world
        pve.add(101, "other", "stopped", qemu_static("192.0.2.10", "BC:24:11:00:00:01"))
        pve.add(102, "sleeper", "stopped", qemu_static("192.0.2.10", "BC:24:11:00:00:02"))
        await _sync(svc)
        assert await svc.address_conflicts_for_start(await _host(repo, "sleeper")) == []

    async def test_a_running_guest_holding_it_by_dhcp_is_named_from_its_agent(self, world):
        """A running DHCP guest leased the address the stopped guest is configured
        with. No config says so; its guest agent does."""
        repo, pve, svc = world
        pve.add(
            101,
            "leaser",
            "running",
            {"net0": f"virtio=BC:24:11:00:00:01,bridge={LAN}", "ipconfig0": "ip=dhcp"},
            agent=[
                {"name": "lo", "ip-addresses": [{"ip-address": "127.0.0.1", "prefix": 8}]},
                {
                    "name": "eth0",
                    "hardware-address": "bc:24:11:00:00:01",
                    "ip-addresses": [{"ip-address": "192.0.2.10", "prefix": 24}],
                },
                {
                    "name": "docker0",
                    "hardware-address": "02:42:00:00:00:01",
                    "ip-addresses": [{"ip-address": "172.17.0.1", "prefix": 16}],
                },
            ],
        )
        pve.add(102, "sleeper", "stopped", qemu_static("192.0.2.10", "BC:24:11:00:00:02"))
        await _sync(svc)

        holders = await svc.address_conflicts_for_start(await _host(repo, "sleeper"))
        assert [(h.hostname, h.source) for h in holders] == [("leaser", "guest-agent")]
        # And the standing warning says so too, as an OBSERVED clash.
        (conflict,) = (await build_summary(repo))["addresses"]["conflicts"]
        assert conflict["kind"] == "observed"
        assert "configured on sleeper (VM 102, stopped)" in conflict["summary"]
        assert "held by leaser (VM 101, running)" in conflict["summary"]
        # docker0 exists only inside the machine: every docker host has a
        # 172.17.0.1, so it is not recorded as an address on the LAN at all.
        leaser = await repo.list_host_addresses((await _host(repo, "leaser"))["id"])
        assert [(r["address"], r["source"], r["bridge"], r["iface"]) for r in leaser] == [
            ("192.0.2.10", "guest-agent", LAN, "eth0")
        ]

    async def test_a_reboot_applying_a_pending_ipconfig_is_checked_against_it(self, world):
        repo, pve, svc = world
        pve.add(101, "holder", "running", qemu_static("192.0.2.10", "BC:24:11:00:00:01"))
        pve.add(
            102,
            "mover",
            "running",
            qemu_static("192.0.2.20", "BC:24:11:00:00:02"),
            pending={"ipconfig0": "ip=192.0.2.10/24,gw=192.0.2.1"},
        )
        await _sync(svc)
        holders = await svc.address_conflicts_for_start(await _host(repo, "mover"))
        assert [h.hostname for h in holders] == ["holder"]

    async def test_a_scan_that_cannot_complete_refuses_rather_than_answers(self, world):
        repo, pve, svc = world
        pve.add(101, "unreadable", "running", qemu_static("192.0.2.10", "BC:24:11:00:00:01"))
        pve.add(102, "sleeper", "stopped", qemu_static("192.0.2.10", "BC:24:11:00:00:02"))
        await _sync(svc)
        pve.fail_config.add(101)
        with pytest.raises(AddressCheckError, match="101"):
            await svc.address_conflicts_for_start(await _host(repo, "sleeper"))

    async def test_a_host_that_is_not_a_guest_cannot_be_checked(self, world):
        repo, _pve, svc = world
        device = await repo.create_host(hostname="nas", host_type="baremetal", source="manual")
        with pytest.raises(AddressCheckError, match="not a Proxmox guest"):
            await svc.address_conflicts_for_start(device)


class TestTheActiveProbeSeam:
    async def test_a_mac_that_is_not_the_configured_guests_is_named(self, world):
        """No probe ships yet; the comparison it feeds is real and tested here."""
        repo, pve, svc = world
        pve.add(101, "llm", "running", qemu_static("192.0.2.10", "BC:24:11:00:00:01"))
        await _sync(svc)
        device = await repo.create_host(hostname="vpn-box", host_type="baremetal", source="manual")
        # The device's MAC is known (e.g. from its own observed row).
        await repo.replace_host_addresses(
            device,
            ("guest-agent",),
            [NicAddress(address="198.51.100.7", mac="02:00:00:00:00:AA", source="guest-agent")],
        )

        class FakeProbe:
            async def answers(self, addresses):
                assert "192.0.2.10" in addresses
                return [NeighbourAnswer(address="192.0.2.10", mac="02:00:00:00:00:aa")]

        assert await record_probe_answers(repo, FakeProbe()) == 1
        (conflict,) = find_conflicts(await repo.list_address_rows())
        assert conflict.kind == "observed"
        assert "configured on llm (VM 101, running)" in conflict.summary
        assert "vpn-box (device, MAC 02:00:00:00:00:AA)" in conflict.summary

    async def test_the_guests_own_mac_answering_is_no_clash(self, world):
        repo, pve, svc = world
        pve.add(101, "llm", "running", qemu_static("192.0.2.10", "BC:24:11:00:00:01"))
        await _sync(svc)

        class OwnMac:
            async def answers(self, addresses):
                return [NeighbourAnswer(address="192.0.2.10", mac="BC:24:11:00:00:01")]

        await record_probe_answers(repo, OwnMac())
        assert find_conflicts(await repo.list_address_rows()) == []

    async def test_no_probe_means_no_answer_not_an_empty_one(self, world):
        repo, pve, svc = world
        pve.add(101, "llm", "running", qemu_static("192.0.2.10", "BC:24:11:00:00:01"))
        await _sync(svc)
        from homepilot.inventory.addresses import NO_PROBE

        assert await record_probe_answers(repo, NO_PROBE) is None


class TestTheReconcilerJournalsEachClashOnce:
    async def test_raised_once_and_resolved_once(self, world):
        repo, pve, svc = world
        pve.add(101, "web-01", "running", qemu_static("192.0.2.10", "BC:24:11:00:00:01"))
        pve.add(102, "db-01", "stopped", qemu_static("192.0.2.10", "BC:24:11:00:00:02"))
        reconciler = InventoryReconciler(svc, repo)

        for _ in range(3):
            result = await reconciler.run()
            assert result.success, result.details
        assert result.details["address_conflicts"] == 1

        async def actions() -> list[tuple[str, str | None]]:
            rows = await repo.db.fetchall(
                "SELECT action, target_host FROM audit_log "
                "WHERE action LIKE 'address_conflict%' ORDER BY id"
            )
            return [(r["action"], r["target_host"]) for r in rows]

        assert sorted(await actions()) == [
            ("address_conflict", "db-01"),
            ("address_conflict", "web-01"),
        ], "a standing clash is journaled when it appears, not every cycle"

        pve.guests[102]["config"]["ipconfig0"] = "ip=192.0.2.12/24,gw=192.0.2.1"
        await reconciler.run()
        await reconciler.run()
        assert [a for a, _ in await actions()].count("address_conflict_resolved") == 1


class TestTheReadApi:
    async def test_hosts_with_address_is_the_reverse_lookup(self, world):
        """For lane B1: which host holds the control plane's own address?"""
        repo, pve, svc = world
        pve.add(100, "control", "running", qemu_static("192.0.2.100", "BC:24:11:00:00:64"))
        await _sync(svc)
        rows = await repo.hosts_with_address("192.0.2.100")
        assert [(r["hostname"], r["proxmox_id"], r["source"], r["mac"]) for r in rows] == [
            ("control", 100, "qemu-ipconfig", "BC:24:11:00:00:64")
        ]
        assert await repo.hosts_with_address("192.0.2.200") == []

    async def test_get_host_detail_carries_the_addresses_and_clashes(self, world):
        """What get_host (REST and MCP) says: the Network tab's answer, verbatim."""
        repo, pve, svc = world
        pve.add(100, "control", "running", qemu_static("192.0.2.100", "BC:24:11:00:00:64"))
        await _sync(svc)
        control_id = (await _host(repo, "control"))["id"]
        detail = await svc.get_host_detail(control_id)
        assert detail is not None
        assert [a["address"] for a in detail["addresses"]] == ["192.0.2.100"]
        assert detail["address_conflicts"] == []

        pve.add(105, "copycat", "stopped", qemu_static("192.0.2.100", "BC:24:11:00:00:69"))
        await _sync(svc)
        detail = await svc.get_host_detail(control_id)
        assert detail is not None
        (conflict,) = detail["address_conflicts"]
        assert "copycat (VM 105, stopped)" in conflict["summary"]
        assert detail["address_conflicts"] == (await svc.host_network(control_id))["conflicts"]


@pytest.fixture
async def http(world):
    from homepilot.inventory.router import router as inventory_router

    repo, pve, svc = world
    app = FastAPI()
    app.include_router(inventory_router, prefix="/inventory")
    app.state.repo = repo
    app.state.inventory_service = svc
    app.dependency_overrides[require_token] = lambda: {"scope": "*", "role": "admin"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, repo, pve, svc


class TestTheOperatorSeesItOnHostsAndTheNetworkTab:
    async def test_hosts_list_carries_addresses_and_marks_the_clash(self, http):
        client, _repo, pve, svc = http
        pve.add(101, "web-01", "running", qemu_static("192.0.2.10", "BC:24:11:00:00:01"))
        pve.add(102, "db-01", "stopped", qemu_static("192.0.2.10", "BC:24:11:00:00:02"))
        pve.add(103, "quiet-03", "running", qemu_static("192.0.2.11", "BC:24:11:00:00:03"))
        await _sync(svc)

        resp = await client.get("/inventory")
        assert resp.status_code == 200, resp.text
        by_name = {h["hostname"]: h["addresses"] for h in resp.json()["items"]}
        assert by_name["web-01"] == [
            {"address": "192.0.2.10", "sources": ["qemu-ipconfig"], "conflict": True}
        ]
        assert by_name["quiet-03"] == [
            {"address": "192.0.2.11", "sources": ["qemu-ipconfig"], "conflict": False}
        ]

    async def test_network_tab_lists_addresses_with_provenance_and_the_clash(self, http):
        client, repo, pve, svc = http
        pve.add(101, "web-01", "running", qemu_static("192.0.2.10", "BC:24:11:00:00:01"))
        pve.add(102, "db-01", "stopped", qemu_static("192.0.2.10", "BC:24:11:00:00:02"))
        await _sync(svc)
        web = await _host(repo, "web-01")

        resp = await client.get(f"/inventory/{web['id']}/network")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        (address,) = body["addresses"]
        assert address["address"] == "192.0.2.10"
        assert address["source"] == "qemu-ipconfig"
        assert address["iface"] == "net0"
        assert address["bridge"] == LAN
        assert address["mac"] == "BC:24:11:00:00:01"
        assert address["observed_at"]
        (conflict,) = body["conflicts"]
        assert "db-01" in conflict["summary"]

        assert (await client.get("/inventory/nope/network")).status_code == 404

    async def test_a_device_typed_with_a_guests_address_clashes_at_once(self, http):
        """The review's acceptance check: add a device on a guest's address and
        the clash is there on the next read, not the next sync."""
        client, repo, pve, svc = http
        pve.add(101, "fence-proof-a", "running", qemu_static("192.0.2.10", "BC:24:11:00:00:01"))
        await _sync(svc)
        resp = await client.post(
            "/inventory", json={"hostname": "vpn-box", "ip_address": "192.0.2.10"}
        )
        assert resp.status_code == 201, resp.text
        (conflict,) = (await build_summary(repo))["addresses"]["conflicts"]
        assert "fence-proof-a" in conflict["summary"] and "vpn-box" in conflict["summary"]

        # Re-addressing it in the UI clears the clash just as promptly.
        device = await _host(repo, "vpn-box")
        resp = await client.patch(f"/inventory/{device['id']}", json={"ip_address": "192.0.2.77"})
        assert resp.status_code == 200, resp.text
        assert (await build_summary(repo))["addresses"]["conflicts"] == []


class TestParsing:
    async def test_qemu_and_lxc_lines(self):
        qemu = parse_guest_config(
            {
                "net0": "virtio=bc:24:11:00:00:01,bridge=vmbr0,tag=20,firewall=1",
                "net1": "e1000=BC:24:11:00:00:02,bridge=vmbr1",
                "ipconfig0": "ip=192.0.2.10/24,gw=192.0.2.1,ip6=2001:db8::10/64",
                "ipconfig1": "ip=dhcp",
            },
            "qemu",
        )
        assert [(a.iface, a.address, a.prefixlen, a.mac, a.bridge, a.vlan) for a in qemu] == [
            ("net0", "192.0.2.10", 24, "BC:24:11:00:00:01", "vmbr0", 20),
            ("net0", "2001:db8::10", 64, "BC:24:11:00:00:01", "vmbr0", 20),
        ]
        lxc = parse_guest_config(
            {"net0": "name=eth0,bridge=vmbr0,hwaddr=BC:24:11:75:77:E6,ip=dhcp,type=veth"}, "lxc"
        )
        assert lxc == []

    async def test_agent_interfaces_skip_loopback_and_link_local(self):
        out = parse_agent_interfaces(
            {
                "data": {
                    "result": [
                        {
                            "name": "eth0",
                            "hardware-address": "bc:24:11:00:00:01",
                            "ip-addresses": [
                                {"ip-address": "192.0.2.10", "prefix": 24},
                                {"ip-address": "fe80::1", "prefix": 64},
                            ],
                        }
                    ]
                }
            },
            {"net0": "virtio=BC:24:11:00:00:01,bridge=vmbr0"},
        )
        assert [(a.address, a.bridge) for a in out] == [("192.0.2.10", "vmbr0")]


def _now_minus(minutes: int) -> str:
    return (datetime.now(UTC) - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


class TestObservationsAge:
    async def test_a_stale_agent_observation_raises_nothing(self):
        rows = [
            {
                "host_id": "a",
                "address": "192.0.2.10",
                "source": "qemu-ipconfig",
                "bridge": LAN,
                "vlan": None,
                "hostname": "a",
                "pve_status": "stopped",
                "observed_at": _now_minus(1),
            },
            {
                "host_id": "b",
                "address": "192.0.2.10",
                "source": "guest-agent",
                "bridge": LAN,
                "vlan": None,
                "hostname": "b",
                "pve_status": "running",
                "observed_at": _now_minus(60),
            },
        ]
        assert find_conflicts(rows) == []
        rows[1]["observed_at"] = _now_minus(1)
        assert [c.kind for c in find_conflicts(rows)] == ["observed"]


async def test_summary_is_json_safe():
    rows = [
        {
            "host_id": "a",
            "address": "192.0.2.10",
            "source": "qemu-ipconfig",
            "bridge": LAN,
            "vlan": None,
            "hostname": "a",
            "host_type": "qemu",
            "proxmox_id": 1,
            "pve_status": "running",
            "observed_at": _now_minus(1),
        },
        {
            "host_id": "b",
            "address": "192.0.2.10",
            "source": "manual",
            "bridge": None,
            "vlan": None,
            "hostname": "b",
            "host_type": "baremetal",
            "proxmox_id": None,
            "pve_status": None,
            "observed_at": _now_minus(1),
        },
    ]
    json.dumps([c.as_dict() for c in find_conflicts(rows)])
