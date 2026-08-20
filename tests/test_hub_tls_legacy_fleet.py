"""TLS-by-default must not strand a fleet that predates it (#468).

The bug these gates forbid: S3 gave the new TLS default to every install whose
operator had never named `HP_AGENT_HUB_TLS`, including installs with agents
already enrolled over plaintext. Upgrading the backend flipped the listener and
every existing agent died at `EOF` forever - reproduced live on the dev box
against the real database and the real enrolled agents.

Recovery was not a backend setting: `agent/go/config.go` reads TLS from
`HP_AGENT_TLS` in `/etc/homepilot/agent.env`, written once at enrolment, so even
a newer binary keeps dialling plaintext. Every managed host would need editing
by hand, through the channel the upgrade had just taken down.

The decided behaviour (owner, 2026-08-20): the TLS default belongs to a NEW
install. An install that already had a fleet keeps its transport, decided once
and remembered, and says so. These assert the OUTCOME - which transport the hub
actually ends up serving - not that a helper returned a string.

Teeth: delete the `_hub_legacy_plaintext` branch in
``app_state.create_app_state`` and ``test_existing_fleet_keeps_plaintext`` and
``test_the_decision_is_not_retaken_when_the_fleet_is_removed`` both fail.
"""

from __future__ import annotations

import os
import shutil
import socket
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from homepilot.agent_hub.tls_mode import MODE_LEGACY_PLAINTEXT, MODE_TLS, SETTING_KEY
from homepilot.app_state import create_app_state
from homepilot.config import Settings
from homepilot.db.repository import Repository

pytestmark = pytest.mark.asyncio


@pytest.fixture
def hp_dir() -> Iterator[str]:
    """Must live under $HOME: create_app_state refuses an artifacts_dir in /tmp."""
    path = tempfile.mkdtemp(dir=str(Path.home()), prefix=".hp-test-legacyfleet-")
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _settings(hp_dir: str, **overrides: object) -> Settings:
    """A stock install on a ROUTABLE bind - the shape a real deployment has, and
    the one where plaintext meets the fail-closed check."""
    overrides.setdefault("agent_hub_port", _free_port())
    return Settings(
        secret_key="test-secret-key-for-pytest-only-not-for-production",
        data_dir=hp_dir,
        artifacts_dir=os.path.join(hp_dir, "artifacts"),
        **overrides,  # type: ignore[arg-type]
    )


async def _seed_legacy_install(hp_dir: str, agents: int = 1) -> None:
    """Leave the data directory as a 2.7.1 install would: a migrated database
    with enrolled agents and NO recorded TLS decision.

    Seeding through create_app_state instead would be the wrong fixture - that
    call is the thing under test, and running it first records the decision this
    test needs to watch being taken. The install being upgraded never ran this
    code, which is the whole point.
    """
    from homepilot.db.connection import Database
    from homepilot.db.migrations import run_migrations

    db = Database(str(Path(hp_dir) / "homepilot.db"))
    await db.connect()
    await run_migrations(db)
    for index in range(agents):
        await db.execute(
            "INSERT INTO agents (agent_id, hostname, connected) VALUES (?, ?, 0)",
            (f"legacy-agent-{index}", "hp-test-server"),
        )
    await db.conn.commit()
    await db.close()


async def _shutdown(state) -> None:
    if state.agent_hub is not None:
        await state.agent_hub.stop()
    await state.database.close()


async def test_a_new_install_still_gets_tls(hp_dir: str):
    """S3's win is preserved: nothing enrolled, so TLS with zero input."""
    state = await create_app_state(_settings(hp_dir))
    try:
        assert state.agent_hub is not None
        assert state.agent_hub.tls_enabled is True
        stored = await Repository(state.database).get_setting(SETTING_KEY)
        assert stored is not None and stored["value"] == MODE_TLS
    finally:
        await _shutdown(state)


async def test_existing_fleet_keeps_plaintext(hp_dir: str):
    """The heart of #468: an upgrade must not change the transport under a fleet.

    The data dir is left exactly as a 2.7.1 install leaves it, then this code
    boots on it for the first time - which is the upgrade. The assertion is on
    the listener that boot ends up with, not on any flag it read.
    """
    await _seed_legacy_install(hp_dir)

    upgraded = await create_app_state(_settings(hp_dir))
    try:
        assert upgraded.agent_hub is not None, (
            "the hub must still serve - a stranded fleet is the bug, and a dark hub is no better"
        )
        assert upgraded.agent_hub.tls_enabled is False, (
            "agents enrolled over plaintext must still be able to dial in"
        )
        stored = await Repository(upgraded.database).get_setting(SETTING_KEY)
        assert stored is not None and stored["value"] == MODE_LEGACY_PLAINTEXT
    finally:
        await _shutdown(upgraded)


async def test_the_decision_is_not_retaken_when_the_fleet_is_removed(hp_dir: str):
    """Decided once, remembered. Recomputing per boot would let the transport
    flip later - when the last legacy agent is deleted - which is the same
    surprise in slower motion."""
    await _seed_legacy_install(hp_dir)

    second = await create_app_state(_settings(hp_dir))
    await second.database.execute("DELETE FROM agents")
    await second.database.conn.commit()
    await _shutdown(second)

    third = await create_app_state(_settings(hp_dir))
    try:
        assert third.agent_hub is not None
        assert third.agent_hub.tls_enabled is False, (
            "an empty agents table must not silently flip the transport"
        )
    finally:
        await _shutdown(third)


async def test_an_explicit_operator_setting_still_wins(hp_dir: str):
    """Naming HP_AGENT_HUB_TLS is obeyed, and records no decision that could
    outlive the env var and contradict it later."""
    await _seed_legacy_install(hp_dir)

    state = await create_app_state(_settings(hp_dir, agent_hub_tls=True))
    try:
        assert state.agent_hub is not None
        assert state.agent_hub.tls_enabled is True
    finally:
        await _shutdown(state)


async def test_legacy_plaintext_is_reported_not_hidden(hp_dir: str):
    """Keeping plaintext is a compromise, so it must be visible rather than
    quietly correct-looking."""
    from homepilot import selfcheck

    await _seed_legacy_install(hp_dir)

    upgraded_settings = _settings(hp_dir)
    state = await create_app_state(upgraded_settings)
    try:
        report = await selfcheck.selfcheck_report(state, upgraded_settings)
        hub = next(s for s in report["subsystems"] if s["name"] == "agent_hub")
        assert hub["configured"] is True
        assert state.agent_hub.tls_enabled is False
    finally:
        await _shutdown(state)
