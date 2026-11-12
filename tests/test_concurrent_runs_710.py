"""One artifact, one run in flight - enforced by the database (#710).

WHAT THIS FORBIDS, all reproduced on the shipped path:

* two concurrent ``start_replay`` (or ``start_revoke``) calls on one artifact
  both dispatched: the runner checked for an active task, awaited, then
  inserted, so both checks passed before either insert;
* an auto-apply tick during a running apply dispatched the same artifact again
  (it stays ``approved`` while applying, and the reconciler called the executor
  with no task-row gate);
* the second apply then died on ``applied -> applied`` with a ``ConflictError``
  the reconciler did not catch, and every later approved artifact in that tick
  was skipped.

The contract now: every apply/replay/revoke - API, MCP, CLI-through-API and
auto-apply - claims the artifact's one slot with a single INSERT that a partial
UNIQUE index (migration 34) refuses while another pending/running row exists, so
it holds across connections and processes. A refused claim is a 409 that names
the task holding the slot.

Teeth: restore the check-then-insert in ``start_replay``/``start_revoke`` and
the gather tests start two runs; drop the index and the second-connection test
inserts a duplicate; send the reconciler back to ``executor.apply`` and the
auto-apply tests dispatch the busy artifact again (or crash out of the tick).
"""

from __future__ import annotations

import asyncio
import sqlite3
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from homepilot.artifacts.lifecycle import ConflictError
from homepilot.artifacts.models import ArtifactBusyError
from homepilot.db.connection import Database
from homepilot.db.migrations import MIGRATIONS, run_migrations
from homepilot.db.repository import Repository
from homepilot.executor.orchestrator import ExecutionResult, RevokeResult
from homepilot.reconciler import ApplyReconciler
from homepilot.tasks.repository import TaskRepository
from homepilot.tasks.runner import TaskRunner

AID = "2026-09-24-concurrent-run-aaa111"
OTHER = "2026-09-24-concurrent-run-bbb222"


class _Store:
    """The two calls the runner and reconciler make on a store."""

    def __init__(self, statuses: dict[str, str]) -> None:
        self.statuses = statuses

    def read(self, artifact_id: str) -> tuple[dict[str, Any], str]:
        if artifact_id not in self.statuses:
            raise FileNotFoundError(artifact_id)
        return {"id": artifact_id, "status": self.statuses[artifact_id]}, "body"

    def list(self, status: str | None = None, **_k: Any) -> list[dict[str, Any]]:
        return [{"id": a} for a, s in self.statuses.items() if status is None or s == status]


