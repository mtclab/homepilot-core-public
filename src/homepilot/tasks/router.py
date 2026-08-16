from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ..auth.deps import require_scope

router = APIRouter()


def _get_task_repo(request: Request) -> Any:
    return request.app.state.task_repo


def _get_task_runner(request: Request) -> Any:
    return request.app.state.task_runner


@router.get("/{task_id}", dependencies=[Depends(require_scope("read"))])
async def get_task(request: Request, task_id: str) -> Any:
    repo = _get_task_repo(request)
    task = await repo.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"Task not found: {task_id}")
    return task


@router.get("", dependencies=[Depends(require_scope("read"))])
async def list_tasks(
    request: Request,
    artifact_id: str | None = Query(None),
    limit: int = Query(50, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> Any:
    repo = _get_task_repo(request)
    items = await repo.list_tasks(artifact_id, limit=limit, offset=offset)
    total = await repo.count_tasks(artifact_id)
    return {"items": items, "total": total}


@router.post("/{task_id}/cancel", dependencies=[Depends(require_scope("write"))])
async def cancel_task(request: Request, task_id: str) -> Any:
    # Cancels an in-flight apply/revoke and marks the record 'cancelled' so it
    # stops blocking future actions for the artifact. Cancelling an already
    # finished task is a no-op that returns its current status (not an error).
    runner = _get_task_runner(request)
    try:
        return await runner.cancel_task(task_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
