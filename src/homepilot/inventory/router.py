from __future__ import annotations

import contextlib
import json as _json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, field_validator

from ..auth.deps import require_scope
from .service import InventoryService

router = APIRouter()


class HostPatchRequest(BaseModel):
    managed: bool | None = None
    tags: str | None = None

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if isinstance(v, str):
            return v
        return _json.dumps(v)


def _get_service(request: Request) -> InventoryService:
    svc: InventoryService = request.app.state.inventory_service
    return svc


@router.get("")
async def list_inventory(
    request: Request,
    role: str | None = Query(None),
    status: str | None = Query(None),
    managed: bool | None = Query(None),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    repo = request.app.state.repo
    hosts = await repo.list_hosts(managed=managed, role=role, limit=limit, offset=offset)
    return {"items": hosts, "total": len(hosts)}


@router.get("/{host_id}")
async def get_host(request: Request, host_id: str) -> dict[str, Any]:
    repo = request.app.state.repo
    host = await repo.get_host(host_id)
    if host is None:
        raise HTTPException(status_code=404, detail=f"Host not found: {host_id}")
    host_dict = dict(host)
    services = await repo.list_services(host_id=host_id)
    host_dict["services"] = [dict(s) for s in services]
    return host_dict


@router.post("/refresh", dependencies=[Depends(require_scope("write"))])
async def refresh_inventory(request: Request) -> Any:
    svc = _get_service(request)
    body = {}
    with contextlib.suppress(Exception):
        body = await request.json()
    scope = body.get("scope")
    result = await svc.refresh_inventory(scope=scope)
    return result


@router.get("/{host_id}/doc")
async def get_host_doc(request: Request, host_id: str) -> dict[str, Any]:
    repo = request.app.state.repo
    host = await repo.get_host(host_id)
    if host is None:
        raise HTTPException(status_code=404, detail=f"Host not found: {host_id}")
    svc = _get_service(request)
    hostname = dict(host).get("hostname", host_id)
    doc = await svc.get_environment_doc(hostname)
    return doc


@router.patch("/{host_id}", dependencies=[Depends(require_scope("write"))])
async def update_host(request: Request, host_id: str, body: HostPatchRequest) -> dict[str, Any]:
    repo = request.app.state.repo
    host = await repo.get_host(host_id)
    if host is None:
        raise HTTPException(status_code=404, detail=f"Host not found: {host_id}")
    updates: dict[str, Any] = {}
    if body.managed is not None:
        updates["managed"] = int(body.managed)
    if body.tags is not None:
        updates["tags"] = body.tags
    if not updates:
        raise HTTPException(status_code=400, detail="No valid fields to update")
    await repo.update_host(host_id, **updates)
    updated = await repo.get_host(host_id)
    return dict(updated)
