from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from homepilot.config import Settings


class TestSettingsDefaults:
    def test_vault_passphrase_is_generated_by_default(self, tmp_path):
        """ADR-004: a stock install must have a vault without being told to.

        The Proxmox token an operator supplies has nowhere to live otherwise, so
        the one input the install asks for would fail on a default config.
        """
        data_dir = tmp_path / "hp"
        data_dir.mkdir(parents=True, exist_ok=True)
        with patch.dict(os.environ, {"HP_VAULT_AUTO_INIT": ""}, clear=False):
            s = Settings(
                vault_passphrase="",
                data_dir=str(data_dir),
                artifacts_dir=str(data_dir / "artifacts"),
            )
        assert s.vault_passphrase != ""

    def test_vault_passphrase_not_generated_when_opted_out(self, tmp_path):
        data_dir = tmp_path / "hp"
        data_dir.mkdir(parents=True, exist_ok=True)
        with patch.dict(os.environ, {"HP_VAULT_AUTO_INIT": "0"}, clear=False):
            s = Settings(
                vault_passphrase="",
                data_dir=str(data_dir),
                artifacts_dir=str(data_dir / "artifacts"),
            )
        assert s.vault_passphrase == ""

    def test_production_refuses_to_invent_a_passphrase(self, tmp_path):
        """The vault passphrase is never invented in production.

        A passphrase that exists only on one host is not a credential anyone can
        restore from, so production must be handed one deliberately.
        """
        data_dir = tmp_path / "hp"
        data_dir.mkdir(parents=True, exist_ok=True)
        with patch.dict(os.environ, {"HP_VAULT_AUTO_INIT": "1"}, clear=False):
            s = Settings(
                vault_passphrase="",
                env="production",
                data_dir=str(data_dir),
                artifacts_dir=str(data_dir / "artifacts"),
            )
        assert s.vault_passphrase == ""

    def test_vault_passphrase_auto_generated_with_env(self, tmp_path):
        data_dir = tmp_path / "hp"
        data_dir.mkdir(parents=True, exist_ok=True)
        with patch.dict(os.environ, {"HP_VAULT_AUTO_INIT": "true"}, clear=False):
            s = Settings(
                vault_passphrase="",
                data_dir=str(data_dir),
                artifacts_dir=str(data_dir / "artifacts"),
            )
        assert s.vault_passphrase != ""
        assert len(s.vault_passphrase) > 30
        pf = data_dir / ".vault_passphrase"
        assert pf.exists()
        assert pf.stat().st_mode & 0o777 == 0o600
        assert pf.read_text().strip() == s.vault_passphrase

    def test_vault_passphrase_auto_generated_persisted_and_reused(self, tmp_path):
        data_dir = tmp_path / "hp"
        data_dir.mkdir(parents=True, exist_ok=True)
        with patch.dict(os.environ, {"HP_VAULT_AUTO_INIT": "true"}, clear=False):
            s1 = Settings(
                vault_passphrase="",
                data_dir=str(data_dir),
                artifacts_dir=str(data_dir / "artifacts"),
            )
            s2 = Settings(
                vault_passphrase="",
                data_dir=str(data_dir),
                artifacts_dir=str(data_dir / "artifacts"),
            )
        assert s1.vault_passphrase == s2.vault_passphrase

    def test_vault_passphrase_auto_generated_empty_file_regenerates(self, tmp_path):
        data_dir = tmp_path / "hp"
        data_dir.mkdir(parents=True, exist_ok=True)
        pf = data_dir / ".vault_passphrase"
        pf.write_text("")
        with patch.dict(os.environ, {"HP_VAULT_AUTO_INIT": "1"}, clear=False):
            s = Settings(
                vault_passphrase="",
                data_dir=str(data_dir),
                artifacts_dir=str(data_dir / "artifacts"),
            )
        assert s.vault_passphrase != ""
        assert pf.read_text().strip() == s.vault_passphrase

    def test_vault_passphrase_auto_generated_oserror_falls_back(self, tmp_path):
        data_dir = tmp_path / "hp"
        data_dir.mkdir(parents=True, exist_ok=True)
        with patch.dict(os.environ, {"HP_VAULT_AUTO_INIT": "yes"}, clear=False):
            s = Settings(
                vault_passphrase="",
                data_dir=str(data_dir),
                artifacts_dir=str(data_dir / "artifacts"),
            )
        assert s.vault_passphrase != ""

    def test_default_data_dir(self):
        s = Settings()
        assert s.data_dir == str(Path.home() / ".hp")

    def test_default_artifacts_dir(self):
        s = Settings()
        assert s.artifacts_dir == str(Path.home() / ".hp" / "artifacts")

    def test_default_proxmox_port(self):
        s = Settings()
        assert s.proxmox_port == 8006

    def test_default_log_level(self):
        s = Settings()
        assert s.log_level == "info"

    def test_default_daemon_port(self):
        s = Settings()
        assert s.daemon_port == 8000

    def test_default_cors_origins(self):
        s = Settings()
        assert "localhost:5173" in s.cors_origins

    def test_vault_dir_defaults_to_data_dir_vault(self, tmp_path):
        s = Settings(
            data_dir=str(tmp_path / "hp"),
            artifacts_dir=str(tmp_path / "hp" / "artifacts"),
            vault_dir="",
        )
        assert s.vault_dir == str(tmp_path / "hp" / "vault")

    def test_vault_passphrase_file(self, tmp_path):
        pf = tmp_path / "pass.txt"
        pf.write_text("my-vault-passphrase\n")
        s = Settings(
            vault_passphrase_file=str(pf),
            vault_passphrase="",
            data_dir=str(tmp_path / "hp"),
            artifacts_dir=str(tmp_path / "hp" / "artifacts"),
        )
        assert s.vault_passphrase == "my-vault-passphrase"

    def test_vault_passphrase_file_not_found_raises(self, tmp_path):
        with pytest.raises(Exception, match="HP_VAULT_PASSPHRASE_FILE not found"):
            Settings(
                vault_passphrase_file=str(tmp_path / "nonexistent.txt"),
                data_dir=str(tmp_path / "hp"),
                artifacts_dir=str(tmp_path / "hp" / "artifacts"),
            )

    def test_vault_passphrase_not_auto_generated_when_provided(self, caplog):
        import logging

        with caplog.at_level(logging.DEBUG, logger="homepilot.config"):
            s = Settings(vault_passphrase="env-pass")
        assert s.vault_passphrase == "env-pass"
        assert not any(
            "auto-generated" in r.message for r in caplog.records if "passphrase" in r.message
        )

    def test_get_settings_cached(self):
        from homepilot.config import get_settings

        s1 = get_settings()
        s2 = get_settings()
        assert s1 is s2
