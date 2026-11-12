"""Re-installing an agent over a credential an earlier install left behind (#815).

Found on prod (3.10.0): ``POST /agents/install`` on guests that had an agent
months ago rewrote ``agent.env`` (with the fresh one-time bootstrap token) and
``agent.id``, installed the new binary and started it - and left the OLD
``agent.token`` in place. That file held what a #348-era hub had handed back: the
shared fleet token. The agent prefers its persisted credential, presented it, and
the hub refused it ("no enrolment window is open"); the bootstrap token minted for
the install was never used and the task failed with "check journalctl". A guest
that never had an agent enrolled fine.

Three fixes, each held here or beside its code:

* the installer moves a persisted ``agent.token`` aside to ``agent.token.prev``
  before it starts the agent with the new token (``TestInstallerMovesTheOld
  CredentialAside`` runs the SHIPPED script's identity block);
* the agent, refused for ENROLMENT on one credential, tries the other one once:
  the configured token after a refused stored credential, the kept
  ``agent.token.prev`` after a refused configured token (agent/go
  reenrol_fallback_test.go, and the journeys below on the real binary);
* the install task names a refused credential instead of "check journalctl"
  (tests/test_agent_enroll.py ``TestAStaleCredentialIsNamed``).

The journeys drive the REAL ``hp-agent`` binary against a REAL hub, configured by
the REAL installer identity block, with the enrolment window CLOSED throughout.

Teeth (each proven by reverting the named fix):
  * revert only the agent's enrolment-refusal fallback (rebuilt binary) ->
    ``test_a_host_left_stuck_by_an_old_installer_recovers`` (never connects) and
    ``test_reinstall_with_the_shared_token_over_a_healthy_host_keeps_it`` (locked
    out, then peer-banned) fail;
  * revert only the installer move-aside -> ``TestInstallerMovesTheOldCredential
    Aside`` fails, and every journey fails on its outcome: the healthy reinstall
    keeps the OLD credential and never spends the operator's new token, and the
    old credential is not kept as agent.token.prev;
  * revert both -> ``test_reinstall_over_an_old_installs_credential_connects``
    never connects: the prod failure, reproduced.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import subprocess
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from homepilot.agent_hub.enrolment_window import is_open as enrolment_window_is_open
from homepilot.agent_hub.server import (
    ENROLMENT_WINDOW_CLOSED_ERROR,
    HOST_HOLDS_CREDENTIAL_ERROR,
)
from homepilot.agent_hub.tokens import hash_token
from homepilot.db.repository import Repository

from .go_toolchain import GO as _GO
from .go_toolchain import GO_SKIP_REASON
from .test_install_agent_identity import _identity_block

_needs_go = pytest.mark.skipif(not _GO, reason=GO_SKIP_REASON)


def _install(conf: Path, token: str, *, hub_port: int = 8443, pin: str = "") -> str:
    """Run the SHIPPED installer's identity block against ``conf``; return its output."""
    script = "\n".join(
        [
            "set -euo pipefail",
            "SUDO=''",
            f'CONF_DIR="{conf}"',
            'HUB_HOST="127.0.0.1"',
            f"HUB_PORT={hub_port}",
            # The token reaches the block through the environment, as the
            # zero-touch wrapper hands it over - never through an argv.
            'TOKEN="$HP_TEST_TOKEN"',
            f'USE_TLS="{"true" if pin else "false"}"',
            f'TLS_PIN="{pin}"',
            'PRIVILEGED="false"',
            'ALLOW_PACKAGE_INSTALL="false"',
            f'WRITE_PREFIXES_ENV="{conf}/"',
            'mkdir -p "$CONF_DIR"',
            _identity_block(),
        ]
    )
    proc = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=60,
        env={"PATH": "/usr/bin:/bin", "HP_TEST_TOKEN": token},
    )
    assert proc.returncode == 0, f"identity block failed: {proc.stderr}"
    output = proc.stdout + proc.stderr
    assert token not in output, "the installer printed the enrolment token"
    return output


def _mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


