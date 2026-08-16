from __future__ import annotations

from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from homepilot.cli.main import app
from homepilot.config import Settings, get_settings

runner = CliRunner()


@pytest.fixture(autouse=True)
def clear_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class TestSecretKeyDefault:
    def test_settings_auto_generates_secret_key_when_missing(self):
        env = {"HP_DATA_DIR": "/tmp/hp-test-init-missing"}
        with patch.dict("os.environ", env, clear=True):
            s = Settings(secret_key="")
        assert s.secret_key
        assert len(s.secret_key) >= 32

    def test_settings_auto_generates_different_keys(self, tmp_path):
        import os

        dir1 = str(tmp_path / "hp1")
        dir2 = str(tmp_path / "hp2")
        os.makedirs(dir1, exist_ok=True)
        os.makedirs(dir2, exist_ok=True)
        with patch.dict("os.environ", {"HP_DATA_DIR": dir1}, clear=False):
            s1 = Settings(secret_key="")
        with patch.dict("os.environ", {"HP_DATA_DIR": dir2}, clear=False):
            s2 = Settings(secret_key="")
        assert s1.secret_key != s2.secret_key

    def test_settings_preserves_explicit_secret_key(self):
        s = Settings(secret_key="my-explicit-key")
        assert s.secret_key == "my-explicit-key"


class TestSecretKeyWarning:
    def test_warns_when_secret_key_auto_generated(self, caplog):
        import logging

        with caplog.at_level(logging.WARNING, logger="homepilot.config"):
            Settings(secret_key="")
        assert any("HP_SECRET_KEY not set" in r.message for r in caplog.records)

    def test_no_warning_when_secret_key_provided(self, caplog):
        import logging

        with caplog.at_level(logging.WARNING, logger="homepilot.config"):
            Settings(secret_key="explicit-key")
        assert not any("HP_SECRET_KEY not set" in r.message for r in caplog.records)


class TestHpInit:
    def test_init_writes_vault_passphrase_to_env(self, tmp_path):
        env = {"HP_DATA_DIR": str(tmp_path)}
        with patch.dict("os.environ", env, clear=True):
            result = runner.invoke(app, ["init"], input="\n\n\n\n\n")
        assert result.exit_code == 0, result.output
        env_file = tmp_path / ".env"
        assert env_file.exists()
        content = env_file.read_text()
        assert "HP_VAULT_PASSPHRASE=" in content
        assert "HP_SECRET_KEY=" not in content
        assert "HP_ADMIN_SECRET=" not in content

    def test_init_stores_secrets_in_vault(self, tmp_path):
        env = {"HP_DATA_DIR": str(tmp_path)}
        with patch.dict("os.environ", env, clear=True):
            result = runner.invoke(app, ["init"], input="\n\n\n\n\n")
        assert result.exit_code == 0, result.output
        vault_dir = tmp_path / "vault" / "secrets"
        assert (vault_dir / "secret-key.age").exists()
        assert (vault_dir / "admin-secret.age").exists()

    def test_init_creates_directories(self, tmp_path):
        env = {"HP_DATA_DIR": str(tmp_path)}
        with patch.dict("os.environ", env, clear=True):
            result = runner.invoke(app, ["init"], input="\n\n\n\n\n")
        assert result.exit_code == 0
        assert tmp_path.exists()
        assert (tmp_path / "artifacts").exists()
        assert (tmp_path / "vault").exists()
        assert (tmp_path / "ssh").exists()

    def test_init_env_permissions(self, tmp_path):
        env = {"HP_DATA_DIR": str(tmp_path)}
        with patch.dict("os.environ", env, clear=True):
            result = runner.invoke(app, ["init"], input="\n\n\n\n\n")
        assert result.exit_code == 0
        env_file = tmp_path / ".env"

        mode = env_file.stat().st_mode & 0o777
        assert mode == 0o600, f"Expected 0o600, got {oct(mode)}"


class TestHpInitReinitGuard:
    """#384: `hp init` must not silently destroy an existing vault on re-run."""

    def _init_once(self, tmp_path):
        env = {"HP_DATA_DIR": str(tmp_path)}
        with patch.dict("os.environ", env, clear=True):
            result = runner.invoke(app, ["init"], input="\n\n\n\n\n")
        assert result.exit_code == 0, result.output
        return env

    def test_rerun_refused_and_mutates_nothing(self, tmp_path):
        env = self._init_once(tmp_path)
        env_file = tmp_path / ".env"
        identity = tmp_path / "vault" / "identities" / "master.protected"
        original_env = env_file.read_text()
        original_identity = identity.read_bytes()

        with patch.dict("os.environ", env, clear=True):
            result = runner.invoke(app, ["init"], input="\n\n\n\n\n")

        assert result.exit_code == 1
        assert "already initialized" in result.output
        # The passphrase (and thus the whole vault) is untouched.
        assert env_file.read_text() == original_env
        assert identity.read_bytes() == original_identity
        # No backup was made without --force.
        assert not list(tmp_path.glob(".env.*.bak"))

    def test_rerun_refused_when_only_identity_present(self, tmp_path):
        # Even if .env is gone, an existing master identity means data is at
        # risk — refuse without --force.
        env = self._init_once(tmp_path)
        (tmp_path / ".env").unlink()

        with patch.dict("os.environ", env, clear=True):
            result = runner.invoke(app, ["init"], input="\n\n\n\n\n")

        assert result.exit_code == 1
        assert "already initialized" in result.output

    def test_force_backs_up_before_overwrite(self, tmp_path):
        env = self._init_once(tmp_path)
        env_file = tmp_path / ".env"
        identity_dir = tmp_path / "vault" / "identities"
        identity = identity_dir / "master.protected"
        original_env = env_file.read_text()
        original_identity = identity.read_bytes()

        with patch.dict("os.environ", env, clear=True):
            result = runner.invoke(app, ["init", "--force"], input="\n\n\n\n\n")
        assert result.exit_code == 0, result.output

        # Old .env + identity were backed up with their ORIGINAL content first.
        env_backups = list(tmp_path.glob(".env.*.bak"))
        assert len(env_backups) == 1
        assert env_backups[0].read_text() == original_env

        id_backups = list(identity_dir.glob("master.protected.*.bak"))
        assert len(id_backups) == 1
        assert id_backups[0].read_bytes() == original_identity

        # A fresh, working vault was written (new passphrase, new identity).
        assert env_file.exists()
        assert "HP_VAULT_PASSPHRASE=" in env_file.read_text()
        assert env_file.read_text() != original_env
        assert identity.exists()
        assert identity.read_bytes() != original_identity
