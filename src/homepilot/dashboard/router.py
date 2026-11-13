"""Dashboard summary — current-state aggregates for the overview page.

Counts and breakdowns only. Time series live under ``/monitoring`` (ADR-004 S5) —
HomePilot now collects its own history, so the old "current state here, history
in an external monitoring server" boundary is gone.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..app_settings import resolver_from_state
from ..auth.deps import require_scope
from ..auth.principal import audit_actor
from ..config import get_settings
from ..updates.checker import update_status
from .service import build_summary

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("/config", dependencies=[Depends(require_scope("read"))])
async def ui_config(request: Request) -> dict[str, Any]:
    """Small UI config shared across pages.

    The retention window is resolved rather than read off boot Settings (#553
    C2): the charts must be sized by the horizon actually in force, not the one
    this process started with.
    """
    settings = getattr(request.app.state, "settings", None) or get_settings()
    resolver = resolver_from_state(request.app.state)
    days = (
        await resolver.value("metrics_retention_days")
        if resolver is not None
        else settings.metrics_retention_days
    )
    return {"metrics_retention_days": days}


@router.get("/summary", dependencies=[Depends(require_scope("read"))])
async def summary(request: Request) -> dict[str, Any]:
    return await build_summary(request.app.state.repo)


@router.get("/updates", dependencies=[Depends(require_scope("read"))])
async def updates(request: Request) -> dict[str, Any]:
    """Is a newer HomePilot released (review A+)? The release check's last
    verdict - read from its state row, never a live request to GitHub. Notify
    only: nothing here or anywhere upgrades the control plane."""
    return await update_status(request.app.state.repo)


class SnoozeIn(BaseModel):
    """Put one Overview line off ("Not now, 7 days")."""

    # The line's stable key as the summary and the Overview name it, e.g.
    # `gone:machines`, `task:<id>`, `alert:<rule>:<host>`.
    key: str = Field(min_length=1, max_length=300)
    # How serious the line is NOW: if it becomes more serious, it shows again.
    severity: Literal["notice", "warning", "critical"]


_admin = Depends(require_scope("admin"))


@router.post("/attention/snoozes")
async def snooze_line(
    request: Request, body: SnoozeIn, token: dict[str, Any] = _admin
) -> dict[str, Any]:
    """Hide one attention line for 7 days, or until it becomes more serious.

    Journaled (`attention_snoozed`). Snoozing a key again restarts its week.
    Read back through GET /dashboard/summary (`attention.snoozes`, `put_off`)."""
    from .attention import snooze

    return await snooze(request.app.state.repo, body.key, body.severity, audit_actor(token))


@router.delete("/attention/snoozes/{key:path}")
async def unsnooze_line(
    request: Request, key: str, token: dict[str, Any] = _admin
) -> dict[str, Any]:
    """Bring a put-off line back now. 404 when nothing is put off under `key`."""
    from .attention import unsnooze

    if not await unsnooze(request.app.state.repo, key, audit_actor(token)):
        raise HTTPException(status_code=404, detail=f"Nothing is put off under {key!r}")
    return {"key": key, "snoozed": False}
