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

#823 (found on prod, 3.11.0): an agent installed before agent.id existed had no
id on disk, so the installer minted a NEW one; verify refused it as a second
agent, and the rollback put back only the binary - the old binary came up under
the new id and config, and the hub held two records for one host. Now the
upgrade hands the installer the LIVE id (HP_AGENT_ID in the staged env file),
and the installer snapshots everything it may change so the rollback restores
it all. Teeth:
  * drop ``agent_id=`` from ``_env_file`` in ``_run`` -> ``test_an_old_agent_with
    _no_stored_id_is_upgraded_and_keeps_its_id`` fails (refused as a second agent);
  * restore the binary-only ``_rollback_script`` -> every ``TestTheRollback
    RestoresEverything`` test and the no-id failed-upgrade journey fail.
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
    _KEPT_TOKEN_MARKER,
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
from .test_install_agent_identity import _identity_block
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


def _snapshot(install_dir: Path, conf: Path, snapshot: Path, unit: Path, agent_id: str = "") -> str:
    """Run the SHIPPED upgrade-snapshot block, as the installer does before it
    touches anything."""
    script = "\n".join(
        [
            "set -euo pipefail",
            "SUDO=''",
            f'INSTALL_DIR="{install_dir}"',
            f'CONF_DIR="{conf}"',
            f'SNAPSHOT_DIR="{snapshot}"',
            f'UNIT_PATH="{unit}"',
            f'HP_AGENT_ID="{agent_id}"',
            _block(">>> upgrade-snapshot block", "<<< end upgrade-snapshot block"),
        ]
    )
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def _roll_back_in(
    install_dir: Path, conf: Path, shim: Path, snapshot: Path | None = None, ok: bool = True
) -> str:
    """Run the REAL rollback text in a sandbox; ``systemctl`` is a shim that
    records what it was asked to do. Without ``snapshot`` it points at an empty
    path: the rollback of an installer that took none."""
    shim.mkdir(exist_ok=True)
    (shim / "systemctl").write_text('#!/bin/sh\necho "$@" >> "$(dirname "$0")/calls"\n')
    (shim / "systemctl").chmod(0o755)
    env = {
        "PATH": f"{shim}:/usr/bin:/bin",
        "INSTALL_DIR": str(install_dir),
        "CONF_DIR": str(conf),
        "SNAPSHOT_DIR": str(snapshot or install_dir.parent / "no-snapshot"),
    }
    proc = subprocess.run(
        ["sh", "-c", _rollback_script()], capture_output=True, text=True, timeout=60, env=env
    )
    assert (proc.returncode == 0) == ok, (proc.returncode, proc.stderr)
    return proc.stdout


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

    def test_without_a_snapshot_rollback_restores_binary_and_a_missing_credential(
        self, tmp_path: Path
    ):
        """The fallback for an installer that took no snapshot."""
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
# #823: a failed upgrade leaves the host exactly as it was. The installer's real
# blocks run in a temp root in the installer's own order (resolve -> snapshot ->
# binary swap -> identity + env -> unit), then the real rollback text.
# ---------------------------------------------------------------------------

_LIVE_ID = "11111111-2222-4333-8444-555555555555"


