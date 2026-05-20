from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from homepilot.config import Settings


class TestSettingsDefaults:
    def test_vault_passphrase_auto_generated_when_missing(self, tmp_path):
        data_dir = tmp_path / "hp"
        data_dir.mkdir(parents=True, exist_ok=True)
        s = Settings(
            secret_key="testkey",
            data_dir=str(data_dir),
            artifacts_dir=str(data_dir / "artifacts"),
        )
        assert s.vault_passphrase is not None
        assert s.vault_passphrase != ""
        assert len(s.vault_passphrase) > 30
        pf = data_dir / ".vault_passphrase"
        assert pf.exists()
        assert pf.stat().st_mode & 0o777 == 0o600
        assert pf.read_text().strip() == s.vault_passphrase

    def test_vault_passphrase_auto_generated_persisted_and_reused(self, tmp_path):
        data_dir = tmp_path / "hp"
        data_dir.mkdir(parents=True, exist_ok=True)
        s1 = Settings(
            secret_key="testkey",
            data_dir=str(data_dir),
            artifacts_dir=str(data_dir / "artifacts"),
        )
        s2 = Settings(
            secret_key="testkey2",
            data_dir=str(data_dir),
            artifacts_dir=str(data_dir / "artifacts"),
        )
        assert s1.vault_passphrase == s2.vault_passphrase

    def test_vault_passphrase_auto_generated_empty_file_regenerates(self, tmp_path):
        data_dir = tmp_path / "hp"
        data_dir.mkdir(parents=True, exist_ok=True)
        pf = data_dir / ".vault_passphrase"
        pf.write_text("")
        s = Settings(
            secret_key="testkey",
            data_dir=str(data_dir),
            artifacts_dir=str(data_dir / "artifacts"),
        )
        assert s.vault_passphrase is not None
        assert s.vault_passphrase != ""
        assert pf.read_text().strip() == s.vault_passphrase

    def test_vault_passphrase_auto_generated_oserror_falls_back(self, tmp_path):
        data_dir = tmp_path / "hp"
        data_dir.mkdir(parents=True, exist_ok=True)
        s = Settings(
            secret_key="testkey",
            data_dir=str(data_dir),
            artifacts_dir=str(data_dir / "artifacts"),
        )
        assert s.vault_passphrase is not None
        assert s.vault_passphrase != ""

    def test_vault_passphrase_empty_string_disables_vault(self, tmp_path):
        data_dir = tmp_path / "hp"
        data_dir.mkdir(parents=True, exist_ok=True)
        s = Settings(
            secret_key="testkey",
            vault_passphrase="",
            data_dir=str(data_dir),
            artifacts_dir=str(data_dir / "artifacts"),
        )
        assert s.vault_passphrase == ""

    def test_default_data_dir(self):
        s = Settings(secret_key="testkey")
        assert s.data_dir == str(Path.home() / ".hp")

    def test_default_artifacts_dir(self):
        s = Settings(secret_key="testkey")
        assert s.artifacts_dir == str(Path.home() / ".hp" / "artifacts")

    def test_default_proxmox_port(self):
        s = Settings(secret_key="testkey")
        assert s.proxmox_port == 8006

    def test_default_log_level(self):
        s = Settings(secret_key="testkey")
        assert s.log_level == "info"

    def test_default_daemon_port(self):
        s = Settings(secret_key="testkey")
        assert s.daemon_port == 8000

    def test_default_cors_origins(self):
        s = Settings(secret_key="testkey")
        assert "localhost:5173" in s.cors_origins

    def test_secret_key_provided(self):
        s = Settings(secret_key="my-secret-key")
        assert s.secret_key == "my-secret-key"

    def test_secret_key_auto_generated_when_missing(self, tmp_path):
        data_dir = tmp_path / "hp"
        with patch.dict(os.environ, {"HP_SECRET_KEY": ""}, clear=False):
            s = Settings(
                secret_key="",
                data_dir=str(data_dir),
                artifacts_dir=str(data_dir / "artifacts"),
            )
            assert s.secret_key != ""
            assert len(s.secret_key) > 20

    def test_secret_key_file_via_env(self, tmp_path, monkeypatch):
        key_file = tmp_path / "secret.txt"
        key_file.write_text("file-based-secret-key\n")
        data_dir = tmp_path / "hp"
        monkeypatch.setenv("HP_SECRET_KEY_FILE", str(key_file))
        monkeypatch.delenv("HP_SECRET_KEY", raising=False)
        s = Settings(
            secret_key="",
            data_dir=str(data_dir),
            artifacts_dir=str(data_dir / "artifacts"),
        )
        assert s.secret_key == "file-based-secret-key"

    def test_secret_key_file_not_found_via_env_raises(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HP_SECRET_KEY_FILE", str(tmp_path / "nonexistent.txt"))
        monkeypatch.delenv("HP_SECRET_KEY", raising=False)
        with pytest.raises(Exception, match="HP_SECRET_KEY_FILE not found"):
            Settings(
                secret_key="",
                data_dir=str(tmp_path / "hp"),
                artifacts_dir=str(tmp_path / "hp" / "artifacts"),
            )

    def test_vault_dir_defaults_to_data_dir_vault(self, tmp_path):
        s = Settings(
            secret_key="testkey",
            data_dir=str(tmp_path / "hp"),
            artifacts_dir=str(tmp_path / "hp" / "artifacts"),
            vault_dir="",
        )
        assert s.vault_dir == str(tmp_path / "hp" / "vault")

    def test_ssh_key_dir_defaults_to_data_dir_ssh(self, tmp_path):
        s = Settings(
            secret_key="testkey",
            data_dir=str(tmp_path / "hp"),
            artifacts_dir=str(tmp_path / "hp" / "artifacts"),
            ssh_key_dir="",
        )
        assert s.ssh_key_dir == str(tmp_path / "hp" / "ssh")

    def test_vault_passphrase_file(self, tmp_path):
        pf = tmp_path / "pass.txt"
        pf.write_text("my-vault-passphrase\n")
        s = Settings(
            secret_key="testkey",
            vault_passphrase_file=str(pf),
            data_dir=str(tmp_path / "hp"),
            artifacts_dir=str(tmp_path / "hp" / "artifacts"),
        )
        assert s.vault_passphrase == "my-vault-passphrase"

    def test_vault_passphrase_file_not_found_raises(self, tmp_path):
        with pytest.raises(Exception, match="HP_VAULT_PASSPHRASE_FILE not found"):
            Settings(
                secret_key="testkey",
                vault_passphrase_file=str(tmp_path / "nonexistent.txt"),
                data_dir=str(tmp_path / "hp"),
                artifacts_dir=str(tmp_path / "hp" / "artifacts"),
            )

    def test_vault_passphrase_not_auto_generated_when_provided(self, caplog):
        import logging

        with caplog.at_level(logging.DEBUG, logger="homepilot.config"):
            s = Settings(secret_key="testkey", vault_passphrase="env-pass")
        assert s.vault_passphrase == "env-pass"
        assert not any(
            "auto-generated" in r.message for r in caplog.records if "passphrase" in r.message
        )

    def test_prod_env_stable_key_required(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HP_ENV", "production")
        monkeypatch.delenv("HP_SECRET_KEY", raising=False)
        monkeypatch.delenv("HP_SECRET_KEY_FILE", raising=False)

        data_dir = tmp_path / "hp"
        persisted = data_dir / ".secret_key"
        data_dir.mkdir(parents=True, exist_ok=True)
        persisted.write_text("stable-production-key")

        s = Settings(
            secret_key="",
            data_dir=str(data_dir),
            artifacts_dir=str(data_dir / "artifacts"),
        )
        assert s.secret_key == "stable-production-key"

    def test_prod_env_auto_generated_key_raises(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HP_ENV", "production")
        monkeypatch.delenv("HP_SECRET_KEY", raising=False)
        monkeypatch.delenv("HP_SECRET_KEY_FILE", raising=False)

        data_dir = tmp_path / "hp_no_key"
        data_dir.mkdir(parents=True, exist_ok=True)

        with pytest.raises(Exception, match="HP_SECRET_KEY or HP_SECRET_KEY_FILE must be set"):
            Settings(
                secret_key="",
                data_dir=str(data_dir),
                artifacts_dir=str(data_dir / "artifacts"),
            )

    def test_get_settings_cached(self):
        from homepilot.config import get_settings

        s1 = get_settings()
        s2 = get_settings()
        assert s1 is s2
