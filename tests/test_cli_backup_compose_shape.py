"""The compose deployment's shape - #648 tranche 4.

`tests/test_cli_backup_restore.py` seeds `<data_dir>/.env`, and every gate in it
rests on that. NO shipped deployment produces that shape: `docker-compose.yml`
sets `HP_DATA_DIR=/data` and reads its `.env` with `env_file:` from beside the
compose file, and `hp init` writes `HP_VAULT_PASSPHRASE` into THAT file. So the
passphrase has never been inside the data dir on a real install, and
`SECRET_PATHS` - every entry data-dir relative - never archived it.

Its journey test then hands the RESTORE the same `HP_VAULT_PASSPHRASE` it gave
the export, so the archive never had to carry one to pass. A green test guarding
a shape the product does not take.

Live on dev 3.6.15: `hp export --include-secrets` printed "It holds the vault
identity and passphrase", wrote a manifest listing exactly
`["vault/identities", "vault/secrets"]`, and the restored instance exited 3 on
`VaultError: Failed to decrypt identity (wrong passphrase?)`.
"""

from __future__ import annotations

import asyncio
import io
import json
import shutil
import tarfile
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from homepilot.cli.main import app
from homepilot.config import get_settings
from homepilot.vault import VaultManager
from tests.test_cli_backup_restore import (
    PASSPHRASE,
    PVE_TOKEN,
    _hostnames,
    _out,
    _seed_db,
    _seed_vault,
    _vault_secret,
)

runner = CliRunner()


@pytest.fixture(autouse=True)
def clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _compose_env(data_dir: Path, passphrase: str | None = PASSPHRASE) -> dict[str, str]:
    """The real shape: the passphrase in the ENVIRONMENT, never in the data dir."""
    env = {
        "HP_DATA_DIR": str(data_dir),
        "HP_ARTIFACTS_DIR": str(data_dir / "artifacts"),
    }
    if passphrase is not None:
        env["HP_VAULT_PASSPHRASE"] = passphrase
    return env


def _seed_compose_host(data_dir: Path, hostname: str = "pve1.lan") -> None:
    """A data dir with NO `.env` and NO `.vault_passphrase` inside it."""
    data_dir.mkdir(parents=True, exist_ok=True)
    asyncio.run(_seed_db(data_dir, hostname))
    asyncio.run(_seed_vault(data_dir))
    artifacts = data_dir / "artifacts" / "2026" / "08"
    artifacts.mkdir(parents=True, exist_ok=True)
    (artifacts / "note.md").write_text("# artifact\n", encoding="utf-8")
    assert not (data_dir / ".env").exists()
    assert not (data_dir / ".vault_passphrase").exists()


def _manifest(tarball: Path) -> dict:
    with tarfile.open(str(tarball)) as tar:
        handle = tar.extractfile("manifest.json")
        assert handle is not None
        return json.loads(handle.read())


