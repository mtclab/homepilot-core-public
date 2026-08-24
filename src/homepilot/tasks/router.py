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
    # Cancels an in-flight task and marks the record 'cancelled' so it stops
    # blocking future actions. Cancelling an already finished task is a no-op
    # that returns its current status (not an error).
    #
    # Which coroutine to reach depends on WHO is running it: apply/revoke/replay
    # live in the TaskRunner, but a provision runs inside ProvisionService and
    # the runner has never heard of it (#452). Routing a provision cancel to the
    # runner marked the row and left the clone running, which then overwrote the
    # row with 'succeeded' - a cancel that cancelled nothing.
    repo = _get_task_repo(request)
    task = await repo.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"Task not found: {task_id}")

    if task["action"] == "provision":
        service = getattr(request.app.state, "provision_service", None)
        if service is None:
            # No service in this process to hold the coroutine, so the row mark
            # is all we can do - and the record must say that plainly rather
            # than imply the guest was dealt with.
            result = await repo.cancel_task(task_id)
            if result is None:
                raise HTTPException(status_code=404, detail=f"Task not found: {task_id}")
            if task["status"] == "running" and result["status"] == "cancelled":
                result = await repo.record_cancel_outcome(
                    task_id, error="process restarted; in-flight PVE state unknown"
                )
            return result
        result = await service.cancel(task_id)
        if result is None:
            raise HTTPException(status_code=404, detail=f"Task not found: {task_id}")
        return result

    runner = _get_task_runner(request)
    try:
        return await runner.cancel_task(task_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
