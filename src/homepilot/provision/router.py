from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status

from ..auth.deps import require_scope
from .models import ProvisionRequest
from .service import ProvisionConflictError

logger = logging.getLogger(__name__)

router = APIRouter()

_require_admin_dep = Depends(require_scope("admin"))


@router.post("/provision", status_code=status.HTTP_202_ACCEPTED)
async def provision_guest(
    request: Request,
    body: ProvisionRequest,
    token: dict[str, Any] = _require_admin_dep,
) -> dict[str, Any]:
    """Queue a clone-from-template provision. Progress is read via /tasks/{id}."""
    service = getattr(request.app.state, "provision_service", None)
    if service is None or getattr(service, "proxmox", None) is None:
        raise HTTPException(status_code=503, detail="Proxmox not configured")
    try:
        task_id = await service.start(body, actor=token.get("user_id") or "system")
    except ProvisionConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"task_id": task_id, "status": "pending"}
