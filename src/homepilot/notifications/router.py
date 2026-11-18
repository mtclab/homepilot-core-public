"""`/admin/notifications/channels` - native alert channels (#660).

Admin scope throughout, like `/admin/webhooks`: a channel decides who is woken
up, and its URL is a credential. The URL is accepted once, on create, stored in
the vault, and never returned - every response shows the endpoint as
scheme://host[:port] only.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from ..auth.deps import require_scope, require_token
from ..auth.principal import audit_actor
from ..config import get_settings
from .bundled import PhoneAlerts, PhoneAlertsConflictError, PhoneAlertsError, create_ntfy_sh_channel
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


# ── Phone alerts (#822) ──────────────────────────────────────────────────────
# The guided "Get alerts on your phone" flow: the bundled ntfy server, its phone
# logins, and the ntfy.sh choice. Admin scope like every channel route. Two
# answers carry a credential ONCE - a new phone's password and the ntfy.sh
# topic URL - because the phone has to be given it; both are sent with
# `Cache-Control: no-store` and never audited, logged or stored in the clear.
# HomePilot's own ntfy tokens are in NO answer.


class PublicUrlIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=1, max_length=512)


class PhoneIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=32)


def _phone_alerts(request: Request) -> PhoneAlerts:
    """One PhoneAlerts per app, so its lock serialises enable/disable."""
    existing = getattr(request.app.state, "phone_alerts", None)
    if isinstance(existing, PhoneAlerts):
        return existing
    repo = getattr(request.app.state, "repo", None)
    if repo is None:
        raise HTTPException(status_code=503, detail="Phone alerts are not available")
    settings = get_settings()
    if not settings.ntfy_internal_url:
        raise HTTPException(
            status_code=503,
            detail=(
                "HP_NTFY_INTERNAL_URL is not set, so HomePilot does not know where the "
                "bundled ntfy service answers. The shipped compose files set it "
                "(http://ntfy); an install without them points it at its ntfy service."
            ),
        )
    made = PhoneAlerts(
        notifier=_notifier(request),
        repo=repo,
        data_dir=settings.data_dir,
        internal_url=settings.ntfy_internal_url,
    )
    request.app.state.phone_alerts = made
    return made


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, PhoneAlertsConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, LookupError):
        return HTTPException(status_code=404, detail=f"No phone named {exc.args[0]!r}")
    return HTTPException(status_code=503, detail=str(exc))


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


@router.get("/phone", dependencies=[_admin])
async def phone_alerts_status(request: Request) -> dict[str, Any]:
    return await _phone_alerts(request).status()


@router.post("/phone/bundled", dependencies=[_admin])
async def enable_bundled_ntfy(
    request: Request, token: dict[str, Any] = _principal
) -> dict[str, Any]:
    try:
        result = await _phone_alerts(request).enable()
    except PhoneAlertsError as exc:
        raise _http_error(exc) from None
    await _audit(request, token, "phone_alerts_bundled_enabled", {})
    return result


@router.delete("/phone/bundled", dependencies=[_admin])
async def disable_bundled_ntfy(
    request: Request, token: dict[str, Any] = _principal
) -> dict[str, Any]:
    try:
        result = await _phone_alerts(request).disable()
    except PhoneAlertsError as exc:
        raise _http_error(exc) from None
    await _audit(request, token, "phone_alerts_bundled_disabled", {})
    return result


@router.put("/phone/bundled/public-url", dependencies=[_admin])
async def set_bundled_public_url(
    request: Request, body: PublicUrlIn, token: dict[str, Any] = _principal
) -> dict[str, Any]:
    try:
        result = await _phone_alerts(request).set_public_url(body.url)
    except (PhoneAlertsError, ValueError) as exc:
        raise _http_error(exc) from None
    await _audit(
        request,
        token,
        "phone_alerts_public_url_set",
        {"public_url": result["public_url"], "ok": result["ok"]},
    )
    return result


@router.get("/phone/phones", dependencies=[_admin])
async def list_phones(request: Request) -> dict[str, Any]:
    try:
        items = await _phone_alerts(request).list_phones()
    except PhoneAlertsError as exc:
        raise _http_error(exc) from None
    return {"items": items, "total": len(items)}


@router.post("/phone/phones", dependencies=[_admin], status_code=201)
async def add_phone(
    request: Request, body: PhoneIn, response: Response, token: dict[str, Any] = _principal
) -> dict[str, Any]:
    _no_store(response)
    try:
        result = await _phone_alerts(request).add_phone(body.name)
    except (PhoneAlertsError, ValueError) as exc:
        raise _http_error(exc) from None
    await _audit(request, token, "phone_alerts_phone_added", {"name": result["name"]})
    return result


@router.post("/phone/phones/{name}/allow", dependencies=[_admin])
async def allow_phone(
    request: Request, name: str, token: dict[str, Any] = _principal
) -> dict[str, Any]:
    try:
        result = await _phone_alerts(request).allow_phone(name)
    except (PhoneAlertsError, ValueError, LookupError) as exc:
        raise _http_error(exc) from None
    await _audit(request, token, "phone_alerts_phone_allowed", {"name": result["name"]})
    return result


@router.delete("/phone/phones/{name}", dependencies=[_admin])
async def remove_phone(
    request: Request, name: str, token: dict[str, Any] = _principal
) -> dict[str, Any]:
    try:
        result = await _phone_alerts(request).remove_phone(name)
    except (PhoneAlertsError, ValueError, LookupError) as exc:
        raise _http_error(exc) from None
    await _audit(request, token, "phone_alerts_phone_removed", {"name": result["name"]})
    return result


@router.post("/phone/ntfy-sh", dependencies=[_admin], status_code=201)
async def use_ntfy_sh(
    request: Request, response: Response, token: dict[str, Any] = _principal
) -> dict[str, Any]:
    _no_store(response)
    try:
        result = await create_ntfy_sh_channel(_notifier(request))
    except ChannelStoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    view = result["channel"]
    await _audit(
        request,
        token,
        "notification_channel_created",
        {"id": view["id"], "kind": view["kind"], "endpoint": view["endpoint"]},
    )
    return result
