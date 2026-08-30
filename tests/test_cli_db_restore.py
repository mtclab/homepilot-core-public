"""`hp db restore` - the supported form of the operation that had none (#648 t4).

HomePilot writes `<data_dir>/backups/pre-migration-v<N>.db` before every schema
migration and its own refusal names that file. Until this command there was no
way to put one back except `cp`, which corrupts the database: SQLite replays the
`-wal` left by the database being replaced onto the file that replaced it. That
destroyed a dev database and the backup being restored, in one command, on
2026-08-29.

Both gates below correspond to a failure found by RUNNING the command against a
genuinely malformed dev database - neither showed up in unit tests written
against healthy fixtures.
"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
import traceback
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from homepilot.cli.main import app
from homepilot.config import get_settings
from homepilot.db.backup import integrity_problems, wal_sidecars
from homepilot.db.connection import Database
from homepilot.db.migrations import MIGRATIONS, run_migrations

runner = CliRunner()


@pytest.fixture(autouse=True)
def clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _out(result) -> str:
    """The command's output as ONE line, whitespace collapsed.

    `rich` hard-wraps to the console width, so a phrase a test asserts on gets a
    newline wherever the terminal happens to end: `"is not a sound SQLite
    database"` came back as `"is not \na sound\nSQLite database"` on CI and
    passed here. conftest pins COLUMNS so that cannot happen, and this collapses
    whitespace so an assertion is about the WORDS, not the layout.
    """
    text = result.stdout
    with contextlib.suppress(ValueError):
        text += result.stderr
    if result.exception is not None and not isinstance(result.exception, SystemExit):
        text += "".join(
            traceback.format_exception(
                type(result.exception), result.exception, result.exception.__traceback__
            )
        )
    return " ".join(text.split())


async def _seed(db_path: Path, hostname: str) -> None:
    db = Database(str(db_path))
    await db.connect()
    try:
        await run_migrations(db)
        await db.execute(
            "INSERT INTO hosts (id, hostname, host_type, role, status, created_at, updated_at) "
            "VALUES ('h1', ?, 'vm', 'guest', 'running', 'x', 'x')",
            (hostname,),
        )
        await db.conn.commit()
    finally:
        await db.close()


def _env(data_dir: Path) -> dict[str, str]:
    return {
        "HP_DATA_DIR": str(data_dir),
        "HP_ARTIFACTS_DIR": str(data_dir / "artifacts"),
        "HP_VAULT_PASSPHRASE": "restore-gate-passphrase",
    }


def _hostnames(db_path: Path) -> list[str]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return [r[0] for r in conn.execute("SELECT hostname FROM hosts ORDER BY hostname")]
    finally:
        conn.close()


def _restore(data_dir: Path, source: Path, *extra: str):
    get_settings.cache_clear()
    with patch.dict("os.environ", _env(data_dir), clear=True):
        return runner.invoke(app, ["db", "restore", str(source), "--yes", *extra])


class TestDbRestore:
    def test_it_restores_and_clears_the_sidecars(self, tmp_path):
        data_dir = tmp_path / "hp"
        data_dir.mkdir()
        db_path = data_dir / "homepilot.db"
        asyncio.run(_seed(db_path, "live.lan"))

        backup = tmp_path / "pre-migration-vN.db"
        asyncio.run(_seed(backup, "backup.lan"))

        # A stale journal beside the live database - what an OOM kill leaves.
        wal, shm = wal_sidecars(db_path)
        wal.write_bytes(b"stale journal")
        shm.write_bytes(b"stale index")

        result = _restore(data_dir, backup)
        assert result.exit_code == 0, _out(result)
        # The restore runs migrations afterwards, so a FRESH `-wal` is expected.
        # What must not survive is the STALE one - the thing that gets replayed
        # into the file that replaced its database.
        assert not wal.exists() or wal.read_bytes() != b"stale journal"
        assert not shm.exists() or shm.read_bytes() != b"stale index"
        assert integrity_problems(db_path) == []
        assert _hostnames(db_path) == ["backup.lan"]

        # What it replaced is kept.
        saved = sorted((data_dir / "backups").glob("pre-restore-*"))
        assert len(saved) == 1
        assert _hostnames(saved[0] / "homepilot.db") == ["live.lan"]

    def test_it_refuses_a_corrupt_source_file(self, tmp_path):
        """A corrupt backup laid over a working database loses both copies."""
        data_dir = tmp_path / "hp"
        data_dir.mkdir()
        db_path = data_dir / "homepilot.db"
        asyncio.run(_seed(db_path, "live.lan"))

        good = tmp_path / "good.db"
        asyncio.run(_seed(good, "backup.lan"))
        broken = tmp_path / "broken.db"
        broken.write_bytes(good.read_bytes()[:2048] + b"\xff" * 8192)

        result = _restore(data_dir, broken)
        assert result.exit_code == 1
        assert "not a sound" in _out(result)
        assert _hostnames(db_path) == ["live.lan"], "the live database was touched anyway"

    def test_it_works_when_the_live_database_is_corrupt(self, tmp_path):
        """The case the command exists for, and the one it first failed.

        Two defects, both found by running it against a genuinely malformed dev
        database rather than a fixture: `read_schema_version` let
        `DatabaseError` escape (traceback before anything happened), and the
        pre-restore snapshot - `VACUUM INTO`, which reads every page - cannot
        snapshot a corrupt file, so the command failed closed and left the
        operator with no way forward.
        """
        data_dir = tmp_path / "hp"
        data_dir.mkdir()
        db_path = data_dir / "homepilot.db"
        asyncio.run(_seed(db_path, "live.lan"))
        healthy_bytes = db_path.read_bytes()
        db_path.write_bytes(healthy_bytes[:2048] + b"\xff" * 8192 + healthy_bytes[10240:])
        assert integrity_problems(db_path) != [], "the fixture is not actually corrupt"

        backup = tmp_path / "pre-migration-vN.db"
        asyncio.run(_seed(backup, "backup.lan"))

        result = _restore(data_dir, backup)
        assert result.exit_code == 0, _out(result)
        assert integrity_problems(db_path) == []
        assert _hostnames(db_path) == ["backup.lan"]

        # The unrestorable original is kept as evidence, under a name SQLite
        # will never treat as a database beside a journal.
        saved = sorted((data_dir / "backups").glob("pre-restore-*"))
        assert len(saved) == 1
        assert (saved[0] / "homepilot.db.corrupt").exists()
        assert not (saved[0] / "homepilot.db").exists()
        assert "EVIDENCE, not a backup" in _out(result)

    def test_it_refuses_while_a_backend_holds_the_database(self, tmp_path):
        data_dir = tmp_path / "hp"
        data_dir.mkdir()
        db_path = data_dir / "homepilot.db"
        asyncio.run(_seed(db_path, "live.lan"))
        backup = tmp_path / "backup.db"
        asyncio.run(_seed(backup, "backup.lan"))

        holder = sqlite3.connect(str(db_path))
        holder.execute("PRAGMA journal_mode=WAL")
        holder.execute("BEGIN IMMEDIATE")
        try:
            result = _restore(data_dir, backup)
        finally:
            holder.rollback()
            holder.close()
        assert result.exit_code == 1
        assert "in use" in _out(result)
        assert _hostnames(db_path) == ["live.lan"]

    def test_it_refuses_a_source_newer_than_this_build(self, tmp_path):
        data_dir = tmp_path / "hp"
        data_dir.mkdir()
        db_path = data_dir / "homepilot.db"
        asyncio.run(_seed(db_path, "live.lan"))

        newer = tmp_path / "newer.db"
        asyncio.run(_seed(newer, "future.lan"))
        conn = sqlite3.connect(str(newer))
        conn.execute(
            "UPDATE settings SET value = ? WHERE key = 'schema_version'",
            (str(max(MIGRATIONS.keys()) + 3),),
        )
        conn.commit()
        conn.close()

        result = _restore(data_dir, newer)
        assert result.exit_code == 1
        assert "newer than this build supports" in _out(result)
        assert _hostnames(db_path) == ["live.lan"]

    def test_it_refuses_the_live_database_as_its_own_source(self, tmp_path):
        data_dir = tmp_path / "hp"
        data_dir.mkdir()
        db_path = data_dir / "homepilot.db"
        asyncio.run(_seed(db_path, "live.lan"))

        result = _restore(data_dir, db_path)
        assert result.exit_code == 1
        assert "live database" in _out(result)


class TestDbCheck:
    def test_it_passes_a_sound_database_and_fails_a_corrupt_one(self, tmp_path):
        data_dir = tmp_path / "hp"
        data_dir.mkdir()
        db_path = data_dir / "homepilot.db"
        asyncio.run(_seed(db_path, "live.lan"))

        get_settings.cache_clear()
        with patch.dict("os.environ", _env(data_dir), clear=True):
            result = runner.invoke(app, ["db", "check"])
        assert result.exit_code == 0, _out(result)
        assert "quick_check" in _out(result)

        healthy = db_path.read_bytes()
        db_path.write_bytes(healthy[:2048] + b"\xff" * 8192 + healthy[10240:])
        get_settings.cache_clear()
        with patch.dict("os.environ", _env(data_dir), clear=True):
            result = runner.invoke(app, ["db", "check"])
        assert result.exit_code == 1
        assert "CORRUPT" in _out(result)
