# The fixtures below are shared by import (test_agent_enroll, test_reinstall_stale
# _credential); pytest resolves them by parameter name, which ruff reads as a
# redefinition.
# ruff: noqa: F811
"""Upgrade the agent on a host that has a LIVE one, through HomePilot.

Found on prod: the control plane's own host ran an old agent - connected, no
``agent_version``, no metric state (so "Host went dark" fired for it), and an
allowlist that refused commands the hub treats as read-only. The only answer
HomePilot had was ``already_enrolled ... Revoke it ... before enrolling again``,
and revoking leaves a dead ``agent.token`` on the host: the #815 stuck state.

``POST /agents/install {"upgrade": true}`` reinstalls binary and config over
qemu-guest-agent and succeeds only when the SAME agent id is back on a NEW
connection and is no longer older than the control plane. If it never comes
back, the binary the installer kept (``hp-agent.prev``) is put back.

Credential: ROTATED. The upgrade enrols the new binary with a fresh bootstrap
token (the installer's config refresh needs one), so the hub mints a new
per-agent credential for the same agent id. The old credential stays valid on
the hub until that exact moment, and the installer keeps it as
``agent.token.prev``, so an upgrade that fails before the new agent registers
rolls back to a working credential.

Teeth (each proven by reverting the named piece):
  * verify by "agent id in the registry" (drop the ``agent is not prior`` test in
    ``_wait_for_upgrade``) -> ``test_the_old_connection_is_not_success`` passes the
    task on the OLD binary and fails here;
  * drop ``_roll_back``'s call -> the rollback tests and the failing-upgrade
    journey fail (the host is left with no agent);
  * drop the binary-swap block's ``hp-agent.prev`` copy -> ``test_the_binary_
    swap_keeps_the_running_binary`` and the failing-upgrade journey fail;
  * keep ``already_enrolled`` for upgrades -> every upgrade test fails at check.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from homepilot.agent_hub.enroll import (
    _AGENT_ID_MARKER,
    _ENV_PATH,
    AgentEnrollService,
    EnrollPreconditionError,
    _rollback_script,
)
from homepilot.agent_hub.registry import AgentRegistry
from homepilot.agent_hub.version_skew import control_plane_version
from homepilot.db.connection import Database
from homepilot.db.repository import Repository
from homepilot.tasks.repository import TaskRepository

from .go_toolchain import GO as _GO
from .go_toolchain import GO_SKIP_REASON
from .test_agent_enroll import (  # noqa: F401 - pytest fixtures
    GUEST_AGENT_ID,
    HUB_HOST,
    HUB_PORT,
    _FakeGuest,
    db,
    host,
    proxmox,
    registry,
    service,
)
from .test_reinstall_stale_credential import _mode, _Rig, rig  # noqa: F401

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "install-agent.sh"
OLD_VERSION = "3.0.0"


def _block(start: str, end: str) -> str:
    text = SCRIPT.read_text()
    a = text.index(start)
    b = text.index(end)
    return text[text.index("\n", a) + 1 : text.rindex("\n", a, b)]


def _binary_swap_block() -> str:
    return _block(">>> binary-swap block", "<<< end binary-swap block")


def _swap(install_dir: Path, new_binary: Path) -> str:
    """Run the SHIPPED binary-swap block: install ``new_binary`` into ``install_dir``."""
    script = "\n".join(
        [
            "set -euo pipefail",
            "SUDO=''",
            f'INSTALL_DIR="{install_dir}"',
            f'BIN="{new_binary}"',
            _binary_swap_block(),
        ]
    )
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def _roll_back_in(install_dir: Path, conf: Path, shim: Path) -> None:
    """Run the REAL rollback text in a sandbox; ``systemctl`` is a shim that
    records what it was asked to do."""
    shim.mkdir(exist_ok=True)
    (shim / "systemctl").write_text('#!/bin/sh\necho "$@" >> "$(dirname "$0")/calls"\n')
    (shim / "systemctl").chmod(0o755)
    env = {"PATH": f"{shim}:/usr/bin:/bin", "INSTALL_DIR": str(install_dir), "CONF_DIR": str(conf)}
    proc = subprocess.run(
        ["sh", "-c", _rollback_script()], capture_output=True, text=True, timeout=60, env=env
    )
    assert proc.returncode == 0, proc.stderr


class TestTheShellPieces:
    def test_the_binary_swap_keeps_the_running_binary(self, tmp_path: Path):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        (bin_dir / "hp-agent").write_text("old binary")
        new = tmp_path / "new"
        new.write_text("new binary")

        out = _swap(bin_dir, new)

        assert (bin_dir / "hp-agent").read_text() == "new binary"
        assert (bin_dir / "hp-agent.prev").read_text() == "old binary"
        assert "Installed to" in out, "the task reads this line as 'the binary was swapped'"

    def test_a_first_install_keeps_no_prev(self, tmp_path: Path):
        new = tmp_path / "new"
        new.write_text("new binary")
        _swap(tmp_path, new)
        assert not (tmp_path / "hp-agent.prev").exists()

    def test_rollback_restores_binary_and_only_a_missing_credential(self, tmp_path: Path):
        bin_dir, conf = tmp_path / "bin", tmp_path / "conf"
        bin_dir.mkdir()
        conf.mkdir()
        (bin_dir / "hp-agent").write_text("new binary")
        (bin_dir / "hp-agent.prev").write_text("old binary")
        (conf / "agent.token.prev").write_text("old credential")

        _roll_back_in(bin_dir, conf, tmp_path / "shim")

        assert (bin_dir / "hp-agent").read_text() == "old binary"
        assert (conf / "agent.token").read_text() == "old credential"
        assert _mode(conf / "agent.token") == 0o600
        assert (tmp_path / "shim" / "calls").read_text().split() == ["restart", "hp-agent"]

        # A credential the new agent DID persist is the live one: kept.
        (conf / "agent.token").write_text("new credential")
        _roll_back_in(bin_dir, conf, tmp_path / "shim")
        assert (conf / "agent.token").read_text() == "new credential"


# ---------------------------------------------------------------------------
# The service, real registry, guest mocked at the Proxmox boundary.
# ---------------------------------------------------------------------------


class _UpgradingGuest(_FakeGuest):
    """A guest whose installer restarts the live agent: the old connection goes,
    and (``comes_back``) the same agent id returns on a new one."""

    def __init__(self, registry: AgentRegistry, *, comes_back: bool, restarts: bool = True):
        super().__init__(registry, "web-01", connects=False)
        self.comes_back = comes_back
        self.restarts = restarts
        self.rollbacks = 0

    async def exec(self, node: str, vmid: int, command: list[str], capture_output: bool = False):
        if command and _rollback_script() in command[-1]:
            self.rollbacks += 1
            self.registry.unregister(GUEST_AGENT_ID)
            self.registry.register(GUEST_AGENT_ID, "web-01", {"agent_version": OLD_VERSION})
            self.mode = "rollback"
        else:
            self.mode = "install"
        return await super().exec(node, vmid, command, capture_output)

    async def exec_status(self, node: str, vmid: int, pid: int) -> dict[str, Any]:
        if self.mode == "rollback":
            return {"data": {"exited": 1, "exitcode": 0, "out-data": "", "err-data": ""}}
        if self.restarts:
            self.registry.unregister(GUEST_AGENT_ID)
            if self.comes_back:
                self.registry.register(
                    GUEST_AGENT_ID, "web-01", {"agent_version": control_plane_version()}
                )
        out = f"Installed to /usr/local/bin/hp-agent\n{_AGENT_ID_MARKER}{GUEST_AGENT_ID}\n"
        return {"data": {"exited": 1, "exitcode": 0, "out-data": out, "err-data": ""}}


def _wire(proxmox: AsyncMock, guest: _FakeGuest) -> None:
    proxmox.agent_write_file = AsyncMock(side_effect=guest.write_file)
    proxmox.agent_exec = AsyncMock(side_effect=guest.exec)
    proxmox.agent_exec_status = AsyncMock(side_effect=guest.exec_status)


async def _run(service: AgentEnrollService, host: dict[str, Any], upgrade: bool) -> dict[str, Any]:
    task_id = await service.start(host, HUB_HOST, HUB_PORT, actor="tester", upgrade=upgrade)
    for task in list(service._running_tasks):
        await task
    result = await service.task_repo.get_task(task_id)
    assert result is not None
    return result


class TestTheUpgradeTask:
    async def test_an_install_over_a_live_agent_points_at_upgrade_not_revoke(
        self, service: AgentEnrollService, host: dict[str, Any], registry: AgentRegistry
    ):
        registry.register(GUEST_AGENT_ID, "web-01", {})
        with pytest.raises(EnrollPreconditionError) as exc:
            await service.check(host, HUB_HOST)
        assert exc.value.code == "already_enrolled"
        assert "Upgrade agent" in exc.value.message
        assert "Revoke" not in exc.value.message

    async def test_an_upgrade_needs_a_live_agent(
        self, service: AgentEnrollService, host: dict[str, Any]
    ):
        with pytest.raises(EnrollPreconditionError) as exc:
            await service.check(host, HUB_HOST, upgrade=True)
        assert exc.value.code == "not_connected"

    async def test_the_same_agent_back_on_the_new_version_succeeds(
        self,
        service: AgentEnrollService,
        host: dict[str, Any],
        registry: AgentRegistry,
        proxmox: AsyncMock,
    ):
        registry.register(GUEST_AGENT_ID, "web-01", {})
        guest = _UpgradingGuest(registry, comes_back=True)
        _wire(proxmox, guest)

        task = await _run(service, host, upgrade=True)

        assert task["status"] == "succeeded", task["error"]
        import json

        result = json.loads(task["result_json"])
        assert result["agent_id"] == GUEST_AGENT_ID
        assert result["upgrade"] is True
        assert result["previous_version"] == "no version reported"
        assert result["agent_version"] == control_plane_version()
        assert guest.rollbacks == 0

    async def test_the_old_connection_is_not_success(
        self,
        service: AgentEnrollService,
        host: dict[str, Any],
        registry: AgentRegistry,
        proxmox: AsyncMock,
    ):
        """The installer never restarted the service: the agent in the registry
        is the OLD binary. That must not read as an upgrade, and nothing is
        rolled back because nothing was stopped. Unversioned, like the prod
        host: the version check alone cannot catch it."""
        registry.register(GUEST_AGENT_ID, "web-01", {})
        guest = _UpgradingGuest(registry, comes_back=False, restarts=False)
        _wire(proxmox, guest)

        task = await _run(service, host, upgrade=True)

        assert task["status"] == "failed"
        assert "did not reconnect" in task["error"]
        assert "still connected; nothing was rolled back" in task["error"]
        assert guest.rollbacks == 0
        assert registry.is_connected("web-01")

    async def test_a_new_agent_that_never_connects_is_rolled_back(
        self,
        service: AgentEnrollService,
        host: dict[str, Any],
        registry: AgentRegistry,
        proxmox: AsyncMock,
    ):
        registry.register(GUEST_AGENT_ID, "web-01", {"agent_version": OLD_VERSION})
        guest = _UpgradingGuest(registry, comes_back=False)
        _wire(proxmox, guest)

        task = await _run(service, host, upgrade=True)

        assert task["status"] == "failed"
        assert guest.rollbacks == 1
        assert "put the previous agent back and it is connected again" in task["error"]
        assert registry.is_connected("web-01")
        for token in registry.hub_server._token_store.created:  # type: ignore[union-attr]
            assert token not in task["error"]


class TestTheUpgradesList:
    async def test_lists_old_and_unversioned_agents_with_their_host(
        self, db: Database, host: dict[str, Any]
    ):
        from types import SimpleNamespace

        from homepilot.agent_hub import router as agents_router

        reg = AgentRegistry()
        reg.register("a-old", "web-01", {"agent_version": OLD_VERSION})
        reg.register("a-none", "no-inventory", {})
        reg.register("a-current", "fresh", {"agent_version": control_plane_version()})
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=Repository(db))))
        original = agents_router._get_registry
        agents_router._get_registry = lambda: reg  # type: ignore[assignment]
        try:
            rows = await agents_router.agent_upgrades(request)  # type: ignore[arg-type]
        finally:
            agents_router._get_registry = original  # type: ignore[assignment]
        got = {r["hostname"]: (r["agent_version"], r["behind"], r["host_id"]) for r in rows}
        assert got == {
            "web-01": (OLD_VERSION, True, host["id"]),
            "no-inventory": (None, None, None),
        }


# ---------------------------------------------------------------------------
# The journeys: the REAL binaries, the REAL hub, the REAL installer blocks, the
# REAL service - only the qemu-guest-agent channel is played by the test, which
# runs inside the guest sandbox exactly what the wrapper would run there.
# ---------------------------------------------------------------------------

_needs_go = pytest.mark.skipif(not _GO, reason=GO_SKIP_REASON)


def _build(out: Path, version: str) -> str:
    repo_root = Path(__file__).resolve().parents[1]
    env = {
        **os.environ,
        "CGO_ENABLED": "0",
        "GOCACHE": os.environ.get("GOCACHE") or str(out.parent / "gocache"),
        "GOFLAGS": "-mod=mod",
    }
    proc = subprocess.run(
        [str(_GO), "build", "-ldflags", f"-X main.version={version}", "-o", str(out), "."],
        cwd=str(repo_root / "agent" / "go"),
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if proc.returncode != 0:
        pytest.fail(f"could not build hp-agent {version}: {proc.stderr}")
    return str(out)


@pytest.fixture(scope="module")
def binaries(tmp_path_factory: pytest.TempPathFactory) -> dict[str, str]:
    if not _GO:
        pytest.skip(GO_SKIP_REASON)
    d = tmp_path_factory.mktemp("hp-agent-versions")
    broken = d / "hp-agent-broken"
    # A "new binary" that starts and exits at once: the failure an upgrade must
    # survive without leaving the host agentless.
    broken.write_text("#!/bin/sh\nexit 1\n")
    broken.chmod(0o755)
    return {
        "old": _build(d / "hp-agent-old", OLD_VERSION),
        "new": _build(d / "hp-agent-new", control_plane_version()),
        "broken": str(broken),
    }


class _GuestChannel:
    """The qemu-guest-agent channel into the rig's sandbox."""

    def __init__(self, rig: _Rig, install_dir: Path, new_binary: str) -> None:
        self.rig = rig
        self.install_dir = install_dir
        self.new_binary = new_binary
        self.staged = ""

    async def get_vm_current(self, node: str, vmid: int) -> dict[str, Any]:
        return {"data": {"status": "running"}}

    async def agent_ping(self, node: str, vmid: int) -> bool:
        return True

    async def agent_write_file(self, node: str, vmid: int, path: str, content: str) -> dict:
        assert path == _ENV_PATH
        self.staged = content
        return {"data": {}}

    async def agent_exec(self, *args: Any, **kwargs: Any) -> dict:
        return {"data": {}}

    async def agent_run(self, node: str, vmid: int, script: str, **_: Any) -> tuple[int, str, str]:
        if script == _rollback_script():
            shim = self.install_dir.parent / "shim"
            _roll_back_in(self.install_dir, self.rig.conf, shim)
            if "restart hp-agent" in (shim / "calls").read_text():
                await self.rig.start()
            return 0, "", ""
        # The install wrapper: binary swap, identity + env, then the restart.
        token = next(
            line.partition("=")[2] for line in self.staged.splitlines() if line.startswith("TOKEN=")
        )
        out = _swap(self.install_dir, Path(self.new_binary))
        out += self.rig.install(token)
        self.rig.extra_env = {"HP_AGENT_METRICS_INTERVAL": "2"}
        await self.rig.start()
        agent_id = (self.rig.conf / "agent.id").read_text().strip()
        return 0, out + f"{_AGENT_ID_MARKER}{agent_id}\n", ""


