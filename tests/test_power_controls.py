"""THE GATE for lane B3 (#781): operator power controls, their guards, their honesty.

What the operator sees is asserted, not what a function returned:

* Start / Shutdown / Reboot / Stop for a VM AND a container end with the guest
  in the state the action promised on the (fake) cluster, an audit row naming
  the actor, the guest, the PVE task and the outcome;
* a guest that ignores the shutdown request ends as ``timed_out`` - still
  running, nothing forced, Stop offered - never as a success;
* a Reboot applies the pending cloud-init ``ipconfig`` (``/pending`` empty after);
* every guard refuses with its reason and SENDS NOTHING: a running guest holding
  the address (named), a running guest holding the passthrough disk (named),
  a different machine at the VMID, a gone guest, a locked guest, the self guest
  or a protected one without its name typed back;
* an assistant's ``power_guest`` yields a proposal and never a powered guest;
  only the owner's approve-and-apply runs it, through the same guards.

The cluster is the ONE fake, at the HTTP boundary: ``FakeCluster`` subclasses
the real ``ProxmoxClient`` and answers its ``call``, so the real power, status,
pending and task-wait code paths run. It models PVE where it matters: a plain
``GET .../config`` answers with PENDING changes applied, ``current=1`` with what
is attached now; a reboot ends its task once the guest has stopped and the
guest comes back afterwards; a guest may ignore ACPI.

Teeth (each proven by reverting the named code and watching the named test fail;
evidence in the PR):
  * drop the ``_refuse_address_clash`` call in ``PowerService._guard`` ->
    ``test_start_is_refused_naming_the_running_holder_of_its_address`` fails;
  * drop the ``_refuse_shared_disk`` call ->
    ``test_start_is_refused_naming_the_running_guest_holding_its_disk`` fails;
  * read the other guests without ``current=True`` in ``_refuse_shared_disk`` ->
    ``test_a_disk_queued_for_detach_is_still_attached_until_the_holder_reboots`` fails;
  * drop ``{"current": 1}`` for running holders in ``AddressGuard.holders`` ->
    ``test_a_running_holder_is_judged_by_the_address_it_booted_with`` fails;
  * report a shutdown ``ok`` without judging the guest's state afterwards ->
    ``test_a_guest_that_ignores_shutdown_is_timed_out_not_stopped`` fails;
  * drop the typed-confirmation check -> ``test_the_self_guest_needs_its_name_typed``
    fails;
  * drop the ``same_guest`` check -> ``test_a_different_machine_at_the_vmid_is_refused``
    fails;
  * make ``power_guest`` execute instead of propose ->
    ``test_power_guest_proposes_and_powers_nothing`` fails;
  * drop the ``via_artifact`` refusal ->
    ``test_the_apply_refuses_a_guest_marked_self_since_the_proposal`` fails;
  * let a non-operator change ``protected`` ->
    ``test_only_an_operator_may_change_protected`` fails;
  * drop the ``requires_snapshot`` refusal in ``validate_guest_power_spec`` ->
    ``test_a_power_artifact_cannot_ask_for_a_snapshot_or_a_replay`` fails.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from homepilot.adapters.proxmox import ProxmoxClient, ProxmoxError
from homepilot.artifacts.lifecycle import ArtifactLifecycle
from homepilot.artifacts.models import LifecycleError
from homepilot.artifacts.store import ArtifactStore
from homepilot.auth.deps import require_token
from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.db.repository import Repository
from homepilot.executor.orchestrator import ArtifactExecutor
from homepilot.inventory.power import PowerRefusedError, PowerService
from homepilot.inventory.service import InventoryService
from homepilot.mcp.server import _handle_tool
from homepilot.tasks.repository import TaskRepository
from homepilot.tasks.runner import TaskRunner

pytestmark = pytest.mark.asyncio

NODE = "pve1"
OTHER_NODE = "pve2"
LAN = "vmbr0"
DISK = "/dev/disk/by-id/ata-EXAMPLE_SSD_0001"

ADMIN = {"scope": "*", "role": "admin", "label": "owner", "prefix": "hp_owner000001"}
ASSISTANT = {"scope": "read,write", "label": "assistant", "prefix": "hp_asst0000001"}


def qemu_net(address: str, mac: str) -> dict[str, Any]:
    return {"net0": f"virtio={mac},bridge={LAN}", "ipconfig0": f"ip={address}/24,gw=192.0.2.1"}


def lxc_net(address: str, mac: str) -> dict[str, Any]:
    return {"net0": f"name=eth0,bridge={LAN},hwaddr={mac},ip={address}/24"}


class FakeCluster(ProxmoxClient):
    """A Proxmox that answers ``ProxmoxClient.call`` from state; nothing leaves the process.

    ``guests[vmid] = {type, node, name, status, config, pending, lock, ignore_acpi,
    start_fails, hang, comeback_reads}``. ``config`` is what is attached NOW;
    ``pending`` maps key -> new value (``None`` = queued delete).
    """

    def __init__(self) -> None:
        super().__init__("https://pve.example:8006", "root@pam!hp=00000000")
        self.guests: dict[int, dict[str, Any]] = {}
        self.posts: list[tuple[str, dict[str, Any] | None]] = []
        self.tasks: dict[str, dict[str, Any]] = {}
        self.fail_status = False
        self._n = 0

    def add(
        self,
        vmid: int,
        name: str,
        status: str,
        config: dict[str, Any],
        *,
        guest_type: str = "qemu",
        node: str = NODE,
        pending: dict[str, Any] | None = None,
        **behaviour: Any,
    ) -> None:
        name_key = "hostname" if guest_type == "lxc" else "name"
        self.guests[vmid] = {
            "type": guest_type,
            "node": node,
            "name": name,
            "status": status,
            "config": {name_key: name, **config},
            "pending": dict(pending or {}),
            "lock": None,
            "ignore_acpi": False,
            "start_fails": False,
            "hang": False,
            "comeback_reads": 0,
            **behaviour,
        }

    @property
    def power_posts(self) -> list[str]:
        return [p for p, _ in self.posts if "/status/" in p]

    # ── what PVE would do ────────────────────────────────────────────────────

    def _boot_config(self, g: dict[str, Any]) -> dict[str, Any]:
        out = dict(g["config"])
        for key, value in g["pending"].items():
            if value is None:
                out.pop(key, None)
            else:
                out[key] = value
        return out

    def _apply_pending(self, g: dict[str, Any]) -> None:
        g["config"] = self._boot_config(g)
        g["pending"] = {}

    def _task(self, node: str, exitstatus: str | None) -> str:
        self._n += 1
        upid = f"UPID:{node}:0000{self._n:04X}:00000000:00000000:qmpower:{self._n}:root@pam:"
        self.tasks[upid] = {"status": "running" if exitstatus is None else "stopped"}
        if exitstatus is not None:
            self.tasks[upid]["exitstatus"] = exitstatus
        return upid

    def _power(self, g: dict[str, Any], action: str, body: dict[str, Any]) -> str:
        node = g["node"]
        if g["hang"]:
            return self._task(node, None)
        if action == "start":
            if g["start_fails"]:
                return self._task(node, "start failed: QEMU exited with code 1")
            self._apply_pending(g)
            g["status"] = "running"
            return self._task(node, "OK")
        if action == "stop":
            g["status"] = "stopped"
            return self._task(node, "OK")
        assert body.get("timeout"), "a shutdown/reboot is always sent with its timeout"
        if action == "shutdown":
            assert body.get("forceStop") == 0, "a shutdown must never be sent with forceStop"
        if g["ignore_acpi"]:
            return self._task(node, "VM quit/powerdown failed - got timeout")
        if action == "shutdown":
            g["status"] = "stopped"
            return self._task(node, "OK")
        # reboot: the task ends once the guest has stopped; it comes back after.
        self._apply_pending(g)
        if g["comeback_reads"]:
            g["status"] = "stopped"
        return self._task(node, "OK")

    async def call(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        path = "/" + path.lstrip("/")
        if method != "GET":
            self.posts.append((path, body))
            parts = path.strip("/").split("/")
            if len(parts) == 6 and parts[4] == "status":
                g = self.guests[int(parts[3])]
                return {"data": self._power(g, parts[5], body or {})}
            raise AssertionError(f"unexpected write {method} {path}")
        return {"data": self._read(path, query or {})}

    def _read(self, path: str, query: dict[str, Any]) -> Any:
        if path == "/nodes":
            return [{"node": NODE, "status": "online"}, {"node": OTHER_NODE, "status": "online"}]
        if path.endswith("/network") and path.count("/") == 3:
            return [
                {"iface": LAN, "type": "bridge", "address": "192.0.2.2", "cidr": "192.0.2.2/24"}
            ]
        if path == "/cluster/resources":
            return [
                {
                    "vmid": vmid,
                    "node": g["node"],
                    "type": g["type"],
                    "name": g["name"],
                    "status": g["status"],
                    "template": 0,
                }
                for vmid, g in sorted(self.guests.items())
            ]
        parts = path.strip("/").split("/")
        if len(parts) == 5 and parts[2] == "tasks" and parts[4] == "status":
            return dict(self.tasks[unquote(parts[3])])
        if len(parts) == 3 and parts[2] in ("qemu", "lxc"):
            return [
                {"vmid": vmid, "name": g["name"], "status": g["status"]}
                for vmid, g in sorted(self.guests.items())
                if g["type"] == parts[2] and g["node"] == parts[1]
            ]
        if len(parts) >= 5 and parts[2] in ("qemu", "lxc"):
            g = self.guests[int(parts[3])]
            tail = "/".join(parts[4:])
            if tail == "config":
                # PVE's trap: without current=1 the answer has pending applied.
                return dict(g["config"]) if query.get("current") else self._boot_config(g)
            if tail == "pending":
                rows: list[dict[str, Any]] = []
                for key, value in g["config"].items():
                    row: dict[str, Any] = {"key": key, "value": value}
                    if key in g["pending"]:
                        if g["pending"][key] is None:
                            row["delete"] = 1
                        else:
                            row["pending"] = g["pending"][key]
                    rows.append(row)
                for key, value in g["pending"].items():
                    if key not in g["config"] and value is not None:
                        rows.append({"key": key, "pending": value})
                return rows
            if tail == "status/current":
                if self.fail_status:
                    raise ProxmoxError("GET", path, 0, "connection refused")
                if g["status"] == "stopped" and g["comeback_reads"]:
                    g["comeback_reads"] -= 1
                    if g["comeback_reads"] == 0:
                        g["status"] = "running"
                out = {"status": g["status"], "name": g["name"], "vmid": int(parts[3])}
                if g["lock"]:
                    out["lock"] = g["lock"]
                return out
            if tail == "agent/network-get-interfaces":
                raise ProxmoxError("GET", path, 500, "QEMU guest agent is not running")
        raise AssertionError(f"unexpected read {path}")


@pytest.fixture
async def world(tmp_path: Path):
    db = Database(str(tmp_path / "homepilot.db"))
    await db.connect()
    await run_migrations(db)
    repo = Repository(db)
    pve = FakeCluster()
    svc = InventoryService(repo, pve)
    power = PowerService(repo, pve, poll_interval=0, reboot_comeback_s=5)
    try:
        yield repo, pve, svc, power
    finally:
        await db.close()


async def _sync(svc: InventoryService) -> None:
    result = await svc.refresh_inventory()
    assert result["complete"], result


async def _id(repo: Repository, name: str) -> str:
    row = await repo.get_host_by_hostname(name)
    assert row is not None, name
    return str(row["id"])


async def _audit(repo: Repository) -> list[dict[str, Any]]:
    rows = await repo.db.fetchall("SELECT * FROM audit_log ORDER BY id")
    return [dict(r) for r in rows]


# ── the actions: the guest ends where the action promised ────────────────────

GUEST_TYPES = [
    pytest.param("qemu", qemu_net, id="vm"),
    pytest.param("lxc", lxc_net, id="ct"),
]


class TestEachActionReachesItsState:
    @pytest.mark.parametrize(("guest_type", "net"), GUEST_TYPES)
    async def test_start_runs_the_guest_and_audits_actor_and_task(self, world, guest_type, net):
        repo, pve, svc, power = world
        pve.add(
            101, "web-01", "stopped", net("192.0.2.10", "BC:24:11:00:00:01"), guest_type=guest_type
        )
        await _sync(svc)

        result = await power.execute(
            await _id(repo, "web-01"), "start", actor="owner (hp_o)", source="api"
        )

        assert result.outcome == "ok", result.message
        assert pve.guests[101]["status"] == "running"
        assert result.upid and result.upid.startswith("UPID:")
        (row,) = [a for a in await _audit(repo) if a["action"] == "guest_start"]
        details = json.loads(row["details_json"])
        assert row["user_id"] == "owner (hp_o)"
        assert row["target_host"] == "web-01"
        assert details["upid"] == result.upid
        assert details["outcome"] == "ok"
        assert details["vmid"] == 101

    @pytest.mark.parametrize(("guest_type", "net"), GUEST_TYPES)
    async def test_shutdown_stops_the_guest_gracefully(self, world, guest_type, net):
        repo, pve, svc, power = world
        pve.add(
            101, "web-01", "running", net("192.0.2.10", "BC:24:11:00:00:01"), guest_type=guest_type
        )
        await _sync(svc)

        result = await power.execute(await _id(repo, "web-01"), "shutdown", actor="o", source="api")

        assert result.outcome == "ok", result.message
        assert pve.guests[101]["status"] == "stopped"
        assert pve.power_posts == [f"/nodes/{NODE}/{guest_type}/101/status/shutdown"]

    @pytest.mark.parametrize(("guest_type", "net"), GUEST_TYPES)
    async def test_a_guest_that_ignores_shutdown_is_timed_out_not_stopped(
        self, world, guest_type, net
    ):
        """The issue's acceptance: 'shutdown timed out - Stop?', never a false success."""
        repo, pve, svc, power = world
        pve.add(
            101,
            "stubborn",
            "running",
            net("192.0.2.10", "BC:24:11:00:00:01"),
            guest_type=guest_type,
            ignore_acpi=True,
        )
        await _sync(svc)

        result = await power.execute(
            await _id(repo, "stubborn"), "shutdown", actor="o", source="api", timeout_s=30
        )

        assert result.outcome == "timed_out"
        assert not result.ok
        assert result.next_actions == ["stop"]
        assert "did not shut down within 30s" in result.message
        assert "nothing was forced" in result.message
        # Nothing escalated behind the operator's back.
        assert pve.guests[101]["status"] == "running"
        assert pve.power_posts == [f"/nodes/{NODE}/{guest_type}/101/status/shutdown"]

    @pytest.mark.parametrize(("guest_type", "net"), GUEST_TYPES)
    async def test_stop_forces_the_guest_off(self, world, guest_type, net):
        repo, pve, svc, power = world
        pve.add(
            101, "web-01", "running", net("192.0.2.10", "BC:24:11:00:00:01"), guest_type=guest_type
        )
        await _sync(svc)

        result = await power.execute(await _id(repo, "web-01"), "stop", actor="o", source="api")

        assert result.outcome == "ok"
        assert pve.guests[101]["status"] == "stopped"

    @pytest.mark.parametrize(("guest_type", "net"), GUEST_TYPES)
    async def test_reboot_applies_the_pending_address(self, world, guest_type, net):
        """The issue's acceptance: after the reboot /pending is empty and the guest has it."""
        repo, pve, svc, power = world
        new = net("192.0.2.20", "BC:24:11:00:00:01")
        key = "ipconfig0" if guest_type == "qemu" else "net0"
        pve.add(
            101,
            "web-01",
            "running",
            net("192.0.2.10", "BC:24:11:00:00:01"),
            guest_type=guest_type,
            pending={key: new[key]},
        )
        await _sync(svc)
        hid = await _id(repo, "web-01")
        described = await power.describe(hid)
        assert [p["key"] for p in described["pending"]] == [key]

        result = await power.execute(hid, "reboot", actor="o", source="api")

        assert result.outcome == "ok", result.message
        assert result.pending_after == []
        assert pve.guests[101]["config"][key] == new[key]
        assert pve.guests[101]["status"] == "running"

    async def test_a_reboot_waits_for_the_guest_to_come_back(self, world):
        """PVE's reboot task ends when the guest has STOPPED; it is started after."""
        repo, pve, svc, power = world
        pve.add(
            101, "web-01", "running", qemu_net("192.0.2.10", "BC:24:11:00:00:01"), comeback_reads=3
        )
        await _sync(svc)

        result = await power.execute(await _id(repo, "web-01"), "reboot", actor="o", source="api")

        assert result.outcome == "ok", result.message
        assert result.status_after == "running"

    async def test_a_reboot_the_guest_ignores_is_timed_out_and_applies_nothing(self, world):
        repo, pve, svc, power = world
        pve.add(
            101,
            "web-01",
            "running",
            qemu_net("192.0.2.10", "BC:24:11:00:00:01"),
            ignore_acpi=True,
            pending={"ipconfig0": "ip=192.0.2.20/24,gw=192.0.2.1"},
        )
        await _sync(svc)

        result = await power.execute(await _id(repo, "web-01"), "reboot", actor="o", source="api")

        assert result.outcome == "timed_out"
        assert "pending changes are not applied" in result.message
        assert pve.guests[101]["pending"], "the pending change must still be pending"

    async def test_a_start_that_fails_is_failed(self, world):
        repo, pve, svc, power = world
        pve.add(
            101, "web-01", "stopped", qemu_net("192.0.2.10", "BC:24:11:00:00:01"), start_fails=True
        )
        await _sync(svc)

        result = await power.execute(await _id(repo, "web-01"), "start", actor="o", source="api")

        assert result.outcome == "failed"
        assert "QEMU exited" in result.message

    async def test_a_task_still_running_is_unknown_not_failed_or_ok(self, world):
        repo, pve, svc, _power = world
        power = PowerService(repo, pve, poll_interval=0, start_stop_wait_s=0)
        pve.add(101, "web-01", "stopped", qemu_net("192.0.2.10", "BC:24:11:00:00:01"), hang=True)
        await _sync(svc)

        result = await power.execute(await _id(repo, "web-01"), "start", actor="o", source="api")

        assert result.outcome == "unknown"
        assert "still running" in result.message


