"""A plaintext fleet moves onto TLS without anyone touching a host (#468).

This is the other half of #468. Keeping an existing fleet on plaintext stops the
upgrade stranding it, but the way OFF plaintext must not be "edit
/etc/homepilot/agent.env on every managed host and restart each agent" - that is
precisely the manual sprawl ADR-004 exists to abolish, and on LXC or bare metal
it means a physical visit.

So the hub pushes the transport down the channel it already has. These gates
drive the REAL `hp-agent` binary through the whole journey and assert the
OUTCOME an operator cares about - the agent comes back over a genuinely
verified TLS connection - rather than that an endpoint returned 200.

The negative cases matter as much: a migration that would strand an offline
agent must REFUSE, and an unusable pin must never be persisted, because an agent
holding a pin it cannot parse can never reach the hub that would fix it.

Skipped when no Go toolchain is present (set ``HP_GO_BIN`` to point at one).
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from homepilot.agent_hub.migrate_tls import (
    MigrationRefusedError,
    migrate_fleet_to_tls,
    plan_migration,
)
from homepilot.agent_hub.tls_mode import MODE_LEGACY_PLAINTEXT, MODE_TLS, SETTING_KEY
from homepilot.db.repository import Repository

_GO = os.environ.get("HP_GO_BIN") or shutil.which("go") or ""
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not _GO, reason="Go toolchain not available"),
]


@pytest.fixture
def agent_binary(hp_agent_binary: str) -> str:
    return hp_agent_binary


@pytest.fixture
def hp_dir() -> Iterator[str]:
    path = tempfile.mkdtemp(dir=str(Path.home()), prefix=".hp-test-migrate-")
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


async def _legacy_install(hp_dir: str):
    """A control plane whose fleet predates TLS, exactly as an upgrade finds it.

    The agents row is seeded BEFORE create_app_state so the install is judged
    legacy on first contact - the shape a real 2.7.1 upgrade has.
    """
    import socket

    from homepilot.app_state import create_app_state
    from homepilot.config import Settings
    from homepilot.db.connection import Database
    from homepilot.db.migrations import run_migrations

    db = Database(str(Path(hp_dir) / "homepilot.db"))
    await db.connect()
    await run_migrations(db)
    await db.execute(
        "INSERT INTO agents (agent_id, hostname, connected) VALUES (?, ?, 0)",
        ("pre-tls-agent", "legacy-host"),
    )
    await db.conn.commit()
    await db.close()

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])

    settings = Settings(
        secret_key="test-secret-key-for-pytest-only-not-for-production",
        data_dir=hp_dir,
        artifacts_dir=os.path.join(hp_dir, "artifacts"),
        agent_hub_host="127.0.0.1",
        agent_hub_port=port,
    )
    state = await create_app_state(settings)
    assert state.agent_hub is not None
    assert state.agent_hub.tls_enabled is False, "a legacy install must start on plaintext"
    await state.agent_hub.start()
    return state, settings


def _spawn_plaintext_agent(binary: str, conf: Path, settings) -> subprocess.Popen[str]:
    """An agent enrolled the way a pre-TLS install enrolled it: no TLS in its
    environment at all, which is the state that made the flip unsurvivable."""
    conf.mkdir(parents=True, exist_ok=True)
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(conf),
        "HP_AGENT_HUB_HOST": "127.0.0.1",
        "HP_AGENT_HUB_PORT": str(settings.agent_hub_port),
        "HP_AGENT_AUTH_TOKEN": settings.agent_hub_auth_token,
        "HP_AGENT_TOKEN_FILE": str(conf / "agent.token"),
        "HP_AGENT_ID_FILE": str(conf / "agent.id"),
        "HP_AGENT_TRANSPORT_FILE": str(conf / "agent.transport"),
        "HP_AGENT_HEARTBEAT_INTERVAL": "5",
        "HP_AGENT_METRICS_ENABLED": "false",
    }
    return subprocess.Popen(
        [binary], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )


async def _wait_connected(state, timeout: float = 20.0) -> list:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        connected = state.agent_registry.list_connected()
        if connected:
            return connected
        await asyncio.sleep(0.2)
    return []


async def _wait_disconnected(state, timeout: float = 30.0) -> bool:
    """Wait until the hub holds no live connection.

    Needed because terminating the agent process does not instantly clear the
    registry: asserting "connected" too soon reads the DEPARTING connection and
    mistakes it for the new one.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if not state.agent_registry.list_connected():
            return True
        await asyncio.sleep(0.2)
    return False


