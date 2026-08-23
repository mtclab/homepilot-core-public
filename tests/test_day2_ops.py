"""Day-2 operations: retention, logging, and one backend per data dir (#431).

Three slow-burn defects, each invisible until the day it matters:

* **nothing was ever pruned.** No retention DELETE existed anywhere. `audit_log`,
  `agent_audit`, `tasks` and `webhook_deliveries` gain a row per operation - a
  year on a homelab VM is a multi-GB SQLite file and a backup too big to move.
* **`settings.log_level` was read by nothing.** Defined, documented in
  `.env.example`, and no `basicConfig` anywhere outside the MCP entrypoints, so
  every `logger.debug` diagnostic was invisible in production and could not be
  turned on. The diagnostics existed; the switch did not.
* **a second backend killed the first one's work.** `fail_orphaned_tasks()` ran
  unconditionally at every start, marking every pending/running task failed - so
  a rolling restart or a stray `docker compose up` failed an apply that was
  halfway through a host while it carried on running.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.db.repository import Repository
from homepilot.instance_lock import InstanceLock, InstanceLockError, another_instance_is_running
from homepilot.main import configure_logging
from homepilot.reconciler.retention import RetentionReconciler
from homepilot.tasks.repository import TaskRepository

pytestmark = pytest.mark.asyncio


def _iso(days_ago: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
async def db(tmp_path: Path):
    database = Database(str(tmp_path / "homepilot.db"))
    await database.connect()
    await run_migrations(database)
    yield database, Repository(database)
    await database.close()


class TestRetentionPrunesTheUnboundedTables:
    async def test_old_audit_rows_go_and_recent_ones_stay(self, db):
        database, repo = db
        for days in (200, 120, 3):
            await database.execute(
                "INSERT INTO audit_log (timestamp, user_id, source, action) VALUES (?, ?, ?, ?)",
                (_iso(days), "admin", "ui", "apply"),
            )
        await database.conn.commit()

        result = await RetentionReconciler(repo, retention_days=90).run()

        rows = await database.fetchall("SELECT timestamp FROM audit_log")
        assert len(rows) == 1, "the recent entry was pruned too"
        assert result.details["deleted"]["audit_log"] == 2

    async def test_a_finished_task_is_pruned(self, db):
        database, repo = db
        tasks = TaskRepository(database)
        task_id = await tasks.create_task("2026-01-01-old", "apply")
        await tasks.update_task_status(task_id, "succeeded")
        await database.execute("UPDATE tasks SET created_at = ? WHERE id = ?", (_iso(200), task_id))
        await database.conn.commit()

        await RetentionReconciler(repo, retention_days=90).run()

        assert await tasks.get_task(task_id) is None

    async def test_a_stuck_task_is_never_pruned(self, db):
        """A pending or running task older than the horizon is a stuck task.
        Deleting it would hide the problem and strand whatever waits on it."""
        database, repo = db
        tasks = TaskRepository(database)
        task_id = await tasks.create_task("2026-01-01-stuck", "apply")
        await tasks.update_task_status(task_id, "running")
        await database.execute("UPDATE tasks SET created_at = ? WHERE id = ?", (_iso(200), task_id))
        await database.conn.commit()

        await RetentionReconciler(repo, retention_days=90).run()

        assert await tasks.get_task(task_id) is not None, "a stuck task was quietly deleted"

    async def test_artifacts_are_never_pruned(self, db):
        """They are the record of intent, not history. A retention policy that
        eats them destroys the thing the product is for."""
        database, repo = db
        await repo.create_artifact(
            id="2020-01-01-ancient",
            kind="kb-note",
            intent="very old",
            status="applied",
            mutating=False,
            hash="x",
            target_json=None,
            idempotence=None,
            produced_by_json="{}",
            file_path="2020/01/2020-01-01-ancient.md",
        )

        await RetentionReconciler(repo, retention_days=1).run()

        rows = await database.fetchall("SELECT id FROM artifacts")
        assert len(rows) == 1

    async def test_it_refuses_an_unknown_table(self, db):
        """The table name is interpolated into SQL, so it may only ever come
        from this module's allowlist."""
        _database, repo = db

        with pytest.raises(ValueError, match="refusing to prune"):
            await repo.prune_before("api_tokens", "created_at", _iso(1))

    async def test_a_horizon_of_zero_is_floored_to_a_day(self, db):
        """Zero would delete everything written this instant, which is never
        what an operator means."""
        _database, repo = db
        assert RetentionReconciler(repo, retention_days=0)._retention_days == 1


class TestTheLogLevelSwitchIsWired:
    async def test_it_applies_the_configured_level(self):
        try:
            configure_logging("debug")
            assert logging.getLogger("homepilot").level == logging.DEBUG
        finally:
            configure_logging("info")

    async def test_an_unknown_level_falls_back_rather_than_crashing_the_boot(self):
        try:
            configure_logging("chatty")
            assert logging.getLogger("homepilot").level == logging.INFO
        finally:
            configure_logging("info")


class TestOneBackendPerDataDirectory:
    async def test_a_second_instance_is_refused(self, tmp_path: Path):
        """The second backend used to mark the first one's running tasks failed
        while they carried on running."""
        first = InstanceLock(tmp_path)
        first.acquire()
        try:
            with pytest.raises(InstanceLockError, match="already using"):
                InstanceLock(tmp_path).acquire()
        finally:
            first.release()

    async def test_the_lock_is_released_on_exit(self, tmp_path: Path):
        with InstanceLock(tmp_path):
            pass

        second = InstanceLock(tmp_path)
        second.acquire()  # must not raise
        second.release()
        assert another_instance_is_running(tmp_path) is False

    async def test_probing_does_not_become_the_holder(self, tmp_path: Path):
        """The CLI asks whether a server is running; asking must not make the
        answer yes for everyone after it."""
        assert another_instance_is_running(tmp_path) is False
        assert another_instance_is_running(tmp_path) is False

    async def test_probing_sees_a_held_lock(self, tmp_path: Path):
        lock = InstanceLock(tmp_path)
        lock.acquire()
        try:
            assert another_instance_is_running(tmp_path) is True
        finally:
            lock.release()


class TestTheCliWillNotMigrateUnderARunningServer:
    async def test_every_cli_migration_goes_through_the_guard(self):
        """Five copies of a guard is five chances to forget the fifth, so the
        rule is that the CLI never calls `run_migrations` directly."""
        source = (
            Path(__file__).resolve().parents[1] / "src" / "homepilot" / "cli" / "main.py"
        ).read_text(encoding="utf-8")
        body = source[source.index("async def _migrate_or_refuse") :]
        # The helper itself is the one legitimate caller.
        helper_end = body.index("\ndef _refuse_if_server_running")
        after_helper = body[helper_end:]

        assert "await run_migrations(" not in after_helper, (
            "a CLI command migrates directly again - it can change the schema "
            "under a running backend"
        )
