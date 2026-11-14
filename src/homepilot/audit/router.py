from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request

from ..auth.deps import require_scope

router = APIRouter()


@router.get("", dependencies=[Depends(require_scope("read"))])
async def list_audit(
    request: Request,
    action: str | None = Query(None),
    artifact_id: str | None = Query(None),
    target_host: str | None = Query(None),
    source: str | None = Query(None),
    q: str | None = Query(None, description="Free text over artifact, host, command, actor"),
    actor: Literal["people", "system"] | None = Query(
        None,
        description=(
            "`system`: reconcilers, migrations and rows with no actor; "
            "`people`: everything else (people and assistants)"
        ),
    ),
    since: str | None = Query(None, description="Only entries at or after this ISO time"),
    limit: int = Query(100, ge=1, le=10000),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    repo = request.app.state.repo
    rows = await repo.query_audit_log(
        action=action,
        artifact_id=artifact_id,
        target_host=target_host,
        source=source,
        q=q,
        actor=actor,
        since=since,
        limit=limit,
        offset=offset,
    )
    # Same filters, same helper: `total` is what the pager shows, and a count
    # that ignores the search would report "50 of 4000" for a 50-row search.
    total = await repo.count_audit_log(
        action=action,
        artifact_id=artifact_id,
        target_host=target_host,
        source=source,
        q=q,
        actor=actor,
        since=since,
    )
    return {"items": rows, "total": total}
