from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status

from ..auth.deps import require_scope
from .defaults import MissingProvisioningDefaultError, provisioning_defaults
from .models import ProvisionRequestIn
from .service import ProvisionConflictError

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

    node, template_vmid, pool and ipconfig0 may be omitted when this instance
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
