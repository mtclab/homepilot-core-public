"""The operator overview, computed from the estate itself.

Lives in a service rather than in the route because TWO surfaces answer this
question - GET /dashboard/summary and the `get_dashboard_summary` MCP tool - and
an operator's console and their assistant must never be able to report different
counts for the same estate.
"""

from __future__ import annotations

from typing import Any

from ..app_settings import effective
from ..config import get_settings


async def build_summary(repo: Any) -> dict[str, Any]:
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
    # A host with a linked agent IS managed for coverage purposes (#514 S1):
    # HomePilot has a live channel onto it, which is what "covered" means to an
    # operator. The `managed` flag keeps its adoption semantics elsewhere.
    managed = (
        await db.fetchone("SELECT COUNT(*) c FROM hosts WHERE managed = 1 OR agent_id IS NOT NULL")
    )["c"]
    # "uncovered" = a discovered guest still pending adoption (not yet managed).
    uncovered = (
        await db.fetchone(
            "SELECT COUNT(*) c FROM hosts WHERE source = 'discovered' AND import_state = 'pending'"
        )
    )["c"]
    coverage_pct = round(100 * managed / hosts_total) if hosts_total else 0

    # ── Drift ────────────────────────────────────────────────────────────────
    drift_total = (await db.fetchone("SELECT COUNT(*) c FROM drift_checks"))["c"]
    drifted = (await db.fetchone("SELECT COUNT(*) c FROM drift_checks WHERE state = 'drifted'"))[
        "c"
    ]
    in_spec = (await db.fetchone("SELECT COUNT(*) c FROM drift_checks WHERE state = 'in_spec'"))[
        "c"
    ]
    # Percentage of what was actually CHECKED (#425). It used to be
    # (total - drifted) / total, which counted every unverifiable and errored
    # artifact as healthy - the headline number on the operator's first screen,
    # inflated by exactly the things nobody had looked at.
    checked = in_spec + drifted
    unknown = drift_total - checked
    in_spec_pct = round(100 * in_spec / checked) if checked else 100

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

    # ── First-run path (#445 A7) ──────────────────────────────────────────────
    # Derived entirely from values this endpoint already computed: no extra
    # query, and - more importantly - no separate notion of "have you done the
    # setup" that could disagree with the estate. A checklist that ticks itself
    # off from a stored flag rather than from the thing it claims happened is
    # just a tutorial that lies.
    #
    # The steps are the actual path from an empty install to a managed change:
    # something in inventory -> adopt it -> give it an agent -> apply a change.
    applied_artifacts = artifacts_by_status.get("applied", 0)
    onboarding_steps = [
        {
            "key": "inventory",
            "title": "Get a host into inventory",
            "detail": "Sync from Proxmox, or add a host by hand if it is not a Proxmox guest.",
            "href": "/inventory",
            "done": hosts_total > 0,
        },
        {
            "key": "adopt",
            "title": "Adopt a host to manage",
            "detail": "Adopting says HomePilot may act on it. Discovery alone never does.",
            "href": "/inventory",
            "done": managed > 0,
        },
        {
            "key": "agent",
            "title": "Install the agent on it",
            "detail": "The agent is how a change reaches the host, and how metrics come back.",
            "href": "/agents",
            "done": agents_connected > 0,
        },
        {
            "key": "artifact",
            "title": "Approve and apply your first change",
            "detail": "Propose an artifact, review what it will do on the host, then apply it.",
            "href": "/artifacts",
            "done": applied_artifacts > 0,
        },
    ]

    return {
        "onboarding": {
            "steps": onboarding_steps,
            "complete": all(step["done"] for step in onboarding_steps),
        },
        "inventory": {
            "total": hosts_total,
            "managed": managed,
            "uncovered": uncovered,
            "coverage_pct": coverage_pct,
            "by_status": by_status,
            "by_role": by_role,
            "by_type": by_type,
        },
        "drift": {
            "total": drift_total,
            "drifted": drifted,
            "in_spec": in_spec,
            # Surfaced, not hidden: "we have not checked 40 of these" is
            # actionable, and rolling it into a green percentage is not.
            "unknown": unknown,
            "checked": checked,
            "in_spec_pct": in_spec_pct,
        },
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
            # firing_alerts on its own is the #642 shape: on an install with no
            # rules it is 0, and 0 reads as "everything is well" when it means
            # "nothing is being looked at". These two say which it is - how many
            # rules are enabled, and how many of those matched no host on their
            # last pass. Both come off alert_rules, one row per rule.
            "rules_enabled": (
                await db.fetchone("SELECT COUNT(*) c FROM alert_rules WHERE enabled = 1")
            )["c"],
            "rules_watching_nothing": (
                await db.fetchone(
                    "SELECT COUNT(*) c FROM alert_rules "
                    "WHERE enabled = 1 AND COALESCE(hosts_matched, 0) = 0"
                )
            )["c"],
            # Resolved, not read off boot Settings: the number the overview
            # states is the horizon actually in force (#553 C2).
            "retention_days": await effective("metrics_retention_days", get_settings()),
        },
    }
