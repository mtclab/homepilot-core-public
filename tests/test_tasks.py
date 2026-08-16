from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from homepilot.artifacts.lifecycle import ArtifactLifecycle, ConflictError, LifecycleError
from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.executor.orchestrator import ExecutorError
from homepilot.tasks.repository import TaskRepository
from homepilot.tasks.runner import TaskRunner


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.connect()
    await run_migrations(database)
    yield database
    await database.close()


@pytest_asyncio.fixture
async def task_repo(db: Database) -> TaskRepository:
    return TaskRepository(db)


def _make_frontmatter(
    artifact_id: str = "2025-01-01-test-abc123",
    status: str = "approved",
    kind: str = "ansible-playbook",
) -> dict[str, Any]:
    return {
        "id": artifact_id,
        "kind": kind,
        "intent": f"Test {kind}",
        "mutating": kind != "kb-note",
        "status": status,
        "hash": "sha256:fake",
        "produced_by": {"session": "s1", "agent": "a1", "user": "u1"},
        "target": {"kind": "vm", "vmid": 100, "node": "pve1"},
        "idempotence": "via-precheck",
    }


def _store_artifact(
    mock_store: MagicMock, artifact_id: str = "2025-01-01-test-abc123", status: str = "approved"
) -> None:
    fm = _make_frontmatter(artifact_id=artifact_id, status=status)
    mock_store._storage[artifact_id] = (fm, "body content")


class TestTaskRepository:
    async def test_create_task(self, task_repo: TaskRepository):
        task_id = await task_repo.create_task("2025-01-01-test-abc123", "apply")
        assert task_id is not None
        task = await task_repo.get_task(task_id)
        assert task is not None
        assert task["artifact_id"] == "2025-01-01-test-abc123"
        assert task["action"] == "apply"
        assert task["status"] == "pending"

    async def test_list_tasks_system_wide(self, task_repo: TaskRepository):
        # artifact_id=None lists across all artifacts (the operator's view).
        await task_repo.create_task("art-a", "apply")
        await task_repo.create_task("art-b", "apply")
        all_tasks = await task_repo.list_tasks()
        assert len(all_tasks) == 2
        assert await task_repo.count_tasks() == 2
        scoped = await task_repo.list_tasks("art-a")
        assert len(scoped) == 1
        assert await task_repo.count_tasks("art-a") == 1

    async def test_create_task_revoke(self, task_repo: TaskRepository):
        task_id = await task_repo.create_task("2025-01-01-test-abc123", "revoke")
        task = await task_repo.get_task(task_id)
        assert task["action"] == "revoke"

    async def test_get_task_not_found(self, task_repo: TaskRepository):
        result = await task_repo.get_task("nonexistent-id")
        assert result is None

    async def test_update_task_status_succeeded(self, task_repo: TaskRepository):
        task_id = await task_repo.create_task("2025-01-01-test-abc123", "apply")
        await task_repo.update_task_status(task_id, "running")
        task = await task_repo.get_task(task_id)
        assert task["status"] == "running"

        await task_repo.update_task_status(task_id, "succeeded")
        task = await task_repo.get_task(task_id)
        assert task["status"] == "succeeded"
        assert task["finished_at"] is not None
        assert task["error"] is None

    async def test_update_task_status_failed(self, task_repo: TaskRepository):
        task_id = await task_repo.create_task("2025-01-01-test-abc123", "apply")
        await task_repo.update_task_status(task_id, "failed", error="something went wrong")
        task = await task_repo.get_task(task_id)
        assert task["status"] == "failed"
        assert task["error"] == "something went wrong"
        assert task["finished_at"] is not None

    async def test_get_active_task_none_when_no_tasks(self, task_repo: TaskRepository):
        result = await task_repo.get_active_task("2025-01-01-test-abc123")
        assert result is None

    async def test_get_active_task_returns_pending(self, task_repo: TaskRepository):
        task_id = await task_repo.create_task("2025-01-01-test-abc123", "apply")
        active = await task_repo.get_active_task("2025-01-01-test-abc123")
        assert active is not None
        assert active["id"] == task_id
        assert active["status"] == "pending"
        assert active["action"] == "apply"

    async def test_get_active_task_returns_running(self, task_repo: TaskRepository):
        task_id = await task_repo.create_task("2025-01-01-test-abc123", "apply")
        await task_repo.update_task_status(task_id, "running")
        active = await task_repo.get_active_task("2025-01-01-test-abc123")
        assert active is not None
        assert active["status"] == "running"

    async def test_get_active_task_returns_none_when_succeeded(self, task_repo: TaskRepository):
        task_id = await task_repo.create_task("2025-01-01-test-abc123", "apply")
        await task_repo.update_task_status(task_id, "succeeded")
        active = await task_repo.get_active_task("2025-01-01-test-abc123")
        assert active is None

    async def test_get_active_task_returns_none_when_failed(self, task_repo: TaskRepository):
        task_id = await task_repo.create_task("2025-01-01-test-abc123", "apply")
        await task_repo.update_task_status(task_id, "failed", error="test error")
        active = await task_repo.get_active_task("2025-01-01-test-abc123")
        assert active is None

    async def test_list_tasks(self, task_repo: TaskRepository):
        aid = "2025-01-01-test-abc123"
        t1 = await task_repo.create_task(aid, "apply")
        t2 = await task_repo.create_task(aid, "revoke")
        tasks = await task_repo.list_tasks(aid)
        assert len(tasks) == 2
        ids = {t["id"] for t in tasks}
        assert t1 in ids
        assert t2 in ids

    async def test_list_tasks_pagination(self, task_repo: TaskRepository):
        aid = "2025-01-01-test-abc123"
        for _ in range(5):
            await task_repo.create_task(aid, "apply")
        tasks = await task_repo.list_tasks(aid, limit=2, offset=0)
        assert len(tasks) == 2
        all_tasks = await task_repo.list_tasks(aid, limit=50, offset=0)
        assert len(all_tasks) == 5

    async def test_count_tasks(self, task_repo: TaskRepository):
        aid = "2025-01-01-test-abc123"
        assert await task_repo.count_tasks(aid) == 0
        await task_repo.create_task(aid, "apply")
        await task_repo.create_task(aid, "revoke")
        assert await task_repo.count_tasks(aid) == 2

    async def test_count_tasks_different_artifact(self, task_repo: TaskRepository):
        await task_repo.create_task("2025-01-01-test-abc123", "apply")
        await task_repo.create_task("2025-01-02-other-def456", "apply")
        assert await task_repo.count_tasks("2025-01-01-test-abc123") == 1
        assert await task_repo.count_tasks("2025-01-02-other-def456") == 1

    async def test_has_active_task_false_when_none(self, task_repo: TaskRepository):
        assert await task_repo.has_active_task("2025-01-01-test-abc123") is False

    async def test_has_active_task_true_when_pending(self, task_repo: TaskRepository):
        await task_repo.create_task("2025-01-01-test-abc123", "apply")
        assert await task_repo.has_active_task("2025-01-01-test-abc123") is True

    async def test_has_active_task_true_when_running(self, task_repo: TaskRepository):
        task_id = await task_repo.create_task("2025-01-01-test-abc123", "apply")
        await task_repo.update_task_status(task_id, "running")
        assert await task_repo.has_active_task("2025-01-01-test-abc123") is True

    async def test_has_active_task_false_when_terminal(self, task_repo: TaskRepository):
        task_id = await task_repo.create_task("2025-01-01-test-abc123", "apply")
        await task_repo.update_task_status(task_id, "succeeded")
        assert await task_repo.has_active_task("2025-01-01-test-abc123") is False

    async def test_fail_orphaned_tasks_marks_running_as_failed(self, task_repo: TaskRepository):
        t1 = await task_repo.create_task("2025-01-01-test-abc123", "apply")
        t2 = await task_repo.create_task("2025-01-02-other-def456", "apply")
        t3 = await task_repo.create_task("2025-01-03-third-ghi789", "apply")

        await task_repo.update_task_status(t1, "running")
        await task_repo.update_task_status(t2, "running")
        await task_repo.update_task_status(t3, "succeeded")

        count = await task_repo.fail_orphaned_tasks()
        assert count == 2

        task1 = await task_repo.get_task(t1)
        assert task1["status"] == "failed"
        assert task1["error"] == "orphaned_after_restart"

        task2 = await task_repo.get_task(t2)
        assert task2["status"] == "failed"

        task3 = await task_repo.get_task(t3)
        assert task3["status"] == "succeeded"

    async def test_fail_orphaned_tasks_no_running(self, task_repo: TaskRepository):
        count = await task_repo.fail_orphaned_tasks()
        assert count == 0

    async def test_fail_orphaned_tasks_sweeps_pending(self, task_repo: TaskRepository):
        # #386: a restart between task creation ('pending') and the first status
        # write ('running') must NOT strand the task — orphan recovery sweeps
        # 'pending' too, otherwise every future apply returns 202 for a dead task.
        tid = await task_repo.create_task("2025-01-01-test-abc123", "apply")
        count = await task_repo.fail_orphaned_tasks()
        assert count == 1
        task = await task_repo.get_task(tid)
        assert task["status"] == "failed"
        assert task["error"] == "orphaned_after_restart"


