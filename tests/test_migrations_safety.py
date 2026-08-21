from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from homepilot.db.connection import Database
from homepilot.db.migrations import MIGRATIONS, run_migrations

TARGET_VERSION = max(MIGRATIONS.keys())
BROKEN_VERSION = TARGET_VERSION + 1


@pytest.fixture
async def db(tmp_path: Path):
    database = Database(str(tmp_path / "homepilot.db"))
    await database.connect()
    yield database
    await database.close()


async def _schema_version(database: Database) -> int | None:
    try:
        row = await database.fetchone("SELECT value FROM settings WHERE key = 'schema_version'")
    except sqlite3.OperationalError:
        return None
    return int(row["value"]) if row else None


async def _tables(database: Database) -> set[str]:
    rows = await database.fetchall("SELECT name FROM sqlite_master WHERE type = 'table'")
    return {r["name"] for r in rows}


class TestFailedMigrationRollsBack:
    """G1 (#420): a mid-version failure must leave no half-applied DDL behind."""

    async def test_broken_version_rolls_back_and_retry_succeeds(self, db, monkeypatch):
        monkeypatch.setitem(
            MIGRATIONS,
            BROKEN_VERSION,
            [
                "ALTER TABLE tasks RENAME TO tasks_broken_old",
                "THIS IS NOT VALID SQL",
            ],
        )

        with pytest.raises(RuntimeError) as excinfo:
            await run_migrations(db)

        message = str(excinfo.value)
        assert f"version {BROKEN_VERSION}" in message
        # A clean rollback must NOT tell the operator to restore the backup -
        # the database is intact and a plain retry is the correct recovery.
        assert "no restore needed" in message
        assert "Restore" not in message

        # The failed version left nothing behind and did not claim its version.
        assert await _schema_version(db) == TARGET_VERSION
        tables = await _tables(db)
        assert "tasks_broken_old" not in tables
        assert "tasks" in tables

        # A retry against the (fixed) migration set starts clean and completes.
        monkeypatch.delitem(MIGRATIONS, BROKEN_VERSION)
        await run_migrations(db)
        assert await _schema_version(db) == TARGET_VERSION

        await db.execute(
            "INSERT INTO tasks (id, artifact_id, action, status, created_at) "
            "VALUES ('t1', 'a1', 'apply', 'cancelled', '2026-01-01T00:00:00Z')"
        )
        await db.conn.commit()
        row = await db.fetchone("SELECT status FROM tasks WHERE id = 't1'")
        assert row is not None
        assert row["status"] == "cancelled"


class TestAlterFailuresAreNotSwallowed:
    """G2/G3: only a duplicate-column ALTER is survivable."""

    async def test_failing_rename_raises(self, db, monkeypatch):
        monkeypatch.setitem(
            MIGRATIONS,
            BROKEN_VERSION,
            ["ALTER TABLE table_that_does_not_exist RENAME TO whatever"],
        )

        with pytest.raises(RuntimeError, match=f"version {BROKEN_VERSION}"):
            await run_migrations(db)

        assert await _schema_version(db) == TARGET_VERSION

    async def test_duplicate_column_alter_is_skipped(self, db, monkeypatch):
        monkeypatch.setitem(
            MIGRATIONS,
            BROKEN_VERSION,
            [
                # agents.revoked_at already exists from version 12.
                "ALTER TABLE agents ADD COLUMN revoked_at TEXT",
                "CREATE TABLE IF NOT EXISTS dup_column_probe (id INTEGER PRIMARY KEY)",
            ],
        )

        await run_migrations(db)

        assert await _schema_version(db) == BROKEN_VERSION
        assert "dup_column_probe" in await _tables(db)


class TestDowngradeGuard:
    """G4: an older build must refuse a future schema instead of mangling it."""

    async def test_future_schema_version_raises_without_executing(self, db, monkeypatch):
        await db.execute(
            "CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT)"
        )
        future = TARGET_VERSION + 5
        await db.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES ('schema_version', ?, ?)",
            (str(future), "2026-01-01T00:00:00Z"),
        )
        await db.conn.commit()

        executed: list[str] = []

        async def _spy(query: str, params: object = None):
            executed.append(query)
            raise AssertionError("no statement may run on a future schema")

        monkeypatch.setattr(db, "execute", _spy)

        with pytest.raises(RuntimeError) as excinfo:
            await run_migrations(db)

        message = str(excinfo.value)
        assert str(future) in message
        assert str(TARGET_VERSION) in message
        assert "backup" in message.lower()
        assert executed == []
        assert not (Path(db.db_path).parent / "backups").exists()


