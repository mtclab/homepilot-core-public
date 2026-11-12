from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from homepilot.artifacts.store import ArtifactStore
from homepilot.db.repository import Repository
from homepilot.executor.orchestrator import ArtifactExecutor, ExecutorError, TamperError
from homepilot.runtime.blocking import run_blocking

from .base import Reconciler, ReconcilerResult

logger = logging.getLogger(__name__)


@dataclass
class ApplyResult:
    artifact_id: str
    success: bool
    execution_log: str = ""
    snapshot_id: str | None = None
    failure_reason: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    # The PVE task still running when the run stopped following it (#774): the
    # outcome is unknown, which the task runner must not record as failed.
    unsettled_task: dict[str, Any] | None = None
    # What a failed run DID change before it stopped (#796), and the PVE task
    # warnings a successful one logged - passed through to the task record.
    partial_apply: dict[str, Any] | None = None
    warnings: list[dict[str, Any]] = field(default_factory=list)


class ApplyReconciler(Reconciler):
    def __init__(
        self,
        store: ArtifactStore,
        repo: Repository,
        executor: ArtifactExecutor,
        auto_apply_enabled: bool | None = None,
    ) -> None:
        self.store = store
        self.repo = repo
        self.executor = executor
        self._auto_apply_enabled_override = auto_apply_enabled
        # The backend's TaskRunner, set once both exist (main.py builds this
        # first and hands it to the runner). Auto-apply claims each artifact's
        # run slot through it (#710); without one, `_task_runner()` builds a
        # runner over this reconciler's own database - the gate is the DB row.
        self.task_runner: Any | None = None

    def _is_auto_apply_enabled(self) -> bool:
        if self._auto_apply_enabled_override is not None:
            return self._auto_apply_enabled_override
        from homepilot.config import get_settings

        return get_settings().auto_apply_enabled

    async def run(self) -> ReconcilerResult:
        if not self._is_auto_apply_enabled():
            logger.warning("ApplyReconciler.run() called but auto_apply is disabled — skipping")
            return ReconcilerResult(
                name="apply",
                success=True,
                details={"skipped": True, "reason": "auto_apply_disabled"},
            )

        approved = await run_blocking("io", self._get_approved_artifacts)
        if not approved:
            return ReconcilerResult(
                name="apply",
                success=True,
                details={"skipped": True, "reason": "no_approved_artifacts"},
            )

        from homepilot.artifacts.models import ArtifactBusyError

        runner = self._task_runner()
        results: list[dict[str, Any]] = []
        success_count = 0
        failure_count = 0
        busy_count = 0
        unknown_count = 0
        for artifact_id in approved:
            # Through the task gate (#710): an artifact whose apply is still
            # running stays `approved`, and calling the executor straight
            # dispatched it a second time on every tick.
            try:
                task = await runner.run_apply_now(artifact_id, "auto-apply")
            except ArtifactBusyError as busy:
                busy_count += 1
                results.append(
                    {
                        "artifact_id": artifact_id,
                        "success": None,
                        "details": {"skipped": "busy", "task_id": busy.task_id},
                    }
                )
                continue
            except Exception:
                # One artifact's failure to even START must not cost every
                # later artifact in this tick its turn.
                failure_count += 1
                logger.exception("Auto-apply could not start for %s", artifact_id)
                results.append(
                    {
                        "artifact_id": artifact_id,
                        "success": False,
                        "details": {"error": "exception"},
                    }
                )
                continue
            status = task.get("status")
            ok = status == "succeeded"
            if status == "unknown":
                # Still running on PVE: not a failure, not yet a success (#774).
                # The runner recorded it so, and settles it from PVE later.
                unknown_count += 1
            elif ok:
                success_count += 1
            else:
                failure_count += 1
            details: dict[str, Any] = {"task_id": task.get("id")}
            if not ok and task.get("error"):
                details["error"] = task["error"]
            results.append({"artifact_id": artifact_id, "success": ok, "details": details})

        return ReconcilerResult(
            name="apply",
            success=failure_count == 0,
            details={
                "applied": success_count,
                "failed": failure_count,
                "busy": busy_count,
                "unknown": unknown_count,
                "total": len(approved),
                "results": results,
            },
        )

    def _task_runner(self) -> Any:
        if self.task_runner is not None:
            return self.task_runner
        from homepilot.tasks.repository import TaskRepository
        from homepilot.tasks.runner import TaskRunner

        self.task_runner = TaskRunner(
            repo=TaskRepository(self.repo.db),
            lifecycle=self.executor.lifecycle,
            executor=self.executor,
            apply_reconciler=self,
            store=self.store,
        )
        return self.task_runner

    def _get_approved_artifacts(self) -> list[str]:
        approved = self.store.list(status="approved")
        return [a.get("id", "") for a in approved if a.get("id")]

    async def apply_single(self, artifact_id: str, approved_by: str = "system") -> ApplyResult:
        try:
            result = await self.executor.apply(artifact_id, approved_by)
        except (ExecutorError, TamperError) as exc:
            await self._log_audit(
                artifact_id=artifact_id,
                approved_by=approved_by,
                action="apply_failed",
                details={"reason": str(exc)},
            )
            raise

        details: dict[str, Any] = {
            "approved_by": approved_by,
            "execution_log": result.execution_log[:500],
            "reconciler_source": "reconciler:apply",
        }
        if result.snapshot_id:
            details["snapshot_id"] = result.snapshot_id
        if result.failure_reason:
            details["failure_reason"] = result.failure_reason

        # ArtifactExecutor.apply already wrote the actor-bearing audit row for
        # this same event; writing a second one here gave the log two rows per
        # apply, which is one too many for the record an operator reads to answer
        # "who applied what". The snapshot id it used to carry is handed to the
        # executor instead, so nothing is lost.

        return ApplyResult(
            artifact_id=artifact_id,
            success=result.success,
            execution_log=result.execution_log,
            snapshot_id=result.snapshot_id,
            failure_reason=result.failure_reason,
            details=details,
            unsettled_task=result.unsettled_task,
            partial_apply=result.partial_apply,
            warnings=list(result.warnings),
        )

    async def _log_audit(
        self,
        artifact_id: str,
        approved_by: str,
        action: str,
        details: dict[str, Any],
    ) -> None:
        try:
            await self.repo.log_audit(
                user_id=approved_by,
                source="reconciler:apply",
                action=action,
                artifact_id=artifact_id,
                details_json=json.dumps(details),
            )
        except (sqlite3.OperationalError, sqlite3.IntegrityError, OSError, RuntimeError) as exc:
            logger.exception("ApplyReconciler audit log failed for %s: %s", artifact_id, exc)
