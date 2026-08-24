"""The guest API (#442 G1): what a cert-holding friend may see and do.

Same backend, different client. The guest portal client (its own container,
#442 G2) consumes THIS surface and nothing else - a guest never touches the
admin UI, the admin API, MCP or the agent hub. The scope is deliberately a
mini hosting panel: my machines, power actions on my machines, my provision
status. Nothing here can name another guest's machine, an operator's host,
a node, a template or a task id that is not the caller's own.

Identity and trust are the portal's, unchanged: the proxy did the mTLS
handshake, and every request must pass the same three factors (trusted source,
proxy shared secret, verified-cert header) before the CN is believed
(portal/trust.py). Ownership is `hosts.owner == CN` - set at provision time by
the invite flow and never writable through this surface.

Authorization shape: every route loads BY OWNER, never by id-then-check. A
route that cannot even fetch someone else's row cannot leak its existence -
a wrong id and another guest's id both answer 404, indistinguishably.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..config import get_settings
from ..portal.trust import (
    PortalNotConfiguredError,
    PortalUntrustedError,
    assert_trusted_cn,
    load_trust,
)

logger = logging.getLogger(__name__)

router = APIRouter()

POWER_ACTIONS = ("start", "stop", "reboot")

# The client page ships INSIDE the backend (#442 G2, revised): the front
# nginx adds one proxy location and nothing else - no files to copy, no
# separate deploy to forget. The page is a data-free shell (the APIs behind
# it stay trust-gated), so serving it takes no certificate.
_PORTAL_HTML = Path(__file__).parent / "portal.html"


@router.get("/", include_in_schema=False)
async def portal_page() -> Any:
    from fastapi.responses import FileResponse

    return FileResponse(_PORTAL_HTML, media_type="text/html")


class PowerRequest(BaseModel):
    action: str


def _client_cn(request: Request) -> str:
    settings = getattr(request.app.state, "settings", None) or get_settings()
    trust = load_trust(settings)
    peer = request.client.host if request.client else None
    return assert_trusted_cn(peer, request.headers, trust)


def _repo(request: Request) -> Any:
    repo = getattr(request.app.state, "repo", None)
    if repo is None:
        raise PortalNotConfiguredError("The backend has not finished starting")
    return repo


def _guest_view(host: dict[str, Any]) -> dict[str, Any]:
    """The fields a guest gets about their own machine - and no others.

    No node, no template, no proxmox id, no tags, no import state: those are
    operator vocabulary, and the guest pages must not leak topology.
    """
    return {
        "id": host["id"],
        "hostname": host["hostname"],
        "status": host.get("status"),
        "ip_address": host.get("ip_address"),
        "cpu_cores": host.get("cpu_cores"),
        "memory_mb": host.get("memory_mb"),
        "disk_gb": host.get("disk_gb"),
        "os_info": host.get("os_info"),
        "created_at": host.get("created_at"),
    }


async def _owned_hosts(repo: Any, cn: str) -> list[dict[str, Any]]:
    # BY OWNER, never id-then-check: a query that cannot fetch another guest's
    # row cannot leak its existence either.
    rows = await repo.db.fetchall("SELECT * FROM hosts WHERE owner = ?", (cn,))
    return [dict(r) for r in rows]


@router.get("/vms")
async def my_vms(request: Request) -> Any:
    try:
        cn = _client_cn(request)
        repo = _repo(request)
    except PortalNotConfiguredError as exc:
        return JSONResponse(status_code=503, content={"detail": str(exc)})
    except PortalUntrustedError:
        return JSONResponse(status_code=403, content={"detail": "no client certificate"})
    hosts = await _owned_hosts(repo, cn)
    return {"items": [_guest_view(h) for h in hosts], "total": len(hosts)}


@router.get("/vms/{host_id}")
async def my_vm(request: Request, host_id: str) -> Any:
    try:
        cn = _client_cn(request)
        repo = _repo(request)
    except PortalNotConfiguredError as exc:
        return JSONResponse(status_code=503, content={"detail": str(exc)})
    except PortalUntrustedError:
        return JSONResponse(status_code=403, content={"detail": "no client certificate"})
    mine = {h["id"]: h for h in await _owned_hosts(repo, cn)}
    host = mine.get(host_id)
    if host is None:
        # Uniform with a wrong id: existence of other people's machines is
        # not distinguishable from a typo.
        raise HTTPException(status_code=404, detail="No such machine")
    return _guest_view(host)


@router.get("/quota")
async def my_quota(request: Request) -> Any:
    """The guest's budget and where they stand against it - shown in the
    portal so "over budget" at redemption is never a surprise."""
    try:
        cn = _client_cn(request)
        repo = _repo(request)
    except PortalNotConfiguredError as exc:
        return JSONResponse(status_code=503, content={"detail": str(exc)})
    except PortalUntrustedError:
        return JSONResponse(status_code=403, content={"detail": "no client certificate"})
    from .quota import get_quota, usage_for

    used = await usage_for(repo, cn)
    quota = await get_quota(repo, cn)
    return {
        "cn": cn,
        "usage": {
            "vms": used.vms,
            "cores": used.cores,
            "memory_mb": used.memory_mb,
            "disk_gb": used.disk_gb,
        },
        "limits": None
        if quota is None
        else {
            "vms": quota.get("max_vms"),
            "cores": quota.get("max_cores"),
            "memory_mb": quota.get("max_memory_mb"),
            "disk_gb": quota.get("max_disk_gb"),
        },
    }


@router.post("/vms/{host_id}/power")
async def power(request: Request, host_id: str, body: PowerRequest) -> Any:
    try:
        cn = _client_cn(request)
        repo = _repo(request)
    except PortalNotConfiguredError as exc:
        return JSONResponse(status_code=503, content={"detail": str(exc)})
    except PortalUntrustedError:
        return JSONResponse(status_code=403, content={"detail": "no client certificate"})

    if body.action not in POWER_ACTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"action must be one of: {', '.join(POWER_ACTIONS)}",
        )

    mine = {h["id"]: h for h in await _owned_hosts(repo, cn)}
    host = mine.get(host_id)
    if host is None:
        raise HTTPException(status_code=404, detail="No such machine")

    proxmox = getattr(request.app.state, "proxmox", None)
    node = host.get("node")
    vmid = host.get("proxmox_id")
    if proxmox is None or not node or vmid is None:
        # Honest and guest-safe: no topology, no Proxmox error text.
        return JSONResponse(
            status_code=503,
            content={"detail": "power control is not available right now"},
        )

    try:
        if body.action == "start":
            await proxmox.start_vm(node, int(vmid))
        elif body.action == "stop":
            await proxmox.stop_vm(node, int(vmid))
        else:
            await proxmox.reboot_vm(node, int(vmid))
    except Exception:
        # The real error goes to the operator's log, never to the guest page.
        logger.exception("guest power %s failed for host %s (cn=%s)", body.action, host_id, cn)
        return JSONResponse(
            status_code=502,
            content={"detail": f"the {body.action} could not be completed"},
        )

    await repo.log_audit(
        user_id=f"guest:{cn}",
        source="guest-portal",
        action=f"vm_{body.action}",
        target_host=str(host.get("hostname") or ""),
    )
    return {"id": host_id, "action": body.action, "accepted": True}