class TestPreMigrationBackup:
    """G5 + fail-closed backup handling."""

    async def test_backup_holds_pre_migration_schema_version(self, db, monkeypatch):
        await run_migrations(db)
        assert await _schema_version(db) == TARGET_VERSION

        monkeypatch.setitem(
            MIGRATIONS,
            BROKEN_VERSION,
            ["CREATE TABLE IF NOT EXISTS backup_probe (id INTEGER PRIMARY KEY)"],
        )
        await run_migrations(db)
        assert await _schema_version(db) == BROKEN_VERSION

        backup = Path(db.db_path).parent / "backups" / f"pre-migration-v{TARGET_VERSION}.db"
        assert backup.is_file()

        conn = sqlite3.connect(str(backup))
        try:
            value = conn.execute(
                "SELECT value FROM settings WHERE key = 'schema_version'"
            ).fetchone()[0]
            tables = {
                r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
        finally:
            conn.close()

        assert int(value) == TARGET_VERSION
        assert "backup_probe" not in tables

    async def test_backup_failure_aborts_migrations(self, tmp_path):
        # An unwritable backup target: migrations must not run without a backup.
        # The database must hold something worth protecting, otherwise there is
        # correctly no backup to fail - see the empty-database test below.
        db_file = tmp_path / "homepilot.db"
        seed = Database(str(db_file))
        await seed.connect()
        await seed.execute("CREATE TABLE legacy_rows (id INTEGER PRIMARY KEY)")
        await seed.conn.commit()
        await seed.close()
        (tmp_path / "backups" / "pre-migration-v0.db").mkdir(parents=True)

        database = Database(str(db_file))
        await database.connect()
        try:
            with pytest.raises(RuntimeError, match="backup"):
                await run_migrations(database)
            assert await _schema_version(database) is None
            assert "hosts" not in await _tables(database)
        finally:
            await database.close()

    async def test_an_empty_database_is_not_backed_up(self, tmp_path):
        """Nothing to lose, nothing to copy.

        A fresh install - and every test that builds its own database - would
        otherwise pay for a snapshot of zero rows on every start, and litter the
        data dir with pre-migration-v0.db files that can restore nothing.
        Deliberately decided on the TABLES rather than on version 0: a legacy
        database predating the schema_version row also reports 0 and does have
        data to protect (asserted by the test above, which seeds a table).
        """
        database = Database(str(tmp_path / "homepilot.db"))
        await database.connect()
        try:
            await run_migrations(database)
            assert await _schema_version(database) == TARGET_VERSION
        finally:
            await database.close()

        assert not (tmp_path / "backups").exists()

    async def test_memory_database_skips_backup(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        database = Database(":memory:")
        await database.connect()
        try:
            await run_migrations(database)
            assert await _schema_version(database) == TARGET_VERSION
        finally:
            await database.close()

        assert not (tmp_path / "backups").exists()


class TestV15TasksRebuildAndHostsOwner:
    """Migration 15 (#442): tasks gains 'provision' + a nullable artifact_id, and
    hosts gains ``owner``.

    Teeth: drop 'provision' from the v15 CHECK, or restore ``artifact_id TEXT NOT
    NULL``, and the inserts below fail with an IntegrityError.
    """

    async def _migrate_to_14(self, database: Database) -> None:
        # Walk the real migration path up to 14 so the rebuild runs against the
        # actual pre-v15 schema, not a hand-written approximation.
        original = dict(MIGRATIONS)
        try:
            for version in list(MIGRATIONS):
                if version > 14:
                    del MIGRATIONS[version]
            await run_migrations(database)
        finally:
            MIGRATIONS.clear()
            MIGRATIONS.update(original)

    async def test_rebuild_preserves_existing_task_rows(self, db):
        await self._migrate_to_14(db)
        assert await _schema_version(db) == 14
        await db.execute(
            "INSERT INTO tasks (id, artifact_id, action, status, result_json, created_at, "
            "finished_at, error) VALUES "
            "('t1', 'art-1', 'apply', 'succeeded', '{\"ok\":1}', '2026-01-01T00:00:00Z', "
            "'2026-01-01T00:01:00Z', NULL)"
        )
        await db.execute(
            "INSERT INTO tasks (id, artifact_id, action, status, created_at) "
            "VALUES ('t2', 'art-2', 'revoke', 'cancelled', '2026-01-02T00:00:00Z')"
        )
        await db.conn.commit()

        await run_migrations(db)
        assert await _schema_version(db) == TARGET_VERSION

        rows = {r["id"]: r for r in await db.fetchall("SELECT * FROM tasks ORDER BY id")}
        assert set(rows) == {"t1", "t2"}
        assert rows["t1"]["artifact_id"] == "art-1"
        assert rows["t1"]["result_json"] == '{"ok":1}'
        assert rows["t1"]["finished_at"] == "2026-01-01T00:01:00Z"
        assert rows["t2"]["status"] == "cancelled"

    async def test_provision_task_with_null_artifact_id_inserts(self, db):
        await run_migrations(db)
        await db.execute(
            "INSERT INTO tasks (id, artifact_id, action, status, created_at) "
            "VALUES ('p1', NULL, 'provision', 'pending', '2026-01-03T00:00:00Z')"
        )
        await db.conn.commit()
        row = await db.fetchone("SELECT * FROM tasks WHERE id = 'p1'")
        assert row is not None
        assert row["artifact_id"] is None
        assert row["action"] == "provision"

    async def test_apply_with_artifact_id_still_works(self, db):
        await run_migrations(db)
        await db.execute(
            "INSERT INTO tasks (id, artifact_id, action, status, created_at) "
            "VALUES ('a1', 'art-9', 'apply', 'pending', '2026-01-03T00:00:00Z')"
        )
        await db.conn.commit()
        row = await db.fetchone("SELECT * FROM tasks WHERE id = 'a1'")
        assert row is not None
        assert row["artifact_id"] == "art-9"

    async def test_unknown_action_is_still_rejected(self, db):
        await run_migrations(db)
        with pytest.raises(sqlite3.IntegrityError):
            await db.execute(
                "INSERT INTO tasks (id, artifact_id, action, status, created_at) "
                "VALUES ('x1', NULL, 'destroy', 'pending', '2026-01-03T00:00:00Z')"
            )

    async def test_task_indexes_exist_on_the_rebuilt_table(self, db):
        await run_migrations(db)
        rows = await db.fetchall(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'tasks'"
        )
        names = {r["name"] for r in rows}
        assert {"idx_tasks_artifact", "idx_tasks_status"} <= names

    async def test_hosts_owner_column_added_and_idempotent(self, db):
        await run_migrations(db)
        cols = {c["name"] for c in await db.fetchall("PRAGMA table_info(hosts)")}
        assert "owner" in cols
        # Re-running migrations must not fail on the already-present column.
        await run_migrations(db)
        assert await _schema_version(db) == TARGET_VERSION
