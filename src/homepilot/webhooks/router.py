"""`/admin/webhooks` - webhook configs on a running install (#700).

Admin scope throughout: a webhook decides where the platform's events go, and
the `artifact_proposed` event carries the approval code to an admin-configured
endpoint (#681). Whoever can add an endpoint can read those codes, so adding
one is operator authority - and it is not offered over MCP at all.

Responses show the endpoint as scheme://host:port. The full URL comes back
once, in the response to the admin who created it.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..auth.deps import require_scope, require_token
from ..auth.principal import audit_actor
from . import service
from .catalog import MAX_URL_LENGTH, validate_event_types, validate_webhook_url

router = APIRouter(prefix="/admin/webhooks", tags=["webhooks"])

_admin = Depends(require_scope("admin"))
_principal = Depends(require_token)


class WebhookIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=1, max_length=MAX_URL_LENGTH)
    event_types: list[str] = Field(min_length=1, max_length=32)
    secret: str | None = Field(default=None, min_length=1, max_length=512)
    max_retries: int = Field(default=3, ge=0, le=10)

    @field_validator("url")
    @classmethod
    def _url(cls, value: str) -> str:
        return validate_webhook_url(value)

    @field_validator("event_types")
    @classmethod
    def _events(cls, value: list[str]) -> list[str]:
        return validate_event_types(value)


def _repo(request: Request) -> Any:
    repo = getattr(request.app.state, "repo", None)
    if repo is None:
        raise HTTPException(status_code=503, detail="The backend has not finished starting")
    return repo


@router.get("", dependencies=[_admin])
async def list_webhooks(request: Request) -> dict[str, Any]:
    return await service.listing(_repo(request))


@router.post("", dependencies=[_admin], status_code=201)
async def create_webhook(
    request: Request, body: WebhookIn, token: dict[str, Any] = _principal
) -> dict[str, Any]:
    try:
        view = await service.create_webhook(
            _repo(request),
            url=body.url,
            event_types=body.event_types,
            secret=body.secret,
            max_retries=body.max_retries,
            actor=audit_actor(token),
            source="api",
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    # The one place the full URL is returned: to the admin who just typed it.
    return {**view, "url": body.url}


@router.delete("/{webhook_id}", dependencies=[_admin])
async def delete_webhook(
    request: Request, webhook_id: int, token: dict[str, Any] = _principal
) -> dict[str, Any]:
    deleted = await service.delete_webhook(
        _repo(request), webhook_id, actor=audit_actor(token), source="api"
    )
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Webhook {webhook_id} not found")
    return {"id": webhook_id, "deleted": True}


@router.post("/{webhook_id}/test", dependencies=[_admin])
async def test_webhook(
    request: Request, webhook_id: int, token: dict[str, Any] = _principal
) -> dict[str, Any]:
    try:
        return await service.send_test_event(
            _repo(request), webhook_id, actor=audit_actor(token), source="api"
        )
    except LookupError:
        raise HTTPException(status_code=404, detail=f"Webhook {webhook_id} not found") from None