async def _shutdown(state, proc: subprocess.Popen[str] | None) -> str:
    output = ""
    if proc is not None:
        proc.terminate()
        try:
            output = proc.communicate(timeout=10)[0] or ""
        except subprocess.TimeoutExpired:
            proc.kill()
            output = proc.communicate(timeout=10)[0] or ""
    if state.agent_hub is not None:
        await state.agent_hub.stop()
    await state.database.close()
    return output


async def test_the_real_agent_adopts_a_pushed_transport(agent_binary: str, hp_dir: str):
    """The journey: a plaintext agent is told about TLS and holds the pin.

    The evidence is what the agent WROTE - a transport file carrying the hub's
    actual fingerprint - not that the push returned success. A binary that acked
    and did nothing would pass the weaker check and strand itself on the next
    restart.
    """
    state, settings = await _legacy_install(hp_dir)
    conf = Path(hp_dir) / "agentconf"
    proc = _spawn_plaintext_agent(agent_binary, conf, settings)
    try:
        connected = await _wait_connected(state)
        assert connected, "the plaintext agent never reached the hub"

        report = await migrate_fleet_to_tls(
            Repository(state.database),
            state.agent_registry,
            state.agent_hub,
            settings.data_dir,
            settings=settings,
            force=True,  # the seeded pre-TLS row is deliberately offline
        )

        assert len(report["adopted"]) == 1
        transport_file = conf / "agent.transport"
        deadline = asyncio.get_running_loop().time() + 10
        while not transport_file.exists():
            if asyncio.get_running_loop().time() > deadline:
                pytest.fail("the agent acked the push but never persisted the transport")
            await asyncio.sleep(0.2)

        stored = json.loads(transport_file.read_text())
        assert stored["tls"] is True
        assert stored["pin"] == report["fingerprint"], (
            "the agent persisted a pin that is not this hub's certificate"
        )
        assert transport_file.stat().st_mode & 0o777 == 0o600
    finally:
        await _shutdown(state, proc)


async def test_the_migration_records_tls_for_the_next_start(agent_binary: str, hp_dir: str):
    """The install's remembered decision must move too, or the next boot would
    put the fleet straight back on plaintext and undo the whole migration."""
    state, settings = await _legacy_install(hp_dir)
    conf = Path(hp_dir) / "agentconf"
    proc = _spawn_plaintext_agent(agent_binary, conf, settings)
    try:
        assert await _wait_connected(state), "the plaintext agent never reached the hub"
        repo = Repository(state.database)
        before = await repo.get_setting(SETTING_KEY)
        assert before is not None and before["value"] == MODE_LEGACY_PLAINTEXT

        await migrate_fleet_to_tls(
            repo,
            state.agent_registry,
            state.agent_hub,
            settings.data_dir,
            settings=settings,
            force=True,
        )

        after = await repo.get_setting(SETTING_KEY)
        assert after is not None and after["value"] == MODE_TLS
    finally:
        await _shutdown(state, proc)


