from __future__ import annotations

import logging
from typing import Any

from homepilot.artifacts.models import extract_composite_steps
from homepilot.executor.composite_lib import _topological_sort

logger = logging.getLogger(__name__)

_SPEC_FENCE = "composite-spec"


async def execute(
    frontmatter: dict[str, Any],
    body: str,
    lifecycle: Any,
    executor: Any,
    rollback: bool = False,
) -> dict[str, Any]:
    from homepilot.artifacts.models import ArtifactStatus

    try:
        steps = extract_composite_steps(body)
    except ValueError:
        steps = []
    if not steps:
        return {
            "success": False,
            "execution_log": f"No ```yaml {_SPEC_FENCE}``` block found",
            "failure_reason": "missing spec",
        }

    try:
        ordered = _topological_sort(steps)
    except ValueError as e:
        return {"success": False, "execution_log": str(e), "failure_reason": str(e)}

    artifact_store = getattr(executor, "store", None)

    log_lines: list[str] = []
    applied_sub_ids: list[str] = []
    failed = False

    if not rollback:
        for step in ordered:
            sid = step.get("id", "unknown")
            sub_artifact_id = step.get("artifact", "")
            on_error = step.get("on_error", "halt")

            if not sub_artifact_id:
                log_lines.append(f"[{sid}] missing artifact reference, skipping")
                continue

            log_lines.append(f"[{sid}] applying sub-artifact {sub_artifact_id}")

            if artifact_store:
                try:
                    sub_fm, _ = artifact_store.read(sub_artifact_id)
                    sub_status = ArtifactStatus(sub_fm.get("status", ""))
                    if sub_status == ArtifactStatus.APPLIED:
                        log_lines.append(f"[{sid}] already applied, skipping")
                        continue
                except FileNotFoundError:
                    log_lines.append(f"[{sid}] sub-artifact {sub_artifact_id} not found")
                    if on_error == "halt":
                        return {
                            "success": False,
                            "execution_log": "\n".join(log_lines),
                            "failure_reason": f"sub-artifact {sub_artifact_id} not found",
                        }
                    continue

            try:
                result = await executor.apply(sub_artifact_id, approved_by="composite")
                result_success = (
                    result.success if hasattr(result, "success") else result.get("success")
                )
                result_reason = (
                    result.failure_reason
                    if hasattr(result, "failure_reason")
                    else result.get("failure_reason", "unknown")
                )
                if result_success:
                    log_lines.append(f"[{sid}] OK")
                    applied_sub_ids.append(sub_artifact_id)
                else:
                    log_lines.append(f"[{sid}] FAILED: {result_reason}")
                    if on_error == "halt":
                        return {
                            "success": False,
                            "execution_log": "\n".join(log_lines),
                            "failure_reason": f"sub-artifact {sub_artifact_id} failed",
                        }
                    failed = True
            except Exception as e:
                log_lines.append(f"[{sid}] ERROR: {e}")
                if on_error == "halt":
                    return {
                        "success": False,
                        "execution_log": "\n".join(log_lines),
                        "failure_reason": str(e),
                    }
                failed = True
    else:
        applied_ids: set[str] = set()
        if artifact_store:
            for step in reversed(ordered):
                sid = step.get("id", "unknown")
                sub_artifact_id = step.get("artifact", "")
                if not sub_artifact_id:
                    continue
                try:
                    sub_fm, _ = artifact_store.read(sub_artifact_id)
                    sub_status = sub_fm.get("status", "")
                    if sub_status in ("applied", "approved"):
                        applied_ids.add(sub_artifact_id)
                except FileNotFoundError:
                    pass
        for step in reversed(ordered):
            sid = step.get("id", "unknown")
            sub_artifact_id = step.get("artifact", "")
            if sub_artifact_id in applied_ids:
                log_lines.append(f"[{sid}] rollback sub-artifact {sub_artifact_id}")
                try:
                    await executor.revoke(
                        sub_artifact_id, user="composite-rollback", reason="composite rollback"
                    )
                except Exception as e:
                    log_lines.append(f"[{sid}] rollback failed (best-effort): {e}")

    success = not failed
    return {"success": success, "execution_log": "\n".join(log_lines)}