class TestTaskCancellation:
    """#376: cancel a pending/running task; terminal tasks are a no-op."""

    async def test_cancel_pending_task_marks_cancelled(self, task_repo: TaskRepository):
        task_id = await task_repo.create_task("2025-01-01-test-abc123", "apply")
        result = await task_repo.cancel_task(task_id)
        assert result is not None
        assert result["status"] == "cancelled"
        assert result["finished_at"] is not None

    async def test_cancel_running_task_marks_cancelled(self, task_repo: TaskRepository):
        task_id = await task_repo.create_task("2025-01-01-test-abc123", "apply")
        await task_repo.update_task_status(task_id, "running")
        result = await task_repo.cancel_task(task_id)
        assert result["status"] == "cancelled"

    async def test_cancel_unblocks_future_apply(self, task_repo: TaskRepository):
        # A cancelled task must stop counting as active so the next apply for the
        # same artifact can start — otherwise a stuck task wedges the artifact.
        aid = "2025-01-01-test-abc123"
        task_id = await task_repo.create_task(aid, "apply")
        assert await task_repo.has_active_task(aid) is True
        await task_repo.cancel_task(task_id)
        assert await task_repo.has_active_task(aid) is False
        assert await task_repo.get_active_task(aid) is None
        # A fresh apply is now admitted by the atomic guard.
        new_id = await task_repo.create_task_if_no_active(aid, "apply")
        assert new_id is not None

    async def test_cancel_succeeded_task_is_noop(self, task_repo: TaskRepository):
        task_id = await task_repo.create_task("2025-01-01-test-abc123", "apply")
        await task_repo.update_task_status(task_id, "succeeded")
        result = await task_repo.cancel_task(task_id)
        assert result["status"] == "succeeded"

    async def test_cancel_failed_task_is_noop(self, task_repo: TaskRepository):
        task_id = await task_repo.create_task("2025-01-01-test-abc123", "apply")
        await task_repo.update_task_status(task_id, "failed", error="boom")
        result = await task_repo.cancel_task(task_id)
        assert result["status"] == "failed"
        assert result["error"] == "boom"

    async def test_cancel_unknown_task_returns_none(self, task_repo: TaskRepository):
        assert await task_repo.cancel_task("nonexistent-id") is None

    async def test_cancelled_status_accepted_by_schema(self, task_repo: TaskRepository):
        # Revert-guard for migration 14: the tasks.status CHECK must admit
        # 'cancelled'. Before the widened constraint this write raises
        # IntegrityError, so cancel could never persist.
        task_id = await task_repo.create_task("2025-01-01-test-abc123", "apply")
        result = await task_repo.cancel_task(task_id)
        assert result["status"] == "cancelled"
        # Round-trips through a fresh read (real column value, not in-memory).
        reread = await task_repo.get_task(task_id)
        assert reread["status"] == "cancelled"

    async def test_runner_cancels_running_task_and_unblocks_apply(
        self, task_repo: TaskRepository, mock_store: MagicMock
    ):
        # End-to-end through the runner: a real in-flight apply is cancelled, its
        # record becomes 'cancelled', and a new apply for the same artifact then
        # starts (the cancelled task no longer blocks it).
        _store_artifact(mock_store, status="approved")

        hang = asyncio.Event()

        async def _never_finishes(_artifact_id: str, _by: str) -> Any:
            await hang.wait()  # blocks until cancelled
            raise AssertionError("should have been cancelled")

        mock_reconciler = MagicMock()
        mock_reconciler.apply_single = _never_finishes

        runner = TaskRunner(
            repo=task_repo,
            lifecycle=MagicMock(),
            executor=None,
            apply_reconciler=mock_reconciler,
            store=mock_store,
        )

        first = await runner.start_apply("2025-01-01-test-abc123", approved_by="test")
        task_id = first["task_id"]

        # Let _run_apply reach 'running' and block inside the reconciler.
        for _ in range(50):
            await asyncio.sleep(0.01)
            t = await task_repo.get_task(task_id)
            if t["status"] == "running":
                break
        assert (await task_repo.get_task(task_id))["status"] == "running"

        cancelled = await runner.cancel_task(task_id)
        assert cancelled["status"] == "cancelled"
        await asyncio.sleep(0.05)  # let the CancelledError settle
        assert (await task_repo.get_task(task_id))["status"] == "cancelled"

        # The artifact is free again — a new apply starts a distinct task.
        second = await runner.start_apply("2025-01-01-test-abc123", approved_by="test")
        assert second["task_id"] != task_id
        assert second["status"] == "pending"

        for t in list(runner._running_tasks):
            t.cancel()
        await asyncio.sleep(0.05)

    async def test_runner_cancel_finished_task_is_noop(
        self, task_repo: TaskRepository, mock_store: MagicMock
    ):
        # Cancelling an already-succeeded task through the runner returns its
        # status unchanged (no error), even though the asyncio.Task is long gone.
        _store_artifact(mock_store, status="approved")
        runner = TaskRunner(
            repo=task_repo,
            lifecycle=MagicMock(),
            executor=None,
            apply_reconciler=None,
            store=mock_store,
        )
        task_id = await task_repo.create_task("2025-01-01-test-abc123", "apply")
        await task_repo.update_task_status(task_id, "succeeded")

        result = await runner.cancel_task(task_id)
        assert result["status"] == "succeeded"

    async def test_runner_cancel_unknown_raises_value_error(
        self, task_repo: TaskRepository, mock_store: MagicMock
    ):
        runner = TaskRunner(
            repo=task_repo,
            lifecycle=MagicMock(),
            executor=None,
            apply_reconciler=None,
            store=mock_store,
        )
        with pytest.raises(ValueError, match="not found"):
            await runner.cancel_task("nonexistent-id")


