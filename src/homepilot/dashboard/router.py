"""Dashboard summary — current-state aggregates for the overview page.

Counts and breakdowns only. Time series live under ``/monitoring`` (ADR-004 S5) —
HomePilot now collects its own history, so the old "current state here, history
in an external monitoring server" boundary is gone.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from ..app_settings import resolver_from_state
from ..auth.deps import require_scope
from ..config import get_settings
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