class TestInstallerMovesTheOldCredentialAside:
    """Revert-check: drop the agent.token move-aside from the identity block in
    scripts/install-agent.sh and every test here fails."""

    def test_a_reinstall_moves_the_old_credential_to_prev(self, tmp_path: Path):
        (tmp_path / "agent.token").write_text("old-install-credential")
        (tmp_path / "agent.token").chmod(0o640)

        out = _install(tmp_path, "fresh-bootstrap-token")

        assert not (tmp_path / "agent.token").exists(), (
            "the old credential is still where the agent would prefer it over the new token"
        )
        prev = tmp_path / "agent.token.prev"
        assert prev.read_text() == "old-install-credential", "moved aside, never deleted"
        assert _mode(prev) == 0o600
        assert "agent.token.prev" in out, "the move is announced, not silent"
        assert "HP_AGENT_AUTH_TOKEN=fresh-bootstrap-token" in (tmp_path / "agent.env").read_text()

    def test_only_one_previous_copy_is_kept(self, tmp_path: Path):
        (tmp_path / "agent.token.prev").write_text("two-installs-ago")
        (tmp_path / "agent.token").write_text("one-install-ago")

        _install(tmp_path, "fresh-bootstrap-token")

        assert (tmp_path / "agent.token.prev").read_text() == "one-install-ago"
        assert sorted(p.name for p in tmp_path.iterdir() if p.name.startswith("agent.token")) == [
            "agent.token.prev"
        ]

    def test_a_first_install_leaves_no_prev(self, tmp_path: Path):
        _install(tmp_path, "fresh-bootstrap-token")
        assert not (tmp_path / "agent.token.prev").exists()
        assert not (tmp_path / "agent.token").exists()


class TestTheHubRefusalsTheAgentRecognises:
    def test_both_enrolment_refusals_carry_the_prefix_the_agent_matches(self):
        """The Go agent recognises an enrolment refusal by its "enrolment refused"
        prefix (isEnrolmentRefusal); rewording either here silently disables
        its fallback. Both stay free of "auth" (the other rotation's trigger)."""
        for wire in (ENROLMENT_WINDOW_CLOSED_ERROR, HOST_HOLDS_CREDENTIAL_ERROR):
            assert wire.lower().startswith("enrolment refused"), wire
            assert "auth" not in wire.lower(), wire


class TestInstallAgainIsOffered:
    """The Hosts > Agents "Waiting to join" line offers Install again for a host
    HomePilot can re-install through qemu-guest-agent (#815 scope 4)."""

    async def test_a_knocking_qemu_guest_names_its_host(self, tmp_path: Path):
        from homepilot.agent_hub import join_requests
        from homepilot.db.connection import Database
        from homepilot.db.migrations import run_migrations

        db = Database(str(tmp_path / "join.db"))
        await db.connect()
        try:
            await run_migrations(db)
            repo = Repository(db)
            guest = await repo.create_host(
                hostname="old-guest", host_type="qemu", proxmox_id=120, node="pve1", managed=True
            )
            await repo.create_host(
                hostname="a-container", host_type="lxc", proxmox_id=121, node="pve1"
            )
            for name in ("old-guest", "a-container", "nobody-knows"):
                await join_requests.record_attempt(
                    repo, hostname=name, peer="192.0.2.5", code="window_closed", reason="refused"
                )
            got = {
                a["hostname"]: a["install_host_id"]
                for a in (await join_requests.payload(repo))["attempts"]
            }
        finally:
            await db.close()
        assert got == {"old-guest": guest, "a-container": None, "nobody-knows": None}


# ---------------------------------------------------------------------------
# The journeys: the real binary, the real hub, the real installer block.
# ---------------------------------------------------------------------------


