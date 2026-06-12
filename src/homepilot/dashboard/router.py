"""Dashboard summary — current-state aggregates for the overview page.

HomePilot owns current state (coverage, drift, inventory shape, fleet, pipeline);
historical telemetry lives in Zabbix. This endpoint returns counts/breakdowns
only — no time series — plus the configured Zabbix URL for deep-linking.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from ..auth.deps import require_scope
from ..config import get_settings

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("/config", dependencies=[Depends(require_scope("read"))])
async def ui_config() -> dict[str, Any]:
    """Small UI config shared across pages (e.g. the Zabbix deep-link base)."""
    return {"zabbix_url": get_settings().zabbix_url or ""}


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
    agents_known = (await db.fetchone("SELECT COUNT(*) c FROM agents"))["c"]
    agents_connected = (await db.fetchone("SELECT COUNT(*) c FROM agents WHERE connected = 1"))["c"]

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
        "zabbix_url": get_settings().zabbix_url or "",
    }
