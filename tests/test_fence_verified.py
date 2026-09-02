"""The fence is asked, not assumed: a provision ends with `fence: verified`,
`unverified` (with the reason) or a destroyed guest - never `written` (#648).

Every layer below this reported the fence as configuration: rules on the tap,
`firewall=1` on the NIC, the datacenter switch on, an enforcing stack. All of
it was true of the first real friend's guest on prod, and nobody had ever run a
command inside a guest and watched a packet towards the operator LAN go nowhere.
These gates drive the provision journey through `ProvisionService` with the
REAL exec-and-wait loop over a faked guest agent, and assert what the record
says about the fence afterwards - and what happened to the guest.

Teeth (each proven by planting the defect and watching the NAMED test fail):
  - skip the destroy on a breach ->
    `test_a_guest_that_reaches_the_isolated_range_is_destroyed_and_the_provision_fails`;
  - treat REFUSED as "not reached" -> `test_a_reset_from_the_isolated_range_is_a_breach_too`;
  - call silence on both sides "verified" ->
    `test_silence_on_both_sides_is_unverified_not_verified`;
  - drop the agent_answered plumbing into the join ->
    `test_a_guest_with_no_agent_is_unverified_and_the_join_does_not_wait_twice`;
  - probe when nothing inside the fence is known alive ->
    `test_no_known_alive_address_inside_the_fence_means_unverified_and_no_probe`;
  - let a bug in the check raise -> `test_a_bug_in_the_check_cannot_fail_the_provision`.

The probe SCRIPT is run through a real /bin/sh at the bottom of this file, so
the tokens the service reads are the tokens a guest would actually print.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from homepilot.adapters.proxmox import ProxmoxClient
from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.db.repository import Repository
from homepilot.provision import fence_verify as fv
from homepilot.provision.defaults import ProvisioningDefaults
from homepilot.provision.guest_network import DesiredGuestNetwork
from homepilot.provision.models import ProvisionRequest
from homepilot.provision.service import ProvisionService
from homepilot.tasks.repository import TaskRepository

PUBKEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGwqABCm8wgfVU6mDkhzQAaGT1kEJmpTB/J0UODkf1Xq me@lab"
KEY = "tskey-auth-kFENCE1CNTRL-abcdefghijklmnopqrstuvwxyz"
PVE_HOST = "192.0.2.10"  # inside the isolated range: the address the guest MUST NOT reach
PVE = f"{PVE_HOST}:8006"
PVE53 = f"{PVE_HOST}:53"  # the same host on a port SELinux lets a confined agent try
GATEWAY = "198.51.100.1:53"  # the control: the fence ACCEPTs tcp/53 to the gateway

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

AGENT_OK = {
    "data": {
        "result": [
            {"name": "lo", "ip-addresses": [{"ip-address": "127.0.0.1"}]},
            {"name": "eth0", "ip-addresses": [{"ip-address": "198.51.100.104"}]},
        ]
    }
}


class FakeFirewall:
    """The per-VM firewall gateway, accepting everything: the WRITE is not on trial here."""

    def __init__(self) -> None:
        self.rules: list[dict[str, Any]] = []

    async def set_vm_firewall_options(self, **kwargs: Any) -> str:
        return "ok"

    async def create_vm_firewall_rule(self, node: str, vmid: int, **rule: Any) -> str:
        self.rules.append(rule)
        return "ok"


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.connect()
    await run_migrations(database)
    yield database
    await database.close()


def _guest(proxmox: AsyncMock, answers: dict[str, str] | None, *, agent: bool = True) -> list[str]:
    """A guest at the exec/exec-status boundary, with the REAL wait loop above it.

    `answers` maps endpoint -> token for the fence probe. The fake reads the
    targets OUT OF THE SCRIPT (`HP_TARGETS='...'`) and answers only those, so a
    probe aimed at the wrong address gets "printed nothing" rather than a
    convenient answer. Every other script (tailscale probe, cloud-init wait,
    rm) exits 0 with no output.
    """
    from homepilot.adapters.proxmox import ProxmoxClient as Real

    scripts: list[str] = []
    outputs: dict[int, str] = {}

    async def agent_exec(node, vmid, command):
        script = command[-1]
        scripts.append(script)
        pid = len(scripts)
        m = re.search(r"HP_TARGETS='([^']*)'", script)
        if m and answers is not None:
            outputs[pid] = "\n".join(
                f"{ep} {answers[ep]}" for ep in m.group(1).split() if ep in answers
            )
        else:
            outputs[pid] = ""
        return {"data": {"pid": pid}}

    async def agent_exec_status(node, vmid, pid):
        return {"data": {"exited": 1, "exitcode": 0, "out-data": outputs[pid], "err-data": ""}}

    async def run(*args: Any, **kwargs: Any) -> tuple[int, str, str]:
        return await Real.agent_run(proxmox, *args, **kwargs)

    proxmox.agent_ping = AsyncMock(return_value=agent)
    proxmox.agent_exec = AsyncMock(side_effect=agent_exec)
    proxmox.agent_exec_status = AsyncMock(side_effect=agent_exec_status)
    proxmox.agent_write_file = AsyncMock(return_value={"data": {}})
    proxmox.agent_run = run
    return scripts


@pytest.fixture
def proxmox() -> AsyncMock:
    px = AsyncMock(spec=ProxmoxClient)
    px.api_host = PVE_HOST
    px.api_port = 8006
    px.next_vmid = AsyncMock(return_value=104)
    px.clone_vm = AsyncMock(return_value="UPID:elizabeth:clone:")
    px.wait_for_task = AsyncMock(return_value={"status": "stopped", "exitstatus": "OK"})
    px.set_vm_config = AsyncMock(return_value={"data": None})
    px.resize_disk = AsyncMock(return_value={"data": None})
    px.start_vm = AsyncMock(return_value="UPID:elizabeth:start:")
    px.stop_vm = AsyncMock(return_value="UPID:elizabeth:stop:")
    px.delete_vm = AsyncMock(return_value="UPID:elizabeth:destroy:")
    px.get_vm_current = AsyncMock(return_value={"data": {"status": "running"}})
    px.get_vm_agent_network = AsyncMock(return_value=AGENT_OK)
    return px


def _service(db, proxmox, bridge: str = "innkeep", guest_network=GUEST_NETWORK) -> ProvisionService:
    service = ProvisionService(
        proxmox=proxmox,
        task_repo=TaskRepository(db),
        repo=Repository(db),
        poll_interval=0.01,
        task_timeout_s=2.0,
        ip_wait_s=0.0,
        ip_interval=0.01,
        agent_wait_s=0.05,
        agent_interval=0.01,
        fence_probe_timeout_s=2.0,
    )
    service.sdn_gateway = FakeFirewall()

    async def _defaults(_source: Any = None) -> ProvisioningDefaults:
        return ProvisioningDefaults(node="elizabeth", template_vmid=9000, bridge=bridge)

    async def _guest_network(_source: Any = None) -> DesiredGuestNetwork | None:
        return guest_network

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


async def _run(service: ProvisionService, request: ProvisionRequest | None = None) -> dict:
    task_id = await service.start(request or _request())
    for _ in range(600):
        row = await service.task_repo.get_task(task_id)
        if row and row["status"] in ("succeeded", "failed", "cancelled"):
            return dict(row)
        await asyncio.sleep(0.01)
    raise AssertionError("the provision never reached a terminal state")


def _hosts(db_path: str) -> list[dict[str, Any]]:
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute("SELECT * FROM hosts").fetchall()]
    finally:
        conn.close()


@pytest.mark.asyncio
class TestTheFenceIsAskedNotAssumed:
    async def test_a_silent_isolated_range_with_a_live_control_is_verified(
        self, db, proxmox, tmp_path
    ) -> None:
        scripts = _guest(proxmox, {PVE: fv.TIMEOUT, PVE53: fv.TIMEOUT, GATEWAY: fv.CONNECTED})
        row = await _run(_service(db, proxmox))
        assert row["status"] == "succeeded", row["error"]
        result = json.loads(row["result_json"])
        assert result["fence"] == "verified"
        assert PVE in result["fence_detail"] and "holds" in result["fence_detail"]
        verification = result["guest_network_fence"]["verification"]
        assert verification["verdict"] == "verified"
        assert {p["target"]: p["outcome"] for p in verification["probes"]} == {
            PVE: fv.TIMEOUT,
            PVE53: fv.TIMEOUT,
            GATEWAY: fv.CONNECTED,
        }
        # The probe named the Proxmox host and the gateway - the two addresses
        # HomePilot can vouch for - and it ran INSIDE the guest.
        probe = next(s for s in scripts if "HP_TARGETS" in s)
        assert PVE in probe and PVE53 in probe and GATEWAY in probe
        assert len(_hosts(str(tmp_path / "test.db"))) == 1

    async def test_a_guest_that_reaches_the_isolated_range_is_destroyed_and_the_provision_fails(
        self, db, proxmox, tmp_path
    ) -> None:
        """WHAT THIS FORBIDS: handing over a guest that can reach the operator's
        LAN because its rules were written. The written rules are exactly what
        was true of every guest so far; this is the first assertion about the
        wire."""
        _guest(proxmox, {PVE: fv.CONNECTED, GATEWAY: fv.CONNECTED})
        row = await _run(_service(db, proxmox))
        assert row["status"] == "failed"
        assert "does not hold" in row["error"] and PVE in row["error"]
        outcome = json.loads(row["result_json"])
        assert outcome == {
            "failed": True,
            "step": "verify_fence",
            "vmid": 104,
            "cleanup": "deleted",
        }
        # A BOOTED guest: stopped first (PVE refuses to destroy a running one),
        # then destroyed, both WAITED for.
        proxmox.stop_vm.assert_awaited_once_with("elizabeth", 104)
        proxmox.delete_vm.assert_awaited_once_with("elizabeth", 104)
        assert proxmox.wait_for_task.await_count >= 4  # clone, start, stop, destroy
        assert _hosts(str(tmp_path / "test.db")) == [], (
            "a breaching guest must never be recorded as a host anyone can hand over"
        )

    async def test_a_reset_from_the_isolated_range_is_a_breach_too(self, db, proxmox) -> None:
        """A RST means no listener on that port - and a host that ANSWERED. Only
        a handshake would be a weaker gate than the fence itself."""
        _guest(proxmox, {PVE: fv.REFUSED, GATEWAY: fv.CONNECTED})
        row = await _run(_service(db, proxmox))
        assert row["status"] == "failed"
        assert "reset" in row["error"]
        proxmox.delete_vm.assert_awaited_once()

    async def test_a_failed_destroy_after_a_breach_says_the_guest_may_still_be_up(
        self, db, proxmox
    ) -> None:
        _guest(proxmox, {PVE: fv.CONNECTED, GATEWAY: fv.CONNECTED})
        proxmox.delete_vm = AsyncMock(side_effect=RuntimeError("500 destroy refused"))
        row = await _run(_service(db, proxmox))
        assert row["status"] == "failed"
        assert (
            "could NOT be destroyed" in row["error"] and "isolated range in reach" in row["error"]
        )
        assert json.loads(row["result_json"])["cleanup"] == "failed"

    async def test_silence_on_both_sides_is_unverified_not_verified(self, db, proxmox) -> None:
        """A guest with no network yet is silent towards EVERYTHING. Reading that
        silence as a fence is #642 in its purest form."""
        _guest(proxmox, {PVE: fv.TIMEOUT, GATEWAY: fv.TIMEOUT})
        row = await _run(_service(db, proxmox))
        assert row["status"] == "succeeded", row["error"]
        result = json.loads(row["result_json"])
        assert result["fence"] == "unverified"
        assert "proves nothing" in result["fence_detail"]

    async def test_a_confined_agent_is_unverified_naming_selinux(self, db, proxmox) -> None:
        _guest(proxmox, {PVE: fv.EPERM, PVE53: fv.EPERM, GATEWAY: fv.EPERM})
        row = await _run(_service(db, proxmox))
        result = json.loads(row["result_json"])
        assert result["fence"] == "unverified"
        assert "SELinux" in result["fence_detail"] and PVE53 in result["fence_detail"]

    async def test_a_confined_agent_that_may_open_dns_still_verifies_the_fence(
        self, db, proxmox
    ) -> None:
        """SELinux lets `virt_qemu_ga_t` open tcp/53 and nothing much else (dev,
        2026-08-29). The fence is a DROP on the whole range, so the DNS port of
        the Proxmox host is silent behind it and answers with a reset without
        it - which is why the probe carries that second port at all."""
        _guest(proxmox, {PVE: fv.EPERM, PVE53: fv.TIMEOUT, GATEWAY: fv.CONNECTED})
        row = await _run(_service(db, proxmox))
        result = json.loads(row["result_json"])
        assert result["fence"] == "verified", result["fence_detail"]
        assert PVE53 in result["fence_detail"]

    async def test_a_reset_from_the_dns_port_behind_a_confined_agent_is_a_breach(
        self, db, proxmox
    ) -> None:
        _guest(proxmox, {PVE: fv.EPERM, PVE53: fv.REFUSED, GATEWAY: fv.CONNECTED})
        row = await _run(_service(db, proxmox))
        assert row["status"] == "failed"
        assert PVE53 in row["error"] and "reset" in row["error"]
        proxmox.delete_vm.assert_awaited_once()

    async def test_a_guest_with_no_agent_is_unverified_and_the_join_does_not_wait_twice(
        self, db, proxmox
    ) -> None:
        """Two bounded waits on an image with no qemu-guest-agent would double
        the time a redeemer watches a spinner. What the fence check learned
        about the agent is handed to the join."""
        _guest(proxmox, None, agent=False)
        row = await _run(_service(db, proxmox), _request(tailscale_auth_key=KEY))
        assert row["status"] == "succeeded", row["error"]
        result = json.loads(row["result_json"])
        assert result["fence"] == "unverified"
        assert "qemu-guest-agent never answered" in result["fence_detail"]
        assert result["tailnet"] == "unknown"
        # ONE wait: agent_wait_s / agent_interval polls, plus the first try.
        # A second wait in the join would double this.
        assert proxmox.agent_ping.await_count <= 7, proxmox.agent_ping.await_count
        proxmox.agent_exec.assert_not_awaited()

    async def test_no_known_alive_address_inside_the_fence_means_unverified_and_no_probe(
        self, db, proxmox
    ) -> None:
        """A Proxmox host on a management network the isolate list does not
        cover leaves HomePilot with nothing it can vouch for. It says so and
        asks the guest nothing - a probe against an address that may simply be
        absent would make silence look like a fence."""
        proxmox.api_host = "203.0.113.5"
        _guest(proxmox, {PVE: fv.TIMEOUT, GATEWAY: fv.CONNECTED})
        row = await _run(_service(db, proxmox))
        result = json.loads(row["result_json"])
        assert result["fence"] == "unverified"
        assert "nothing could be probed" in result["fence_detail"]
        assert result["guest_network_fence"]["verification"]["probes"] == []
        proxmox.agent_ping.assert_not_awaited()
        proxmox.agent_exec.assert_not_awaited()

    async def test_a_guest_off_the_guest_vnet_is_not_probed(self, db, proxmox) -> None:
        _guest(proxmox, {PVE: fv.CONNECTED, GATEWAY: fv.CONNECTED})
        row = await _run(_service(db, proxmox, bridge="vmbr0"))
        assert row["status"] == "succeeded", row["error"]
        result = json.loads(row["result_json"])
        assert result["fence"] is None and result["fence_detail"] is None
        assert result["guest_network_fence"] is None
        proxmox.agent_exec.assert_not_awaited()

    async def test_a_bug_in_the_check_cannot_fail_the_provision(self, db, proxmox) -> None:
        """The check is evidence, never a hazard: whatever goes wrong inside it
        is reported as unverified, and the fenced guest is handed over."""
        _guest(proxmox, {PVE: fv.TIMEOUT, GATEWAY: fv.CONNECTED})
        service = _service(db, proxmox)

        async def boom(*args: Any, **kwargs: Any):
            raise RuntimeError("the probe fell over")

        service._probe_fence = boom  # type: ignore[method-assign]
        row = await _run(service)
        assert row["status"] == "succeeded", row["error"]
        result = json.loads(row["result_json"])
        assert result["fence"] == "unverified"
        assert "the probe fell over" in result["fence_detail"]
        proxmox.delete_vm.assert_not_awaited()

    async def test_the_probe_running_out_of_time_is_unverified(self, db, proxmox) -> None:
        _guest(proxmox, {PVE: fv.TIMEOUT, GATEWAY: fv.CONNECTED})

        async def never_exits(node, vmid, pid):
            return {"data": {"exited": 0}}

        proxmox.agent_exec_status = AsyncMock(side_effect=never_exits)
        service = _service(db, proxmox)
        service.fence_probe_timeout_s = 0.05
        row = await _run(service)
        result = json.loads(row["result_json"])
        assert result["fence"] == "unverified"
        assert "had not finished" in result["fence_detail"]