async def test_the_agent_comes_back_over_tls_after_the_hub_restarts(agent_binary: str, hp_dir: str):
    """THE journey, end to end: a fleet that was speaking plaintext is speaking
    TLS, and nobody touched a host.

    Persisting a pin and flipping a setting are transitions, not the goal. The
    goal is the agent dialling back in to a hub that now demands TLS, using the
    certificate it was handed while still on plaintext. That is the assertion
    that would have caught #468 in the first place - and the one that fails if
    the pushed transport survives only in memory, or if the pin handed out is
    not the certificate later served.
    """
    import socket

    from homepilot.app_state import create_app_state
    from homepilot.config import Settings

    state, settings = await _legacy_install(hp_dir)
    conf = Path(hp_dir) / "agentconf"
    proc = _spawn_plaintext_agent(agent_binary, conf, settings)
    restarted = None
    try:
        assert await _wait_connected(state), "the plaintext agent never reached the hub"

        report = await migrate_fleet_to_tls(
            Repository(state.database),
            state.agent_registry,
            state.agent_hub,
            settings.data_dir,
            settings=settings,
            force=True,
        )

        # The backend restart the migration report asks for. The agent process
        # keeps running throughout - it is retrying, exactly as a real fleet
        # would be while an operator restarts the control plane.
        await state.agent_hub.stop()
        await state.database.close()

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            del probe
        restarted_settings = Settings(
            secret_key="test-secret-key-for-pytest-only-not-for-production",
            data_dir=hp_dir,
            artifacts_dir=os.path.join(hp_dir, "artifacts"),
            agent_hub_host="127.0.0.1",
            agent_hub_port=settings.agent_hub_port,
        )
        restarted = await create_app_state(restarted_settings)
        assert restarted.agent_hub is not None
        assert restarted.agent_hub.tls_enabled is True, (
            "the migration recorded TLS, so the restarted hub must serve it"
        )
        assert restarted.agent_hub.cert_fingerprint == report["fingerprint"], (
            "the hub serves a different certificate than the one the fleet pinned"
        )
        await restarted.agent_hub.start()

        connected = await _wait_connected(restarted, timeout=60.0)
        assert connected, (
            "the migrated agent never came back over TLS - the fleet is stranded, "
            "which is the entire failure this feature exists to prevent"
        )

        # And it must survive the AGENT restarting too, which is what makes the
        # migration durable rather than a property of one live process. The
        # replacement is spawned with the same plaintext environment the host
        # has always had: if the persisted transport is not actually consulted,
        # this agent dials plaintext into a TLS listener and never returns -
        # the original bug, one reboot later.
        proc.terminate()
        proc.communicate(timeout=10)
        # Wait for the hub to actually NOTICE the departure. Skipping this reads
        # the outgoing connection's registry entry and calls it a reconnection -
        # the assertion then passes with the feature ripped out, which is how
        # this gate first failed its own teeth proof.
        assert await _wait_disconnected(restarted, timeout=30.0), (
            "the hub never registered the agent leaving, so a later 'connected' "
            "proves nothing about the restarted process"
        )

        proc = _spawn_plaintext_agent(agent_binary, conf, restarted_settings)

        assert await _wait_connected(restarted, timeout=60.0), (
            "a restarted agent did not use its persisted transport - the migration "
            "only survived in memory, so every host would strand itself on reboot"
        )
    finally:
        if restarted is not None:
            await _shutdown(restarted, proc)
        else:
            await _shutdown(state, proc)


async def test_an_agent_that_would_be_stranded_refuses_the_migration(
    agent_binary: str, hp_dir: str
):
    """An enrolled agent that is offline cannot be told about the new transport,
    so it would return to a hub it cannot speak to. Refuse, and name it."""
    state, settings = await _legacy_install(hp_dir)
    conf = Path(hp_dir) / "agentconf"
    proc = _spawn_plaintext_agent(agent_binary, conf, settings)
    try:
        assert await _wait_connected(state), "the plaintext agent never reached the hub"
        repo = Repository(state.database)

        plan = await plan_migration(repo, state.agent_registry)
        assert plan["can_flip_cleanly"] is False
        assert any(a["hostname"] == "legacy-host" for a in plan["unreachable"])

        with pytest.raises(MigrationRefusedError, match="legacy-host"):
            await migrate_fleet_to_tls(
                repo,
                state.agent_registry,
                state.agent_hub,
                settings.data_dir,
                settings=settings,
            )

        # Refusing must change NOTHING - a half-done migration is the failure
        # mode this whole issue is about.
        unchanged = await repo.get_setting(SETTING_KEY)
        assert unchanged is not None and unchanged["value"] == MODE_LEGACY_PLAINTEXT
        assert not (conf / "agent.transport").exists()
    finally:
        await _shutdown(state, proc)


async def test_an_unusable_pin_is_never_persisted(agent_binary: str, hp_dir: str):
    """A pin the agent cannot parse is unrecoverable: it would fail every
    handshake with the hub that could fix it. The agent must reject the push and
    stay on the transport that works."""
    state, settings = await _legacy_install(hp_dir)
    conf = Path(hp_dir) / "agentconf"
    proc = _spawn_plaintext_agent(agent_binary, conf, settings)
    try:
        connected = await _wait_connected(state)
        assert connected, "the plaintext agent never reached the hub"
        agent_id = connected[0]["agent_id"]

        with pytest.raises(Exception, match="unusable pin"):
            await state.agent_hub.send_action(
                agent_id, "set_transport", {"tls": True, "pin": "not-a-fingerprint"}
            )

        assert not (conf / "agent.transport").exists(), "an unusable pin was persisted"
        # Still serving, on the transport it had.
        assert state.agent_registry.list_connected(), "the agent dropped off after a rejected push"
    finally:
        await _shutdown(state, proc)