@pytest_asyncio.fixture
async def upgrade_rig(rig: _Rig, binaries: dict[str, str], tmp_path: Path):
    """An OLD agent installed and connected: version 3.0.0, no metrics."""
    install_dir = tmp_path / "usr-local-bin"
    install_dir.mkdir()
    shutil.copy2(binaries["old"], install_dir / "hp-agent")
    rig.binary = str(install_dir / "hp-agent")
    rig.install(await rig.bootstrap())
    rig.extra_env = {"HP_AGENT_METRICS_ENABLED": "false"}
    await rig.start()
    old = await rig.connected()
    assert old is not None, rig.log[-2000:]
    assert old["system_info"].get("agent_version") == OLD_VERSION
    repo = Repository(rig.state.database)
    host_id = await repo.create_host(
        hostname=old["hostname"], host_type="qemu", proxmox_id=900, node="pve-test", managed=True
    )
    yield rig, install_dir, old, dict(await repo.get_host(host_id) or {})


def _service(rig: _Rig, channel: _GuestChannel) -> AgentEnrollService:
    return AgentEnrollService(
        proxmox=channel,  # type: ignore[arg-type]
        task_repo=TaskRepository(rig.state.database),
        repo=Repository(rig.state.database),
        registry=rig.state.agent_registry,
        connect_wait_s=20.0,
        connect_interval=0.2,
    )


