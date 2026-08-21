from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from typing import Any

import httpx

from homepilot.artifacts.store import ArtifactStore
from homepilot.db.repository import Repository
from homepilot.executor.orchestrator import ArtifactExecutor, ExecutorError, TamperError

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

        approved = self._get_approved_artifacts()
        if not approved:
            return ReconcilerResult(
                name="apply",
                success=True,
                details={"skipped": True, "reason": "no_approved_artifacts"},
            )

        results: list[dict[str, Any]] = []
        success_count = 0
        failure_count = 0
        for artifact_id in approved:
            try:
                result = await self.apply_single(artifact_id, approved_by="auto-apply")
                if result.success:
                    success_count += 1
                else:
                    failure_count += 1
                results.append(
                    {
                        "artifact_id": artifact_id,
                        "success": result.success,
                        "details": result.details,
                    }
                )
            except (ExecutorError, TamperError, OSError, ValueError, httpx.HTTPError):
                failure_count += 1
                logger.exception("Auto-apply failed for %s", artifact_id)
                results.append(
                    {
                        "artifact_id": artifact_id,
                        "success": False,
                        "details": {"error": "exception"},
                    }
                )

        return ReconcilerResult(
            name="apply",
            success=failure_count == 0,
            details={
                "applied": success_count,
                "failed": failure_count,
                "total": len(approved),
                "results": results,
            },
        )

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
