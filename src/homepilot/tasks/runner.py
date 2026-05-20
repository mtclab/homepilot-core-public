from __future__ import annotations

import asyncio
import logging
import sqlite3
from typing import Any

from ..artifacts.lifecycle import ArtifactLifecycle, LifecycleError
from ..artifacts.models import ArtifactStatus
from ..artifacts.store import ArtifactStore
from ..executor.orchestrator import ArtifactExecutor
from .repository import TaskRepository

logger = logging.getLogger(__name__)


class TaskRunner:
    def __init__(
        self,
        repo: TaskRepository,
        lifecycle: ArtifactLifecycle,
        executor: ArtifactExecutor | None,
        apply_reconciler: Any | None,
        store: ArtifactStore,
    ):
        self.repo = repo
        self.lifecycle = lifecycle
        self.executor = executor
        self.apply_reconciler = apply_reconciler
        self.store = store
        self._running_tasks: set[asyncio.Task[Any]] = set()

    def _track_task(self, task: asyncio.Task[Any]) -> None:
        self._running_tasks.add(task)
        task.add_done_callback(self._running_tasks.discard)

    async def start_apply(self, artifact_id: str, approved_by: str = "system") -> dict[str, Any]:
        task_id = await self.repo.create_task_if_no_active(artifact_id, "apply")
        if task_id is None:
            active = await self.repo.get_active_task(artifact_id)
            if active is not None:
                full_task = await self.repo.get_task(active["id"])
                t = full_task or active
                return {
                    "task_id": t["id"],
                    "artifact_id": t["artifact_id"],
                    "action": t["action"],
                    "status": t["status"],
                }

        try:
            fm, _body = self.store.read(artifact_id)
        except FileNotFoundError:
            raise ValueError(f"Artifact not found: {artifact_id}") from None

        status = ArtifactStatus(fm["status"])
        if status not in (ArtifactStatus.APPROVED,):
            raise LifecycleError(f"Invalid transition: {status.value} → apply")

        assert task_id is not None
        task = asyncio.create_task(self._run_apply(task_id, artifact_id, approved_by))
        self._track_task(task)

        return {
            "task_id": task_id,
            "artifact_id": artifact_id,
            "action": "apply",
            "status": "pending",
        }

    async def start_revoke(
        self, artifact_id: str, user: str = "system", reason: str | None = None
    ) -> dict[str, Any]:
        active = await self.repo.get_active_task(artifact_id)
        if active is not None and active["action"] in ("apply", "replay"):
            raise ValueError(f"apply_in_progress: task {active['id']} is {active['status']}")

        try:
            fm, _body = self.store.read(artifact_id)
        except FileNotFoundError:
            raise ValueError(f"Artifact not found: {artifact_id}") from None

        status = ArtifactStatus(fm["status"])
        if status not in (ArtifactStatus.APPROVED, ArtifactStatus.APPLIED, ArtifactStatus.FAILED):
            raise LifecycleError(f"Invalid transition: {status.value} → revoke")

        task_id = await self.repo.create_task(artifact_id, "revoke")
        task = asyncio.create_task(self._run_revoke(task_id, artifact_id, user, reason))
        self._track_task(task)

        return {
            "task_id": task_id,
            "artifact_id": artifact_id,
            "action": "revoke",
            "status": "pending",
        }

    async def await_task(self, task_id: str, timeout: float = 300.0) -> dict[str, Any]:
        interval = 1.0
        max_interval = 10.0
        elapsed = 0.0
        while elapsed < timeout:
            task = await self.repo.get_task(task_id)
            if task is None:
                raise ValueError(f"Task not found: {task_id}")
            if task["status"] in ("succeeded", "failed"):
                return task
            await asyncio.sleep(interval)
            elapsed += interval
            interval = min(interval * 2, max_interval)
        task = await self.repo.get_task(task_id)
        if task is None:
            raise ValueError(f"Task not found: {task_id}")
        return task

    async def _run_apply(self, task_id: str, artifact_id: str, approved_by: str) -> None:
        await self.repo.update_task_status(task_id, "running")
        try:
            if self.apply_reconciler is not None:
                result = await self.apply_reconciler.apply_single(artifact_id, approved_by)
                if result.success:
                    await self.lifecycle.mark_applied(
                        artifact_id, result.execution_log or "Applied via reconciler"
                    )
                    await self.repo.update_task_status(task_id, "succeeded")
                else:
                    await self.lifecycle.mark_failed(
                        artifact_id, result.failure_reason or "apply failed"
                    )
                    await self.repo.update_task_status(
                        task_id,
                        "failed",
                        error=result.failure_reason or "apply failed",
                    )
            elif self.executor is not None:
                result = await self.executor.apply(artifact_id, approved_by)
                if result.success:
                    await self.repo.update_task_status(task_id, "succeeded")
                else:
                    await self.repo.update_task_status(
                        task_id,
                        "failed",
                        error=result.failure_reason or "apply failed",
                    )
            else:
                await self.lifecycle.mark_applied(artifact_id, "Applied without executor")
                await self.repo.update_task_status(task_id, "succeeded")
        except Exception as exc:
            logger.exception("Apply task %s failed for artifact %s", task_id, artifact_id)
            error_msg = str(exc)
            try:
                await self.lifecycle.mark_failed(artifact_id, error_msg)
            except (OSError, sqlite3.OperationalError):
                logger.warning("Could not mark artifact %s as failed", artifact_id)
            await self.repo.update_task_status(task_id, "failed", error=error_msg)

    async def _run_revoke(
        self, task_id: str, artifact_id: str, user: str, reason: str | None
    ) -> None:
        await self.repo.update_task_status(task_id, "running")
        try:
            if self.executor is not None:
                await self.executor.revoke(artifact_id, user, reason)
            else:
                await self.lifecycle.revoke(artifact_id, user, reason)
            await self.repo.update_task_status(task_id, "succeeded")
        except Exception as exc:
            logger.exception("Revoke task %s failed for artifact %s", task_id, artifact_id)
            await self.repo.update_task_status(task_id, "failed", error=str(exc))