class _Rig:
    def __init__(self, state: Any, repo: Repository, conf: Path, binary: str) -> None:
        self.state = state
        self.repo = repo
        self.conf = conf
        self.binary = binary
        self.proc: subprocess.Popen[str] | None = None
        self.log = ""
        # Test-only overrides on top of agent.env (e.g. a faster metrics tick).
        self.extra_env: dict[str, str] = {}

    @property
    def hub(self) -> Any:
        return self.state.agent_hub

    @property
    def shared_token(self) -> str:
        return str(self.state.settings.agent_hub_auth_token)

    async def bootstrap(self) -> str:
        return str(await self.hub._token_store.create(minted_by="reinstall journey"))

    def install(self, token: str) -> str:
        return _install(
            self.conf,
            token,
            hub_port=self.state.settings.agent_hub_port,
            pin="sha256:" + self.hub.cert_fingerprint,
        )

    async def start(self) -> None:
        """`systemctl restart hp-agent`: the unit's EnvironmentFile is agent.env.

        Waits for the stopped process to leave the registry, so a later
        ``connected()`` can only be answered by the process started here."""
        self.stop()
        for live in self.hub.registry.list_connected():
            await self.gone(str(live["agent_id"]))
        env = {"PATH": "/usr/bin:/bin", "HOME": str(self.conf)}
        for line in (self.conf / "agent.env").read_text().splitlines():
            key, sep, value = line.partition("=")
            if sep:
                env[key] = value
        env["HP_AGENT_CONFIG_DIR"] = str(self.conf)
        env["HP_AGENT_HEARTBEAT_INTERVAL"] = "5"
        env.update(self.extra_env)
        self.proc = subprocess.Popen(
            [self.binary], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
        )

    def stop(self) -> None:
        if self.proc is None:
            return
        self.proc.terminate()
        try:
            self.log += self.proc.communicate(timeout=10)[0] or ""
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.log += self.proc.communicate(timeout=10)[0] or ""
        self.proc = None

    async def connected(self, timeout: float = 25.0) -> dict[str, Any] | None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            live = self.hub.registry.list_connected()
            # Connected, AND the credential it holds is on disk: the agent writes
            # agent.token just after the hub's ack, so the two can be a beat apart.
            token = self.conf / "agent.token"
            if live and token.exists() and token.read_text().strip():
                return dict(live[0])
            await asyncio.sleep(0.2)
        return None

    async def gone(self, agent_id: str, timeout: float = 15.0) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if self.hub.registry.get(agent_id) is None:
                return
            await asyncio.sleep(0.2)
        raise AssertionError(f"agent {agent_id} never left the registry")

    async def records_for(self, hostname: str) -> list[dict[str, Any]]:
        rows = await self.state.database.fetchall(
            "SELECT agent_id, hostname, credential_hash, revoked_at FROM agents WHERE hostname = ?",
            (hostname,),
        )
        return [dict(r) for r in rows]

    def credential(self) -> str:
        return (self.conf / "agent.token").read_text().strip()


@pytest_asyncio.fixture
async def rig(hp_agent_binary: str, tmp_path: Path) -> AsyncIterator[_Rig]:
    from .test_agent_hub_tls_journey import _hub_on

    # Under $HOME: the artifacts-dir guard refuses /tmp (the other journeys' rule).
    hp_dir = tempfile.mkdtemp(dir=str(Path.home()), prefix=".hp-test-reenrol-")
    state, _settings = await _hub_on(hp_dir)
    repo = Repository(state.database)
    # The fleet already has a member, so nothing here is a first rollout (which
    # the shared token may enrol without a window): every join below must be
    # earned by a credential, with the window shut.
    await repo.set_agent_credential("fleet-member", "some-other-host", hash_token("x" * 64))
    conf = tmp_path / "etc-homepilot"
    conf.mkdir()
    r = _Rig(state, repo, conf, hp_agent_binary)
    try:
        yield r
    finally:
        r.stop()
        with contextlib.suppress(Exception):
            await state.agent_hub.stop()
        with contextlib.suppress(Exception):
            await state.database.close()
        shutil.rmtree(hp_dir, ignore_errors=True)


async def _first_install(rig: _Rig) -> dict[str, Any]:
    rig.install(await rig.bootstrap())
    await rig.start()
    agent = await rig.connected()
    assert agent is not None, f"a first install never connected: {rig.log[-2000:]}"
    return agent


def _assert_no_secret_logged(rig: _Rig, *tokens: str) -> None:
    rig.stop()
    for token in tokens:
        assert token not in rig.log, "a credential reached the agent's log"


