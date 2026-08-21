from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from homepilot.app_state import AppState, create_app_state
from homepilot.config import Settings


@pytest.fixture
def hp_dir() -> Iterator[str]:
    """A data dir that exists only for one test.

    It has to live under $HOME: create_app_state refuses an artifacts_dir under
    /tmp and friends, which is why these tests used the home directory in the
    first place. What was wrong was the FIXED name (~/.hp_test_basic and
    friends), never cleaned up, so every run inherited the previous run's
    database. That leaked schema state across branches - a run on a branch
    carrying a newer migration left behind a schema the next build did not
    support, and the downgrade guard then failed a test on an unrelated branch -
    and it made concurrent runs contend on one database file, which surfaced as
    fixture hangs rather than as a clear error.
    """
    path = tempfile.mkdtemp(dir=str(Path.home()), prefix=".hp-test-appstate-")
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _settings(hp_dir: str, **overrides: object) -> Settings:
    return Settings(
        secret_key="test-secret-key-for-pytest-only-not-for-production",
        data_dir=hp_dir,
        artifacts_dir=os.path.join(hp_dir, "artifacts"),
        **overrides,  # type: ignore[arg-type]
    )


class TestAppStateDataclass:
    def test_defaults(self):
        state = AppState(settings=MagicMock(), database=MagicMock(), repo=MagicMock())
        assert state.vault is None
        assert state.proxmox is None
        assert state.pve_token_source == ""


class TestCreateAppState:
    async def test_creates_state_with_settings(self, hp_dir: str):
        settings = _settings(hp_dir)

        state = await create_app_state(settings)
        assert isinstance(state, AppState)
        assert state.settings is settings
        assert state.database is not None
        assert state.repo is not None
        assert state.artifact_store is not None
        assert state.artifact_lifecycle is not None

        await state.database.close()

    async def test_a_stock_instance_has_a_vault(self, hp_dir: str):
        """ADR-004: no passphrase configured still yields a working vault.

        The Proxmox token is the one thing an operator supplies, and it needs
        somewhere to live; an instance that quietly has no vault cannot store it.
        """
        settings = _settings(hp_dir, vault_passphrase="")

        state = await create_app_state(settings)
        assert state.vault is not None

        await state.database.close()

    async def test_no_vault_when_the_operator_opts_out(self, hp_dir: str, monkeypatch):
        monkeypatch.setenv("HP_VAULT_AUTO_INIT", "0")
        settings = _settings(hp_dir, vault_passphrase="")

        state = await create_app_state(settings)
        assert state.vault is None

        await state.database.close()

    async def test_no_proxmox_without_host(self, hp_dir: str):
        settings = _settings(hp_dir, proxmox_host="")

        state = await create_app_state(settings)
        assert state.proxmox is None

        await state.database.close()

    async def test_the_database_is_new_and_goes_away_again(self, hp_dir: str):
        """The regression gate: no state may survive into the next run.

        A test that writes to a fixed path passes on a clean machine and then
        fails - or hangs - on a machine that has run it before.
        """
        settings = _settings(hp_dir)
        db_path = Path(settings.data_dir) / "homepilot.db"
        assert not db_path.exists(), "the data dir must not pre-exist"

        state = await create_app_state(settings)
        await state.database.close()

        assert db_path.exists()
        # Unique per test, and therefore removable: a fixed ~/.hp_test_* name
        # would still be here on the next run.
        assert ".hp-test-appstate-" in str(db_path)
        leftovers = list(Path.home().glob(".hp_test_*"))
        assert not leftovers, f"fixed-name test data dirs are back: {leftovers}"