class _Host:
    """A temp root holding one installed agent: binary, previous binary, config,
    credentials and unit, each with its own content and mode."""

    def __init__(self, root: Path, *, stored_id: bool = True) -> None:
        self.root = root
        self.bin = root / "usr-local-bin"
        self.conf = root / "etc-homepilot"
        self.unit = root / "etc-systemd-system" / "hp-agent.service"
        self.snapshot = root / "var-lib-hp-agent" / "upgrade-snapshot"
        self.shim = root / "shim"
        for d in (self.bin, self.conf, self.unit.parent):
            d.mkdir(parents=True)
        files: dict[Path, tuple[str, int]] = {
            self.bin / "hp-agent": ("old binary", 0o755),
            self.bin / "hp-agent.prev": ("older binary", 0o755),
            self.conf / "agent.token": ("old-per-agent-credential", 0o600),
            self.conf / "agent.token.prev": ("two-installs-ago", 0o600),
            self.unit: ("[Service]\nExecStart=/old/hp-agent\n", 0o644),
        }
        env = "HP_AGENT_HUB_HOST=192.0.2.10\nHP_AGENT_AUTH_TOKEN=consumed-bootstrap\n"
        if stored_id:
            env += f"HP_AGENT_ID={_LIVE_ID}\n"
            files[self.conf / "agent.id"] = (_LIVE_ID, 0o600)
        files[self.conf / "agent.env"] = (env, 0o640)
        for path, (text, mode) in files.items():
            path.write_text(text)
            path.chmod(mode)

    def state(self) -> dict[str, tuple[bytes, int]]:
        """Every file the installer may touch: bytes and mode, or absent."""
        out = {}
        for path in (
            self.bin / "hp-agent",
            self.bin / "hp-agent.prev",
            self.conf / "agent.env",
            self.conf / "agent.id",
            self.conf / "agent.token",
            self.conf / "agent.token.prev",
            self.unit,
        ):
            out[path.name] = (path.read_bytes(), _mode(path)) if path.exists() else None
        return out

    def upgrade(self, agent_id: str = _LIVE_ID) -> str:
        """The installer over this agent, minus download and service start."""
        new = self.root / "new-binary"
        new.write_text("new binary")
        script = "\n".join(
            [
                "set -euo pipefail",
                "SUDO=''",
                f'INSTALL_DIR="{self.bin}"',
                f'CONF_DIR="{self.conf}"',
                f'SNAPSHOT_DIR="{self.snapshot}"',
                f'UNIT_PATH="{self.unit}"',
                f'BIN="{new}"',
                'HUB_HOST="192.0.2.10"',
                "HUB_PORT=8443",
                'TOKEN="fresh-bootstrap"',
                'USE_TLS="false"',
                'TLS_PIN=""',
                'PRIVILEGED="false"',
                'ALLOW_PACKAGE_INSTALL="false"',
                f'WRITE_PREFIXES="{self.conf}"',
                f'WRITE_PREFIXES_ENV="{self.conf}"',
                # resolve block first, exactly as in the script
                _block(">>> agent-id resolve block", "<<< end agent-id resolve block"),
                _block(">>> upgrade-snapshot block", "<<< end upgrade-snapshot block"),
                _binary_swap_block(),
                _identity_block(),
                _block(">>> systemd-unit block", "<<< end systemd-unit block"),
            ]
        )
        env = {"PATH": "/usr/bin:/bin"}
        if agent_id:
            env["HP_AGENT_ID"] = agent_id
        proc = subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True, timeout=60, env=env
        )
        assert proc.returncode == 0, proc.stderr
        return proc.stdout

    def roll_back(self, ok: bool = True) -> str:
        return _roll_back_in(self.bin, self.conf, self.shim, self.snapshot, ok=ok)


