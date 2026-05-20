from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import time
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

from ..auth.deps import require_scope
from ..sse import bus as sse_bus
from .lifecycle import ArtifactLifecycle, LifecycleError
from .store import ArtifactStore

logger = logging.getLogger(__name__)

router = APIRouter()


def _get_store(request: Request) -> ArtifactStore:
    store: ArtifactStore = request.app.state.artifact_store
    return store


def _get_lifecycle(request: Request) -> ArtifactLifecycle:
    lifecycle: ArtifactLifecycle = request.app.state.artifact_lifecycle
    return lifecycle


@router.get("")
async def list_artifacts(
    request: Request,
    status: str | None = Query(None),
    kind: str | None = Query(None),
    limit: int = Query(100, ge=1, le=1000),
) -> dict[str, Any]:
    store = _get_store(request)
    items = store.list(status=status, kind=kind)
    return {"items": items[:limit], "total": len(items)}


@router.get("/drift")
async def get_drift_status(
    request: Request,
    refresh: bool = Query(False),
    artifact_id: str | None = Query(None),
    drifted: bool | None = Query(None),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    from homepilot.reconciler import DriftReconciler

    if refresh:
        drift_reconciler: DriftReconciler | None = getattr(
            request.app.state, "drift_reconciler", None
        )
        if drift_reconciler is None:
            raise HTTPException(status_code=501, detail="DriftReconciler not configured")

        if artifact_id:
            try:
                await drift_reconciler.check_single(artifact_id)
            except FileNotFoundError:
                raise HTTPException(
                    status_code=404,
                    detail=f"Artifact not found: {artifact_id}",
                ) from None
            except Exception as exc:
                raise HTTPException(status_code=500, detail="Drift check failed") from exc
            row = await request.app.state.repo.get_drift_check(artifact_id)
            items = [row] if row else []
        else:
            cycle_result = await drift_reconciler.run()
            if not cycle_result.success:
                raise HTTPException(
                    status_code=500,
                    detail="Drift cycle failed",
                )
            items = await request.app.state.repo.get_drift_checks(
                drifted=drifted, limit=limit, offset=offset
            )
    else:
        if artifact_id:
            row = await request.app.state.repo.get_drift_check(artifact_id)
            items = [row] if row else []
        else:
            items = await request.app.state.repo.get_drift_checks(
                drifted=drifted, limit=limit, offset=offset
            )

    return {"items": items, "total": len(items)}


@router.get("/{artifact_id}")
async def get_artifact(request: Request, artifact_id: str) -> dict[str, Any]:
    store = _get_store(request)
    try:
        fm, body = store.read(artifact_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Artifact not found: {artifact_id}") from None
    task_repo = request.app.state.task_repo
    active_task = await task_repo.get_active_task(artifact_id)
    return {"frontmatter": fm, "body": body, "active_task": active_task}


@router.post("", dependencies=[Depends(require_scope("write"))])
async def propose_artifact(request: Request) -> dict[str, Any]:
    lifecycle = _get_lifecycle(request)
    spec = await request.json()
    try:
        artifact_id = await lifecycle.propose(spec)
    except LifecycleError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"id": artifact_id}


@router.post("/{artifact_id}/preview", dependencies=[Depends(require_scope("read"))])
async def preview_artifact(request: Request, artifact_id: str) -> dict[str, Any]:
    store = _get_store(request)
    try:
        fm, body = store.read(artifact_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Artifact not found: {artifact_id}") from None

    if "/" in artifact_id or "\\" in artifact_id or ".." in artifact_id:
        raise HTTPException(
            status_code=400, detail="Invalid artifact_id: path separators not allowed"
        )

    diff = ""
    try:
        path = store.resolve_path(artifact_id)
        resolved = path.resolve()
        if not str(resolved).startswith(str(store.root.resolve())):
            raise HTTPException(
                status_code=400,
                detail="Invalid artifact_id: path traversal detected",
            )
        rel = path.relative_to(store.root)
        result = subprocess.run(
            ["git", "diff", "HEAD~1", "--", str(rel)],
            cwd=store.root,
            capture_output=True,
            text=True,
            timeout=30,
        )
        diff = result.stdout
        if result.returncode != 0:
            logger.warning("git diff failed (rc=%d): %s", result.returncode, result.stderr.strip())
    except subprocess.SubprocessError:
        logger.warning("git diff timed out for artifact %s", artifact_id)
        diff = ""
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("git diff error for artifact %s: %s", artifact_id, exc)
        diff = ""

    return {
        "id": artifact_id,
        "status": fm.get("status", ""),
        "kind": fm.get("kind", ""),
        "intent": fm.get("intent", ""),
        "diff": diff,
        "body": body,
        "frontmatter": fm,
    }


@router.post("/{artifact_id}/approve", dependencies=[Depends(require_scope("write"))])
async def approve_artifact(request: Request, artifact_id: str) -> dict[str, Any]:
    lifecycle = _get_lifecycle(request)
    body = await request.json()
    user = body.get("user", "system")
    reason = body.get("reason")
    try:
        await lifecycle.approve(artifact_id, user, reason)
    except LifecycleError as e:
        msg = str(e)
        if "Invalid transition" in msg:
            raise HTTPException(status_code=409, detail=msg) from e
        raise HTTPException(status_code=400, detail=msg) from e
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Artifact not found: {artifact_id}") from None
    return {"id": artifact_id, "status": "approved"}


@router.post("/{artifact_id}/reject", dependencies=[Depends(require_scope("write"))])
async def reject_artifact(request: Request, artifact_id: str) -> dict[str, Any]:
    lifecycle = _get_lifecycle(request)
    body = await request.json()
    user = body.get("user", "system")
    reason = body.get("reason")
    try:
        await lifecycle.reject(artifact_id, user, reason)
    except LifecycleError as e:
        msg = str(e)
        if "Invalid transition" in msg:
            raise HTTPException(status_code=409, detail=msg) from e
        raise HTTPException(status_code=400, detail=msg) from e
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Artifact not found: {artifact_id}") from None
    return {"id": artifact_id, "status": "rejected"}


@router.post("/{artifact_id}/apply", dependencies=[Depends(require_scope("write"))])
async def apply_artifact(
    request: Request,
    artifact_id: str,
    sync: bool = Query(False),
) -> Any:
    from ..tasks.runner import TaskRunner

    task_runner: TaskRunner = request.app.state.task_runner
    body = await request.json()
    approved_by = body.get("approved_by", "system")

    try:
        task_info = await task_runner.start_apply(artifact_id, approved_by)
    except LifecycleError as e:
        msg = str(e)
        if "Invalid transition" in msg:
            raise HTTPException(status_code=409, detail=msg) from e
        raise HTTPException(status_code=400, detail=msg) from e
    except ValueError as e:
        msg = str(e)
        if "not found" in msg.lower():
            raise HTTPException(status_code=404, detail=msg) from e
        raise HTTPException(status_code=400, detail=msg) from e

    if sync:
        try:
            task_result = await task_runner.await_task(task_info["task_id"])
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        if task_result["status"] == "succeeded":
            store = _get_store(request)
            fm, body_text = store.read(artifact_id)
            task_repo = request.app.state.task_repo
            active_task = await task_repo.get_active_task(artifact_id)
            return {"frontmatter": fm, "body": body_text, "active_task": active_task}
        return task_result

    return JSONResponse(status_code=202, content=task_info)


@router.delete("/{artifact_id}", dependencies=[Depends(require_scope("write"))])
async def revoke_artifact(
    request: Request,
    artifact_id: str,
    sync: bool = Query(False),
) -> Any:
    from ..tasks.runner import TaskRunner

    task_runner: TaskRunner = request.app.state.task_runner
    raw = await request.body()
    body = json.loads(raw) if raw else {}
    user = body.get("user", "system")
    reason = body.get("reason")

    try:
        task_info = await task_runner.start_revoke(artifact_id, user, reason)
    except LifecycleError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except ValueError as e:
        msg = str(e)
        if "apply_in_progress" in msg:
            active = await request.app.state.task_repo.get_active_task(artifact_id)
            raise HTTPException(
                status_code=409,
                detail={"reason": "apply_in_progress", "task_id": active["id"] if active else None},
            ) from e
        if "not found" in msg.lower():
            raise HTTPException(status_code=404, detail=msg) from e
        raise HTTPException(status_code=400, detail=msg) from e

    if sync:
        try:
            task_result = await task_runner.await_task(task_info["task_id"])
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        if task_result["status"] == "succeeded":
            store = _get_store(request)
            fm, body_text = store.read(artifact_id)
            return {"frontmatter": fm, "body": body_text}
        return task_result

    return JSONResponse(status_code=202, content=task_info)


_SSE_MAX_CONN_PER_IP = 3
_SSE_CONN_TIMEOUT_SEC = 3600
_sse_conn_count: dict[str, int] = {}
_sse_conn_lock = asyncio.Lock()


@router.get("/events/stream", dependencies=[Depends(require_scope("read"))])
async def sse_stream(request: Request) -> EventSourceResponse:
    client_ip = request.client.host if request.client else "unknown"
    async with _sse_conn_lock:
        if _sse_conn_count.get(client_ip, 0) >= _SSE_MAX_CONN_PER_IP:
            raise HTTPException(status_code=429, detail="SSE connection limit reached")

    q = sse_bus.subscribe()
    if q is None:
        raise HTTPException(status_code=503, detail="SSE subscriber limit reached")

    async with _sse_conn_lock:
        _sse_conn_count[client_ip] = _sse_conn_count.get(client_ip, 0) + 1

    async def event_generator() -> AsyncIterator[dict[str, str]]:
        start = time.time()
        try:
            while True:
                if await request.is_disconnected():
                    break
                if (time.time() - start) > _SSE_CONN_TIMEOUT_SEC:
                    yield {"event": "ping", "data": json.dumps({"reason": "timeout"})}
                    break
                try:
                    event = await asyncio.wait_for(q.get(), timeout=15)
                    yield event.to_sse()
                except TimeoutError:
                    yield {"event": "ping", "data": ""}
        finally:
            sse_bus.unsubscribe(q)
            async with _sse_conn_lock:
                _sse_conn_count[client_ip] = max(0, _sse_conn_count.get(client_ip, 1) - 1)
                if _sse_conn_count.get(client_ip, 0) <= 0:
                    _sse_conn_count.pop(client_ip, None)

    return EventSourceResponse(event_generator())
