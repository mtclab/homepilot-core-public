from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request

router = APIRouter()


@router.get("")
async def list_audit(
    request: Request,
    action: str | None = Query(None),
    artifact_id: str | None = Query(None),
    target_host: str | None = Query(None),
    source: str | None = Query(None),
    limit: int = Query(100, ge=1, le=10000),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    repo = request.app.state.repo
    rows = await repo.query_audit_log(
        action=action,
        artifact_id=artifact_id,
        target_host=target_host,
        source=source,
        limit=limit,
        offset=offset,
    )
    total = await repo.count_audit_log(
        action=action,
        artifact_id=artifact_id,
        target_host=target_host,
        source=source,
    )
    return {"items": rows, "total": total}
