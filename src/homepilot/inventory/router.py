from __future__ import annotations

import asyncio
import contextlib
import json as _json
import logging
import re
import sqlite3
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, field_validator

from ..adapters.agent import AgentAdapter
from ..auth.deps import require_scope
from .service import InventoryService

logger = logging.getLogger(__name__)

router = APIRouter()

# Cap on how long adoption-time introspection may block the adopt response. It is
# awaited so the caller gets an immediate summary, but a slow/hung host must
# never make adopt itself slow or fail — the whole run is best-effort.
_INTROSPECT_TIMEOUT = 12.0


def _resolve_agent_adapter(request: Request) -> AgentAdapter | None:
    """Build a read-only agent adapter from app state, or None if the agent hub
    is not enabled. Mirrors how the agent-hub endpoints construct their adapter."""
    hub = getattr(request.app.state, "agent_hub", None)
    if hub is None:
        return None
    pve_nodes: list[str] = []
    lifecycle = getattr(request.app.state, "artifact_lifecycle", None)
    if lifecycle is not None and hasattr(lifecycle, "_pve_nodes_list"):
        pve_nodes = lifecycle._pve_nodes_list or []
    return AgentAdapter(hub_server=hub, pve_nodes=pve_nodes)


async def _introspect_on_adopt(
    request: Request, svc: InventoryService, host: dict[str, Any]
) -> dict[str, Any] | None:
    """Best-effort adoption-time introspection. Never raises and never fails the
    adopt: any error/timeout/absent-agent is caught and logged, returning None."""
    try:
        adapter = _resolve_agent_adapter(request)
        return await asyncio.wait_for(
            svc.introspect_and_record(host, adapter),
            timeout=_INTROSPECT_TIMEOUT,
        )
    except Exception:
        logger.warning(
            "Adoption introspection failed for host %s (adopt still succeeds)",
            host.get("id"),
            exc_info=True,
        )
        return None


# Canonical import_state values, matching the DB CHECK constraint on
# hosts.import_state (migration 10: IN ('pending','adopted','ignored')).
# "discovered" is a hosts.source value, not an import_state, so it is not
# accepted here — allowing it would pass validation but fail the DB CHECK.
_VALID_IMPORT_STATES = frozenset({"pending", "adopted", "ignored"})


class HostPatchRequest(BaseModel):
    managed: bool | None = None
    tags: str | None = None
    role: str | None = None
    ip_address: str | None = None
    description: str | None = None
    import_state: str | None = None
    status: str | None = None

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if isinstance(v, str):
            return v
        return _json.dumps(v)

    @field_validator("import_state")
    @classmethod
    def validate_import_state(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if v not in _VALID_IMPORT_STATES:
            allowed = ", ".join(sorted(_VALID_IMPORT_STATES))
            raise ValueError(f"import_state must be one of: {allowed}")
        return v


# A hostname or a bare IPv4 address. A manually added host is written into
# inventory and later reached over SSH/the agent, so what an operator types has
# to be a name something could actually resolve - not a URL, not a shell string.
_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)([a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)"
    r"(\.[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)*$"
)


