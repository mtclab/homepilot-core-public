from __future__ import annotations

import os
from unittest.mock import MagicMock

from homepilot.app_state import AppState, create_app_state
from homepilot.config import Settings


def _home_subdir(name: str) -> str:
    return os.path.join(os.path.expanduser("~"), f".hp_test_{name}")


class TestAppStateDataclass:
    def test_defaults(self):
        state = AppState(settings=MagicMock(), database=MagicMock(), repo=MagicMock())
        assert state.vault is None
        assert state.proxmox is None
        assert state.pve_token_source == ""


class TestCreateAppState:
    async def test_creates_state_with_settings(self):
        hp_dir = _home_subdir("basic")
        settings = Settings(
            secret_key="test-secret-key-for-pytest-only-not-for-production",
            data_dir=hp_dir,
            artifacts_dir=os.path.join(hp_dir, "artifacts"),
        )

        state = await create_app_state(settings)
        assert isinstance(state, AppState)
        assert state.settings is settings
        assert state.database is not None
        assert state.repo is not None
        assert state.artifact_store is not None
        assert state.artifact_lifecycle is not None

        await state.database.close()

    async def test_no_vault_without_passphrase(self):
        hp_dir = _home_subdir("no_vault")
        settings = Settings(
            secret_key="test-secret-key-for-pytest-only-not-for-production",
            data_dir=hp_dir,
            artifacts_dir=os.path.join(hp_dir, "artifacts"),
            vault_passphrase="",
        )

        state = await create_app_state(settings)
        assert state.vault is None

        await state.database.close()

    async def test_no_proxmox_without_host(self):
        hp_dir = _home_subdir("no_pve")
        settings = Settings(
            secret_key="test-secret-key-for-pytest-only-not-for-production",
            data_dir=hp_dir,
            artifacts_dir=os.path.join(hp_dir, "artifacts"),
            proxmox_host="",
        )

        state = await create_app_state(settings)
        assert state.proxmox is None

        await state.database.close()