class _BlockingExecutor:
    """An executor whose runs do not finish until the test lets them."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.calls: list[tuple[str, str]] = []
        self.lifecycle = MagicMock(mark_failed=AsyncMock())

    async def preflight(self, artifact_id: str, action: str, actor: str) -> None:
        """The run guard (#813) lets every run here start; it is proven in
        tests/test_target_identity.py."""

    async def apply(self, artifact_id: str, _by: str) -> ExecutionResult:
        self.calls.append(("apply", artifact_id))
        await self.release.wait()
        return ExecutionResult(success=True, execution_log="ok")

    async def replay(self, artifact_id: str) -> ExecutionResult:
        self.calls.append(("replay", artifact_id))
        await self.release.wait()
        return ExecutionResult(success=True, execution_log="ok")

    async def revoke(self, artifact_id: str, _user: str, _reason: Any = None) -> RevokeResult:
        self.calls.append(("revoke", artifact_id))
        await self.release.wait()
        return RevokeResult(rolled_back=True, reason="ok")


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "homepilot.db"))
    await database.connect()
    await run_migrations(database)
    yield database
    await database.close()


def _runner(db: Database, store: _Store, executor: Any) -> TaskRunner:
    return TaskRunner(
        repo=TaskRepository(db),
        lifecycle=executor.lifecycle,
        executor=executor,
        apply_reconciler=None,
        store=store,  # type: ignore[arg-type]
    )


async def _settle(runner: TaskRunner, executor: _BlockingExecutor) -> None:
    executor.release.set()
    await runner.drain(timeout=5.0)


# ── the runner: concurrent requests, one slot ────────────────────────────────


@pytest.mark.parametrize(
    ("first", "second"),
    [("replay", "replay"), ("revoke", "revoke"), ("replay", "revoke"), ("revoke", "replay")],
)
async def test_two_concurrent_runs_of_one_artifact_start_exactly_one(db, first, second):
    store = _Store({AID: "applied"})
    executor = _BlockingExecutor()
    runner = _runner(db, store, executor)

    def start(action: str) -> Any:
        if action == "replay":
            return runner.start_replay(AID)
        return runner.start_revoke(AID, "operator", None)

    results = await asyncio.gather(start(first), start(second), return_exceptions=True)
    try:
        started = [r for r in results if isinstance(r, dict)]
        refused = [r for r in results if isinstance(r, ArtifactBusyError)]
        assert len(started) == 1, f"both runs started: {results!r}"
        assert len(refused) == 1, results
        # The refusal NAMES the run that holds the slot.
        assert refused[0].task_id == started[0]["task_id"]
        assert started[0]["task_id"] in str(refused[0])
        rows = await TaskRepository(db).list_tasks(AID)
        assert len(rows) == 1
    finally:
        await _settle(runner, executor)


async def test_the_slot_frees_when_the_run_finishes(db):
    store = _Store({AID: "applied"})
    executor = _BlockingExecutor()
    runner = _runner(db, store, executor)

    first = await runner.start_replay(AID)
    with pytest.raises(ArtifactBusyError):
        await runner.start_replay(AID)
    await _settle(runner, executor)
    assert (await runner.repo.get_task(first["task_id"]))["status"] == "succeeded"

    executor.release = asyncio.Event()
    again = await runner.start_replay(AID)
    assert again["task_id"] != first["task_id"]
    await _settle(runner, executor)


# ── the database: the rule holds for a writer that is not this runner ────────


async def test_a_second_connection_cannot_insert_a_second_active_run(db, tmp_path):
    """The CLI, a stdio MCP server, a second worker: another connection, same file."""
    first = await TaskRepository(db).create_task_if_no_active(AID, "apply")
    assert first is not None

    other = Database(str(tmp_path / "homepilot.db"))
    await other.connect()
    try:
        with pytest.raises(sqlite3.IntegrityError):
            await other.execute(
                "INSERT INTO tasks (id, artifact_id, action, status, created_at) "
                "VALUES ('raw-dup', ?, 'replay', 'pending', '2026-09-24T00:00:00Z')",
                (AID,),
            )
        # Through the repository the loser gets "no", not an exception.
        assert await TaskRepository(other).create_task_if_no_active(AID, "revoke") is None
        # Another artifact, and an artifactless action, are unaffected.
        assert await TaskRepository(other).create_task_if_no_active(OTHER, "apply") is not None
        assert await TaskRepository(other).create_task(None, "provision")
        assert await TaskRepository(other).create_task(None, "provision")
    finally:
        await other.close()


async def test_migration_34_retires_duplicates_already_there(tmp_path):
    database = Database(str(tmp_path / "old.db"))
    await database.connect()
    original = dict(MIGRATIONS)
    try:
        for version in list(MIGRATIONS):
            if version > 33:
                del MIGRATIONS[version]
        await run_migrations(database)
    finally:
        MIGRATIONS.clear()
        MIGRATIONS.update(original)
    try:
        for tid, created in (("old", "2026-09-01T00:00:00Z"), ("new", "2026-09-02T00:00:00Z")):
            await database.execute(
                "INSERT INTO tasks (id, artifact_id, action, status, created_at) "
                "VALUES (?, ?, 'replay', 'running', ?)",
                (tid, AID, created),
            )
        await database.conn.commit()

        await run_migrations(database)

        rows = {
            r["id"]: r
            for r in await database.fetchall("SELECT * FROM tasks WHERE artifact_id = ?", (AID,))
        }
        assert rows["new"]["status"] == "running"
        assert rows["old"]["status"] == "failed"
        assert rows["old"]["error"] == "superseded_duplicate_active_run"
    finally:
        await database.close()


# ── auto-apply goes through the same slot ────────────────────────────────────


async def test_an_auto_apply_tick_during_a_running_apply_does_not_dispatch_it_again(db):
    store = _Store({AID: "approved", OTHER: "approved"})
    executor = _BlockingExecutor()
    runner = _runner(db, store, executor)
    reconciler = ApplyReconciler(
        store=store,  # type: ignore[arg-type]
        repo=Repository(db),
        executor=executor,  # type: ignore[arg-type]
        auto_apply_enabled=True,
    )
    reconciler.task_runner = runner
    runner.apply_reconciler = reconciler

    held = await runner.start_apply(AID, "operator")
    await asyncio.sleep(0)  # let the first apply reach the executor

    # The tick runs while AID is still applying; OTHER is free.
    tick = asyncio.create_task(reconciler.run())
    for _ in range(100):
        if ("apply", OTHER) in executor.calls:
            break
        await asyncio.sleep(0.01)
    executor.release.set()
    result = await asyncio.wait_for(tick, 5.0)
    await runner.drain(timeout=5.0)

    assert executor.calls.count(("apply", AID)) == 1, executor.calls
    assert ("apply", OTHER) in executor.calls
    assert result.details["busy"] == 1
    busy = next(r for r in result.details["results"] if r["artifact_id"] == AID)
    assert busy["details"]["task_id"] == held["task_id"]


async def test_one_artifact_failing_to_apply_does_not_skip_the_rest_of_the_tick(db):
    store = _Store({AID: "approved", OTHER: "approved"})
    executor = _BlockingExecutor()
    executor.release.set()
    applied: list[str] = []

    async def apply(artifact_id: str, _by: str) -> ExecutionResult:
        if artifact_id == AID:
            # What the second, unguarded dispatch died on before #710.
            raise ConflictError("Invalid transition: applied -> applied")
        applied.append(artifact_id)
        return ExecutionResult(success=True, execution_log="ok")

    executor.apply = apply  # type: ignore[method-assign]
    reconciler = ApplyReconciler(
        store=store,  # type: ignore[arg-type]
        repo=Repository(db),
        executor=executor,  # type: ignore[arg-type]
        auto_apply_enabled=True,
    )

    result = await reconciler.run()

    assert applied == [OTHER]
    assert result.details["applied"] == 1
    assert result.details["failed"] == 1
    # Every run auto-apply made has a task row, finished.
    rows = await TaskRepository(db).list_tasks()
    assert {r["artifact_id"]: r["status"] for r in rows} == {AID: "failed", OTHER: "succeeded"}