class TestTheRollbackRestoresEverything:
    def test_a_failed_upgrade_restores_every_file_byte_for_byte(self, tmp_path: Path):
        host = _Host(tmp_path)
        before = host.state()

        out = host.upgrade()
        changed = host.state()
        # The installer really did change every one of them (else this proves nothing).
        for name in ("hp-agent", "hp-agent.prev", "agent.env", "agent.token", "agent.token.prev"):
            assert changed[name] != before[name], f"the installer left {name} alone"
        assert changed["hp-agent.service"] != before["hp-agent.service"]
        assert str(host.snapshot) in out

        assert _KEPT_TOKEN_MARKER not in host.roll_back()

        assert host.state() == before
        assert not host.snapshot.exists(), "the snapshot (old credentials) outlived the restore"
        assert (host.shim / "calls").read_text().split("\n")[:2] == [
            "daemon-reload",
            "restart hp-agent",
        ]

    def test_a_host_with_no_stored_id_gets_the_live_id_back_and_nothing_else(self, tmp_path: Path):
        """The prod host: no agent.id, no HP_AGENT_ID in agent.env. The upgrade
        installs with the live id; the rollback restores every file, and leaves
        agent.id holding the LIVE id - the old binary must come back as the same
        agent, not mint a new one at restart."""
        host = _Host(tmp_path, stored_id=False)
        before = host.state()
        assert before["agent.id"] is None

        out = host.upgrade()
        assert "Keeping the live agent id" in out
        assert (host.conf / "agent.id").read_text() == _LIVE_ID
        assert f"HP_AGENT_ID={_LIVE_ID}" in (host.conf / "agent.env").read_text()

        host.roll_back()

        after = host.state()
        assert after.pop("agent.id") == (_LIVE_ID.encode(), 0o600)
        before.pop("agent.id")
        assert after == before, "agent.env, credentials, binaries and unit are as they were"

    def test_a_credential_the_hub_already_issued_is_kept_and_said(self, tmp_path: Path):
        """The new agent got as far as the hub: it now holds the ONE valid
        credential (the hub replaced the old). Restoring the old one would lock
        the host out, so it stays - and the rollback says so."""
        host = _Host(tmp_path)
        before = host.state()
        host.upgrade()
        (host.conf / "agent.token").write_text("credential-the-hub-issued-the-new-agent")
        (host.conf / "agent.token").chmod(0o600)

        out = host.roll_back()

        assert _KEPT_TOKEN_MARKER in out
        after = host.state()
        assert after.pop("agent.token")[0] == b"credential-the-hub-issued-the-new-agent"
        before.pop("agent.token")
        assert after == before

    def test_a_restore_that_cannot_finish_keeps_the_snapshot_and_fails(self, tmp_path: Path):
        host = _Host(tmp_path)
        host.upgrade()
        shutil.rmtree(host.unit.parent)  # the unit's directory is gone: cannot restore it

        host.roll_back(ok=False)

        assert host.snapshot.exists(), "the only copy of the old agent was deleted"
        assert (host.bin / "hp-agent").read_text() == "old binary", "the rest is still restored"

    def test_an_install_over_nothing_takes_no_snapshot(self, tmp_path: Path):
        host = _Host(tmp_path)
        (host.bin / "hp-agent").unlink()
        assert "Saved" not in _snapshot(host.bin, host.conf, host.snapshot, host.unit)
        assert not host.snapshot.exists()


class TestTheInstallerKeepsTheLiveId:
    """The id an upgrade passes, through the REAL script (flag parsing included)
    and the resolve block."""

    @staticmethod
    def _whole_script(
        conf: Path,
        bindir: Path,
        *args: str,
        agent_id: str = "",
        keep_grant: str = "",
        privileged: str = "",
    ):
        shim = conf.parent / "shim"
        shim.mkdir(exist_ok=True)
        # Non-root runs reach for sudo; in the sandbox it just runs the command.
        (shim / "sudo").write_text('#!/bin/sh\nexec "$@"\n')
        (shim / "sudo").chmod(0o755)
        env = {
            "PATH": f"{shim}:/usr/bin:/bin",
            "HUB": "192.0.2.10:8443",
            "TOKEN": "t",
            "CONF_DIR": str(conf),
            "INSTALL_DIR": str(bindir),
            "SNAPSHOT_DIR": str(conf.parent / "snapshot"),
            "UNIT_PATH": str(conf.parent / "unit"),
            # Anything past the resolve block must not run: no network at all.
            "HP_API": "http://127.0.0.1:9",
        }
        if agent_id:
            env["HP_AGENT_ID"] = agent_id
        if keep_grant:
            env["KEEP_GRANT"] = keep_grant
        if privileged:
            env["PRIVILEGED"] = privileged
        return subprocess.run(
            ["bash", str(SCRIPT), *args], capture_output=True, text=True, timeout=60, env=env
        )

    @pytest.mark.parametrize("how", ["env", "flag"])
    def test_a_stored_id_that_differs_is_refused_before_anything_changes(
        self, tmp_path: Path, how: str
    ):
        host = _Host(tmp_path)
        before = host.state()
        other = "99999999-0000-4000-8000-000000000000"
        args = ("--agent-id", other) if how == "flag" else ()
        proc = self._whole_script(
            host.conf, host.bin, *args, agent_id=other if how == "env" else ""
        )
        assert proc.returncode != 0
        assert f"stored agent id is {_LIVE_ID}" in proc.stderr
        assert f"this install is for agent {other}" in proc.stderr
        assert "Nothing on the host was changed" in proc.stderr
        assert host.state() == before
        assert not (tmp_path / "snapshot").exists()

    def test_an_id_that_is_not_an_id_is_refused(self, tmp_path: Path):
        """Matched on the whole value: a newline would put a second variable
        into agent.env. No stored id, so no other check can catch it."""
        host = _Host(tmp_path, stored_id=False)
        proc = self._whole_script(host.conf, host.bin, "--agent-id", "x\nHP_AGENT_PRIVILEGED=true")
        assert proc.returncode != 0
        assert "--agent-id must be an agent id" in proc.stderr

    def test_the_passed_id_is_used_where_none_is_stored(self, tmp_path: Path):
        host = _Host(tmp_path, stored_id=False)
        host.upgrade(agent_id=_LIVE_ID)
        assert (host.conf / "agent.id").read_text() == _LIVE_ID

    def test_without_a_passed_id_a_bare_host_still_gets_a_new_one(self, tmp_path: Path):
        host = _Host(tmp_path, stored_id=False)
        host.upgrade(agent_id="")
        minted = (host.conf / "agent.id").read_text()
        assert minted and minted != _LIVE_ID


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