class TestTaskRunner:
    def _make_runner(
        self,
        task_repo: TaskRepository,
        mock_store: MagicMock,
        lifecycle: ArtifactLifecycle | None = None,
        executor: Any = None,
        apply_reconciler: Any = None,
    ) -> TaskRunner:
        if lifecycle is None:
            lifecycle = ArtifactLifecycle(store=mock_store)
        return TaskRunner(
            repo=task_repo,
            lifecycle=lifecycle,
            executor=executor,
            apply_reconciler=apply_reconciler,
            store=mock_store,
        )

    async def test_start_apply_creates_task(self, task_repo: TaskRepository, mock_store: MagicMock):
        _store_artifact(mock_store, status="approved")
        runner = self._make_runner(task_repo, mock_store)

        result = await runner.start_apply("2025-01-01-test-abc123", approved_by="testuser")

        assert result["artifact_id"] == "2025-01-01-test-abc123"
        assert result["action"] == "apply"
        assert result["status"] == "pending"
        assert result["task_id"] is not None

        task = await task_repo.get_task(result["task_id"])
        assert task is not None
        assert task["action"] == "apply"

        for t in list(runner._running_tasks):
            t.cancel()
        await asyncio.sleep(0.05)

    async def test_start_apply_returns_existing_task_if_pending(
        self, task_repo: TaskRepository, mock_store: MagicMock
    ):
        _store_artifact(mock_store, status="approved")
        runner = self._make_runner(task_repo, mock_store, lifecycle=MagicMock())

        existing_id = await task_repo.create_task("2025-01-01-test-abc123", "apply")

        result = await runner.start_apply("2025-01-01-test-abc123", approved_by="testuser")

        assert result["task_id"] == existing_id
        total = await task_repo.count_tasks("2025-01-01-test-abc123")
        assert total == 1

    async def test_start_apply_artifact_not_found(
        self, task_repo: TaskRepository, mock_store: MagicMock
    ):
        runner = self._make_runner(task_repo, mock_store)

        with pytest.raises(ValueError, match="not found"):
            await runner.start_apply("nonexistent-id")

    async def test_start_apply_invalid_status(
        self, task_repo: TaskRepository, mock_store: MagicMock
    ):
        _store_artifact(mock_store, status="proposed")
        runner = self._make_runner(task_repo, mock_store)

        with pytest.raises(LifecycleError, match="Invalid transition"):
            await runner.start_apply("2025-01-01-test-abc123")

    async def test_start_revoke_creates_task(
        self, task_repo: TaskRepository, mock_store: MagicMock
    ):
        _store_artifact(mock_store, status="approved")
        runner = self._make_runner(task_repo, mock_store)

        result = await runner.start_revoke("2025-01-01-test-abc123", user="admin")

        assert result["artifact_id"] == "2025-01-01-test-abc123"
        assert result["action"] == "revoke"
        assert result["status"] == "pending"

        for t in list(runner._running_tasks):
            t.cancel()
        await asyncio.sleep(0.05)

    async def test_start_revoke_409_when_apply_in_progress(
        self, task_repo: TaskRepository, mock_store: MagicMock
    ):
        _store_artifact(mock_store, status="approved")
        runner = self._make_runner(task_repo, mock_store, lifecycle=MagicMock())

        await task_repo.create_task("2025-01-01-test-abc123", "apply")

        with pytest.raises(ValueError, match="apply_in_progress"):
            await runner.start_revoke("2025-01-01-test-abc123")

    async def test_start_revoke_409_when_replay_in_progress(
        self, task_repo: TaskRepository, mock_store: MagicMock
    ):
        _store_artifact(mock_store, status="approved")
        runner = self._make_runner(task_repo, mock_store, lifecycle=MagicMock())

        await task_repo.create_task("2025-01-01-test-abc123", "replay")

        with pytest.raises(ValueError, match="apply_in_progress"):
            await runner.start_revoke("2025-01-01-test-abc123")

    async def test_start_revoke_allows_revoke_task_in_parallel(
        self, task_repo: TaskRepository, mock_store: MagicMock
    ):
        _store_artifact(mock_store, status="approved")
        runner = self._make_runner(task_repo, mock_store, lifecycle=MagicMock())

        await task_repo.create_task("2025-01-01-test-abc123", "revoke")

        result = await runner.start_revoke(
            "2025-01-01-test-abc123", user="admin", reason="double revoke"
        )
        assert result["action"] == "revoke"

        total = await task_repo.count_tasks("2025-01-01-test-abc123")
        assert total == 2

        for t in list(runner._running_tasks):
            t.cancel()
        await asyncio.sleep(0.05)

    async def test_start_revoke_artifact_not_found(
        self, task_repo: TaskRepository, mock_store: MagicMock
    ):
        runner = self._make_runner(task_repo, mock_store)

        with pytest.raises(ValueError, match="not found"):
            await runner.start_revoke("nonexistent-id")

    async def test_await_task_polls_until_terminal(
        self, task_repo: TaskRepository, mock_store: MagicMock
    ):
        _store_artifact(mock_store, status="approved")
        runner = self._make_runner(task_repo, mock_store, lifecycle=MagicMock())

        with patch.object(runner, "_run_apply", new_callable=AsyncMock):
            result = await runner.start_apply("2025-01-01-test-abc123", approved_by="test")
            task_id = result["task_id"]

        await task_repo.update_task_status(task_id, "succeeded")

        task = await runner.await_task(task_id, timeout=5.0)
        assert task["status"] == "succeeded"

    async def test_await_task_raises_on_missing_task(
        self, task_repo: TaskRepository, mock_store: MagicMock
    ):
        runner = self._make_runner(task_repo, mock_store)

        with pytest.raises(ValueError, match="not found"):
            await runner.await_task("nonexistent-id", timeout=1.0)

    async def test_run_apply_succeeds_with_reconciler(
        self, task_repo: TaskRepository, mock_store: MagicMock
    ):
        _store_artifact(mock_store, status="approved")

        mock_reconciler = AsyncMock()
        mock_result = MagicMock()
        mock_result.success = True
        mock_result.execution_log = "Reconciler applied"
        mock_result.failure_reason = None
        mock_reconciler.apply_single = AsyncMock(return_value=mock_result)

        runner = TaskRunner(
            repo=task_repo,
            lifecycle=ArtifactLifecycle(store=mock_store),
            executor=None,
            apply_reconciler=mock_reconciler,
            store=mock_store,
        )

        result = await runner.start_apply("2025-01-01-test-abc123", approved_by="test")

        await asyncio.sleep(0.3)

        task = await task_repo.get_task(result["task_id"])
        assert task["status"] == "succeeded"

    async def test_run_apply_fails_with_reconciler_error(
        self, task_repo: TaskRepository, mock_store: MagicMock
    ):
        _store_artifact(mock_store, status="approved")

        mock_reconciler = AsyncMock()
        mock_result = MagicMock()
        mock_result.success = False
        mock_result.failure_reason = "ansible failed"
        mock_result.execution_log = None
        mock_reconciler.apply_single = AsyncMock(return_value=mock_result)

        runner = TaskRunner(
            repo=task_repo,
            lifecycle=ArtifactLifecycle(store=mock_store),
            executor=None,
            apply_reconciler=mock_reconciler,
            store=mock_store,
        )

        result = await runner.start_apply("2025-01-01-test-abc123", approved_by="test")

        await asyncio.sleep(0.3)

        task = await task_repo.get_task(result["task_id"])
        assert task["status"] == "failed"
        assert "ansible failed" in task["error"]

    async def test_run_apply_exception_marks_failed(
        self, task_repo: TaskRepository, mock_store: MagicMock
    ):
        _store_artifact(mock_store, status="approved")

        mock_lifecycle = MagicMock()
        mock_lifecycle.mark_applied = AsyncMock()
        mock_lifecycle.mark_failed = AsyncMock()

        mock_reconciler = AsyncMock()
        mock_reconciler.apply_single = AsyncMock(side_effect=ValueError("boom"))

        runner = TaskRunner(
            repo=task_repo,
            lifecycle=mock_lifecycle,
            executor=None,
            apply_reconciler=mock_reconciler,
            store=mock_store,
        )

        result = await runner.start_apply("2025-01-01-test-abc123", approved_by="test")

        await asyncio.sleep(0.3)

        task = await task_repo.get_task(result["task_id"])
        assert task["status"] == "failed"
        assert "boom" in task["error"]

    async def test_run_revoke_succeeds_no_executor(
        self, task_repo: TaskRepository, mock_store: MagicMock
    ):
        _store_artifact(mock_store, status="approved")

        mock_lifecycle = MagicMock()
        mock_lifecycle.revoke = AsyncMock()

        runner = TaskRunner(
            repo=task_repo,
            lifecycle=mock_lifecycle,
            executor=None,
            apply_reconciler=None,
            store=mock_store,
        )

        result = await runner.start_revoke("2025-01-01-test-abc123")

        await asyncio.sleep(0.3)

        task = await task_repo.get_task(result["task_id"])
        assert task["status"] == "succeeded"
        mock_lifecycle.revoke.assert_called_once()

    async def test_run_revoke_fails_with_exception(
        self, task_repo: TaskRepository, mock_store: MagicMock
    ):
        _store_artifact(mock_store, status="approved")

        mock_lifecycle = MagicMock()
        mock_lifecycle.revoke = AsyncMock(side_effect=OSError("revoke failed"))

        runner = TaskRunner(
            repo=task_repo,
            lifecycle=mock_lifecycle,
            executor=None,
            apply_reconciler=None,
            store=mock_store,
        )

        result = await runner.start_revoke("2025-01-01-test-abc123")

        await asyncio.sleep(0.3)

        task = await task_repo.get_task(result["task_id"])
        assert task["status"] == "failed"
        assert "revoke failed" in task["error"]