async def _metric_rows_since(rig: _Rig, hostname: str, since: float, timeout: float = 20.0) -> int:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        row = await rig.state.database.fetchone(
            "SELECT COUNT(*) AS n FROM metrics WHERE hostname = ? AND ts >= ?", (hostname, since)
        )
        if row and row["n"]:
            return int(row["n"])
        await asyncio.sleep(0.5)
    return 0


@_needs_go
class TestUpgradeJourneys:
    async def test_an_old_live_agent_is_upgraded_in_place(self, upgrade_rig, binaries):
        rig, install_dir, old, host = upgrade_rig
        import time

        hub_registry = rig.state.agent_registry
        channel = _GuestChannel(rig, install_dir, binaries["new"])
        service = _service(rig, channel)
        started = time.time()

        task_id = await service.start(
            host, "127.0.0.1", rig.state.settings.agent_hub_port, upgrade=True
        )
        for t in list(service._running_tasks):
            await t
        task = await service.task_repo.get_task(task_id)

        assert task is not None and task["status"] == "succeeded", (task, rig.log[-3000:])
        now = hub_registry.get(old["agent_id"])
        assert now is not None, "the same agent id is not connected"
        assert now.system_info.get("agent_version") == control_plane_version()
        records = await rig.records_for(old["hostname"])
        assert [r["agent_id"] for r in records] == [old["agent_id"]], "a second agent row"
        assert await _metric_rows_since(rig, old["hostname"], started), "no metrics after upgrade"
        assert (install_dir / "hp-agent.prev").exists()

    async def test_a_failed_upgrade_leaves_the_old_agent_working(self, upgrade_rig, binaries):
        rig, install_dir, old, host = upgrade_rig
        channel = _GuestChannel(rig, install_dir, binaries["broken"])
        service = _service(rig, channel)
        service.connect_wait_s = 8.0

        task_id = await service.start(
            host, "127.0.0.1", rig.state.settings.agent_hub_port, upgrade=True
        )
        for t in list(service._running_tasks):
            await t
        task = await service.task_repo.get_task(task_id)

        assert task is not None and task["status"] == "failed"
        assert "put the previous agent back" in task["error"], task["error"]
        back = await rig.connected(timeout=20)
        assert back is not None, f"the host was left with no agent: {rig.log[-3000:]}"
        assert back["agent_id"] == old["agent_id"]
        assert back["system_info"].get("agent_version") == OLD_VERSION
        records = await rig.records_for(old["hostname"])
        assert [r["agent_id"] for r in records] == [old["agent_id"]]