# ---------------------------------------------------------------------------
# #841: an upgrade keeps the host's grant. The upgrade task's REAL staged env
# file drives the installer's real blocks (keep-grant -> grant -> resolve ->
# identity + env -> unit) over a host first installed privileged with custom
# write prefixes by the same blocks.
# ---------------------------------------------------------------------------

_GRANT_KEYS = ("HP_AGENT_PRIVILEGED", "HP_AGENT_ALLOW_PACKAGE_INSTALL", "HP_AGENT_WRITE_PREFIXES")


def _grant_blocks(root: Path, preamble: dict[str, str]) -> str:
    """The installer from its keep-grant block to its unit, in script order,
    with ``preamble`` standing for what the script's top sets (its defaults,
    then the sourced env file / flags)."""
    lines = ["set -euo pipefail", "SUDO=''"]
    lines += [f'{k}="{v}"' for k, v in preamble.items()]
    lines += [
        f'CONF_DIR="{root / "etc-homepilot"}"',
        f'INSTALL_DIR="{root / "usr-local-bin"}"',
        f'UNIT_PATH="{root / "hp-agent.service"}"',
        'HUB_HOST="192.0.2.10"',
        "HUB_PORT=8443",
        'USE_TLS="false"',
        'TLS_PIN=""',
        'mkdir -p "$CONF_DIR"',
        _block(">>> keep-grant block", "<<< end keep-grant block"),
        _block(">>> grant block", "<<< end grant block"),
        _identity_block(),
        _block(">>> systemd-unit block", "<<< end systemd-unit block"),
    ]
    proc = subprocess.run(
        ["bash", "-c", "\n".join(lines)],
        capture_output=True,
        text=True,
        timeout=60,
        env={"PATH": "/usr/bin:/bin"},
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def _grant_of(root: Path) -> tuple[dict[str, str], str]:
    env = {}
    for line in (root / "etc-homepilot" / "agent.env").read_text().splitlines():
        key, _, value = line.partition("=")
        if key in _GRANT_KEYS:
            env[key] = value
    return env, (root / "hp-agent.service").read_text()


# What the script's top leaves set before the keep-grant block when nobody
# states a grant: its defaults, and GRANT_EXPLICIT empty.
_NO_GRANT_STATED = {
    "PRIVILEGED": "false",
    "ALLOW_PACKAGE_INSTALL": "false",
    "WRITE_PREFIXES": "",
    "GRANT_EXPLICIT": "",
    "KEEP_GRANT": "false",
}


class TestTheUpgradeKeepsTheGrant:
    async def test_a_privileged_agent_with_custom_prefixes_keeps_its_grant(
        self,
        service: AgentEnrollService,
        host: dict[str, Any],
        registry: AgentRegistry,
        proxmox: AsyncMock,
        tmp_path: Path,
    ):
        """Revert-check: drop ``keep_grant=upgrade`` from ``_run`` (or the
        keep-grant block) and the upgraded agent.env says privileged=false with
        the default prefixes, and the unit runs as hp-agent."""
        granted = {
            **_NO_GRANT_STATED,
            "PRIVILEGED": "true",
            "WRITE_PREFIXES": "/etc/homepilot /srv/app-config /etc/nginx",
            "GRANT_EXPLICIT": "1",
            "TOKEN": "first-install-token",
        }
        _grant_blocks(tmp_path, granted)
        before = _grant_of(tmp_path)
        assert before[0]["HP_AGENT_PRIVILEGED"] == "true"
        assert "User=root" in before[1]

        registry.register(GUEST_AGENT_ID, "web-01", {})
        guest = _UpgradingGuest(registry, comes_back=True)
        _wire(proxmox, guest)
        task = await _run(service, host, upgrade=True)
        assert task["status"] == "succeeded", task["error"]
        staged = dict(
            line.split("=", 1) for line in guest.written[0][1].splitlines() if "=" in line
        )
        # `set -a; . env`: the staged values land on top of the script's defaults.
        staged.pop("HP_AGENT_ID", None)  # the live id is another test's subject
        out = _grant_blocks(tmp_path, {**_NO_GRANT_STATED, **staged})

        assert "Keeping this host's grant: privileged=true" in out
        assert _grant_of(tmp_path) == before, "the upgrade changed the host's grant"

    async def test_an_unprivileged_agent_stays_unprivileged(self, tmp_path: Path):
        _grant_blocks(tmp_path, {**_NO_GRANT_STATED, "TOKEN": "t"})
        before = _grant_of(tmp_path)
        _grant_blocks(tmp_path, {**_NO_GRANT_STATED, "TOKEN": "t2", "KEEP_GRANT": "true"})
        assert _grant_of(tmp_path) == before

    def test_keep_grant_with_no_agent_env_refuses(self, tmp_path: Path):
        script = "\n".join(
            [
                "set -euo pipefail",
                "SUDO=''",
                *[f'{k}="{v}"' for k, v in {**_NO_GRANT_STATED, "KEEP_GRANT": "true"}.items()],
                f'CONF_DIR="{tmp_path}"',
                _block(">>> keep-grant block", "<<< end keep-grant block"),
            ]
        )
        proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60)
        assert proc.returncode != 0
        assert "no " in proc.stderr and "agent.env to keep the grant from" in proc.stderr


