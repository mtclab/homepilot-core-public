from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx

from homepilot.artifacts.models import ArtifactKind, ArtifactStatus
from homepilot.artifacts.store import ArtifactStore
from homepilot.db.repository import Repository
from homepilot.events import emit_event
from homepilot.executor.orchestrator import ArtifactExecutor

from .base import Reconciler, ReconcilerResult
from .verify import VerifyResult, verify_artifact

logger = logging.getLogger(__name__)


class DriftReconciler(Reconciler):
    def __init__(
        self,
        store: ArtifactStore,
        repo: Repository,
        executor: ArtifactExecutor | None = None,
        inter_check_delay: float = 0.5,
    ) -> None:
        self.store = store
        self.repo = repo
        self.executor = executor
        self._inter_check_delay = inter_check_delay

    async def run(self) -> ReconcilerResult:
        try:
            applied = self.store.list(status=ArtifactStatus.APPLIED.value)
            checkable = [a for a in applied if a.get("kind") != ArtifactKind.KB_NOTE.value]

            if not checkable:
                return ReconcilerResult(
                    name="drift",
                    success=True,
                    details={
                        "checked": 0,
                        "drifted": 0,
                        "skipped_kb_notes": len(applied) - len(checkable),
                        "errors": 0,
                        "skipped_no_executor": 0,
                    },
                )

            if self.executor is None:
                skipped_no_executor = len(checkable)
                logger.warning(
                    "DriftReconciler: skipping %d artifacts — no executor configured "
                    "(Proxmox client unavailable)",
                    skipped_no_executor,
                )
                return ReconcilerResult(
                    name="drift",
                    success=True,
                    details={
                        "checked": 0,
                        "drifted": 0,
                        "skipped_kb_notes": 0,
                        "errors": 0,
                        "skipped_no_executor": skipped_no_executor,
                    },
                )

            checked = 0
            drifted = 0
            skipped = 0
            errors = 0
            drift_details: list[dict[str, Any]] = []

            for fm in checkable:
                artifact_id = fm.get("id", "")
                try:
                    result = await self.check_single(artifact_id)
                    checked += 1
                    if result.drifted:
                        drifted += 1
                    drift_details.append(
                        {
                            "artifact_id": artifact_id,
                            "drifted": result.drifted,
                            "log": result.verification_log[:200],
                        }
                    )
                except Exception as exc:
                    errors += 1
                    logger.warning("Drift check failed for %s: %s", artifact_id, exc)
                    drift_details.append(
                        {
                            "artifact_id": artifact_id,
                            "drifted": None,
                            "error": str(exc)[:200],
                        }
                    )

                await asyncio.sleep(self._inter_check_delay)

            skipped = len(applied) - len(checkable)

            details: dict[str, Any] = {
                "checked": checked,
                "drifted": drifted,
                "skipped_kb_notes": skipped,
                "errors": errors,
                "skipped_no_executor": 0,
            }

            try:
                await self.repo.log_audit(
                    user_id="system",
                    source="reconciler:drift",
                    action="check_cycle",
                    details_json=json.dumps(details),
                )
            except (OSError, RuntimeError, ValueError):
                logger.exception("DriftReconciler audit log failed")
                details["audit_log_error"] = True

            return ReconcilerResult(name="drift", success=True, details=details)

        except (OSError, RuntimeError, ValueError, httpx.HTTPError) as exc:
            logger.exception("DriftReconciler failed")
            return ReconcilerResult(name="drift", success=False, details={"error": str(exc)})

    async def check_single(self, artifact_id: str) -> VerifyResult:
        try:
            result = await verify_artifact(
                artifact_id=artifact_id,
                repo=self.repo,
                store=self.store,
                executor=self.executor,
            )

            details_json = json.dumps(result.details) if result.details else None
            await self.repo.upsert_drift_check(
                artifact_id=artifact_id,
                drifted=result.drifted,
                details_json=details_json,
                state=result.state.value,
            )

            try:
                await self.repo.log_audit(
                    user_id="system",
                    source="reconciler:drift",
                    action="check_drift",
                    artifact_id=artifact_id,
                    details_json=json.dumps(
                        {
                            "drifted": result.drifted,
                            "state": result.state.value,
                            "log": result.verification_log[:500],
                        }
                    ),
                )
            except (OSError, RuntimeError, ValueError):
                logger.exception("Drift audit log failed for %s", artifact_id)

            # Drift is only actionable if something is told about it. Emit an
            # artifact_drifted event (SSE + outbound webhook fan-out) exactly
            # when the artifact has actually drifted — never for in-spec ones.
            if result.drifted:
                await self._emit_drift_event(artifact_id, result)

            return result

        except FileNotFoundError:
            raise
        except Exception:  # re-raises after logging — intentional broad catch
            logger.exception("Single drift check failed for %s", artifact_id)
            raise

    async def _emit_drift_event(self, artifact_id: str, result: VerifyResult) -> None:
        """Emit an ``artifact_drifted`` event, best-effort.

        An emit failure (SSE bus, webhook fan-out, etc.) must never fail the
        drift cycle — drift has already been detected and persisted by the time
        we get here, so the observation stands regardless of delivery.
        """
        try:
            summary = (result.verification_log or "").strip()[:500] or "drift detected"
            payload: dict[str, Any] = {
                "id": artifact_id,
                "status": "drifted",
                "drifted": True,
                "drift_summary": summary,
            }
            if result.details:
                payload["details"] = result.details
            try:
                fm, _ = self.store.read(artifact_id)
                payload["kind"] = fm.get("kind", "")
                payload["intent"] = fm.get("intent", "")
                target = fm.get("target")
                if target:
                    payload["target"] = target
            except (FileNotFoundError, OSError):
                logger.debug("Could not enrich artifact_drifted payload for %s", artifact_id)
            await emit_event("artifact_drifted", payload, repo=self.repo)
        except Exception:  # best-effort: emission must not break the drift cycle
            logger.warning("artifact_drifted emit failed for %s", artifact_id, exc_info=True)
