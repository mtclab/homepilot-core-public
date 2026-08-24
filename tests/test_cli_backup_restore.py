"""Gates for `hp export` / `hp import` (#421).

The point of these tests is the JOURNEY: a wiped host must come back as a
WORKING host. "export returned 0" and "the tar has N members" are not evidence
of that, so nothing here asserts either.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import sqlite3
import tarfile
import tempfile
import traceback
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from homepilot.cli.main import app
from homepilot.config import get_settings
from homepilot.db.backup import read_schema_version
from homepilot.db.connection import Database
from homepilot.db.migrations import MIGRATIONS, run_migrations
from homepilot.vault import VaultError, VaultManager

runner = CliRunner()

PASSPHRASE = "test-vault-passphrase-not-for-production"
PVE_TOKEN = {"token": "PVEAPIToken=hp@pve!api=1111-2222-3333"}


@pytest.fixture(autouse=True)
def clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _env(data_dir: Path) -> dict[str, str]:
    return {
        "HP_DATA_DIR": str(data_dir),
        "HP_ARTIFACTS_DIR": str(data_dir / "artifacts"),
        "HP_VAULT_PASSPHRASE": PASSPHRASE,
    }


def _out(result) -> str:
    """CliRunner on click 8.3 captures stderr separately; the warnings live there."""
    text = result.stdout
    with contextlib.suppress(ValueError):
        text += result.stderr
    if result.exception is not None and not isinstance(result.exception, SystemExit):
        text += "".join(
            traceback.format_exception(
                type(result.exception), result.exception, result.exception.__traceback__
            )
        )
    return text


async def _seed_db(data_dir: Path, hostname: str = "pve1.lan") -> None:
    db = Database(str(data_dir / "homepilot.db"))
    await db.connect()
    try:
        await run_migrations(db)
        now = datetime.now(UTC).isoformat()
        await db.execute(
            "INSERT INTO hosts (id, hostname, host_type, role, status, created_at, updated_at) "
            "VALUES (?, ?, 'vm', 'guest', 'running', ?, ?)",
            ("host-1", hostname, now, now),
        )
        await db.conn.commit()
    finally:
        await db.close()


async def _seed_vault(data_dir: Path) -> None:
    vault = VaultManager(data_dir, PASSPHRASE)
    await vault.ensure_master_identity()
    await vault.store_secret("pve-token", PVE_TOKEN)


def _seed_host(data_dir: Path, hostname: str = "pve1.lan") -> None:
    """A realistic data dir: DB rows, a real vault, artifacts, key material."""
    data_dir.mkdir(parents=True, exist_ok=True)
    asyncio.run(_seed_db(data_dir, hostname))
    asyncio.run(_seed_vault(data_dir))

    artifacts = data_dir / "artifacts" / "2026" / "08"
    artifacts.mkdir(parents=True, exist_ok=True)
    (artifacts / "note.md").write_text("# artifact\n", encoding="utf-8")

    (data_dir / ".env").write_text(f"HP_VAULT_PASSPHRASE={PASSPHRASE}\n", encoding="utf-8")
    (data_dir / "api-token").write_text("hp_tok_abcdef\n", encoding="utf-8")
    (data_dir / "ssh").mkdir(exist_ok=True)
    (data_dir / "ssh" / "id_ed25519").write_text("PRIVATE KEY\n", encoding="utf-8")


def _hostnames(db_path: Path) -> list[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        return [r[0] for r in conn.execute("SELECT hostname FROM hosts ORDER BY hostname")]
    finally:
        conn.close()


def _vault_secret(data_dir: Path, passphrase: str, name: str = "pve-token") -> dict:
    vault = VaultManager(data_dir, passphrase)
    return asyncio.run(vault.get_secret(name))


def _export(data_dir: Path, tarball: Path, *extra: str):
    with patch.dict("os.environ", _env(data_dir), clear=True):
        return runner.invoke(app, ["export", "-o", str(tarball), *extra])


def _import(data_dir: Path, tarball: Path, *extra: str):
    get_settings.cache_clear()
    with patch.dict("os.environ", _env(data_dir), clear=True):
        return runner.invoke(app, ["import", str(tarball), "--yes", *extra])


class TestJourneyWipedHostComesBack:
    """THE gate: export a real host, wipe it, import, and use it again."""

    def test_wiped_host_is_a_working_host_again(self, tmp_path):
        data_dir = tmp_path / "hp"
        tarball = tmp_path / "backup.tar.gz"
        _seed_host(data_dir)

        result = _export(data_dir, tarball, "--include-secrets")
        assert result.exit_code == 0, _out(result)

        # Wipe the host completely - nothing but the tarball survives.
        shutil.rmtree(data_dir)
        assert not data_dir.exists()

        result = _import(data_dir, tarball, "--restore-secrets")
        assert result.exit_code == 0, _out(result)

        # 1. The inventory is back.
        assert _hostnames(data_dir / "homepilot.db") == ["pve1.lan"]

        # 2. The vault DECRYPTS with the passphrase carried by the backup -
        #    not with one the test happens to know out of band.
        env_text = (data_dir / ".env").read_text()
        restored_passphrase = env_text.split("HP_VAULT_PASSPHRASE=", 1)[1].strip()
        assert _vault_secret(data_dir, restored_passphrase) == PVE_TOKEN

        # 3. The schema is the one this build runs.
        assert read_schema_version(data_dir / "homepilot.db") == max(MIGRATIONS.keys())

        # 4. Artifacts and key material came back too.
        assert (data_dir / "artifacts" / "2026" / "08" / "note.md").exists()
        assert (data_dir / "api-token").read_text().strip() == "hp_tok_abcdef"
        assert (data_dir / "ssh" / "id_ed25519").exists()

    def test_secrets_tarball_is_marked_and_banners_the_operator(self, tmp_path):
        data_dir = tmp_path / "hp"
        tarball = tmp_path / "backup.tar.gz"
        _seed_host(data_dir)

        result = _export(data_dir, tarball, "--include-secrets")
        assert result.exit_code == 0, _out(result)
        assert "CONTAINS SECRETS" in _out(result)

        with tarfile.open(str(tarball)) as tar:
            handle = tar.extractfile("manifest.json")
            assert handle is not None
            manifest = json.loads(handle.read())
            names = {m.name for m in tar.getmembers()}
        assert manifest["includes_secrets"] is True
        assert manifest["db_schema_version"] == max(MIGRATIONS.keys())
        assert "secrets/vault/identities/master.protected" in names
        assert "secrets/vault/secrets/pve-token.age" in names
        # The tarball is a credential: it must not be world readable.
        assert tarball.stat().st_mode & 0o077 == 0


class TestDefaultExportCannotRestoreASecretsHost:
    def test_default_export_warns_loudly_and_loses_the_vault(self, tmp_path):
        data_dir = tmp_path / "hp"
        tarball = tmp_path / "backup.tar.gz"
        _seed_host(data_dir)

        result = _export(data_dir, tarball)
        assert result.exit_code == 0, _out(result)
        output = _out(result)
        assert "CANNOT restore a working host" in output
        assert "vault/secrets" in output
        assert "vault/identities" in output
        assert "--include-secrets" in output

        with tarfile.open(str(tarball)) as tar:
            names = {m.name for m in tar.getmembers()}
            handle = tar.extractfile("manifest.json")
            assert handle is not None
            manifest = json.loads(handle.read())
        assert manifest["includes_secrets"] is False
        assert not any(n.startswith("secrets/") for n in names)

        shutil.rmtree(data_dir)
        result = _import(data_dir, tarball)
        assert result.exit_code == 0, _out(result)

        # The data came back; the ability to read it did not.
        assert _hostnames(data_dir / "homepilot.db") == ["pve1.lan"]
        assert not (data_dir / "vault" / "secrets" / "pve-token.age").exists()
        with pytest.raises(VaultError):
            _vault_secret(data_dir, PASSPHRASE)

    def test_restore_secrets_refused_when_archive_has_none(self, tmp_path):
        data_dir = tmp_path / "hp"
        tarball = tmp_path / "backup.tar.gz"
        _seed_host(data_dir)
        assert _export(data_dir, tarball).exit_code == 0

        result = _import(data_dir, tarball, "--restore-secrets")
        assert result.exit_code == 1
        assert "no secrets" in _out(result)


class TestWalTornSnapshot:
    """Rows committed but not yet checkpointed live in homepilot.db-wal.

    A tar of the .db file alone misses them. This is red against any file copy.
    """

    def test_export_captures_rows_still_in_the_wal(self, tmp_path):
        data_dir = tmp_path / "hp"
        tarball = tmp_path / "backup.tar.gz"
        _seed_host(data_dir)
        db_path = data_dir / "homepilot.db"

        # Hold the connection open so SQLite cannot checkpoint on close.
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            now = datetime.now(UTC).isoformat()
            conn.execute(
                "INSERT INTO hosts (id, hostname, host_type, role, status, "
                "created_at, updated_at) VALUES (?, ?, 'vm', 'guest', 'running', ?, ?)",
                ("host-2", "in-the-wal.lan", now, now),
            )
            conn.commit()

            wal = db_path.with_name(db_path.name + "-wal")
            assert wal.exists() and wal.stat().st_size > 0, "test setup: no uncheckpointed WAL"
            # The bare .db file does NOT hold the row yet - that is the trap.
            result = _export(data_dir, tarball, "--include-secrets")
            assert result.exit_code == 0, _out(result)
        finally:
            conn.close()

        with tarfile.open(str(tarball)) as tar:
            names = {m.name for m in tar.getmembers()}
        # Sidecars must never be archived: they would replay on restore.
        assert not any(n.endswith(("-wal", "-shm")) for n in names)

        target = tmp_path / "fresh"
        result = _import(target, tarball, "--restore-secrets")
        assert result.exit_code == 0, _out(result)
        assert _hostnames(target / "homepilot.db") == ["in-the-wal.lan", "pve1.lan"]


class TestStaleSidecarsAreRemoved:
    def test_stale_wal_without_a_db_cannot_replay_onto_the_restore(self, tmp_path):
        """The classic corruption: db removed, -wal/-shm left behind, then restore.

        SQLite replays the orphan WAL onto the restored file and the operator
        gets the OLD rows back from a "successful" restore.
        """
        source = tmp_path / "src"
        tarball = tmp_path / "backup.tar.gz"
        _seed_host(source, hostname="restored.lan")
        assert _export(source, tarball, "--include-secrets").exit_code == 0

        target = tmp_path / "hp"
        _seed_host(target, hostname="stale.lan")
        target_db = target / "homepilot.db"
        conn = sqlite3.connect(str(target_db))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        now = datetime.now(UTC).isoformat()
        conn.execute(
            "INSERT INTO hosts (id, hostname, host_type, role, status, created_at, updated_at) "
            "VALUES (?, ?, 'vm', 'guest', 'running', ?, ?)",
            ("host-stale", "stale-wal.lan", now, now),
        )
        conn.commit()
        stash = tmp_path / "stash"
        stash.mkdir()
        for suffix in ("-wal", "-shm"):
            shutil.copy2(str(target_db.with_name(target_db.name + suffix)), str(stash / suffix))
        conn.close()

        # The db file is gone, the sidecars are not.
        target_db.unlink()
        for suffix in ("-wal", "-shm"):
            shutil.copy2(str(stash / suffix), str(target_db.with_name(target_db.name + suffix)))

        result = _import(target, tarball, "--restore-secrets")
        assert result.exit_code == 0, _out(result)

        assert not target_db.with_name(target_db.name + "-wal").exists()
        assert not target_db.with_name(target_db.name + "-shm").exists()
        # This is the assertion with teeth: with the sidecars left in place the
        # restored database reads back the OLD host, not the archived one.
        assert _hostnames(target_db) == ["restored.lan"]

    def test_import_over_a_live_looking_db_leaves_no_sidecars(self, tmp_path):
        source = tmp_path / "src"
        tarball = tmp_path / "backup.tar.gz"
        _seed_host(source, hostname="restored.lan")
        assert _export(source, tarball, "--include-secrets").exit_code == 0

        target = tmp_path / "hp"
        _seed_host(target, hostname="stale.lan")
        target_db = target / "homepilot.db"

        result = _import(target, tarball, "--restore-secrets")
        assert result.exit_code == 0, _out(result)
        assert not target_db.with_name(target_db.name + "-wal").exists()
        assert not target_db.with_name(target_db.name + "-shm").exists()
        assert _hostnames(target_db) == ["restored.lan"]


class TestImportRefusals:
    def test_refuses_while_the_database_is_held_open(self, tmp_path):
        source = tmp_path / "src"
        tarball = tmp_path / "backup.tar.gz"
        _seed_host(source, hostname="restored.lan")
        assert _export(source, tarball, "--include-secrets").exit_code == 0

        target = tmp_path / "hp"
        _seed_host(target, hostname="live.lan")

        # Stand in for the running backend: an open connection, idle.
        holder = Database(str(target / "homepilot.db"))
        asyncio.run(holder.connect())
        try:
            result = _import(target, tarball, "--restore-secrets")
        finally:
            asyncio.run(holder.close())

        assert result.exit_code == 1
        assert "in use" in _out(result)
        # Fail closed means nothing moved. Check for the IMPORT's own backup
        # rather than the backups/ directory as a whole: run_migrations writes a
        # pre-migration snapshot there whenever it migrates (#420), so seeding
        # the target already created the directory.
        assert _hostnames(target / "homepilot.db") == ["live.lan"]
        assert not list((target / "backups").glob("pre-import-*"))

    def test_refuses_a_manifest_from_a_newer_build(self, tmp_path):
        tarball = _make_tarball(
            tmp_path / "future.tar.gz",
            {"manifest_schema_version": 99, "includes_secrets": False},
        )
        result = _import(tmp_path / "hp", tarball)
        assert result.exit_code == 1
        assert "newer than this build supports" in _out(result)

    def test_refuses_a_database_schema_from_a_newer_build(self, tmp_path):
        tarball = _make_tarball(
            tmp_path / "future-db.tar.gz",
            {
                "manifest_schema_version": 1,
                "includes_secrets": False,
                "db_schema_version": max(MIGRATIONS.keys()) + 5,
            },
        )
        result = _import(tmp_path / "hp", tarball)
        assert result.exit_code == 1
        assert "newer than this build supports" in _out(result)

    def test_refuses_a_tarball_without_a_manifest(self, tmp_path):
        """Pre-fix tarballs hold a raw live-WAL DB copy; they are not trustworthy."""
        legacy = tmp_path / "legacy.tar.gz"
        payload = tmp_path / "homepilot.db"
        payload.write_bytes(b"not really a database")
        with tarfile.open(str(legacy), "w:gz") as tar:
            tar.add(str(payload), arcname="homepilot.db")

        result = _import(tmp_path / "hp", legacy)
        assert result.exit_code == 1
        assert "no manifest.json" in _out(result)

    def test_rejects_path_traversal_members(self, tmp_path):
        evil = tmp_path / "evil.tar.gz"
        payload = tmp_path / "payload"
        payload.write_text("pwned\n", encoding="utf-8")
        with tarfile.open(str(evil), "w:gz") as tar:
            tar.add(str(payload), arcname="../escaped")

        result = _import(tmp_path / "hp", evil)
        assert result.exit_code == 1
        assert "Unsafe path" in _out(result)
        assert not (tmp_path / "escaped").exists()


class TestImportMigratesAndBacksUp:
    def test_old_schema_archive_is_migrated_to_the_current_version(self, tmp_path):
        old_target = 10
        subset = {v: MIGRATIONS[v] for v in range(1, old_target + 1)}
        data_dir = tmp_path / "hp"
        tarball = tmp_path / "backup.tar.gz"

        with patch.dict(MIGRATIONS, subset, clear=True):
            _seed_host(data_dir)
            assert read_schema_version(data_dir / "homepilot.db") == old_target
            assert _export(data_dir, tarball, "--include-secrets").exit_code == 0

        target = tmp_path / "fresh"
        result = _import(target, tarball, "--restore-secrets")
        assert result.exit_code == 0, _out(result)
        assert read_schema_version(target / "homepilot.db") == max(MIGRATIONS.keys())
        # Migrating must not cost the restored rows.
        assert _hostnames(target / "homepilot.db") == ["pve1.lan"]

    def test_current_state_is_backed_up_before_being_overwritten(self, tmp_path):
        source = tmp_path / "src"
        tarball = tmp_path / "backup.tar.gz"
        _seed_host(source, hostname="restored.lan")
        assert _export(source, tarball, "--include-secrets").exit_code == 0

        target = tmp_path / "hp"
        _seed_host(target, hostname="doomed.lan")
        old_identity = (target / "vault" / "identities" / "master.protected").read_bytes()

        result = _import(target, tarball, "--restore-secrets")
        assert result.exit_code == 0, _out(result)

        backups = sorted((target / "backups").glob("pre-import-*"))
        assert len(backups) == 1
        backup = backups[0]
        assert _hostnames(backup / "homepilot.db") == ["doomed.lan"]
        assert (backup / "artifacts" / "2026" / "08" / "note.md").exists()
        # Live key material is never destroyed without a copy.
        stashed = backup / "secrets" / "vault" / "identities" / "master.protected"
        assert stashed.read_bytes() == old_identity


def _make_tarball(path: Path, manifest: dict) -> Path:
    with tempfile.TemporaryDirectory() as staging_str:
        staging = Path(staging_str)
        manifest_path = staging / "manifest.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with tarfile.open(str(path), "w:gz") as tar:
            tar.add(str(manifest_path), arcname="manifest.json")
    return path
