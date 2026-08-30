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
from ..agent_hub.version_skew import control_plane_version as _control_plane_version
from ..agent_hub.version_skew import is_behind as _is_behind
from ..auth.deps import require_scope
from .service import InventoryService

logger = logging.getLogger(__name__)

router = APIRouter()


class InventoryError(Exception):
    """A domain error raised by the shared inventory callables.

    Carries the HTTP-ish ``status`` the route should surface (404 not found, 409
    conflict, 400 bad request) so one shared function can serve both the HTTP
    route (which maps it to an HTTPException of the same status) and the MCP tool
    (which maps every one to a ValueError the client sees)."""

    def __init__(self, status: int, detail: str) -> None:
        self.status = status
        self.detail = detail
        super().__init__(detail)


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


async def _introspect_with_adapter(
    svc: InventoryService, host: dict[str, Any], adapter: AgentAdapter | None
) -> dict[str, Any] | None:
    """Best-effort adoption-time introspection. Never raises and never fails the
    adopt: any error/timeout/absent-agent is caught and logged, returning None.

    Request-free so both the HTTP route and the MCP adopt tool share it — the
    caller supplies the adapter (the route resolves one from app state, the MCP
    tool passes the one its context already holds)."""
    try:
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


async def _introspect_on_adopt(
    request: Request, svc: InventoryService, host: dict[str, Any]
) -> dict[str, Any] | None:
    return await _introspect_with_adapter(svc, host, _resolve_agent_adapter(request))


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
    # The agent is a property of the host (#514 S1): a fleet list that cannot
    # say "this machine has a live channel" forces a second tab to answer it.
    agent_ids = [h["agent_id"] for h in hosts if h.get("agent_id")]
    if agent_ids:
        marks = ",".join("?" * len(agent_ids))
        agents = await repo.db.fetchall(
            # nosec B608 - only `?` placeholders are interpolated (one per id);
            # every VALUE goes through bound parameters. Bandit sees an f-string
            # near SQL and cannot tell placeholders from data.
            f"SELECT agent_id, connected, system_info FROM agents WHERE agent_id IN ({marks})",  # nosec B608
            agent_ids,
        )
        by_id = {a["agent_id"]: a for a in agents}
        for h in hosts:
            a = by_id.get(h.get("agent_id") or "")
            if a is not None:
                h["agent_connected"] = bool(a["connected"])
                try:
                    h["agent_version"] = _json.loads(a["system_info"] or "{}").get("agent_version")
                except (ValueError, TypeError):
                    h["agent_version"] = None
                # An agent older than the control plane is not running the code
                # that shipped. `None` = could not be told, never "fine".
                h["agent_behind"] = _is_behind(h["agent_version"], _control_plane_version())
    return {"items": hosts, "total": total}