# ── the guards: refused with the reason, and nothing sent ────────────────────


class TestTheGuardsRefuseAndSendNothing:
    async def test_start_is_refused_naming_the_running_holder_of_its_address(self, world):
        """The 2026-11-07 incident: the start would take a running guest's address."""
        repo, pve, svc, power = world
        pve.add(101, "vpn-box", "running", qemu_net("192.0.2.10", "BC:24:11:00:00:01"))
        pve.add(102, "copycat", "stopped", qemu_net("192.0.2.10", "BC:24:11:00:00:02"))
        await _sync(svc)

        with pytest.raises(PowerRefusedError) as exc:
            await power.execute(await _id(repo, "copycat"), "start", actor="o", source="api")

        assert exc.value.code == "address_clash"
        assert "192.0.2.10 is held by vpn-box" in exc.value.message
        assert exc.value.details["holders"][0]["vmid"] == 101
        assert pve.power_posts == []
        assert pve.guests[102]["status"] == "stopped"
        refused = [a for a in await _audit(repo) if a["action"] == "guest_power_refused"]
        assert json.loads(refused[0]["details_json"])["code"] == "address_clash"

    async def test_a_reboot_applying_a_clashing_pending_ipconfig_is_refused(self, world):
        repo, pve, svc, power = world
        pve.add(101, "vpn-box", "running", qemu_net("192.0.2.10", "BC:24:11:00:00:01"))
        pve.add(
            102,
            "web-02",
            "running",
            qemu_net("192.0.2.11", "BC:24:11:00:00:02"),
            pending={"ipconfig0": "ip=192.0.2.10/24,gw=192.0.2.1"},
        )
        await _sync(svc)

        with pytest.raises(PowerRefusedError) as exc:
            await power.execute(await _id(repo, "web-02"), "reboot", actor="o", source="api")

        assert exc.value.code == "address_clash"
        assert "vpn-box" in exc.value.message
        assert pve.power_posts == []

    async def test_a_running_holder_is_judged_by_the_address_it_booted_with(self, world):
        """vpn-box HOLDS .10 now and has a change to .30 queued: .10 is still taken."""
        repo, pve, svc, power = world
        pve.add(
            101,
            "vpn-box",
            "running",
            qemu_net("192.0.2.10", "BC:24:11:00:00:01"),
            pending={"ipconfig0": "ip=192.0.2.30/24,gw=192.0.2.1"},
        )
        pve.add(102, "copycat", "stopped", qemu_net("192.0.2.10", "BC:24:11:00:00:02"))
        await _sync(svc)

        with pytest.raises(PowerRefusedError) as exc:
            await power.execute(await _id(repo, "copycat"), "start", actor="o", source="api")

        assert exc.value.code == "address_clash"
        assert "vpn-box" in exc.value.message
        assert pve.power_posts == []

    async def test_start_is_refused_naming_the_running_guest_holding_its_disk(self, world):
        """The stopped VM that still referenced a running VM's SSD."""
        repo, pve, svc, power = world
        pve.add(
            101,
            "nas",
            "running",
            {**qemu_net("192.0.2.10", "BC:24:11:00:00:01"), "scsi1": f"{DISK},size=1T"},
        )
        pve.add(
            102,
            "old-nas",
            "stopped",
            {**qemu_net("192.0.2.11", "BC:24:11:00:00:02"), "sata2": f"{DISK},backup=0"},
        )
        await _sync(svc)

        with pytest.raises(PowerRefusedError) as exc:
            await power.execute(await _id(repo, "old-nas"), "start", actor="o", source="api")

        assert exc.value.code == "disk_shared"
        assert DISK in exc.value.message
        assert "nas (VMID 101)" in exc.value.message
        assert "sata2" in exc.value.message and "scsi1" in exc.value.message
        assert pve.power_posts == []

    async def test_a_disk_queued_for_detach_is_still_attached_until_the_holder_reboots(self, world):
        """PVE's plain GET would hide the disk (pending delete applied); current=1 shows it."""
        repo, pve, svc, power = world
        pve.add(
            101,
            "nas",
            "running",
            {**qemu_net("192.0.2.10", "BC:24:11:00:00:01"), "scsi1": DISK},
            pending={"scsi1": None},
        )
        pve.add(
            102,
            "old-nas",
            "stopped",
            {**qemu_net("192.0.2.11", "BC:24:11:00:00:02"), "scsi1": DISK},
        )
        await _sync(svc)

        with pytest.raises(PowerRefusedError) as exc:
            await power.execute(await _id(repo, "old-nas"), "start", actor="o", source="api")
        assert exc.value.code == "disk_shared"

    async def test_a_disk_only_queued_on_the_other_guest_or_on_another_node_is_free(self, world):
        repo, pve, svc, power = world
        # Queued for attach at nas's next boot - not attached now.
        pve.add(
            101,
            "nas",
            "running",
            qemu_net("192.0.2.10", "BC:24:11:00:00:01"),
            pending={"scsi1": DISK},
        )
        # Same /dev path on another node is another physical disk.
        pve.add(
            103,
            "far",
            "running",
            {**qemu_net("192.0.2.12", "BC:24:11:00:00:03"), "scsi1": DISK},
            node=OTHER_NODE,
        )
        pve.add(
            102,
            "old-nas",
            "stopped",
            {**qemu_net("192.0.2.11", "BC:24:11:00:00:02"), "scsi1": DISK},
        )
        await _sync(svc)

        result = await power.execute(await _id(repo, "old-nas"), "start", actor="o", source="api")
        assert result.outcome == "ok"

    async def test_a_different_machine_at_the_vmid_is_refused(self, world):
        repo, pve, svc, power = world
        pve.add(101, "web-01", "running", qemu_net("192.0.2.10", "BC:24:11:00:00:01"))
        await _sync(svc)
        hid = await _id(repo, "web-01")
        # PVE reused 101 for another machine since the last sync.
        pve.guests[101]["name"] = "someone-else"

        with pytest.raises(PowerRefusedError) as exc:
            await power.execute(hid, "stop", actor="o", source="api")

        assert exc.value.code == "name_mismatch"
        assert "someone-else" in exc.value.message
        assert pve.power_posts == []

    async def test_a_gone_guest_is_refused(self, world):
        repo, pve, svc, power = world
        pve.add(101, "web-01", "stopped", qemu_net("192.0.2.10", "BC:24:11:00:00:01"))
        await _sync(svc)
        hid = await _id(repo, "web-01")
        await repo.retire_host(hid)

        with pytest.raises(PowerRefusedError) as exc:
            await power.execute(hid, "start", actor="o", source="api")
        assert exc.value.code == "gone"
        assert pve.power_posts == []

    async def test_a_locked_guest_and_an_unreadable_one_are_refused(self, world):
        repo, pve, svc, power = world
        pve.add(101, "web-01", "running", qemu_net("192.0.2.10", "BC:24:11:00:00:01"))
        await _sync(svc)
        hid = await _id(repo, "web-01")
        pve.guests[101]["lock"] = "backup"
        with pytest.raises(PowerRefusedError) as exc:
            await power.execute(hid, "shutdown", actor="o", source="api")
        assert exc.value.code == "locked"
        pve.guests[101]["lock"] = None
        pve.fail_status = True
        with pytest.raises(PowerRefusedError) as exc:
            await power.execute(hid, "shutdown", actor="o", source="api")
        assert exc.value.code == "unreadable"
        assert pve.power_posts == []

    async def test_an_action_the_state_does_not_allow_is_refused(self, world):
        repo, pve, svc, power = world
        pve.add(101, "web-01", "running", qemu_net("192.0.2.10", "BC:24:11:00:00:01"))
        await _sync(svc)
        with pytest.raises(PowerRefusedError) as exc:
            await power.execute(await _id(repo, "web-01"), "start", actor="o", source="api")
        assert exc.value.code == "state"
        assert pve.power_posts == []

    async def test_the_self_guest_needs_its_name_typed(self, world):
        repo, pve, svc, power = world
        pve.add(100, "hp-control", "running", qemu_net("192.0.2.5", "BC:24:11:00:00:05"))
        await _sync(svc)
        hid = await _id(repo, "hp-control")
        await repo.set_self_host(hid)

        for typed in (None, "wrong-name"):
            with pytest.raises(PowerRefusedError) as exc:
                await power.execute(hid, "shutdown", actor="o", source="api", confirm_name=typed)
            assert exc.value.code == "confirmation_required"
            assert exc.value.status == 428
            assert "HomePilot goes down with it" in exc.value.message
        assert pve.power_posts == []

        result = await power.execute(
            hid, "shutdown", actor="o", source="api", confirm_name="HP-Control"
        )
        assert result.outcome == "ok"

    @pytest.mark.parametrize("how", ["flag", "infra"])
    async def test_a_protected_guest_needs_its_name_typed_for_every_off_action(self, world, how):
        repo, pve, svc, power = world
        pve.add(101, "dns-01", "running", qemu_net("192.0.2.10", "BC:24:11:00:00:01"))
        await _sync(svc)
        hid = await _id(repo, "dns-01")
        if how == "flag":
            await repo.update_host(hid, protected=1)
        else:
            await repo.update_host(hid, env="infra", env_source="user")

        for action in ("shutdown", "reboot", "stop"):
            with pytest.raises(PowerRefusedError) as exc:
                await power.execute(hid, action, actor="o", source="api")
            assert exc.value.code == "confirmation_required"
        assert pve.power_posts == []
        # Protection is about switching OFF; starting needs no typed name.
        result = await power.execute(hid, "stop", actor="o", source="api", confirm_name="dns-01")
        assert result.outcome == "ok"
        assert (await power.execute(hid, "start", actor="o", source="api")).outcome == "ok"

    async def test_an_operator_may_unprotect_an_infra_guest(self, world):
        repo, pve, svc, power = world
        pve.add(101, "dns-01", "running", qemu_net("192.0.2.10", "BC:24:11:00:00:01"))
        await _sync(svc)
        hid = await _id(repo, "dns-01")
        await repo.update_host(hid, env="infra", env_source="user", protected=0)
        assert (await power.execute(hid, "stop", actor="o", source="api")).outcome == "ok"