async def _create_authenticated_client(
    db: Database, task_repo: TaskRepository, task_runner: TaskRunner
):
    import httpx
    from fastapi import FastAPI

    from homepilot.artifacts.router import router as artifacts_router
    from homepilot.auth.deps import require_scope, require_token
    from homepilot.auth.router import router as auth_router
    from homepilot.auth.tokens import generate_api_token
    from homepilot.tasks.router import router as tasks_router

    app = FastAPI()
    app.state.task_repo = task_repo
    app.state.task_runner = task_runner
    app.state.artifact_store = task_runner.store
    app.state.artifact_lifecycle = task_runner.lifecycle
    app.state.artifact_executor = None
    app.state.apply_reconciler = None
    app.state.db = db

    from unittest.mock import MagicMock as _MagicMock

    mock_repo = _MagicMock()
    app.state.repo = mock_repo

    raw_token, token_prefix, token_hash = generate_api_token()
    await db.execute(
        """INSERT INTO users (id, display_name, auth_source, created_at)
           VALUES ('test-user', 'Test User', 'api_token', '2026-01-01T00:00:00Z')"""
    )
    await db.execute(
        """INSERT INTO api_tokens (id, user_id, token_type, prefix, hash, scope, created_at)
           VALUES ('test-token', 'test-user', 'api', ?, ?, 'write,read', '2026-01-01T00:00:00Z')""",
        (token_prefix, token_hash),
    )
    await db.conn.commit()

    async def _mock_get_db(request):
        return mock_repo

    app.dependency_overrides[require_token] = lambda: {
        "user_id": "test-user",
        "token_id": "test-token",
        "display_name": "Test User",
        "scope": "write,read",
    }
    app.dependency_overrides[require_scope("write")] = lambda: {
        "user_id": "test-user",
        "token_id": "test-token",
        "display_name": "Test User",
        "scope": "write,read",
    }
    app.dependency_overrides[require_scope("read")] = lambda: {
        "user_id": "test-user",
        "token_id": "test-token",
        "display_name": "Test User",
        "scope": "write,read",
    }

    app.include_router(auth_router, prefix="/auth")
    app.include_router(artifacts_router, prefix="/artifacts")
    app.include_router(tasks_router, prefix="/tasks")

    # httpx AsyncClient over ASGITransport runs the app in the SAME event loop as
    # the test, so the fire-and-forget apply/revoke task (asyncio.create_task in
    # start_apply) is tracked + cancelled by pytest-asyncio at loop close. The old
    # sync TestClient ran requests in a separate portal loop, orphaning that task
    # and intermittently wedging fixture teardown (issue #298).
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    client = httpx.AsyncClient(transport=transport, base_url="http://test", follow_redirects=True)
    client._auth_headers = {"Authorization": f"Bearer {raw_token}"}
    return client