async def create_manual_host_record(repo: Any, body: HostCreateRequest) -> dict[str, Any]:
    """Add a host HomePilot could not otherwise know about (#445 A5).

    Inventory could only ever be filled by a Proxmox sync, so a homelab that is
    not entirely Proxmox guests - the NAS, the router, the Pi, the old tower -
    was literally unrepresentable, and everything downstream (docs, adoption,
    agent install, artifacts targeting a host) was closed to those machines.

    Recorded as `source="manual"` and adopted on the spot: a machine an operator
    typed in by hand is not a discovery awaiting triage. That source is also what
    keeps a Proxmox sync from ever declaring it absent - the hypervisor never
    looked for it.

    Shared by the HTTP route and the MCP ``add_host`` tool. Raises
    ``InventoryError(409)`` on a duplicate hostname.
    """
    existing = await repo.get_host_by_hostname(body.hostname)
    if existing is not None:
        raise InventoryError(
            409,
            f"A host named {body.hostname} is already in inventory "
            f"(id {existing['id']}, source {existing.get('source')})",
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


async def forget_host_record(repo: Any, host_id: str) -> dict[str, Any]:
    """Remove a host from inventory, with its services and observation note.

    Refused for a host the hypervisor still reports: deleting it would be undone
    by the next sync, which is worse than refusing - the operator would believe
    it was gone. Destroy the guest in Proxmox, or Ignore it, instead.

    Shared by the HTTP route and the MCP ``delete_host`` tool. Raises
    ``InventoryError(404)`` when unknown and ``InventoryError(409)`` when the
    hypervisor still reports it.
    """
    host = await repo.get_host(host_id)
    if host is None:
        raise InventoryError(404, f"Host not found: {host_id}")
    source = host.get("source")
    if source != "manual" and not host.get("absent_since"):
        raise InventoryError(
            409,
            f"{host.get('hostname')} is still reported by the hypervisor, so the next "
            "sync would bring it straight back. Destroy the guest in Proxmox, or set "
            "its import state to 'ignored' to keep it out of the way.",
        )
    await repo.delete_host(host_id)
    await repo.log_audit(
        user_id="ui",
        source="ui",
        action="host_forgotten",
        target_host=str(host.get("hostname") or ""),
    )
    return {"id": host_id, "forgotten": True}


@router.post("", status_code=201, dependencies=[Depends(require_scope("write"))])
async def create_manual_host(request: Request, body: HostCreateRequest) -> dict[str, Any]:
    try:
        return await create_manual_host_record(request.app.state.repo, body)
    except InventoryError as e:
        raise HTTPException(status_code=e.status, detail=e.detail) from e


@router.delete("/{host_id}", dependencies=[Depends(require_scope("write"))])
async def forget_host(request: Request, host_id: str) -> dict[str, Any]:
    try:
        return await forget_host_record(request.app.state.repo, host_id)
    except InventoryError as e:
        raise HTTPException(status_code=e.status, detail=e.detail) from e


@router.get("/{host_id}", dependencies=[Depends(require_scope("read"))])
async def get_host(request: Request, host_id: str) -> dict[str, Any]:
    host = await _get_service(request).get_host_detail(host_id)
    if host is None:
        raise HTTPException(status_code=404, detail=f"Host not found: {host_id}")
    return host


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


async def update_host_record(repo: Any, host_id: str, body: HostPatchRequest) -> dict[str, Any]:
    """Apply an operator's edits to one host and PIN the fields they set.

    Shared by the HTTP route and the MCP ``update_host`` tool. Raises
    ``InventoryError(404)`` when unknown and ``InventoryError(400)`` when the
    patch carries no recognised field.
    """
    host = await repo.get_host(host_id)
    if host is None:
        raise InventoryError(404, f"Host not found: {host_id}")
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
        raise InventoryError(400, "No valid fields to update")
    await repo.update_host(host_id, **updates)
    # A PATCH is an operator deciding, so the fields it wrote are PINNED: the
    # next sync or enrich pass leaves them alone (#424). Without this, editing a
    # description or a status in the UI lasted until the next reconciler cycle.
    await repo.pin_host_fields(host_id, set(updates))
    updated = await repo.get_host(host_id)
    return dict(updated)


async def adopt_host_record(
    host_id: str, *, repo: Any, svc: InventoryService, adapter: AgentAdapter | None
) -> dict[str, Any]:
    """Adopt a discovered host and best-effort introspect it on the spot.

    Shared by the HTTP route and the MCP ``adopt_host`` tool. Raises
    ``InventoryError(404)`` when unknown.
    """
    host = await repo.get_host(host_id)
    if host is None:
        raise InventoryError(404, f"Host not found: {host_id}")
    await repo.update_host(host_id, managed=1, source="imported", import_state="adopted")
    updated = await repo.get_host(host_id)
    result = dict(updated)
    summary = await _introspect_with_adapter(svc, result, adapter)
    if summary is not None:
        result["introspection"] = summary
    return result


async def ignore_host_record(repo: Any, host_id: str) -> dict[str, Any]:
    """Mark a host ignored so it stays out of the way. Shared by the HTTP route
    and the MCP ``ignore_host`` tool. Raises ``InventoryError(404)`` when unknown.
    """
    host = await repo.get_host(host_id)
    if host is None:
        raise InventoryError(404, f"Host not found: {host_id}")
    await repo.update_host(host_id, import_state="ignored")
    updated = await repo.get_host(host_id)
    return dict(updated)


@router.patch("/{host_id}", dependencies=[Depends(require_scope("write"))])
async def update_host(request: Request, host_id: str, body: HostPatchRequest) -> dict[str, Any]:
    try:
        return await update_host_record(request.app.state.repo, host_id, body)
    except InventoryError as e:
        raise HTTPException(status_code=e.status, detail=e.detail) from e


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


async def bulk_host_action_record(
    body: BulkRequest, *, repo: Any, svc: InventoryService, adapter: AgentAdapter | None
) -> dict[str, Any]:
    """Apply one action (adopt / ignore / enrich) to many hosts by id.

    Shared by the HTTP route and the MCP ``bulk_host_action`` tool. Best-effort
    per host: an unknown id or a per-host failure counts as ``failed`` and the
    rest still run.
    """
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
                await _introspect_with_adapter(svc, dict(host), adapter)
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


@router.post("/{host_id}/adopt", dependencies=[Depends(require_scope("write"))])
async def adopt_host(request: Request, host_id: str) -> dict[str, Any]:
    try:
        return await adopt_host_record(
            host_id,
            repo=request.app.state.repo,
            svc=_get_service(request),
            adapter=_resolve_agent_adapter(request),
        )
    except InventoryError as e:
        raise HTTPException(status_code=e.status, detail=e.detail) from e


@router.post("/{host_id}/ignore", dependencies=[Depends(require_scope("write"))])
async def ignore_host(request: Request, host_id: str) -> dict[str, Any]:
    try:
        return await ignore_host_record(request.app.state.repo, host_id)
    except InventoryError as e:
        raise HTTPException(status_code=e.status, detail=e.detail) from e


@router.post("/bulk", dependencies=[Depends(require_scope("write"))])
async def bulk_inventory(request: Request, body: BulkRequest) -> dict[str, Any]:
    return await bulk_host_action_record(
        body,
        repo=request.app.state.repo,
        svc=_get_service(request),
        adapter=_resolve_agent_adapter(request),
    )
