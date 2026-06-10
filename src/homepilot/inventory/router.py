from __future__ import annotations

import contextlib
import json as _json
import sqlite3
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, field_validator

from ..auth.deps import require_scope
from .service import InventoryService

router = APIRouter()


class HostPatchRequest(BaseModel):
    managed: bool | None = None
    tags: str | None = None
    role: str | None = None
    ip_address: str | None = None
    description: str | None = None

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


class BulkRequest(BaseModel):
    action: str
    host_ids: list[str]


@router.get("")
async def list_inventory(
    request: Request,
    role: str | None = Query(None),
    status: str | None = Query(None),
    managed: bool | None = Query(None),
    source: str | None = Query(None),
    import_state: str | None = Query(None),
    pve_status: str | None = Query(None),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    repo = request.app.state.repo
    hosts = await repo.list_hosts(
        managed=managed,
        role=role,
        source=source,
        import_state=import_state,
        pve_status=pve_status,
        limit=limit,
        offset=offset,
    )
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
    if body.role is not None:
        updates["role"] = body.role
        updates["role_source"] = "user"
    if body.ip_address is not None:
        updates["ip_address"] = body.ip_address
        updates["ip_source"] = "user"
    if body.description is not None:
        updates["description"] = body.description
    if not updates:
        raise HTTPException(status_code=400, detail="No valid fields to update")
    await repo.update_host(host_id, **updates)
    updated = await repo.get_host(host_id)
    return dict(updated)


@router.post("/enrich", dependencies=[Depends(require_scope("write"))])
async def enrich_inventory(request: Request) -> dict[str, Any]:
    svc = _get_service(request)
    body: dict[str, Any] = {}
    with contextlib.suppress(Exception):
        body = await request.json()
    result = await svc.enrich_inventory(
        host_ids=body.get("host_ids"),
        scope=body.get("scope"),
    )
    return result


@router.post("/{host_id}/adopt", dependencies=[Depends(require_scope("write"))])
async def adopt_host(request: Request, host_id: str) -> dict[str, Any]:
    repo = request.app.state.repo
    host = await repo.get_host(host_id)
    if host is None:
        raise HTTPException(status_code=404, detail=f"Host not found: {host_id}")
    updates: dict[str, Any] = {
        "managed": 1,
        "source": "imported",
        "import_state": "adopted",
    }
    await repo.update_host(host_id, **updates)
    updated = await repo.get_host(host_id)
    return dict(updated)


@router.post("/{host_id}/ignore", dependencies=[Depends(require_scope("write"))])
async def ignore_host(request: Request, host_id: str) -> dict[str, Any]:
    repo = request.app.state.repo
    host = await repo.get_host(host_id)
    if host is None:
        raise HTTPException(status_code=404, detail=f"Host not found: {host_id}")
    await repo.update_host(host_id, import_state="ignored")
    updated = await repo.get_host(host_id)
    return dict(updated)


@router.post("/bulk", dependencies=[Depends(require_scope("write"))])
async def bulk_inventory(request: Request, body: BulkRequest) -> dict[str, Any]:
    repo = request.app.state.repo
    svc = _get_service(request)
    succeeded = 0
    failed = 0
    for host_id in body.host_ids:
        try:
            host = await repo.get_host(host_id)
            if host is None:
                failed += 1
                continue
            if body.action == "adopt":
                await repo.update_host(
                    host_id,
                    managed=1,
                    source="imported",
                    import_state="adopted",
                )
            elif body.action == "ignore":
                await repo.update_host(host_id, import_state="ignored")
            elif body.action == "enrich":
                await svc._enrich_single_host(dict(host))
            else:
                failed += 1
                continue
            succeeded += 1
        except (KeyError, ValueError, httpx.HTTPError, sqlite3.Error, OSError):
            failed += 1
    return {"succeeded": succeeded, "failed": failed}