class TestAStatedGrantWins:
    """Through the REAL script top (environment and flag parsing): --keep-grant
    keeps the host's grant unless the caller states one."""

    @staticmethod
    def _run(tmp_path: Path, *args: str, **env: str) -> str:
        conf = tmp_path / "etc-homepilot"
        conf.mkdir()
        (conf / "agent.env").write_text(
            "HP_AGENT_PRIVILEGED=true\nHP_AGENT_WRITE_PREFIXES=/etc/homepilot:/srv/app-config\n"
        )
        (tmp_path / "bin").mkdir()
        proc = TestTheInstallerKeepsTheLiveId._whole_script(conf, tmp_path / "bin", *args, **env)
        # It stops at the download (nothing listens): everything before it ran.
        assert "Downloading" in proc.stdout, proc.stderr
        return proc.stdout

    @pytest.mark.parametrize("how", ["env", "flag"])
    def test_keep_grant_reads_the_hosts_grant(self, tmp_path: Path, how: str):
        args = ("--keep-grant",) if how == "flag" else ()
        env = {"keep_grant": "true"} if how == "env" else {}
        out = self._run(tmp_path, *args, **env)
        assert "Keeping this host's grant: privileged=true" in out
        assert "/srv/app-config" in out

    @pytest.mark.parametrize(
        ("args", "env"),
        [
            (("--write-prefix", "/opt/homepilot"), {}),
            (("--privileged",), {}),
            ((), {"privileged": "false"}),
        ],
    )
    def test_a_stated_grant_wins(self, tmp_path: Path, args, env):
        out = self._run(tmp_path, "--keep-grant", *args, **env)
        assert "Keeping this host's grant" not in out


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

    @property
    def snapshot(self) -> Path:
        return self.install_dir.parent / "upgrade-snapshot"

    async def agent_run(self, node: str, vmid: int, script: str, **_: Any) -> tuple[int, str, str]:
        if script == _rollback_script():
            shim = self.install_dir.parent / "shim"
            out = _roll_back_in(self.install_dir, self.rig.conf, shim, self.snapshot)
            if "restart hp-agent" in (shim / "calls").read_text():
                await self.rig.start()
            return 0, out, ""
        # The install wrapper: `set -a; . env` hands the staged values to the
        # installer's environment; then snapshot, binary swap, identity + env,
        # and the restart.
        staged = dict(line.split("=", 1) for line in self.staged.splitlines() if "=" in line)
        token, agent_id = staged["TOKEN"], staged.get("HP_AGENT_ID", "")
        unit = self.install_dir.parent / "hp-agent.service"
        out = _snapshot(self.install_dir, self.rig.conf, self.snapshot, unit, agent_id)
        out += _swap(self.install_dir, Path(self.new_binary))
        out += self.rig.install(token, agent_id=agent_id)
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

    async def test_an_old_agent_with_no_stored_id_is_upgraded_and_keeps_its_id(
        self, upgrade_rig, binaries
    ):
        """#823, the prod host: the live agent's id is on no file on the host.
        The upgrade hands the installer that id, so the SAME agent comes back on
        the new version - one attempt, one record."""
        rig, install_dir, old, host = upgrade_rig
        _forget_stored_id(rig)
        channel = _GuestChannel(rig, install_dir, binaries["new"])
        service = _service(rig, channel)

        task_id = await service.start(
            host, "127.0.0.1", rig.state.settings.agent_hub_port, upgrade=True
        )
        for t in list(service._running_tasks):
            await t
        task = await service.task_repo.get_task(task_id)

        assert task is not None and task["status"] == "succeeded", (task, rig.log[-3000:])
        now = rig.state.agent_registry.get(old["agent_id"])
        assert now is not None, "the live agent's id is not the one connected"
        assert now.system_info.get("agent_version") == control_plane_version()
        assert (rig.conf / "agent.id").read_text().strip() == old["agent_id"]
        records = await rig.records_for(old["hostname"])
        assert [r["agent_id"] for r in records] == [old["agent_id"]], "a second agent row"

    async def test_a_failed_upgrade_of_an_agent_with_no_stored_id_puts_it_all_back(
        self, upgrade_rig, binaries
    ):
        """The same host, a new binary that never starts: the rollback restores
        the binary, agent.env and the credentials as they were, and the old
        agent returns as the SAME agent (agent.id now holds its live id)."""
        rig, install_dir, old, host = upgrade_rig
        _forget_stored_id(rig)
        before = {
            name: (rig.conf / name).read_bytes()
            for name in ("agent.env", "agent.token")
            if (rig.conf / name).exists()
        }
        old_binary = (install_dir / "hp-agent").read_bytes()
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
        assert "put the previous agent back and it is connected again" in task["error"], task[
            "error"
        ]
        back = await rig.connected(timeout=20)
        assert back is not None, f"the host was left with no agent: {rig.log[-3000:]}"
        assert back["agent_id"] == old["agent_id"]
        assert back["system_info"].get("agent_version") == OLD_VERSION
        assert (install_dir / "hp-agent").read_bytes() == old_binary
        after = {name: (rig.conf / name).read_bytes() for name in before}
        assert after == before, "agent.env / agent.token are not as they were"
        assert (rig.conf / "agent.id").read_text().strip() == old["agent_id"]
        assert not channel.snapshot.exists()
        records = await rig.records_for(old["hostname"])
        assert [r["agent_id"] for r in records] == [old["agent_id"]], "a second agent row"


def _forget_stored_id(rig: _Rig) -> None:
    """Put the host in the state the June agent left it in: the agent runs under
    an id no file on the host names (no agent.id, no HP_AGENT_ID in agent.env).
    The running process keeps its id; only what an installer can read changes."""
    env = rig.conf / "agent.env"
    kept = [
        line
        for line in env.read_text().splitlines()
        if not line.startswith(("HP_AGENT_ID=", "HP_AGENT_ID_FILE="))
    ]
    env.write_text("\n".join(kept) + "\n")
    (rig.conf / "agent.id").unlink()
