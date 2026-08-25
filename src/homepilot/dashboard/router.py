"""Dashboard summary — current-state aggregates for the overview page.

Counts and breakdowns only. Time series live under ``/monitoring`` (ADR-004 S5) —
HomePilot now collects its own history, so the old "current state here, history
in an external monitoring server" boundary is gone.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from ..auth.deps import require_scope
from ..config import get_settings
from .service import build_summary

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("/config", dependencies=[Depends(require_scope("read"))])
async def ui_config() -> dict[str, Any]:
    """Small UI config shared across pages."""
    return {"metrics_retention_days": get_settings().metrics_retention_days}


@router.get("/summary", dependencies=[Depends(require_scope("read"))])
async def summary(request: Request) -> dict[str, Any]:
    return await build_summary(request.app.state.repo)
