from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

router = APIRouter()


def _get_task_repo(request: Request) -> Any:
    return request.app.state.task_repo


@router.get("/{task_id}")
async def get_task(request: Request, task_id: str) -> Any:
    repo = _get_task_repo(request)
    task = await repo.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"Task not found: {task_id}")
    return task


@router.get("")
async def list_tasks(
    request: Request,
    artifact_id: str = Query(...),
    limit: int = Query(50, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> Any:
    repo = _get_task_repo(request)
    items = await repo.list_tasks(artifact_id, limit=limit, offset=offset)
    total = await repo.count_tasks(artifact_id)
    return {"items": items, "total": total}
