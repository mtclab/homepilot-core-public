from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from homepilot.executor.orchestrator import ExecutionResult, ExecutorError, TamperError
from homepilot.reconciler import ApplyReconciler, ApplyResult, ReconcilerResult


@pytest.fixture
async def real_db(tmp_path: Path):
    from homepilot.db.connection import Database
    from homepilot.db.migrations import run_migrations

    db = Database(str(tmp_path / "test.db"))
    await db.connect()
    await run_migrations(db)
    yield db
    await db.close()


@pytest.fixture
async def repo(real_db):
    from homepilot.db.repository import Repository

    return Repository(real_db)


def _make_executor(success=True, execution_log="ok", snapshot_id=None, failure_reason=None):
    executor = AsyncMock()
    executor.apply.return_value = ExecutionResult(
        success=success,
        execution_log=execution_log,
        snapshot_id=snapshot_id,
        failure_reason=failure_reason,
    )
    return executor


class TestApplyReconcilerRun:
    async def test_run_returns_skipped_when_disabled(self, repo):
        reconciler = ApplyReconciler(
            store=MagicMock(), repo=repo, executor=_make_executor(), auto_apply_enabled=False
        )
        result = await reconciler.run()

        assert isinstance(result, ReconcilerResult)
        assert result.name == "apply"
        assert result.success is True
        assert result.details["skipped"] is True
        assert "auto_apply_disabled" in result.details["reason"]


class TestApplyReconcilerApplySingle:
    async def test_apply_single_success(self, repo):
        executor = _make_executor(success=True, execution_log="applied successfully")
        reconciler = ApplyReconciler(store=MagicMock(), repo=repo, executor=executor)
        result = await reconciler.apply_single("test-artifact-1", approved_by="admin")

        assert isinstance(result, ApplyResult)
        assert result.artifact_id == "test-artifact-1"
        assert result.success is True
        assert result.execution_log == "applied successfully"
        assert result.details["approved_by"] == "admin"
        assert result.details["reconciler_source"] == "reconciler:apply"
        executor.apply.assert_awaited_once_with("test-artifact-1", "admin")

    async def test_apply_single_default_approved_by(self, repo):
        executor = _make_executor()
        reconciler = ApplyReconciler(store=MagicMock(), repo=repo, executor=executor)
        result = await reconciler.apply_single("a1")

        assert result.success is True
        assert result.details["approved_by"] == "system"
        executor.apply.assert_awaited_once_with("a1", "system")

    async def test_apply_single_with_snapshot(self, repo):
        executor = _make_executor(snapshot_id="snap-123")
        reconciler = ApplyReconciler(store=MagicMock(), repo=repo, executor=executor)
        result = await reconciler.apply_single("a1")

        assert result.snapshot_id == "snap-123"
        assert result.details["snapshot_id"] == "snap-123"

    async def test_apply_single_executor_returns_failure(self, repo):
        executor = _make_executor(success=False, execution_log="partial", failure_reason="timeout")
        reconciler = ApplyReconciler(store=MagicMock(), repo=repo, executor=executor)
        result = await reconciler.apply_single("a1")

        assert result.success is False
        assert result.failure_reason == "timeout"
        assert result.details["failure_reason"] == "timeout"

    async def test_apply_single_executor_error_raises(self, repo):
        executor = _make_executor()
        executor.apply.side_effect = ExecutorError("status not approved")
        reconciler = ApplyReconciler(store=MagicMock(), repo=repo, executor=executor)

        with pytest.raises(ExecutorError, match="status not approved"):
            await reconciler.apply_single("a1")

    async def test_apply_single_tamper_error_raises(self, repo):
        executor = _make_executor()
        executor.apply.side_effect = TamperError("hash mismatch")
        reconciler = ApplyReconciler(store=MagicMock(), repo=repo, executor=executor)

        with pytest.raises(TamperError, match="hash mismatch"):
            await reconciler.apply_single("a1")

    async def test_apply_single_executor_error_writes_audit(self, repo):
        executor = _make_executor()
        executor.apply.side_effect = ExecutorError("bad status")
        reconciler = ApplyReconciler(store=MagicMock(), repo=repo, executor=executor)

        with pytest.raises(ExecutorError):
            await reconciler.apply_single("a1", approved_by="admin")

        rows = await repo.db.fetchall(
            "SELECT * FROM audit_log WHERE source = 'reconciler:apply' AND action = 'apply_failed'"
        )
        assert len(rows) >= 1
        assert rows[0]["user_id"] == "admin"
        assert rows[0]["artifact_id"] == "a1"

    async def test_apply_single_does_not_write_its_own_apply_audit(self, repo):
        """One apply, one actor-bearing audit row - written by the executor.

        The reconciler used to add a second row under its own source, so the log
        an operator reads to answer "who applied this" had two answers for one
        event. The executor's row carries the actor and the snapshot id.
        """
        executor = _make_executor(success=True, execution_log="done")
        reconciler = ApplyReconciler(store=MagicMock(), repo=repo, executor=executor)
        await reconciler.apply_single("a1", approved_by="admin")

        rows = await repo.db.fetchall("SELECT * FROM audit_log WHERE source = 'reconciler:apply'")
        assert rows == []

    async def test_apply_single_failure_does_not_write_its_own_audit(self, repo):
        executor = _make_executor(success=False, execution_log="fail", failure_reason="err")
        reconciler = ApplyReconciler(store=MagicMock(), repo=repo, executor=executor)
        result = await reconciler.apply_single("a1", approved_by="admin")

        rows = await repo.db.fetchall("SELECT * FROM audit_log WHERE source = 'reconciler:apply'")
        assert rows == []
        # The failure itself must still reach the caller.
        assert result.success is False
        assert result.failure_reason == "err"

    async def test_apply_single_audit_failure_does_not_fail_apply(self, repo):
        executor = _make_executor(success=True)
        reconciler = ApplyReconciler(store=MagicMock(), repo=repo, executor=executor)

        async def failing_log_audit(*args, **kwargs):
            raise RuntimeError("DB connection lost")

        repo.log_audit = failing_log_audit

        result = await reconciler.apply_single("a1")

        assert result.success is True

    async def test_apply_single_audit_failure_on_error_does_not_suppress(self, repo):
        executor = _make_executor()
        executor.apply.side_effect = ExecutorError("bad")

        async def failing_log_audit(*args, **kwargs):
            raise RuntimeError("DB down")

        repo.log_audit = failing_log_audit

        reconciler = ApplyReconciler(store=MagicMock(), repo=repo, executor=executor)

        with pytest.raises(ExecutorError, match="bad"):
            await reconciler.apply_single("a1")

    async def test_apply_single_long_execution_log_truncated(self, repo):
        long_log = "x" * 600
        executor = _make_executor(success=True, execution_log=long_log)
        reconciler = ApplyReconciler(store=MagicMock(), repo=repo, executor=executor)
        result = await reconciler.apply_single("a1")

        assert len(result.details["execution_log"]) == 500
        assert result.execution_log == long_log