# ── the REST surface ─────────────────────────────────────────────────────────


@pytest.fixture
async def http(world, tmp_path: Path):
    from homepilot.artifacts.router import router as artifacts_router
    from homepilot.inventory.router import router as inventory_router

    repo, pve, svc, _power = world
    store = ArtifactStore(tmp_path / "artifacts")
    lifecycle = ArtifactLifecycle(store, repository=repo)
    executor = ArtifactExecutor(
        store=store, lifecycle=lifecycle, repo=repo, proxmox=pve, vault=None
    )
    runner = TaskRunner(
        repo=TaskRepository(repo.db),
        lifecycle=lifecycle,
        executor=executor,
        apply_reconciler=None,
        store=store,
    )
    app = FastAPI()
    app.include_router(inventory_router, prefix="/inventory")
    app.include_router(artifacts_router, prefix="/artifacts")
    app.state.repo = repo
    app.state.inventory_service = svc
    app.state.artifact_store = store
    app.state.artifact_lifecycle = lifecycle
    app.state.task_runner = runner
    app.state.task_repo = TaskRepository(repo.db)
    token: dict[str, Any] = dict(ADMIN)
    app.dependency_overrides[require_token] = lambda: token
    ctx = {
        "repo": repo,
        "lifecycle": lifecycle,
        "store": store,
        "inventory_service": svc,
        "_mcp_caller_id": "assistant (hp_asst0000001)",
        "_mcp_token_scope": "full",
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, token, ctx, repo, pve, svc
    await runner.drain(timeout=5.0)


class TestTheOperatorRoute:
    async def test_post_power_starts_the_guest_and_answers_the_outcome(self, http):
        client, _token, _ctx, repo, pve, svc = http
        pve.add(101, "web-01", "stopped", qemu_net("192.0.2.10", "BC:24:11:00:00:01"))
        await _sync(svc)
        hid = await _id(repo, "web-01")

        resp = await client.post(f"/inventory/{hid}/power", json={"action": "start"})

        assert resp.status_code == 200, resp.text
        assert resp.json()["outcome"] == "ok"
        assert pve.guests[101]["status"] == "running"
        (row,) = [a for a in await _audit(repo) if a["action"] == "guest_start"]
        assert row["user_id"] == "owner (hp_owner000001)"
        assert row["source"] == "api"

    async def test_a_refused_start_names_the_holder_over_http(self, http):
        client, _token, _ctx, repo, pve, svc = http
        pve.add(101, "vpn-box", "running", qemu_net("192.0.2.10", "BC:24:11:00:00:01"))
        pve.add(102, "copycat", "stopped", qemu_net("192.0.2.10", "BC:24:11:00:00:02"))
        await _sync(svc)

        resp = await client.post(
            f"/inventory/{await _id(repo, 'copycat')}/power", json={"action": "start"}
        )

        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert detail["code"] == "address_clash"
        assert "vpn-box" in detail["message"]
        assert pve.power_posts == []

    async def test_a_timed_out_shutdown_is_not_reported_as_success(self, http):
        client, _token, _ctx, repo, pve, svc = http
        pve.add(
            101,
            "stubborn",
            "running",
            qemu_net("192.0.2.10", "BC:24:11:00:00:01"),
            ignore_acpi=True,
        )
        await _sync(svc)

        resp = await client.post(
            f"/inventory/{await _id(repo, 'stubborn')}/power",
            json={"action": "shutdown", "timeout_s": 15},
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["outcome"] == "timed_out"
        assert body["next_actions"] == ["stop"]

    async def test_an_assistant_token_cannot_power_directly(self, http):
        client, token, _ctx, repo, pve, svc = http
        pve.add(101, "web-01", "stopped", qemu_net("192.0.2.10", "BC:24:11:00:00:01"))
        await _sync(svc)
        token.clear()
        token.update(ASSISTANT)

        resp = await client.post(
            f"/inventory/{await _id(repo, 'web-01')}/power", json={"action": "start"}
        )

        assert resp.status_code == 403
        assert pve.power_posts == []

    async def test_get_power_shows_state_pending_and_confirmations(self, http):
        client, token, _ctx, repo, pve, svc = http
        pve.add(
            100,
            "hp-control",
            "running",
            qemu_net("192.0.2.5", "BC:24:11:00:00:05"),
            pending={"ipconfig0": "ip=192.0.2.6/24,gw=192.0.2.1"},
        )
        await _sync(svc)
        hid = await _id(repo, "hp-control")
        await repo.set_self_host(hid)
        token.clear()
        token.update(ASSISTANT)  # a read

        resp = await client.get(f"/inventory/{hid}/power")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "running"
        assert body["is_self"] is True
        assert body["pending"] == [
            {
                "key": "ipconfig0",
                "current": "ip=192.0.2.5/24,gw=192.0.2.1",
                "pending": "ip=192.0.2.6/24,gw=192.0.2.1",
            }
        ]
        assert body["actions"]["start"]["available"] is False
        assert body["actions"]["reboot"] == {
            "available": True,
            "reason": None,
            "needs_typed_confirmation": True,
        }
        assert pve.power_posts == []

    async def test_only_an_operator_may_change_protected(self, http):
        client, token, _ctx, repo, pve, svc = http
        pve.add(101, "dns-01", "running", qemu_net("192.0.2.10", "BC:24:11:00:00:01"))
        await _sync(svc)
        hid = await _id(repo, "dns-01")
        token.clear()
        token.update(ASSISTANT)
        resp = await client.patch(f"/inventory/{hid}", json={"protected": True})
        assert resp.status_code == 403
        token.clear()
        token.update(ADMIN)
        resp = await client.patch(f"/inventory/{hid}", json={"protected": True})
        assert resp.status_code == 200, resp.text
        assert resp.json()["protected"] == 1


# ── the assistant path: a proposal, never a powered guest ────────────────────


class TestTheAssistantProposes:
    async def test_power_guest_proposes_and_powers_nothing(self, http):
        _client, _token, ctx, repo, pve, svc = http
        pve.add(101, "web-01", "stopped", qemu_net("192.0.2.10", "BC:24:11:00:00:01"))
        await _sync(svc)
        hid = await _id(repo, "web-01")

        out = await _handle_tool("power_guest", {"host_id": hid, "action": "start"}, ctx)

        assert out["status"] == "proposed"
        assert out["powered"] is False
        assert pve.power_posts == []
        assert pve.guests[101]["status"] == "stopped"
        fm, body = ctx["store"].read(out["id"])
        assert fm["kind"] == "guest-power"
        assert fm["status"] == "proposed"
        assert fm["target"] == {"kind": "vm", "host": "web-01", "vmid": 101, "node": NODE}
        assert "guest-power-spec" in body

    async def test_power_guest_is_refused_at_once_naming_the_address_holder(self, http):
        _client, _token, ctx, repo, pve, svc = http
        pve.add(101, "vpn-box", "running", qemu_net("192.0.2.10", "BC:24:11:00:00:01"))
        pve.add(102, "copycat", "stopped", qemu_net("192.0.2.10", "BC:24:11:00:00:02"))
        await _sync(svc)

        with pytest.raises(ValueError, match="vpn-box"):
            await _handle_tool(
                "power_guest", {"host_id": await _id(repo, "copycat"), "action": "start"}, ctx
            )
        assert ctx["store"].list() == []
        assert pve.power_posts == []

    async def test_power_guest_never_proposes_switching_off_the_self_guest(self, http):
        _client, _token, ctx, repo, pve, svc = http
        pve.add(100, "hp-control", "running", qemu_net("192.0.2.5", "BC:24:11:00:00:05"))
        await _sync(svc)
        hid = await _id(repo, "hp-control")
        await repo.set_self_host(hid)

        with pytest.raises(ValueError, match="Hosts page"):
            await _handle_tool("power_guest", {"host_id": hid, "action": "reboot"}, ctx)
        assert ctx["store"].list() == []

    async def test_a_read_only_token_cannot_propose(self, http):
        _client, _token, ctx, repo, pve, svc = http
        pve.add(101, "web-01", "stopped", qemu_net("192.0.2.10", "BC:24:11:00:00:01"))
        await _sync(svc)
        with pytest.raises(ValueError, match="full tier"):
            await _handle_tool(
                "power_guest",
                {"host_id": await _id(repo, "web-01"), "action": "start"},
                {**ctx, "_mcp_token_scope": "read_only"},
            )
        got = await _handle_tool(
            "get_guest_power",
            {"host_id": await _id(repo, "web-01")},
            {**ctx, "_mcp_token_scope": "read_only"},
        )
        assert got["status"] == "stopped"

    async def test_the_owner_approves_and_applies_in_one_step(self, http):
        client, _token, ctx, repo, pve, svc = http
        pve.add(101, "web-01", "stopped", qemu_net("192.0.2.10", "BC:24:11:00:00:01"))
        await _sync(svc)
        out = await _handle_tool(
            "power_guest", {"host_id": await _id(repo, "web-01"), "action": "start"}, ctx
        )

        resp = await client.post(f"/artifacts/{out['id']}/approve-and-apply?sync=true")

        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "succeeded", resp.json()
        assert pve.guests[101]["status"] == "running"
        fm, _ = ctx["store"].read(out["id"])
        assert fm["status"] == "applied"
        (row,) = [a for a in await _audit(repo) if a["action"] == "guest_start"]
        assert row["artifact_id"] == out["id"]
        assert row["source"] == "artifact"

    async def test_approve_and_apply_reads_artifacts_off_the_event_loop(self, http, monkeypatch):
        """Every artifact read on the one-step path runs on a pool thread (#801).

        The route's own kind check read the artifact file on the loop when lane
        B3 and the no-blocking lane met in 3.9.0; a read is disk I/O, and #801
        moved every other one onto the `io` pool.
        """
        client, _token, ctx, repo, pve, svc = http
        pve.add(101, "web-01", "stopped", qemu_net("192.0.2.10", "BC:24:11:00:00:01"))
        await _sync(svc)
        out = await _handle_tool(
            "power_guest", {"host_id": await _id(repo, "web-01"), "action": "start"}, ctx
        )
        store = ctx["store"]
        real_read = store.read
        on_loop: list[bool] = []

        def recording_read(*args: Any, **kwargs: Any) -> Any:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                on_loop.append(False)
            else:
                on_loop.append(True)
            return real_read(*args, **kwargs)

        monkeypatch.setattr(store, "read", recording_read)

        resp = await client.post(f"/artifacts/{out['id']}/approve-and-apply?sync=true")

        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "succeeded", resp.json()
        assert on_loop, "the one-step path read no artifact through the store"
        assert not any(on_loop), f"artifact read on the event loop ({on_loop.count(True)}x)"

    async def test_the_apply_re_checks_the_guards(self, http):
        """Proposed while free; a holder started since; the apply refuses and names it."""
        client, _token, ctx, repo, pve, svc = http
        pve.add(101, "vpn-box", "stopped", qemu_net("192.0.2.10", "BC:24:11:00:00:01"))
        pve.add(102, "copycat", "stopped", qemu_net("192.0.2.10", "BC:24:11:00:00:02"))
        await _sync(svc)
        out = await _handle_tool(
            "power_guest", {"host_id": await _id(repo, "copycat"), "action": "start"}, ctx
        )
        pve.guests[101]["status"] = "running"

        resp = await client.post(f"/artifacts/{out['id']}/approve-and-apply?sync=true")

        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "failed"
        fm, _ = ctx["store"].read(out["id"])
        assert fm["status"] == "failed"
        assert "vpn-box" in fm["failure_reason"]
        assert pve.guests[102]["status"] == "stopped"
        assert pve.power_posts == []

    async def test_the_apply_refuses_a_guest_marked_self_since_the_proposal(self, http):
        client, _token, ctx, repo, pve, svc = http
        pve.add(100, "hp-control", "running", qemu_net("192.0.2.5", "BC:24:11:00:00:05"))
        await _sync(svc)
        hid = await _id(repo, "hp-control")
        out = await _handle_tool("power_guest", {"host_id": hid, "action": "shutdown"}, ctx)
        await repo.set_self_host(hid)

        resp = await client.post(f"/artifacts/{out['id']}/approve-and-apply?sync=true")

        assert resp.json()["status"] == "failed"
        fm, _ = ctx["store"].read(out["id"])
        # An approval is not the typed name: the reason sends the owner to the
        # Hosts page rather than asking for a name the artifact cannot carry.
        assert "never done through a proposed change" in fm["failure_reason"]
        assert pve.guests[100]["status"] == "running"
        assert pve.power_posts == []

    async def test_approve_and_apply_is_for_operators_and_proposals(self, http):
        client, token, ctx, repo, pve, svc = http
        pve.add(101, "web-01", "stopped", qemu_net("192.0.2.10", "BC:24:11:00:00:01"))
        await _sync(svc)
        out = await _handle_tool(
            "power_guest", {"host_id": await _id(repo, "web-01"), "action": "start"}, ctx
        )
        token.clear()
        token.update(ASSISTANT)
        resp = await client.post(f"/artifacts/{out['id']}/approve-and-apply")
        assert resp.status_code == 403
        assert pve.power_posts == []

        token.clear()
        token.update(ADMIN)
        note = await ctx["lifecycle"].propose(
            {
                "id": "2026-11-08-a-note-abc123",
                "kind": "kb-note",
                "intent": "a note",
                "body": "# note\n",
                "produced_by": {"session": "s", "agent": "a", "user": "u"},
            }
        )
        # Every kind may now be approved and applied in one step (3.10); a kb-note
        # is never proposed (it applies on propose), so it is refused as such.
        resp = await client.post(f"/artifacts/{note}/approve-and-apply")
        assert resp.status_code == 409
        assert "not proposed" in resp.json()["detail"]


class TestTheArtifactKindIsHonest:
    async def test_a_power_artifact_cannot_ask_for_a_snapshot_or_a_replay(self, world, tmp_path):
        repo, *_ = world
        lifecycle = ArtifactLifecycle(ArtifactStore(tmp_path / "a2"), repository=repo)
        from homepilot.artifacts.models import render_guest_power_body

        base = {
            "id": "2026-11-08-power-start-web-abc123",
            "kind": "guest-power",
            "intent": "start web-01",
            "target": {"kind": "vm", "host": "web-01", "vmid": 101, "node": NODE},
            "idempotence": "replay-only",
            "requires_snapshot": False,
            "produced_by": {"session": "s", "agent": "a", "user": "u"},
            "body": render_guest_power_body("start", "web-01"),
        }
        for bad, needle in (
            ({"requires_snapshot": True}, "requires_snapshot: false"),
            ({"idempotence": "via-precheck"}, "replay-only"),
            ({"body": "# nothing\n"}, "guest-power-spec"),
            (
                {"body": render_guest_power_body("start", "x").replace("start\n", "explode\n")},
                "action",
            ),
        ):
            with pytest.raises(LifecycleError, match=needle):
                await lifecycle.propose({**base, **bad})
        assert await lifecycle.propose(base) == base["id"]