class TestTaskEndpoints:
    async def test_get_task_returns_task(self, task_repo: TaskRepository, mock_store: MagicMock):
        task_id = await task_repo.create_task("2025-01-01-test-abc123", "apply")

        lifecycle = ArtifactLifecycle(store=mock_store)
        runner = TaskRunner(
            repo=task_repo,
            lifecycle=lifecycle,
            executor=None,
            apply_reconciler=None,
            store=mock_store,
        )

        client = await _create_authenticated_client(task_repo.db, task_repo, runner)

        resp = await client.get(f"/tasks/{task_id}", headers=client._auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == task_id
        assert data["action"] == "apply"
        assert data["status"] == "pending"

    async def test_get_task_not_found(self, task_repo: TaskRepository, mock_store: MagicMock):
        lifecycle = ArtifactLifecycle(store=mock_store)
        runner = TaskRunner(
            repo=task_repo,
            lifecycle=lifecycle,
            executor=None,
            apply_reconciler=None,
            store=mock_store,
        )

        client = await _create_authenticated_client(task_repo.db, task_repo, runner)

        resp = await client.get("/tasks/nonexistent-id", headers=client._auth_headers)
        assert resp.status_code == 404

    async def test_list_tasks_for_artifact(self, task_repo: TaskRepository, mock_store: MagicMock):
        aid = "2025-01-01-test-abc123"

        t1 = await task_repo.create_task(aid, "apply")
        await task_repo.update_task_status(t1, "succeeded")
        await task_repo.create_task(aid, "revoke")

        lifecycle = ArtifactLifecycle(store=mock_store)
        runner = TaskRunner(
            repo=task_repo,
            lifecycle=lifecycle,
            executor=None,
            apply_reconciler=None,
            store=mock_store,
        )

        client = await _create_authenticated_client(task_repo.db, task_repo, runner)

        resp = await client.get(
            "/tasks/", params={"artifact_id": aid}, headers=client._auth_headers
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        assert len(data["items"]) == 2

    async def test_list_tasks_pagination(self, task_repo: TaskRepository, mock_store: MagicMock):
        aid = "2025-01-01-test-abc123"
        for _ in range(5):
            await task_repo.create_task(aid, "apply")

        lifecycle = ArtifactLifecycle(store=mock_store)
        runner = TaskRunner(
            repo=task_repo,
            lifecycle=lifecycle,
            executor=None,
            apply_reconciler=None,
            store=mock_store,
        )

        client = await _create_authenticated_client(task_repo.db, task_repo, runner)

        resp = await client.get(
            "/tasks/",
            params={"artifact_id": aid, "limit": 2, "offset": 0},
            headers=client._auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["items"]) == 2
        assert data["total"] == 5

    async def test_list_tasks_global_across_artifacts(
        self, task_repo: TaskRepository, mock_store: MagicMock
    ):
        # #376: GET /tasks with NO artifact_id is the system-wide view — it must
        # return tasks from every artifact, not error on the missing filter.
        await task_repo.create_task("2025-01-01-alpha-aaa111", "apply")
        await task_repo.create_task("2025-01-02-beta-bbb222", "revoke")

        runner = TaskRunner(
            repo=task_repo,
            lifecycle=ArtifactLifecycle(store=mock_store),
            executor=None,
            apply_reconciler=None,
            store=mock_store,
        )
        client = await _create_authenticated_client(task_repo.db, task_repo, runner)

        resp = await client.get("/tasks/", headers=client._auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        artifacts = {t["artifact_id"] for t in data["items"]}
        assert artifacts == {"2025-01-01-alpha-aaa111", "2025-01-02-beta-bbb222"}

    async def test_cancel_endpoint_cancels_pending_task(
        self, task_repo: TaskRepository, mock_store: MagicMock
    ):
        task_id = await task_repo.create_task("2025-01-01-test-abc123", "apply")

        runner = TaskRunner(
            repo=task_repo,
            lifecycle=MagicMock(),
            executor=None,
            apply_reconciler=None,
            store=mock_store,
        )
        client = await _create_authenticated_client(task_repo.db, task_repo, runner)

        resp = await client.post(f"/tasks/{task_id}/cancel", headers=client._auth_headers)
        assert resp.status_code == 200
        assert resp.json()["status"] == "cancelled"

    async def test_cancel_endpoint_unknown_task_404(
        self, task_repo: TaskRepository, mock_store: MagicMock
    ):
        runner = TaskRunner(
            repo=task_repo,
            lifecycle=MagicMock(),
            executor=None,
            apply_reconciler=None,
            store=mock_store,
        )
        client = await _create_authenticated_client(task_repo.db, task_repo, runner)

        resp = await client.post("/tasks/nonexistent-id/cancel", headers=client._auth_headers)
        assert resp.status_code == 404


class TestArtifactEndpointsAsync:
    async def test_apply_returns_202(self, task_repo: TaskRepository, mock_store: MagicMock):
        _store_artifact(mock_store, status="approved")

        mock_lifecycle = MagicMock(spec=ArtifactLifecycle)
        mock_lifecycle.mark_applied = AsyncMock()
        mock_lifecycle.mark_failed = AsyncMock()

        runner = TaskRunner(
            repo=task_repo,
            lifecycle=mock_lifecycle,
            executor=None,
            apply_reconciler=None,
            store=mock_store,
        )

        client = await _create_authenticated_client(task_repo.db, task_repo, runner)

        resp = await client.post(
            "/artifacts/2025-01-01-test-abc123/apply",
            json={"approved_by": "test"},
            headers=client._auth_headers,
        )
        assert resp.status_code == 202
        data = resp.json()
        assert data["action"] == "apply"
        assert data["status"] == "pending"
        assert data["artifact_id"] == "2025-01-01-test-abc123"

        for t in list(runner._running_tasks):
            t.cancel()
        await asyncio.sleep(0.05)

    async def test_apply_returns_existing_task_when_pending(
        self, task_repo: TaskRepository, mock_store: MagicMock
    ):
        _store_artifact(mock_store, status="approved")

        mock_lifecycle = MagicMock(spec=ArtifactLifecycle)
        mock_lifecycle.mark_applied = AsyncMock()
        mock_lifecycle.mark_failed = AsyncMock()

        runner = TaskRunner(
            repo=task_repo,
            lifecycle=mock_lifecycle,
            executor=None,
            apply_reconciler=None,
            store=mock_store,
        )

        existing_id = await task_repo.create_task("2025-01-01-test-abc123", "apply")

        client = await _create_authenticated_client(task_repo.db, task_repo, runner)

        resp = await client.post(
            "/artifacts/2025-01-01-test-abc123/apply",
            json={"approved_by": "test"},
            headers=client._auth_headers,
        )
        assert resp.status_code == 202
        data = resp.json()
        assert data["task_id"] == existing_id

    async def test_apply_404_nonexistent_artifact(
        self, task_repo: TaskRepository, mock_store: MagicMock
    ):
        runner = TaskRunner(
            repo=task_repo,
            lifecycle=MagicMock(),
            executor=None,
            apply_reconciler=None,
            store=mock_store,
        )

        client = await _create_authenticated_client(task_repo.db, task_repo, runner)

        resp = await client.post(
            "/artifacts/nonexistent-id/apply",
            json={"approved_by": "test"},
            headers=client._auth_headers,
        )
        assert resp.status_code == 404

    async def test_revoke_returns_409_when_apply_in_progress(
        self, task_repo: TaskRepository, mock_store: MagicMock
    ):
        _store_artifact(mock_store, status="approved")

        mock_lifecycle = MagicMock(spec=ArtifactLifecycle)
        mock_lifecycle.mark_applied = AsyncMock()
        mock_lifecycle.mark_failed = AsyncMock()

        runner = TaskRunner(
            repo=task_repo,
            lifecycle=mock_lifecycle,
            executor=None,
            apply_reconciler=None,
            store=mock_store,
        )

        await task_repo.create_task("2025-01-01-test-abc123", "apply")

        client = await _create_authenticated_client(task_repo.db, task_repo, runner)

        resp = await client.request(
            "DELETE",
            "/artifacts/2025-01-01-test-abc123",
            json={"user": "admin"},
            headers=client._auth_headers,
        )
        assert resp.status_code == 409
        data = resp.json()
        assert "apply_in_progress" in str(data)

    async def test_revoke_returns_202_normal(
        self, task_repo: TaskRepository, mock_store: MagicMock
    ):
        _store_artifact(mock_store, status="approved")

        mock_lifecycle = MagicMock(spec=ArtifactLifecycle)
        mock_lifecycle.revoke = AsyncMock()

        runner = TaskRunner(
            repo=task_repo,
            lifecycle=mock_lifecycle,
            executor=None,
            apply_reconciler=None,
            store=mock_store,
        )

        client = await _create_authenticated_client(task_repo.db, task_repo, runner)

        resp = await client.request(
            "DELETE",
            "/artifacts/2025-01-01-test-abc123",
            json={"user": "admin"},
            headers=client._auth_headers,
        )
        assert resp.status_code == 202
        data = resp.json()
        assert data["action"] == "revoke"

        for t in list(runner._running_tasks):
            t.cancel()
        await asyncio.sleep(0.05)

    async def test_get_artifact_includes_active_task(
        self, task_repo: TaskRepository, mock_store: MagicMock
    ):
        _store_artifact(mock_store, status="approved")

        lifecycle = ArtifactLifecycle(store=mock_store)
        runner = TaskRunner(
            repo=task_repo,
            lifecycle=lifecycle,
            executor=None,
            apply_reconciler=None,
            store=mock_store,
        )

        task_id = await task_repo.create_task("2025-01-01-test-abc123", "apply")

        client = await _create_authenticated_client(task_repo.db, task_repo, runner)

        resp = await client.get("/artifacts/2025-01-01-test-abc123", headers=client._auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["active_task"] is not None
        assert data["active_task"]["id"] == task_id
        assert data["active_task"]["status"] == "pending"
        assert data["active_task"]["action"] == "apply"

    async def test_get_artifact_active_task_null_when_none(
        self, task_repo: TaskRepository, mock_store: MagicMock
    ):
        _store_artifact(mock_store, status="approved")

        lifecycle = ArtifactLifecycle(store=mock_store)
        runner = TaskRunner(
            repo=task_repo,
            lifecycle=lifecycle,
            executor=None,
            apply_reconciler=None,
            store=mock_store,
        )

        client = await _create_authenticated_client(task_repo.db, task_repo, runner)

        resp = await client.get("/artifacts/2025-01-01-test-abc123", headers=client._auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["active_task"] is None


class TestStartupRecovery:
    async def test_fail_orphaned_tasks_on_startup(self, task_repo: TaskRepository):
        t1 = await task_repo.create_task("2025-01-01-test-abc123", "apply")
        await task_repo.update_task_status(t1, "running")

        t2 = await task_repo.create_task("2025-01-02-other-def456", "apply")
        await task_repo.update_task_status(t2, "running")

        count = await task_repo.fail_orphaned_tasks()
        assert count == 2

        task1 = await task_repo.get_task(t1)
        assert task1["status"] == "failed"
        assert task1["error"] == "orphaned_after_restart"

        task2 = await task_repo.get_task(t2)
        assert task2["status"] == "failed"

    async def test_fail_orphaned_tasks_preserves_completed(self, task_repo: TaskRepository):
        t1 = await task_repo.create_task("2025-01-01-test-abc123", "apply")
        await task_repo.update_task_status(t1, "succeeded")

        t2 = await task_repo.create_task("2025-01-02-other-def456", "apply")
        await task_repo.update_task_status(t2, "failed", error="real failure")

        t3 = await task_repo.create_task("2025-01-03-third-ghi789", "apply")
        await task_repo.update_task_status(t3, "running")

        count = await task_repo.fail_orphaned_tasks()
        assert count == 1

        assert (await task_repo.get_task(t1))["status"] == "succeeded"
        assert (await task_repo.get_task(t2))["status"] == "failed"
        assert (await task_repo.get_task(t3))["status"] == "failed"


class TestTaskRepositoryAtomic:
    async def test_create_task_if_no_active_creates_when_none(self, task_repo: TaskRepository):
        task_id = await task_repo.create_task_if_no_active("2025-01-01-test-abc123", "apply")
        assert task_id is not None
        task = await task_repo.get_task(task_id)
        assert task is not None
        assert task["action"] == "apply"
        assert task["status"] == "pending"

    async def test_create_task_if_no_active_returns_none_when_active(
        self, task_repo: TaskRepository
    ):
        await task_repo.create_task("2025-01-01-test-abc123", "apply")
        task_id = await task_repo.create_task_if_no_active("2025-01-01-test-abc123", "apply")
        assert task_id is None

    async def test_create_task_if_no_active_creates_after_terminal(self, task_repo: TaskRepository):
        t1 = await task_repo.create_task("2025-01-01-test-abc123", "apply")
        await task_repo.update_task_status(t1, "succeeded")
        t2 = await task_repo.create_task_if_no_active("2025-01-01-test-abc123", "apply")
        assert t2 is not None


class TestTaskRunnerRevoke:
    async def test_start_revoke_rejects_proposed_status(
        self, task_repo: TaskRepository, mock_store: MagicMock
    ):
        _store_artifact(mock_store, status="proposed")
        runner = TaskRunner(
            repo=task_repo,
            lifecycle=ArtifactLifecycle(store=mock_store),
            executor=None,
            apply_reconciler=None,
            store=mock_store,
        )

        with pytest.raises(LifecycleError, match="Invalid transition"):
            await runner.start_revoke("2025-01-01-test-abc123")

    async def test_start_revoke_rejects_rejected_status(
        self, task_repo: TaskRepository, mock_store: MagicMock
    ):
        _store_artifact(mock_store, status="rejected")
        runner = TaskRunner(
            repo=task_repo,
            lifecycle=ArtifactLifecycle(store=mock_store),
            executor=None,
            apply_reconciler=None,
            store=mock_store,
        )

        with pytest.raises(LifecycleError, match="Invalid transition"):
            await runner.start_revoke("2025-01-01-test-abc123")

    async def test_start_revoke_allows_approved_status(
        self, task_repo: TaskRepository, mock_store: MagicMock
    ):
        _store_artifact(mock_store, status="approved")
        mock_lifecycle = MagicMock()
        mock_lifecycle.revoke = AsyncMock()

        runner = TaskRunner(
            repo=task_repo,
            lifecycle=mock_lifecycle,
            executor=None,
            apply_reconciler=None,
            store=mock_store,
        )

        result = await runner.start_revoke("2025-01-01-test-abc123")
        assert result["action"] == "revoke"
        assert "task_id" in result

        for t in list(runner._running_tasks):
            t.cancel()
        await asyncio.sleep(0.05)

    async def test_start_revoke_allows_applied_status(
        self, task_repo: TaskRepository, mock_store: MagicMock
    ):
        _store_artifact(mock_store, status="applied")
        mock_lifecycle = MagicMock()
        mock_lifecycle.revoke = AsyncMock()

        runner = TaskRunner(
            repo=task_repo,
            lifecycle=mock_lifecycle,
            executor=None,
            apply_reconciler=None,
            store=mock_store,
        )

        result = await runner.start_revoke("2025-01-01-test-abc123")
        assert result["action"] == "revoke"

        for t in list(runner._running_tasks):
            t.cancel()
        await asyncio.sleep(0.05)

    async def test_start_revoke_allows_failed_status(
        self, task_repo: TaskRepository, mock_store: MagicMock
    ):
        _store_artifact(mock_store, status="failed")
        mock_lifecycle = MagicMock()
        mock_lifecycle.revoke = AsyncMock()

        runner = TaskRunner(
            repo=task_repo,
            lifecycle=mock_lifecycle,
            executor=None,
            apply_reconciler=None,
            store=mock_store,
        )

        result = await runner.start_revoke("2025-01-01-test-abc123")
        assert result["action"] == "revoke"

        for t in list(runner._running_tasks):
            t.cancel()
        await asyncio.sleep(0.05)


class TestApplyOnAppliedRegression:
    """#386: applying an already-APPLIED artifact must never strand a task."""

    def _make_runner(
        self,
        task_repo: TaskRepository,
        mock_store: MagicMock,
        lifecycle: Any,
        executor: Any = None,
    ) -> TaskRunner:
        return TaskRunner(
            repo=task_repo,
            lifecycle=lifecycle,
            executor=executor,
            apply_reconciler=None,
            store=mock_store,
        )

    async def test_apply_on_applied_rejects_without_creating_task(
        self, task_repo: TaskRepository, mock_store: MagicMock
    ):
        _store_artifact(mock_store, status="applied")
        runner = self._make_runner(task_repo, mock_store, lifecycle=MagicMock())

        with pytest.raises(ConflictError, match="Invalid transition"):
            await runner.start_apply("2025-01-01-test-abc123")

        # No task was created — nothing left running to block future actions.
        assert await task_repo.count_tasks("2025-01-01-test-abc123") == 0
        assert await task_repo.get_active_task("2025-01-01-test-abc123") is None

        # A later apply on a freshly-approved artifact still works.
        _store_artifact(mock_store, status="approved")
        result = await runner.start_apply("2025-01-01-test-abc123", approved_by="test")
        assert result["status"] == "pending"
        assert result["task_id"] is not None

        for t in list(runner._running_tasks):
            t.cancel()
        await asyncio.sleep(0.05)

    async def test_run_apply_marks_task_failed_even_when_mark_failed_raises(
        self, task_repo: TaskRepository, mock_store: MagicMock
    ):
        # The executor errors, AND the artifact is in a state whose →failed
        # transition is forbidden (mark_failed raises ConflictError). The TASK
        # must still be recorded failed, never stranded in 'running'.
        _store_artifact(mock_store, status="approved")

        mock_lifecycle = MagicMock()
        mock_lifecycle.mark_failed = AsyncMock(
            side_effect=ConflictError("Invalid transition: applied → failed")
        )
        mock_executor = AsyncMock()
        mock_executor.apply = AsyncMock(side_effect=ExecutorError("apply blew up"))

        runner = self._make_runner(
            task_repo, mock_store, lifecycle=mock_lifecycle, executor=mock_executor
        )

        result = await runner.start_apply("2025-01-01-test-abc123", approved_by="test")

        await asyncio.sleep(0.3)

        task = await task_repo.get_task(result["task_id"])
        assert task["status"] == "failed"
        assert "apply blew up" in task["error"]


class TestTaskRunnerReconcilerLifecycle:
    async def test_run_apply_reconciler_marks_applied(
        self, task_repo: TaskRepository, mock_store: MagicMock
    ):
        _store_artifact(mock_store, status="approved")

        mock_lifecycle = MagicMock()
        mock_lifecycle.mark_applied = AsyncMock()
        mock_reconciler = AsyncMock()
        mock_result = MagicMock()
        mock_result.success = True
        mock_result.execution_log = "Ansible applied successfully"
        mock_result.failure_reason = None
        mock_reconciler.apply_single = AsyncMock(return_value=mock_result)

        runner = TaskRunner(
            repo=task_repo,
            lifecycle=mock_lifecycle,
            executor=None,
            apply_reconciler=mock_reconciler,
            store=mock_store,
        )

        result = await runner.start_apply("2025-01-01-test-abc123", approved_by="test")
        assert "task_id" in result

        await asyncio.sleep(0.3)

        task = await task_repo.get_task(result["task_id"])
        assert task["status"] == "succeeded"
        mock_lifecycle.mark_applied.assert_called_once_with(
            "2025-01-01-test-abc123", "Ansible applied successfully"
        )

    async def test_run_apply_reconciler_marks_failed(
        self, task_repo: TaskRepository, mock_store: MagicMock
    ):
        _store_artifact(mock_store, status="approved")

        mock_lifecycle = MagicMock()
        mock_lifecycle.mark_failed = AsyncMock()
        mock_reconciler = AsyncMock()
        mock_result = MagicMock()
        mock_result.success = False
        mock_result.failure_reason = "ansible failed"
        mock_result.execution_log = None
        mock_reconciler.apply_single = AsyncMock(return_value=mock_result)

        runner = TaskRunner(
            repo=task_repo,
            lifecycle=mock_lifecycle,
            executor=None,
            apply_reconciler=mock_reconciler,
            store=mock_store,
        )

        result = await runner.start_apply("2025-01-01-test-abc123", approved_by="test")
        assert "task_id" in result

        await asyncio.sleep(0.3)

        task = await task_repo.get_task(result["task_id"])
        assert task["status"] == "failed"
        mock_lifecycle.mark_failed.assert_called_once_with(
            "2025-01-01-test-abc123", "ansible failed"
        )


class TestTaskRunnerResponseShape:
    async def test_start_apply_returns_task_id_key(
        self, task_repo: TaskRepository, mock_store: MagicMock
    ):
        _store_artifact(mock_store, status="approved")
        runner = TaskRunner(
            repo=task_repo,
            lifecycle=ArtifactLifecycle(store=mock_store),
            executor=None,
            apply_reconciler=None,
            store=mock_store,
        )

        result = await runner.start_apply("2025-01-01-test-abc123", approved_by="test")

        assert "task_id" in result
        assert "id" not in result
        assert result["artifact_id"] == "2025-01-01-test-abc123"
        assert result["action"] == "apply"
        assert result["status"] == "pending"

        for t in list(runner._running_tasks):
            t.cancel()
        await asyncio.sleep(0.05)

    async def test_start_revoke_returns_task_id_key(
        self, task_repo: TaskRepository, mock_store: MagicMock
    ):
        _store_artifact(mock_store, status="approved")
        mock_lifecycle = MagicMock()
        mock_lifecycle.revoke = AsyncMock()

        runner = TaskRunner(
            repo=task_repo,
            lifecycle=mock_lifecycle,
            executor=None,
            apply_reconciler=None,
            store=mock_store,
        )

        result = await runner.start_revoke("2025-01-01-test-abc123")

        assert "task_id" in result
        assert "id" not in result
        assert result["artifact_id"] == "2025-01-01-test-abc123"
        assert result["action"] == "revoke"
        assert result["status"] == "pending"

        for t in list(runner._running_tasks):
            t.cancel()
        await asyncio.sleep(0.05)

    async def test_duplicate_apply_returns_normalized_shape(
        self, task_repo: TaskRepository, mock_store: MagicMock
    ):
        _store_artifact(mock_store, status="approved")
        runner = TaskRunner(
            repo=task_repo,
            lifecycle=MagicMock(),
            executor=None,
            apply_reconciler=None,
            store=mock_store,
        )

        first = await runner.start_apply("2025-01-01-test-abc123", approved_by="test")
        await asyncio.sleep(0.1)

        second = await runner.start_apply("2025-01-01-test-abc123", approved_by="test")

        assert set(first.keys()) == set(second.keys())
        assert "task_id" in second
        assert "id" not in second

        for t in list(runner._running_tasks):
            t.cancel()
        await asyncio.sleep(0.05)
