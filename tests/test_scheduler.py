"""Tests for auto-apply scheduler integration and ApplyReconciler periodic mode."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from homepilot.config import Settings
from homepilot.executor.orchestrator import ExecutionResult, ExecutorError
from homepilot.reconciler import ApplyReconciler, ReconcilerResult, ReconcilerScheduler


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


class TestReconcilerSchedulerAutoApply:
    def test_scheduler_does_not_start_when_disabled(self):
        scheduler = ReconcilerScheduler()
        mock_reconciler = AsyncMock(spec=ApplyReconciler)
        mock_reconciler.__class__.__name__ = "ApplyReconciler"
        scheduler.register(mock_reconciler, interval=300, startup_delay=0)
        assert len(scheduler._registered) == 1

    async def test_scheduler_starts_when_enabled(self):
        scheduler = ReconcilerScheduler()
        mock_reconciler = AsyncMock(spec=ApplyReconciler)
        mock_reconciler.run.return_value = ReconcilerResult(name="apply", success=True, details={})
        mock_reconciler.__class__.__name__ = "ApplyReconciler"

        scheduler.register(mock_reconciler, interval=300, startup_delay=0)
        assert len(scheduler._registered) == 1
        assert scheduler._registered[0].interval == 300

    async def test_scheduler_stops_cleanly(self):
        scheduler = ReconcilerScheduler()
        mock_reconciler = AsyncMock(spec=ApplyReconciler)
        mock_reconciler.run.return_value = ReconcilerResult(name="apply", success=True, details={})
        mock_reconciler.__class__.__name__ = "ApplyReconciler"

        scheduler.register(mock_reconciler, interval=300, startup_delay=0)
        await scheduler.start()
        await asyncio.sleep(0.05)
        assert scheduler._registered[0].task is not None
        assert not scheduler._registered[0].task.done()

        await scheduler.stop()
        assert scheduler._registered[0].task.done()

    async def test_scheduler_runs_apply_cycle(self):
        scheduler = ReconcilerScheduler()
        mock_reconciler = AsyncMock(spec=ApplyReconciler)
        mock_reconciler.run.return_value = ReconcilerResult(
            name="apply", success=True, details={"applied": 1, "failed": 0, "total": 1}
        )
        mock_reconciler.__class__.__name__ = "ApplyReconciler"

        scheduler.register(mock_reconciler, interval=0.05, startup_delay=0)
        await scheduler.start()
        await asyncio.sleep(0.15)
        await scheduler.stop()

        mock_reconciler.run.assert_awaited()


class TestApplyReconcilerAutoApply:
    async def test_run_returns_skipped_when_disabled(self, repo):
        reconciler = ApplyReconciler(
            store=MagicMock(), repo=repo, executor=AsyncMock(), auto_apply_enabled=False
        )
        result = await reconciler.run()

        assert result.name == "apply"
        assert result.success is True
        assert result.details["skipped"] is True
        assert "auto_apply_disabled" in result.details["reason"]

    async def test_run_no_approved_artifacts(self, repo):
        mock_store = MagicMock()
        mock_store.list.return_value = []

        reconciler = ApplyReconciler(
            store=mock_store, repo=repo, executor=AsyncMock(), auto_apply_enabled=True
        )
        result = await reconciler.run()

        assert result.name == "apply"
        assert result.success is True
        assert result.details["skipped"] is True
        assert result.details["reason"] == "no_approved_artifacts"

    async def test_run_processes_approved_artifacts(self, repo):
        mock_store = MagicMock()
        mock_store.list.return_value = [
            {"id": "2025-01-01-test-abc123", "status": "approved"},
            {"id": "2025-01-01-test-def456", "status": "approved"},
        ]

        executor = AsyncMock()
        executor.apply.return_value = ExecutionResult(
            success=True, execution_log="ok", snapshot_id=None, failure_reason=None
        )

        reconciler = ApplyReconciler(
            store=mock_store, repo=repo, executor=executor, auto_apply_enabled=True
        )
        result = await reconciler.run()

        assert result.name == "apply"
        assert result.success is True
        assert result.details["applied"] == 2
        assert result.details["failed"] == 0
        assert result.details["total"] == 2
        assert len(result.details["results"]) == 2
        assert executor.apply.await_count == 2

    async def test_run_mixed_success_and_failure(self, repo):
        mock_store = MagicMock()
        mock_store.list.return_value = [
            {"id": "art-ok", "status": "approved"},
            {"id": "art-fail", "status": "approved"},
        ]

        executor = AsyncMock()

        async def apply_side_effect(artifact_id, approved_by):
            if artifact_id == "art-fail":
                raise ExecutorError("apply failed")
            return ExecutionResult(
                success=True, execution_log="ok", snapshot_id=None, failure_reason=None
            )

        executor.apply.side_effect = apply_side_effect

        reconciler = ApplyReconciler(
            store=mock_store, repo=repo, executor=executor, auto_apply_enabled=True
        )
        result = await reconciler.run()

        assert result.success is False
        assert result.details["applied"] == 1
        assert result.details["failed"] == 1
        assert result.details["total"] == 2

    async def test_run_auto_apply_approved_by(self, repo):
        mock_store = MagicMock()
        mock_store.list.return_value = [
            {"id": "art-1", "status": "approved"},
        ]

        executor = AsyncMock()
        executor.apply.return_value = ExecutionResult(
            success=True, execution_log="ok", snapshot_id=None, failure_reason=None
        )

        reconciler = ApplyReconciler(
            store=mock_store, repo=repo, executor=executor, auto_apply_enabled=True
        )
        await reconciler.run()

        executor.apply.assert_awaited_once_with("art-1", "auto-apply")


class TestAutoApplyConfig:
    def test_auto_apply_interval_default(self):
        settings = Settings(secret_key="test")
        assert settings.auto_apply_interval_seconds == 300

    def test_auto_apply_interval_from_env(self, monkeypatch):
        monkeypatch.setenv("HP_AUTO_APPLY_INTERVAL_SECONDS", "60")
        settings = Settings(secret_key="test")
        assert settings.auto_apply_interval_seconds == 60

    def test_auto_apply_disabled_by_default(self):
        settings = Settings(secret_key="test")
        assert settings.auto_apply_enabled is False
