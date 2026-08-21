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

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("/config", dependencies=[Depends(require_scope("read"))])
async def ui_config() -> dict[str, Any]:
    """Small UI config shared across pages."""
    return {"metrics_retention_days": get_settings().metrics_retention_days}


@router.get("/summary", dependencies=[Depends(require_scope("read"))])
async def summary(request: Request) -> dict[str, Any]:
    repo = request.app.state.repo
    db = repo.db

    async def grouped(sql: str) -> dict[str, int]:
        rows = await db.fetchall(sql)
        out: dict[str, int] = {}
        for r in rows:
            d = dict(r)
            keys = list(d.values())
            out[str(keys[0]) if keys[0] is not None else "unknown"] = int(keys[1])
        return out

    # ── Inventory ────────────────────────────────────────────────────────────
    hosts_total = (await db.fetchone("SELECT COUNT(*) c FROM hosts"))["c"]
    by_status = await grouped("SELECT status, COUNT(*) FROM hosts GROUP BY status")
    by_role = await grouped("SELECT role, COUNT(*) FROM hosts GROUP BY role")
    by_type = await grouped("SELECT host_type, COUNT(*) FROM hosts GROUP BY host_type")
    managed = (await db.fetchone("SELECT COUNT(*) c FROM hosts WHERE managed = 1"))["c"]
    # "uncovered" = a discovered guest still pending adoption (not yet managed).
    uncovered = (
        await db.fetchone(
            "SELECT COUNT(*) c FROM hosts WHERE source = 'discovered' AND import_state = 'pending'"
        )
    )["c"]
    coverage_pct = round(100 * managed / hosts_total) if hosts_total else 0

    # ── Drift ────────────────────────────────────────────────────────────────
    drift_total = (await db.fetchone("SELECT COUNT(*) c FROM drift_checks"))["c"]
    drifted = (await db.fetchone("SELECT COUNT(*) c FROM drift_checks WHERE drifted = 1"))["c"]
    in_spec_pct = round(100 * (drift_total - drifted) / drift_total) if drift_total else 100

    # ── Pipeline ───────────────────────────────────────────────────────────────
    artifacts_by_status = await grouped("SELECT status, COUNT(*) FROM artifacts GROUP BY status")
    tasks_by_status = await grouped("SELECT status, COUNT(*) FROM tasks GROUP BY status")

    # ── Agent fleet (persisted + live) ─────────────────────────────────────────
    # "Connected" comes from the LIVE registry, never from agents.connected. That
    # column is written best-effort on register/unregister, so it stays 1 for any
    # agent that vanished without a clean goodbye - a killed process, a dropped
    # link, or a backend restart. It was therefore most wrong exactly when an
    # operator loads this page to check the fleet survived a restart, reporting a
    # healthy fleet over stranded agents (#469, and it masked #468).
    #
    # GET /agents/ overlays the same registry on the same rows, so both endpoints
    # now answer from one source and cannot disagree.
    from ..app_state import get_agent_registry

    registry = get_agent_registry()
    live_ids = {a["agent_id"] for a in registry.list_connected()} if registry else set()
    known_ids = {row["agent_id"] for row in await repo.list_agents()}
    agents_known = len(known_ids | live_ids)
    agents_connected = len(live_ids)

    return {
        "inventory": {
            "total": hosts_total,
            "managed": managed,
            "uncovered": uncovered,
            "coverage_pct": coverage_pct,
            "by_status": by_status,
            "by_role": by_role,
            "by_type": by_type,
        },
        "drift": {"total": drift_total, "drifted": drifted, "in_spec_pct": in_spec_pct},
        "artifacts": artifacts_by_status,
        "tasks": tasks_by_status,
        "agents": {"known": agents_known, "connected": agents_connected},
        # Deliberately NOT counting metric rows or distinct series here: the
        # metrics table is the one table that grows without an operator doing
        # anything, and a COUNT(DISTINCT ...) over it would put a full scan on
        # the page every operator opens first. alert_state has one row per
        # (rule, host) and is cheap.
        "metrics": {
            "firing_alerts": (
                await db.fetchone(
                    "SELECT COUNT(*) c FROM alert_state WHERE firing_since IS NOT NULL"
                )
            )["c"],
            "retention_days": get_settings().metrics_retention_days,
        },
    }