class TestTheVerdictRules:
    """`judge` on its own: the table every journey above is a row of."""

    ISO = fv.ProbeTarget(PVE_HOST, 8006, "isolated", "the Proxmox host")
    ISO53 = fv.ProbeTarget(PVE_HOST, 53, "isolated", "the Proxmox host, dns port")
    CTL = fv.ProbeTarget("198.51.100.1", 53, "control", "the guest gateway")

    def _judge(self, iso: str, ctl: str | None) -> tuple[fv.FenceVerdict, str]:
        results = [fv.ProbeResult(self.ISO, iso)]
        if ctl is not None:
            results.append(fv.ProbeResult(self.CTL, ctl))
        return fv.judge(results)

    @pytest.mark.parametrize(
        ("iso", "ctl", "verdict"),
        [
            (fv.TIMEOUT, fv.CONNECTED, fv.FenceVerdict.VERIFIED),
            (fv.TIMEOUT, fv.REFUSED, fv.FenceVerdict.VERIFIED),
            (fv.CONNECTED, fv.CONNECTED, fv.FenceVerdict.BREACHED),
            (fv.REFUSED, fv.CONNECTED, fv.FenceVerdict.BREACHED),
            (fv.CONNECTED, fv.TIMEOUT, fv.FenceVerdict.BREACHED),
            (fv.TIMEOUT, fv.TIMEOUT, fv.FenceVerdict.UNVERIFIED),
            (fv.TIMEOUT, None, fv.FenceVerdict.UNVERIFIED),
            (fv.EPERM, fv.CONNECTED, fv.FenceVerdict.UNVERIFIED),
            (fv.UNREACH, fv.CONNECTED, fv.FenceVerdict.UNVERIFIED),
            (fv.NOTOOL, fv.NOTOOL, fv.FenceVerdict.UNVERIFIED),
            ("OTHER:Bad file descriptor", fv.CONNECTED, fv.FenceVerdict.UNVERIFIED),
        ],
    )
    def test_table(self, iso: str, ctl: str | None, verdict: fv.FenceVerdict) -> None:
        got, detail = self._judge(iso, ctl)
        assert got is verdict, detail
        assert detail

    @pytest.mark.parametrize(
        ("api", "dns", "ctl", "verdict"),
        [
            (fv.EPERM, fv.TIMEOUT, fv.CONNECTED, fv.FenceVerdict.VERIFIED),
            (fv.EPERM, fv.REFUSED, fv.CONNECTED, fv.FenceVerdict.BREACHED),
            (fv.TIMEOUT, fv.REFUSED, fv.CONNECTED, fv.FenceVerdict.BREACHED),
            (fv.EPERM, fv.EPERM, fv.CONNECTED, fv.FenceVerdict.UNVERIFIED),
            (fv.EPERM, fv.TIMEOUT, fv.TIMEOUT, fv.FenceVerdict.UNVERIFIED),
        ],
    )
    def test_two_isolated_ports(
        self, api: str, dns: str, ctl: str, verdict: fv.FenceVerdict
    ) -> None:
        got, detail = fv.judge(
            [
                fv.ProbeResult(self.ISO, api),
                fv.ProbeResult(self.ISO53, dns),
                fv.ProbeResult(self.CTL, ctl),
            ]
        )
        assert got is verdict, detail

    def test_nothing_to_probe_is_unverified(self) -> None:
        verdict, detail = fv.judge([])
        assert verdict is fv.FenceVerdict.UNVERIFIED and "nothing could be probed" in detail

    def test_a_target_the_guest_said_nothing_about_is_not_a_timeout(self) -> None:
        results = fv.parse_probe_output(f"{GATEWAY} CONNECTED\n", [self.ISO, self.CTL])
        assert results[0].outcome.startswith("OTHER:")
        assert fv.judge(results)[0] is fv.FenceVerdict.UNVERIFIED

    def test_an_unknown_token_is_not_trusted(self) -> None:
        results = fv.parse_probe_output(f"{PVE} FENCED\n", [self.ISO])
        assert results[0].outcome == "OTHER:FENCED"


