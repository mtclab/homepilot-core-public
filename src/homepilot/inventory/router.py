from __future__ import annotations

import asyncio
import contextlib
import json as _json
import logging
import re
import sqlite3
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, field_validator

from ..adapters.agent import AgentAdapter
from ..agent_hub.version_skew import control_plane_version as _control_plane_version
from ..agent_hub.version_skew import is_behind as _is_behind
from ..auth.deps import require_scope, require_token
from ..auth.principal import audit_actor
from .addresses import find_conflicts
from .fleet_model import ENV_SOURCE_USER, ENVS, imported_env, parse_pve_tags, proxmox_ui_url
from .self_guest import (
    DISMISSED_KEY,
    colocated_agents,
    control_plane_addresses,
    default_gateway,
    running_in_container,
    self_guest_status,
)
from .service import InventoryService

logger = logging.getLogger(__name__)

router = APIRouter()

# The authenticated token, for attribution (#702). FastAPI resolves
# require_token once per request, so this is the same check the scope made.
_principal = Depends(require_token)


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
    # Which part of the estate (lane B1). A value pins it against later syncs;
    # an EMPTY string withdraws the operator's value and hands the field back
    # to what Proxmox says (its `hp-env-*` tag, or `template`).
    env: str | None = None

    @field_validator("env")
    @classmethod
    def validate_env(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip().lower()
        if v and v not in ENVS:
            raise ValueError(f"env must be one of: {', '.join(ENVS)} (or empty to clear)")
        return v

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
    # A closed set: an unknown action used to be accepted and answered as a
    # per-host "failed" count, so `action="nuke"` looked like a partial run of
    # something rather than a request nobody can carry out (#696).
    action: Literal["adopt", "ignore", "enrich"]
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
    env: str | None = Query(None, description="Only hosts in this environment"),
    templates: Literal["include", "exclude", "only"] = Query(
        "include",
        description=(
            "Proxmox templates are provisioning inputs, not fleet members: "
            "`exclude` leaves them out (the fleet list does), `only` lists just them"
        ),
    ),
    q: str | None = Query(None, description="Free text over hostname, address, role, tags"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    repo = request.app.state.repo
    filters: dict[str, Any] = {
        "q": q,
        "managed": managed,
        "role": role,
        "source": source,
        "import_state": import_state,
        "pve_status": pve_status,
        "status": status,
        "env": env or None,
        "is_template": {"include": None, "exclude": False, "only": True}[templates],
    }
    hosts = await repo.list_hosts(limit=limit, offset=offset, **filters)
    # A real COUNT, not the page size (#428). `len(hosts)` capped the UI at 100
    # with no way to reach page 2 and told the operator their estate was smaller
    # than it is.
    total = await repo.count_hosts(**filters)
    # How many the filter left out for being templates, so a hidden population
    # is a number with a "show" next to it, never silently missing.
    templates_hidden = (
        await repo.count_hosts(**{**filters, "is_template": True}) if templates == "exclude" else 0
    )
    proxmox = getattr(getattr(request.app.state, "inventory_service", None), "proxmox", None)
    for h in hosts:
        h["proxmox_url"] = proxmox_ui_url(proxmox, h)
    # The agent is a property of the host (#514 S1): a fleet list that cannot
    # say "this machine has a live channel" forces a second tab to answer it.
    agent_ids = [h["agent_id"] for h in hosts if h.get("agent_id")]
    if agent_ids:
        marks = ",".join("?" * len(agent_ids))
        agents = await repo.db.fetchall(
            # nosec B608 - only `?` placeholders are interpolated (one per id);
            # every VALUE goes through bound parameters. Bandit sees an f-string
            # near SQL and cannot tell placeholders from data.
            "SELECT agent_id, connected, system_info, disconnected_at, last_heartbeat "
            f"FROM agents WHERE agent_id IN ({marks})",  # nosec B608
            agent_ids,
        )
        by_id = {a["agent_id"]: a for a in agents}
        # Connected means dialled in to THIS hub now - the live registry, the
        # same source /dashboard/summary and /agents/ answer from (#469). The
        # `connected` column stays 1 for an agent that vanished without a clean
        # goodbye (a killed process, a backend restart), so believing it here
        # let the Overview say "no agent is connected" above per-host lines all
        # reading connected. The column is only the fallback when no hub runs.
        from ..app_state import get_agent_registry

        # The app's own registry first (the one its hub registers into), the
        # process singleton otherwise - the same pair GET /agents/ consults.
        registry = getattr(request.app.state, "agent_registry", None) or get_agent_registry()
        live_ids = (
            {a["agent_id"] for a in registry.list_connected()} if registry is not None else None
        )
        for h in hosts:
            a = by_id.get(h.get("agent_id") or "")
            if a is not None:
                connected = (
                    a["agent_id"] in live_ids if live_ids is not None else bool(a["connected"])
                )
                h["agent_connected"] = connected
                # Since when the channel has been dark: the clean goodbye if
                # there was one, else the last heartbeat heard. A 30 s blip and
                # a week-long outage must not read as the same line.
                h["agent_offline_since"] = (
                    None if connected else (a["disconnected_at"] or a["last_heartbeat"])
                )
                try:
                    h["agent_version"] = _json.loads(a["system_info"] or "{}").get("agent_version")
                except (ValueError, TypeError):
                    h["agent_version"] = None
                # An agent older than the control plane is not running the code
                # that shipped. `None` = could not be told, never "fine".
                h["agent_behind"] = _is_behind(h["agent_version"], _control_plane_version())
    # Every address a host holds, with its provenance, and whether any of them
    # clashes (lane B2). One read of the address table for the whole page: the
    # clash scan needs every host's rows anyway, not just this page's.
    address_rows = await repo.list_address_rows()
    clashing = {(hid, c.address) for c in find_conflicts(address_rows) for hid in c.host_ids}
    by_host: dict[str, list[dict[str, Any]]] = {}
    for r in address_rows:
        by_host.setdefault(str(r["host_id"]), []).append(r)
    for h in hosts:
        h["addresses"] = _host_address_list(by_host.get(str(h["id"]), []), clashing, h["id"])
    return {"items": hosts, "total": total, "templates_hidden": templates_hidden}


# ── The self-guest (lane B1, review 02 step 3) ───────────────────────────────
# Declared BEFORE the `/{host_id}` routes: those would otherwise read
# "self-guest" as a host id.


class SelfGuestRequest(BaseModel):
    host_id: str


async def _self_guest_answer(request: Request) -> dict[str, Any]:
    from ..app_state import get_agent_registry
    from ..config import get_settings

    cp = await control_plane_addresses(
        get_settings(), request.url.hostname, request.headers.get("x-forwarded-host")
    )
    # A default install runs in a container with no advertise address, so no
    # address above is one a guest holds; the agent dialling in from the
    # container's gateway is what names the machine (3.8.0-rc1 dev walk).
    registry = getattr(request.app.state, "agent_registry", None) or get_agent_registry()
    connected = registry.list_connected() if registry is not None else []
    colocated = colocated_agents(connected, default_gateway(), running_in_container())
    return await self_guest_status(request.app.state.repo, cp, colocated)


@router.get("/self-guest", dependencies=[Depends(require_scope("read"))])
async def get_self_guest(request: Request) -> dict[str, Any]:
    """Which host HomePilot runs on: the confirmed one, else HomePilot's proposal.

    Nothing is marked until an operator confirms: an address the control plane
    answers on can belong to a proxy or a NAT box in front of it, and a wrong
    marker would guard the wrong machine.
    """
    return await _self_guest_answer(request)


@router.put("/self-guest", dependencies=[Depends(require_scope("admin"))])
async def confirm_self_guest(
    request: Request, body: SelfGuestRequest, token: dict[str, Any] = _principal
) -> dict[str, Any]:
    """An operator says: this row is the machine HomePilot runs on.

    Admin scope: the mark is what a later stop/power action is guarded by, so
    moving or clearing it is not a routine inventory edit.
    """
    repo = request.app.state.repo
    host = await repo.get_host(body.host_id)
    if host is None:
        raise HTTPException(status_code=404, detail=f"Host not found: {body.host_id}")
    if host.get("is_template"):
        raise HTTPException(
            status_code=409, detail=f"{host.get('hostname')} is a Proxmox template; it cannot run"
        )
    await repo.set_self_host(body.host_id)
    await repo.log_audit(
        user_id=audit_actor(token),
        source="api",
        action="self_guest_confirmed",
        target_host=str(host.get("hostname") or ""),
    )
    return await _self_guest_answer(request)


@router.delete("/self-guest", dependencies=[Depends(require_scope("admin"))])
async def clear_self_guest(request: Request, token: dict[str, Any] = _principal) -> dict[str, Any]:
    """Withdraw the mark: HomePilot no longer claims to know which host it runs on."""
    repo = request.app.state.repo
    current = await repo.get_self_host()
    await repo.set_self_host(None)
    if current is not None:
        await repo.log_audit(
            user_id=audit_actor(token),
            source="api",
            action="self_guest_cleared",
            target_host=str(current.get("hostname") or ""),
        )
    return await _self_guest_answer(request)


@router.post("/self-guest/dismiss", dependencies=[Depends(require_scope("admin"))])
async def dismiss_self_guest(request: Request, body: SelfGuestRequest) -> dict[str, Any]:
    """ "Not this one": stop proposing this host. A different match is still proposed."""
    repo = request.app.state.repo
    if await repo.get_host(body.host_id) is None:
        raise HTTPException(status_code=404, detail=f"Host not found: {body.host_id}")
    await repo.set_setting(DISMISSED_KEY, body.host_id)
    return await _self_guest_answer(request)


def _host_address_list(
    rows: list[dict[str, Any]], clashing: set[tuple[str, str]], host_id: str
) -> list[dict[str, Any]]:
    """One entry per distinct address, configured provenance first."""
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        entry = out.setdefault(
            r["address"],
            {
                "address": r["address"],
                "sources": [],
                "conflict": (host_id, r["address"]) in clashing,
            },
        )
        if r["source"] not in entry["sources"]:
            entry["sources"].append(r["source"])
    return list(out.values())


async def create_manual_host_record(
    repo: Any, body: HostCreateRequest, *, actor: str, source: str
) -> dict[str, Any]:
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
    # A device's address is a claim like a guest's (lane B2): recorded now,
    # so a clash with it is raised on the next read, not the next sync.
    await repo.sync_operator_address(host_id)
    host = await repo.get_host(host_id)
    await repo.log_audit(
        user_id=actor,
        source=source,
        action="host_added",
        target_host=body.hostname,
    )
    return dict(host) if host else {"id": host_id}


async def forget_host_record(repo: Any, host_id: str, *, actor: str, source: str) -> dict[str, Any]:
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
        user_id=actor,
        source=source,
        action="host_forgotten",
        target_host=str(host.get("hostname") or ""),
    )
    return {"id": host_id, "forgotten": True}


@router.post("", status_code=201, dependencies=[Depends(require_scope("write"))])
async def create_manual_host(
    request: Request, body: HostCreateRequest, token: dict[str, Any] = _principal
) -> dict[str, Any]:
    try:
        return await create_manual_host_record(
            request.app.state.repo, body, actor=audit_actor(token), source="api"
        )
    except InventoryError as e:
        raise HTTPException(status_code=e.status, detail=e.detail) from e


@router.delete("/{host_id}", dependencies=[Depends(require_scope("write"))])
async def forget_host(
    request: Request, host_id: str, token: dict[str, Any] = _principal
) -> dict[str, Any]:
    try:
        return await forget_host_record(
            request.app.state.repo, host_id, actor=audit_actor(token), source="api"
        )
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


@router.get("/{host_id}/network", dependencies=[Depends(require_scope("read"))])
async def get_host_network(request: Request, host_id: str) -> dict[str, Any]:
    """Every address this host holds, by source and age, and every clash it is in.

    Read-only (lane B2): the Network tab on the host page. A clash names the
    other machines, so the operator can see who else answers for the address.
    """
    host = await request.app.state.repo.get_host(host_id)
    if host is None:
        raise HTTPException(status_code=404, detail=f"Host not found: {host_id}")
    return await _get_service(request).host_network(host_id)


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
    clear_env = body.env == ""
    if body.env:
        updates["env"] = body.env
        updates["env_source"] = ENV_SOURCE_USER
    if not updates and not clear_env:
        raise InventoryError(400, "No valid fields to update")
    if updates:
        await repo.update_host(host_id, **updates)
        # A PATCH is an operator deciding, so the fields it wrote are PINNED:
        # the next sync or enrich pass leaves them alone (#424). Without this,
        # editing a description or a status in the UI lasted until the next
        # reconciler cycle.
        await repo.pin_host_fields(host_id, set(updates))
        if "ip_address" in updates:
            await repo.sync_operator_address(host_id)
    if clear_env:
        # The operator withdrew their env: Proxmox's answer applies again, from
        # the tags and template flag the last sync stored - now, not after the
        # next sync, so the page does not show a blank in the meantime.
        env, source = imported_env(
            parse_pve_tags(host.get("pve_tags")), bool(host.get("is_template"))
        )
        await repo.update_host(host_id, env=env, env_source=source)
        await repo.unpin_host_fields(host_id, {"env"})
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
    _refuse_template(host)
    await repo.update_host(host_id, managed=1, source="imported", import_state="adopted")
    updated = await repo.get_host(host_id)
    result = dict(updated)
    summary = await _introspect_with_adapter(svc, result, adapter)
    if summary is not None:
        result["introspection"] = summary
    return result


def _refuse_template(host: dict[str, Any]) -> None:
    """A Proxmox template is a provisioning input, never a machine to manage."""
    if host.get("is_template"):
        raise InventoryError(
            409,
            f"{host.get('hostname')} is a Proxmox template: it cannot run, so there is "
            "nothing to adopt. Clone it into a guest and adopt that.",
        )


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
                if host.get("is_template"):
                    # Counted as failed like any host that cannot be adopted.
                    failed += 1
                    continue
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
