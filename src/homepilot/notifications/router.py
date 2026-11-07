"""`/admin/notifications/channels` - native alert channels (#660).

Admin scope throughout, like `/admin/webhooks`: a channel decides who is woken
up, and its URL is a credential. The URL is accepted once, on create, stored in
the vault, and never returned - every response shows the endpoint as
scheme://host[:port] only.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from ..auth.deps import require_scope, require_token
from ..auth.principal import audit_actor
from .ntfy import MAX_TOKEN_LENGTH, MAX_URL_LENGTH
from .service import MAX_NAME_LENGTH, ChannelStoreError, Notifier

router = APIRouter(prefix="/admin/notifications", tags=["notifications"])

_admin = Depends(require_scope("admin"))
_principal = Depends(require_token)


class ChannelIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str = Field(default="ntfy", max_length=16)
    name: str = Field(min_length=1, max_length=MAX_NAME_LENGTH)
    url: str = Field(min_length=1, max_length=MAX_URL_LENGTH)
    token: str | None = Field(default=None, max_length=MAX_TOKEN_LENGTH)


class ChannelPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool


def _notifier(request: Request) -> Notifier:
    notifier = getattr(request.app.state, "notifier", None)
    if not isinstance(notifier, Notifier):
        raise HTTPException(status_code=503, detail="Notification channels are not available")
    return notifier


async def _audit(
    request: Request, token: dict[str, Any], action: str, details: dict[str, Any]
) -> None:
    repo = getattr(request.app.state, "repo", None)
    if repo is not None:
        await repo.log_audit(
            user_id=audit_actor(token),
            source="api",
            action=action,
            details_json=json.dumps(details),
        )


@router.get("/channels", dependencies=[_admin])
async def list_channels(request: Request) -> dict[str, Any]:
    items = await _notifier(request).list_channels()
    return {"items": items, "total": len(items)}


@router.post("/channels", dependencies=[_admin], status_code=201)
async def create_channel(
    request: Request, body: ChannelIn, token: dict[str, Any] = _principal
) -> dict[str, Any]:
    try:
        view = await _notifier(request).create_channel(
            kind=body.kind, name=body.name, url=body.url, token=body.token
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except ChannelStoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    await _audit(
        request,
        token,
        "notification_channel_created",
        {"id": view["id"], "kind": view["kind"], "endpoint": view["endpoint"]},
    )
    return view


@router.patch("/channels/{channel_id}", dependencies=[_admin])
async def update_channel(
    request: Request, channel_id: int, body: ChannelPatch, token: dict[str, Any] = _principal
) -> dict[str, Any]:
    notifier = _notifier(request)
    if not await notifier.store.set_enabled(channel_id, body.enabled):
        raise HTTPException(status_code=404, detail=f"Channel {channel_id} not found")
    await _audit(
        request,
        token,
        "notification_channel_enabled" if body.enabled else "notification_channel_disabled",
        {"id": channel_id},
    )
    row = await notifier.store.get(channel_id)
    from .service import public_view

    return public_view(row or {"id": channel_id})


@router.delete("/channels/{channel_id}", dependencies=[_admin])
async def delete_channel(
    request: Request, channel_id: int, token: dict[str, Any] = _principal
) -> dict[str, Any]:
    if not await _notifier(request).delete_channel(channel_id):
        raise HTTPException(status_code=404, detail=f"Channel {channel_id} not found")
    await _audit(request, token, "notification_channel_deleted", {"id": channel_id})
    return {"id": channel_id, "deleted": True}


@router.post("/channels/{channel_id}/test", dependencies=[_admin])
async def test_channel(
    request: Request, channel_id: int, token: dict[str, Any] = _principal
) -> dict[str, Any]:
    try:
        result = await _notifier(request).send_test(channel_id)
    except LookupError:
        raise HTTPException(status_code=404, detail=f"Channel {channel_id} not found") from None
    await _audit(
        request,
        token,
        "notification_channel_tested",
        {"id": channel_id, "endpoint": result["endpoint"], "delivered": result["delivered"]},
    )
    return result