class TestComposeShapeBackupIsRestorable:
    def test_restored_host_can_decrypt_with_nothing_but_the_tarball(self, tmp_path):
        """THE gate: the rebuilt host is told nothing out of band."""
        data_dir = tmp_path / "hp"
        tarball = tmp_path / "backup.tar.gz"
        _seed_compose_host(data_dir)

        with patch.dict("os.environ", _compose_env(data_dir), clear=True):
            result = runner.invoke(app, ["export", "-o", str(tarball), "--include-secrets"])
        assert result.exit_code == 0, _out(result)

        manifest = _manifest(tarball)
        assert manifest["includes_vault_passphrase"] is True, (
            f"the archive calls itself restorable and carries no key: {manifest['secret_paths']}"
        )

        shutil.rmtree(data_dir)

        get_settings.cache_clear()
        with patch.dict("os.environ", _compose_env(data_dir, passphrase=None), clear=True):
            result = runner.invoke(app, ["import", str(tarball), "--yes", "--restore-secrets"])
        assert result.exit_code == 0, _out(result)

        restored = (data_dir / ".vault_passphrase").read_text(encoding="utf-8").strip()
        assert _vault_secret(data_dir, restored) == PVE_TOKEN
        assert _hostnames(data_dir / "homepilot.db") == ["pve1.lan"]

    def test_import_says_so_when_the_restored_vault_cannot_be_opened(self, tmp_path):
        """A keyless restore must be stated, not discovered from a crash loop."""
        data_dir = tmp_path / "hp"
        tarball = tmp_path / "backup.tar.gz"
        _seed_compose_host(data_dir)

        with patch.dict("os.environ", _compose_env(data_dir), clear=True):
            result = runner.invoke(app, ["export", "-o", str(tarball), "--include-secrets"])
        assert result.exit_code == 0, _out(result)

        # An archive shaped like every one written before this fix: identity
        # present, no passphrase, `includes_secrets` true.
        stripped = tmp_path / "stripped.tar.gz"
        with tarfile.open(str(tarball)) as src, tarfile.open(str(stripped), "w:gz") as dst:
            for member in src.getmembers():
                if member.name == "secrets/.vault_passphrase":
                    continue
                if member.name == "manifest.json":
                    handle = src.extractfile(member)
                    assert handle is not None
                    manifest = json.loads(handle.read())
                    manifest["includes_vault_passphrase"] = False
                    manifest["secret_paths"] = [
                        rel for rel in manifest["secret_paths"] if rel != ".vault_passphrase"
                    ]
                    payload = json.dumps(manifest).encode("utf-8")
                    info = tarfile.TarInfo("manifest.json")
                    info.size = len(payload)
                    dst.addfile(info, io.BytesIO(payload))
                    continue
                dst.addfile(member, src.extractfile(member) if member.isfile() else None)

        shutil.rmtree(data_dir)
        get_settings.cache_clear()
        with patch.dict("os.environ", _compose_env(data_dir, passphrase=None), clear=True):
            result = runner.invoke(app, ["import", str(stripped), "--yes", "--restore-secrets"])
        assert result.exit_code == 0, _out(result)
        assert "CANNOT BE OPENED" in _out(result)

    def test_export_refuses_to_claim_a_passphrase_that_opens_nothing(self, tmp_path):
        """`Settings` auto-generates a passphrase; that is not the vault's key."""
        data_dir = tmp_path / "hp"
        tarball = tmp_path / "backup.tar.gz"
        _seed_compose_host(data_dir)
        (data_dir / ".vault_passphrase").write_text("not-the-right-one\n", encoding="utf-8")

        with patch.dict("os.environ", _compose_env(data_dir, passphrase=None), clear=True):
            result = runner.invoke(app, ["export", "-o", str(tarball), "--include-secrets"])
        assert result.exit_code == 0, _out(result)
        assert "no usable vault passphrase" in _out(result).lower()

        manifest = _manifest(tarball)
        with tarfile.open(str(tarball)) as tar:
            names = {m.name for m in tar.getmembers()}
        assert manifest["includes_vault_passphrase"] is False
        assert "secrets/.vault_passphrase" not in names

    def test_a_stale_passphrase_file_does_not_beat_the_one_in_force(self, tmp_path):
        """Found by running the fixed export against a real dev data dir.

        That directory had a leftover auto-generated `.vault_passphrase` from an
        earlier restore AND a working `HP_VAULT_PASSPHRASE` in the environment.
        `config.py` resolves env first, so the backend was decrypting happily -
        while a first cut of this fix read the FILE, decided the host had no
        usable key, and shipped an archive with no passphrase. The archive must
        carry the passphrase that WORKS, in the product's own resolution order.
        """
        data_dir = tmp_path / "hp"
        tarball = tmp_path / "backup.tar.gz"
        _seed_compose_host(data_dir)
        (data_dir / ".vault_passphrase").write_text("stale-and-wrong\n", encoding="utf-8")

        with patch.dict("os.environ", _compose_env(data_dir), clear=True):
            result = runner.invoke(app, ["export", "-o", str(tarball), "--include-secrets"])
        assert result.exit_code == 0, _out(result)
        assert "no usable vault passphrase" not in _out(result).lower()
        assert _manifest(tarball)["includes_vault_passphrase"] is True

        with tarfile.open(str(tarball)) as tar:
            handle = tar.extractfile("secrets/.vault_passphrase")
            assert handle is not None
            archived = handle.read().decode("utf-8").strip()
        assert archived == PASSPHRASE, "the archive carries the stale file, not the working key"

        shutil.rmtree(data_dir)
        get_settings.cache_clear()
        with patch.dict("os.environ", _compose_env(data_dir, passphrase=None), clear=True):
            result = runner.invoke(app, ["import", str(tarball), "--yes", "--restore-secrets"])
        assert result.exit_code == 0, _out(result)
        assert "CANNOT BE OPENED" not in _out(result)
        restored = (data_dir / ".vault_passphrase").read_text(encoding="utf-8").strip()
        assert _vault_secret(data_dir, restored) == PVE_TOKEN

    def test_the_banner_only_claims_a_passphrase_it_actually_holds(self, tmp_path):
        data_dir = tmp_path / "hp"
        tarball = tmp_path / "backup.tar.gz"
        _seed_compose_host(data_dir)

        with patch.dict("os.environ", _compose_env(data_dir), clear=True):
            result = runner.invoke(app, ["export", "-o", str(tarball), "--include-secrets"])
        output = _out(result)
        assert "CONTAINS SECRETS" in output
        assert "passphrase that unwraps it" in output

        # And with no key to be had, it says the opposite rather than the same.
        keyless = tmp_path / "hp2"
        keyless.mkdir()
        asyncio.run(_seed_db(keyless))
        asyncio.run(VaultManager(keyless, PASSPHRASE).ensure_master_identity())
        (keyless / ".vault_passphrase").write_text("wrong\n", encoding="utf-8")
        # `get_settings` is lru_cached, so a second invocation in one test would
        # otherwise export the FIRST data dir all over again.
        get_settings.cache_clear()
        with patch.dict("os.environ", _compose_env(keyless, passphrase=None), clear=True):
            result = runner.invoke(
                app, ["export", "-o", str(tmp_path / "b2.tar.gz"), "--include-secrets"]
            )
        assert "passphrase that unwraps it" not in _out(result)