@pytest.mark.asyncio
class TestTargets:
    async def test_the_proxmox_host_inside_the_fence_is_probed_on_two_ports(self) -> None:
        ts = await fv.isolated_targets(PVE_HOST, 8006, ("192.0.2.0/24",))
        assert [t.endpoint for t in ts] == [PVE, PVE53]
        assert all(t.role == "isolated" for t in ts)

    async def test_a_proxmox_host_outside_the_fence_is_not(self) -> None:
        assert await fv.isolated_targets("203.0.113.5", 8006, ("192.0.2.0/24",)) == []
        assert await fv.isolated_targets(None, 8006, ("192.0.2.0/24",)) == []
        assert await fv.isolated_targets(PVE_HOST, 8006, ()) == []

    def test_controls_are_the_gateway_then_a_nameserver_outside_the_fence(self) -> None:
        cts = fv.control_targets("198.51.100.1", "1.1.1.1", ("192.0.2.0/24",))
        assert [c.endpoint for c in cts] == ["198.51.100.1:53", "1.1.1.1:53"]
        # A nameserver INSIDE the fence is not a control - the fence drops it.
        cts = fv.control_targets("198.51.100.1", "192.0.2.53", ("192.0.2.0/24",))
        assert [c.endpoint for c in cts] == ["198.51.100.1:53"]
        # No gateway, a resolver by name: nothing usable, and nothing invented.
        assert fv.control_targets("", "resolver.example", ()) == []


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs /bin/sh")
class TestTheScriptThroughARealShell:
    """The tokens the service reads are the tokens a guest actually prints."""

    def _run(self, script: str, env: dict[str, str] | None = None) -> str:
        proc = subprocess.run(
            ["sh", "-c", script], capture_output=True, text=True, timeout=30, env=env
        )
        return proc.stdout

    def test_python_path_prints_connected_and_refused(self) -> None:
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        live = srv.getsockname()[1]
        closed = socket.socket()
        closed.bind(("127.0.0.1", 0))
        dead = closed.getsockname()[1]
        closed.close()  # bound then released: nothing listens, a RST comes back
        targets = [
            fv.ProbeTarget("127.0.0.1", live, "control", "listener"),
            fv.ProbeTarget("127.0.0.1", dead, "isolated", "closed port"),
        ]
        try:
            out = self._run(fv.probe_script(targets))
        finally:
            srv.close()
        results = fv.parse_probe_output(out, targets)
        assert [r.outcome for r in results] == [fv.CONNECTED, fv.REFUSED], out
        assert fv.judge(results)[0] is fv.FenceVerdict.BREACHED

    @pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
    def test_bash_fallback_prints_the_same_tokens(self, tmp_path) -> None:
        # A PATH with sh, bash, timeout and the text tools - and NO python3.
        bindir = tmp_path / "bin"
        bindir.mkdir()
        for name in ("sh", "bash", "timeout", "tr", "cut", "echo"):
            path = shutil.which(name)
            if path is None:
                pytest.skip(f"needs {name}")
            os.symlink(path, bindir / name)
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        live = srv.getsockname()[1]
        closed = socket.socket()
        closed.bind(("127.0.0.1", 0))
        dead = closed.getsockname()[1]
        closed.close()
        targets = [
            fv.ProbeTarget("127.0.0.1", live, "control", "listener"),
            fv.ProbeTarget("127.0.0.1", dead, "isolated", "closed port"),
        ]
        try:
            out = self._run(
                "command -v python3 >/dev/null 2>&1 && exit 99; " + fv.probe_script(targets),
                env={"PATH": str(bindir)},
            )
        finally:
            srv.close()
        results = fv.parse_probe_output(out, targets)
        assert [r.outcome for r in results] == [fv.CONNECTED, fv.REFUSED], out

    def test_a_guest_with_no_tools_says_notool(self, tmp_path) -> None:
        bindir = tmp_path / "bin"
        bindir.mkdir()
        for name in ("sh", "echo"):
            os.symlink(shutil.which(name), bindir / name)
        targets = [fv.ProbeTarget("127.0.0.1", 9, "isolated", "x")]
        out = self._run(fv.probe_script(targets), env={"PATH": str(bindir)})
        results = fv.parse_probe_output(out, targets)
        assert results[0].outcome == fv.NOTOOL
        assert fv.judge(results)[0] is fv.FenceVerdict.UNVERIFIED