@_needs_go
class TestReinstallJourneys:
    async def test_reinstall_over_an_old_installs_credential_connects(self, rig: _Rig):
        """The prod journey: install -> connected; the hub forgets the agent and
        the host's agent.token holds what a #348-era hub handed back (the shared
        token); Install again with a new bootstrap token -> connected. The window
        is never opened."""
        agent = await _first_install(rig)
        hostname, agent_id = agent["hostname"], agent["agent_id"]
        rig.stop()
        await rig.gone(agent_id)

        # Forget the agent on the hub (what DELETE /agents/{id} does) and plant
        # the old install's credential.
        await rig.repo.revoke_agent_credential(agent_id)
        assert await rig.repo.delete_agent(agent_id)
        (rig.conf / "agent.token").write_text(rig.shared_token)
        (rig.conf / "agent.token").chmod(0o600)

        bootstrap = await rig.bootstrap()
        rig.install(bootstrap)
        await rig.start()
        back = await rig.connected()

        assert back is not None, (
            f"the reinstalled agent never connected with its new bootstrap token: {rig.log[-3000:]}"
        )
        assert back["hostname"] == hostname
        assert not await enrolment_window_is_open(rig.repo), "the window was opened"
        minted = rig.credential()
        assert minted not in (rig.shared_token, bootstrap), "no per-agent credential was minted"
        assert _mode(rig.conf / "agent.token") == 0o600
        assert (rig.conf / "agent.token.prev").read_text() == rig.shared_token
        records = await rig.records_for(hostname)
        assert len(records) == 1 and records[0]["credential_hash"] == hash_token(minted)
        _assert_no_secret_logged(rig, rig.shared_token, bootstrap, minted)

    async def test_a_host_left_stuck_by_an_old_installer_recovers(self, rig: _Rig):
        """The state prod hosts are in now: an installer that did NOT move the
        old credential aside (hand-run, or pre-fix) left agent.token holding the
        shared token beside a fresh bootstrap token. The agent itself must try
        the bootstrap token once instead of knocking with the old one for ever.

        Revert-check: drop the isEnrolmentRefusal case in the agent's register()
        and this fails."""
        agent = await _first_install(rig)
        rig.stop()
        await rig.gone(agent["agent_id"])
        await rig.repo.revoke_agent_credential(agent["agent_id"])
        await rig.repo.delete_agent(agent["agent_id"])

        bootstrap = await rig.bootstrap()
        rig.install(bootstrap)
        # What the old installer left: the old credential still in place.
        (rig.conf / "agent.token.prev").rename(rig.conf / "agent.token")
        (rig.conf / "agent.token").write_text(rig.shared_token)
        await rig.start()

        back = await rig.connected()
        assert back is not None, f"the stuck host never used its bootstrap token: {rig.log[-3000:]}"
        assert not await enrolment_window_is_open(rig.repo)
        assert rig.credential() not in (rig.shared_token, bootstrap)
        _assert_no_secret_logged(rig, rig.shared_token, bootstrap)

    async def test_reinstall_over_a_healthy_agent_stays_one_connected_agent(self, rig: _Rig):
        """The upgrade case: Install again over a healthy enrolled agent with a
        new bootstrap token -> still connected, the same agent id, ONE record for
        the host, a new credential, and the restart after that reconnects."""
        agent = await _first_install(rig)
        hostname, agent_id = agent["hostname"], agent["agent_id"]
        first = rig.credential()

        # The installer runs while the agent is live, then restarts it.
        bootstrap = await rig.bootstrap()
        rig.install(bootstrap)
        await rig.start()
        back = await rig.connected()
        assert back is not None, f"a healthy host was locked out by a reinstall: {rig.log[-3000:]}"
        assert back["agent_id"] == agent_id
        second = rig.credential()
        assert second not in (first, bootstrap)
        records = await rig.records_for(hostname)
        assert [r["agent_id"] for r in records] == [agent_id]
        assert records[0]["credential_hash"] == hash_token(second)

        # And the next restart reconnects on that credential.
        await rig.start()
        assert await rig.connected() is not None, rig.log[-3000:]
        assert not await enrolment_window_is_open(rig.repo)
        _assert_no_secret_logged(rig, bootstrap, first, second)

    async def test_reinstall_with_the_shared_token_over_a_healthy_host_keeps_it(self, rig: _Rig):
        """#789 guard: an operator re-runs the installer over a healthy host with
        the SHARED token (the hub-token one-liner). The installer moves the live
        credential aside; the hub refuses the shared token because the host holds
        its own credential (#683). The agent must fall back to the kept
        credential, put it back, and stay connected across a restart.

        Revert-check: drop the agent.token.prev fallback (storedCredential) or the
        enrolment-refusal case in register() and this host is locked out."""
        agent = await _first_install(rig)
        hostname, agent_id = agent["hostname"], agent["agent_id"]
        first = rig.credential()

        rig.install(rig.shared_token)
        await rig.start()
        back = await rig.connected()
        assert back is not None, f"the healthy host was locked out: {rig.log[-3000:]}"
        assert back["agent_id"] == agent_id
        assert rig.credential() == first, "the kept credential is back in agent.token"
        assert _mode(rig.conf / "agent.token") == 0o600

        await rig.start()
        assert await rig.connected() is not None, rig.log[-3000:]
        records = await rig.records_for(hostname)
        assert [r["agent_id"] for r in records] == [agent_id]
        assert records[0]["credential_hash"] == hash_token(first)
        assert not await enrolment_window_is_open(rig.repo)
        # #683: once the host holds its credential again the shared token is
        # removed from agent.env.
        assert rig.shared_token not in (rig.conf / "agent.env").read_text()
        _assert_no_secret_logged(rig, rig.shared_token, first)
        assert os.path.exists(rig.conf / "agent.token.prev")