class HostCreateRequest(BaseModel):
    """A host that exists but Proxmox has never heard of (#445 A5).

    A homelab is not only Proxmox guests: the NAS, the router, the Raspberry Pi
    and the old tower in the cupboard were all unrepresentable, which meant they
    could not be documented, adopted, given an agent, or carry an artifact.
    """

    hostname: str
    ip_address: str | None = None
    role: str = "guest"
    host_type: str = "baremetal"
    description: str | None = None
    tags: str | None = None
    fqdn: str | None = None

    @field_validator("hostname")
    @classmethod
    def validate_hostname(cls, v: str) -> str:
        v = (v or "").strip()
        if not _HOSTNAME_RE.match(v):
            raise ValueError("hostname must be a DNS hostname or an IPv4 address")
        return v

    @field_validator("ip_address", "fqdn")
    @classmethod
    def validate_optional_address(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        v = v.strip()
        if not _HOSTNAME_RE.match(v):
            raise ValueError("must be a DNS hostname or an IPv4 address")
        return v

    @field_validator("tags")
    @classmethod
    def normalise_tags(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return v if isinstance(v, str) else _json.dumps(v)


def _get_service(request: Request) -> InventoryService:
    svc: InventoryService = request.app.state.inventory_service
    return svc


class BulkRequest(BaseModel):
    action: str
    host_ids: list[str]


@router.get("", dependencies=[Depends(require_scope("read"))])
async def list_inventory(
    request: Request,
    role: str | None = Query(None),
    status: str | None = Query(None),
    managed: bool | None = Query(None),
    source: str | None = Query(None),
    import_state: str | None = Query(None),
    pve_status: str | None = Query(None),
    q: str | None = Query(None, description="Free text over hostname, address, role, tags"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    repo = request.app.state.repo
    filters = {
        "q": q,
        "managed": managed,
        "role": role,
        "source": source,
        "import_state": import_state,
        "pve_status": pve_status,
        "status": status,
    }
    hosts = await repo.list_hosts(limit=limit, offset=offset, **filters)
    # A real COUNT, not the page size (#428). `len(hosts)` capped the UI at 100
    # with no way to reach page 2 and told the operator their estate was smaller
    # than it is.
    total = await repo.count_hosts(**filters)
    return {"items": hosts, "total": total}


@router.post("", status_code=201, dependencies=[Depends(require_scope("write"))])
async def create_manual_host(request: Request, body: HostCreateRequest) -> dict[str, Any]:
    """Add a host HomePilot could not otherwise know about (#445 A5).

    Inventory could only ever be filled by a Proxmox sync, so a homelab that is
    not entirely Proxmox guests - the NAS, the router, the Pi, the old tower -
    was literally unrepresentable, and everything downstream (docs, adoption,
    agent install, artifacts targeting a host) was closed to those machines.

    Recorded as `source="manual"` and adopted on the spot: a machine an operator
    typed in by hand is not a discovery awaiting triage. That source is also what
    keeps a Proxmox sync from ever declaring it absent - the hypervisor never
    looked for it.
    """
    repo = request.app.state.repo
    existing = await repo.get_host_by_hostname(body.hostname)
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"A host named {body.hostname} is already in inventory "
                f"(id {existing['id']}, source {existing.get('source')})"
            ),
        )
    host_id = await repo.create_host(
        hostname=body.hostname,
        host_type=body.host_type,
        role=body.role,
        ip_address=body.ip_address,
        fqdn=body.fqdn,
        description=body.description,
        tags=body.tags,
        source="manual",
        import_state="adopted",
        managed=True,
        managed_by="user",
        # Typed by a person, so neither value is a guess the enricher may
        # overwrite (#416: an enrich pass demotes anything marked "inferred").
        role_source="user",
        ip_source="user" if body.ip_address else None,
        status="unknown",
    )
    host = await repo.get_host(host_id)
    await repo.log_audit(
        user_id="ui",
        source="ui",
        action="host_added",
        target_host=body.hostname,
    )
    return dict(host) if host else {"id": host_id}


@router.delete("/{host_id}", dependencies=[Depends(require_scope("write"))])
async def forget_host(request: Request, host_id: str) -> dict[str, Any]:
    """Remove a host from inventory, with its services and observation note.

    Refused for a host the hypervisor still reports: deleting it would be undone
    by the next sync, which is worse than refusing - the operator would believe
    it was gone. Destroy the guest in Proxmox, or Ignore it, instead.
    """
    repo = request.app.state.repo
    host = await repo.get_host(host_id)
    if host is None:
        raise HTTPException(status_code=404, detail=f"Host not found: {host_id}")
    source = host.get("source")
    if source != "manual" and not host.get("absent_since"):
        raise HTTPException(
            status_code=409,
            detail=(
                f"{host.get('hostname')} is still reported by the hypervisor, so the next "
                "sync would bring it straight back. Destroy the guest in Proxmox, or set "
                "its import state to 'ignored' to keep it out of the way."
            ),
        )
    await repo.delete_host(host_id)
    await repo.log_audit(
        user_id="ui",
        source="ui",
        action="host_forgotten",
        target_host=str(host.get("hostname") or ""),
    )
    return {"id": host_id, "forgotten": True}


@router.get("/{host_id}", dependencies=[Depends(require_scope("read"))])
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


@router.get("/{host_id}/doc", dependencies=[Depends(require_scope("read"))])
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
    if body.import_state is not None:
        updates["import_state"] = body.import_state
    if body.status is not None:
        updates["status"] = body.status
    if not updates:
        raise HTTPException(status_code=400, detail="No valid fields to update")
    await repo.update_host(host_id, **updates)
    # A PATCH is an operator deciding, so the fields it wrote are PINNED: the
    # next sync or enrich pass leaves them alone (#424). Without this, editing a
    # description or a status in the UI lasted until the next reconciler cycle.
    await repo.pin_host_fields(host_id, set(updates))
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
    result = dict(updated)
    summary = await _introspect_on_adopt(request, _get_service(request), result)
    if summary is not None:
        result["introspection"] = summary
    return result


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
                # Best-effort observed-state capture; never affects adopt success.
                await _introspect_on_adopt(request, svc, dict(host))
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
