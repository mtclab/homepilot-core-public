from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..artifacts.lifecycle import ArtifactLifecycle, ConflictError
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
        # task_id → in-flight asyncio.Task, so cancel() can reach the coroutine
        # that is actually executing an apply/revoke. Cleared on completion.
        self._task_by_id: dict[str, asyncio.Task[Any]] = {}

    def _track_task(self, task: asyncio.Task[Any], task_id: str) -> None:
        self._running_tasks.add(task)
        self._task_by_id[task_id] = task

        def _done(t: asyncio.Task[Any]) -> None:
            self._running_tasks.discard(t)
            # Only drop the mapping if it still points at THIS task — a fresh
            # task for a reused id (shouldn't happen: ids are uuid4) must win.
            if self._task_by_id.get(task_id) is t:
                del self._task_by_id[task_id]

        task.add_done_callback(_done)

    async def start_apply(self, artifact_id: str, approved_by: str = "system") -> dict[str, Any]:
        # Validate the artifact BEFORE creating a task (#386). An already-APPLIED
        # (or otherwise non-approved) artifact must be rejected cleanly here: if
        # we created the task first and then rejected, that task would be stranded
        # (executor.apply raises, the APPLIED→FAILED transition also raises, and
        # the task never leaves 'running'), blocking all future apply/revoke.
        try:
            fm, _body = self.store.read(artifact_id)
        except FileNotFoundError:
            raise ValueError(f"Artifact not found: {artifact_id}") from None

        status = ArtifactStatus(fm["status"])
        if status != ArtifactStatus.APPROVED:
            raise ConflictError(f"Invalid transition: {status.value} → apply")

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

        # task_id is non-None here (the None branch returned above); guard
        # explicitly so it survives `python -O` (which strips asserts).
        if task_id is None:  # pragma: no cover - defensive
            raise RuntimeError("task_id unexpectedly None after creation")
        task = asyncio.create_task(self._run_apply(task_id, artifact_id, approved_by))
        self._track_task(task, task_id)

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
            raise ConflictError(f"Invalid transition: {status.value} → revoke")

        task_id = await self.repo.create_task(artifact_id, "revoke")
        task = asyncio.create_task(self._run_revoke(task_id, artifact_id, user, reason))
        self._track_task(task, task_id)

        return {
            "task_id": task_id,
            "artifact_id": artifact_id,
            "action": "revoke",
            "status": "pending",
        }

    async def cancel_task(self, task_id: str) -> dict[str, Any]:
        # Best-effort stop-then-mark. If we still hold the in-flight asyncio.Task
        # (same process, still running) cancel it — the CancelledError raised into
        # _run_apply/_run_revoke is a BaseException, so their `except Exception`
        # never fires and never overwrites the record we mark below. If the task
        # is gone (a different process, or after a restart) we STILL mark the
        # record cancelled so it stops blocking future applies for the artifact.
        # Cancelling an already-finished task is a no-op returning its status.
        at = self._task_by_id.get(task_id)
        if at is not None and not at.done():
            at.cancel()
        result = await self.repo.cancel_task(task_id)
        if result is None:
            raise ValueError(f"Task not found: {task_id}")
        return result

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
                # ArtifactExecutor.apply already performs the artifact transition
                # and writes the audit row (orchestrator.py). Repeating it here
                # meant a SUCCESSFUL apply attempted applied -> applied, which the
                # transition table forbids, so every apply through the API and the
                # UI - the product's primary action - reported the task as failed
                # while the host had in fact been changed. This branch owns the
                # TASK record only; the artifact's state has exactly one owner.
                result = await self.apply_reconciler.apply_single(artifact_id, approved_by)
                if result.success:
                    await self.repo.update_task_status(task_id, "succeeded")
                else:
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
            # Marking the artifact failed is best-effort: it may already be in a
            # terminal state that forbids the →failed transition (e.g. an
            # apply-on-APPLIED raises ConflictError). That must NEVER prevent the
            # task itself from being marked failed — otherwise the task is stranded
            # in 'running' forever and blocks all future apply/revoke (#386).
            try:
                await self.lifecycle.mark_failed(artifact_id, error_msg)
            except Exception:
                logger.warning("Could not mark artifact %s as failed", artifact_id, exc_info=True)
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
