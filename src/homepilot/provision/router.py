from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status

from ..auth.deps import require_scope
from .defaults import MissingProvisioningDefaultError, provisioning_defaults
from .models import ProvisionRequestIn, TailnetJoinRequest
from .service import (
    ProvisionConflictError,
    TailnetJoinConflictError,
    TailnetJoinTargetError,
    resolve_join_target,
)

logger = logging.getLogger(__name__)

router = APIRouter()

_require_admin_dep = Depends(require_scope("admin"))


@router.post("/provision", status_code=status.HTTP_202_ACCEPTED)
async def provision_guest(
    request: Request,
    body: ProvisionRequestIn,
    token: dict[str, Any] = _require_admin_dep,
) -> dict[str, Any]:
    """Queue a clone-from-template provision. Progress is read via /tasks/{id}.

    node, template_vmid, pool, storage and ipconfig0 may be omitted when this instance
    has provisioning defaults for them (#553 C3); a request that names them is
    unchanged, and one that names neither is refused saying which setting would
    have filled the gap.
    """
    service = getattr(request.app.state, "provision_service", None)
    if service is None or getattr(service, "proxmox", None) is None:
        raise HTTPException(status_code=503, detail="Proxmox not configured")
    defaults = await provisioning_defaults(request.app.state)
    try:
        resolved = body.resolve(defaults)
    except MissingProvisioningDefaultError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        task_id = await service.start(resolved, actor=token.get("user_id") or "system")
    except ProvisionConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"task_id": task_id, "status": "pending"}


@router.post("/{vmid}/tailnet-join", status_code=status.HTTP_202_ACCEPTED)
async def rejoin_tailnet(
    request: Request,
    vmid: int,
    body: TailnetJoinRequest,
    token: dict[str, Any] = _require_admin_dep,
) -> dict[str, Any]:
    """Retry the tailnet join on an EXISTING guest with a fresh key (#628).

    Admin, like `POST /guests/provision`: it runs a command inside somebody's
    machine. Progress is read via /tasks/{id}, and the task result carries both
    the outcome (`tailnet`: joined / failed / unknown) and the reason
    (`tailnet_detail`).

    The key is used once and forgotten - not stored, not audited, not logged -
    so this route cannot tell a caller whether an EARLIER key was the problem.
    It can only run a new one and report what the guest said.
    """
    service = getattr(request.app.state, "provision_service", None)
    if service is None or getattr(service, "proxmox", None) is None:
        raise HTTPException(status_code=503, detail="Proxmox not configured")

    try:
        node, hostname = await resolve_join_target(
            service,
            vmid,
            node=body.node,
            hostname=body.tailnet_hostname,
            defaults_source=request.app.state,
        )
    except TailnetJoinTargetError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    try:
        task_id = await service.start_tailnet_join(
            node=node,
            vmid=vmid,
            hostname=hostname,
            key=body.auth_key,
            actor=token.get("user_id") or "system",
        )
    except TailnetJoinConflictError as exc:
        # 409, not 202: two joins on one guest overwrite each other's staged key
        # file, so the second must be refused rather than queued.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"task_id": task_id, "status": "pending", "vmid": vmid, "node": node}
