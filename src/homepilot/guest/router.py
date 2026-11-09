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
    # A machine the hypervisor no longer reports is GONE, whatever the last
    # status we happened to record was. Passing the stale value through showed
    # a guest their destroyed machine as "online" (#613) - the one thing the
    # page must never do, because it is the guest's only window onto whether
    # their machine still exists.
    status = "gone" if host.get("absent_since") else host.get("status")
    return {
        "id": host["id"],
        "hostname": host["hostname"],
        "status": status,
        "gone_since": host.get("absent_since"),
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

    # The SAME guards as the operator's button and an approved guest-power
    # change (#781): one PowerService, so a friend can never start their
    # machine into an address a running guest holds, onto a passthrough disk a
    # running guest has open, past a lock, at a reused VMID (#706) or while it
    # is gone. The portal used to carry its own shorter copy of these checks,
    # which had neither the address nor the disk guard.
    from ..inventory.power import OUTCOME_FAILED, PowerRefusedError, PowerService

    proxmox = getattr(request.app.state, "proxmox", None)
    try:
        result = await PowerService(repo, proxmox).execute(
            host_id, body.action, actor=f"guest:{cn}", source="guest-portal"
        )
    except PowerRefusedError as refused:
        # The full reason (holder names, VMIDs, nodes) is the operator's; the
        # guest gets a sentence that names nothing outside their own machine.
        logger.info(
            "guest power %s refused for host %s (cn=%s): %s",
            body.action,
            host_id,
            cn,
            refused.message,
        )
        status, detail = _guest_refusal(refused.code, body.action)
        return JSONResponse(status_code=status, content={"detail": detail, "code": refused.code})

    if result.outcome == OUTCOME_FAILED:
        # The real error goes to the operator's log, never to the guest page.
        logger.warning(
            "guest power %s failed for host %s (cn=%s): %s",
            body.action,
            host_id,
            cn,
            result.message,
        )
        return JSONResponse(
            status_code=502,
            content={"detail": f"the {body.action} could not be completed", "outcome": "failed"},
        )
    return {
        "id": host_id,
        "action": body.action,
        "accepted": True,
        "outcome": result.outcome,
        "status": result.status_after,
        "message": _guest_outcome(result.outcome, body.action),
    }


# What a friend reads when a guard refuses. Never a holder's name, a VMID, a
# node or Proxmox's own text: the other machine may be an operator's host or
# another friend's, and this surface must not even confirm it exists.
_GUEST_REFUSALS: dict[str, tuple[int, str]] = {
    "gone": (409, "this machine no longer exists, so it cannot be powered"),
    "name_mismatch": (409, "this machine no longer exists, so it cannot be powered"),
    "unreadable": (409, "this machine could not be found where it was, so nothing was sent"),
    "not_found": (404, "No such machine"),
    "template": (409, "this machine is a template and cannot run"),
    "not_a_guest": (503, "power control is not available right now"),
    "no_proxmox": (503, "power control is not available right now"),
    "locked": (
        409,
        "your machine is busy (a backup or snapshot is running); try again when it "
        "finishes. Nothing was sent.",
    ),
    "address_clash": (
        409,
        "another running machine is using this machine's network address, so starting "
        "it would knock that machine off the network. Nothing was sent; ask the "
        "operator to sort the address out.",
    ),
    "disk_shared": (
        409,
        "a disk this machine uses is attached to another running machine, and two "
        "machines writing one disk corrupts it. Nothing was sent; ask the operator.",
    ),
    "address_unknown": (
        409,
        "whether it is safe to start this machine could not be checked right now; "
        "nothing was sent. Try again in a minute.",
    ),
    "disk_unknown": (
        409,
        "whether it is safe to start this machine could not be checked right now; "
        "nothing was sent. Try again in a minute.",
    ),
    "confirmation_required": (
        409,
        "the operator has protected this machine, so it can only be switched off or "
        "restarted by them. Nothing was sent.",
    ),
}


def _guest_outcome(outcome: str, action: str) -> str:
    """The settled result in the guest's words (Proxmox was followed to the end)."""
    if outcome == "ok":
        return {
            "start": "your machine is running",
            "stop": "your machine is off",
            "reboot": "your machine restarted and is running",
        }.get(action, f"the {action} is done")
    if outcome == "timed_out":
        return (
            "your machine did not shut down in time, so it was not restarted; it is still running"
        )
    return f"the {action} was sent but has not finished yet; check again in a minute"


def _guest_refusal(code: str, action: str) -> tuple[int, str]:
    if code == "state":
        return 409, (
            "your machine is already running"
            if action == "start"
            else "your machine is not running"
        )
    return _GUEST_REFUSALS.get(code, (409, f"the {action} was refused; nothing was sent"))
