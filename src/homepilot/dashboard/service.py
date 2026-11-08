"""The operator overview, computed from the estate itself.

Lives in a service rather than in the route because TWO surfaces answer this
question - GET /dashboard/summary and the `get_dashboard_summary` MCP tool - and
an operator's console and their assistant must never be able to report different
counts for the same estate.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from ..app_settings import effective
from ..config import get_settings
from ..inventory.addresses import find_conflicts

# The statuses whose reality can disagree with the artifact - the drift
# universe. MUST equal ACTIVE_STATUSES in web/src/lib/drift.ts: the Overview's
# drift line links to the page built from that list, and the two may not count
# different sets.
ACTIVE_DRIFT_STATUSES = ("applied", "approved")

# How long the whole fleet may be without a connected agent before the Overview
# raises it. Long enough that a hub restart or a reconnect storm is not an
# incident, short enough that a dark week is not a warning line nobody reads.
FLEET_OFFLINE_GRACE_SECONDS = 300

# Every `unknown` drift verdict lands in exactly one of these, by what the
# operator does next. Reasons are the `details_json.reason` values written by
# reconciler/verify.py and reconciler/drift.py.
UNKNOWN_REASON_BUCKETS = ("no_agent", "check_failed", "no_proxmox", "by_design", "other")
_REASON_TO_BUCKET: dict[str, str] = {
    "no_agent": "no_agent",
    "check_failed": "check_failed",
    "probe_failed": "check_failed",
    "survey_failed": "check_failed",
    "nothing_read": "check_failed",
    "items_unanswered": "check_failed",
    "no_executor": "no_proxmox",
    "no_proxmox": "no_proxmox",
    # Nothing a check could compare: describe/read-only steps, or a kind that
    # has no drift check. True and expected, not a fault.
    "nothing_evaluated": "by_design",
    "shell_script_unverifiable": "by_design",
    "ansible_unverifiable": "by_design",
    "kb_note_skipped": "by_design",
}


def unknown_reason_bucket(details_json: str | None) -> str:
    """The bucket an `unknown` drift row belongs to; `other` when unrecorded."""
    try:
        details = json.loads(details_json) if details_json else {}
    except (ValueError, TypeError):
        details = {}
    reason = details.get("reason") if isinstance(details, dict) else None
    return _REASON_TO_BUCKET.get(str(reason or ""), "other")


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


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
    # Every fleet number counts `is_template = 0` rows only: a Proxmox template is a
    # provisioning input, not a machine. It can never run, so counted it was an
    # "offline" guest forever and a "pending adoption" nobody can adopt (lane B1).
    hosts_total = (await db.fetchone("SELECT COUNT(*) c FROM hosts WHERE is_template = 0"))["c"]
    templates = (await db.fetchone("SELECT COUNT(*) c FROM hosts WHERE is_template = 1"))["c"]
    by_status = await grouped(
        "SELECT status, COUNT(*) FROM hosts WHERE is_template = 0 GROUP BY status"
    )
    by_role = await grouped("SELECT role, COUNT(*) FROM hosts WHERE is_template = 0 GROUP BY role")
    by_type = await grouped(
        "SELECT host_type, COUNT(*) FROM hosts WHERE is_template = 0 GROUP BY host_type"
    )
    by_env = await grouped(
        "SELECT COALESCE(env, 'unset'), COUNT(*) FROM hosts WHERE is_template = 0 GROUP BY env"
    )
    # A host with a linked agent IS managed for coverage purposes (#514 S1):
    # HomePilot has a live channel onto it, which is what "covered" means to an
    # operator. The `managed` flag keeps its adoption semantics elsewhere.
    managed = (
        await db.fetchone(
            "SELECT COUNT(*) c FROM hosts WHERE is_template = 0 "
            "AND (managed = 1 OR agent_id IS NOT NULL)"
        )
    )["c"]
    # "uncovered" = a discovered guest still pending adoption (not yet managed).
    uncovered = (
        await db.fetchone(
            "SELECT COUNT(*) c FROM hosts WHERE is_template = 0 "
            "AND source = 'discovered' AND import_state = 'pending'"
        )
    )["c"]
    # The machine HomePilot runs on, once an operator confirmed it (lane B1).
    # "Its own host has no agent" is the line the Overview raises from this.
    self_row = await db.fetchone(
        "SELECT id, hostname, proxmox_id, agent_id, absent_since FROM hosts WHERE is_self = 1"
    )
    self_guest = (
        {
            "host_id": self_row["id"],
            "hostname": self_row["hostname"],
            "proxmox_id": self_row["proxmox_id"],
            "has_agent": self_row["agent_id"] is not None,
            "absent": self_row["absent_since"] is not None,
        }
        if self_row is not None
        else None
    )
    coverage_pct = round(100 * managed / hosts_total) if hosts_total else 0

    # ── Drift ────────────────────────────────────────────────────────────────
    # Only ACTIVE artifacts' verdicts count. A drift row outlives its artifact's
    # status: the loop re-checks `applied` only, so a revoked or superseded
    # artifact kept its last verdict forever, and the Overview said "2 artifacts
    # disagree" while the drift page it links to - which filters to the same
    # ACTIVE_DRIFT_STATUSES (web/src/lib/drift.ts) - listed one.
    drift_rows = await db.fetchall(
        "SELECT d.state, d.details_json FROM drift_checks d "
        "JOIN artifacts a ON a.id = d.artifact_id "
        f"WHERE a.status IN ({', '.join('?' for _ in ACTIVE_DRIFT_STATUSES)})",  # nosec B608
        ACTIVE_DRIFT_STATUSES,
    )
    drift_total = len(drift_rows)
    drifted = sum(1 for r in drift_rows if r["state"] == "drifted")
    in_spec = sum(1 for r in drift_rows if r["state"] == "in_spec")
    # Percentage of what was actually CHECKED (#425). It used to be
    # (total - drifted) / total, which counted every unverifiable and errored
    # artifact as healthy - the headline number on the operator's first screen,
    # inflated by exactly the things nobody had looked at.
    checked = in_spec + drifted
    unknown = drift_total - checked
    in_spec_pct = round(100 * in_spec / checked) if checked else 100
    # WHY nothing was established, by what the operator does next: "no agent on
    # the host" and "the check failed" are work; "describe-only" is the
    # artifact's nature and is fine. One number hid all three (16/17 on prod).
    unknown_by_reason = {bucket: 0 for bucket in UNKNOWN_REASON_BUCKETS}
    for r in drift_rows:
        if r["state"] in ("drifted", "in_spec"):
            continue
        unknown_by_reason[unknown_reason_bucket(r["details_json"])] += 1

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
    agent_rows = await repo.list_agents()
    known_ids = {row["agent_id"] for row in agent_rows}
    agents_known = len(known_ids | live_ids)
    agents_connected = len(live_ids)

    # "Every agent is gone" is ONE fact, not N warning lines (#660 class): prod
    # sat a week with no agent connected and said so only as one quiet line per
    # host. `enrolled` leaves out agents whose credential was revoked - nobody
    # expects those back. `offline_since` is the last time ANY enrolled agent was
    # heard from (null when none ever was), and `offline_alarm` says the outage
    # has outlived FLEET_OFFLINE_GRACE_SECONDS - measured also from when this hub
    # started listening, so a backend restart does not raise it before the
    # agents have had their chance to dial back in.
    enrolled_rows = [r for r in agent_rows if not r.get("revoked_at")]
    agents_enrolled = len({r["agent_id"] for r in enrolled_rows} | live_ids)
    offline_since: str | None = None
    offline_alarm = False
    if agents_enrolled > 0 and agents_connected == 0:
        contacts = [
            ts
            for r in enrolled_rows
            for ts in (
                _parse_ts(r.get("disconnected_at")),
                _parse_ts(r.get("last_heartbeat")),
                _parse_ts(r.get("connected_at")),
            )
            if ts is not None
        ]
        last_contact = max(contacts) if contacts else None
        if last_contact is not None:
            offline_since = last_contact.strftime("%Y-%m-%dT%H:%M:%SZ")
        started = getattr(registry, "started_at", None) if registry else None
        listening_since = started if isinstance(started, datetime) else None
        quiet_from = max(
            (t for t in (last_contact, listening_since) if t is not None),
            default=None,
        )
        offline_alarm = (
            quiet_from is None
            or (datetime.now(UTC) - quiet_from).total_seconds() >= FLEET_OFFLINE_GRACE_SECONDS
        )

    # ── Address clashes (lane B2) ────────────────────────────────────────────
    # Two machines configured onto one address, or one holding an address
    # configured on another. Computed from the address table the inventory
    # reconciler keeps, so it is as fresh as the last sync - the live check is
    # the start precondition's job, this one is the standing warning.
    address_conflicts = [c.as_dict() for c in find_conflicts(await repo.list_address_rows())]

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

    # ── Updates available (review A+) ────────────────────────────────────────
    # The release check's verdict, through the same function GET
    # /dashboard/updates and get_update_status answer from - the Overview's one
    # "X.Y.Z is available" line and those two cannot disagree. A settings-row
    # read: no network on this path.
    from ..updates.checker import update_status

    updates = await update_status(repo)

    return {
        "updates": updates,
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
            "by_env": by_env,
            # Templates are not in any number above; this says how many were
            # left out, so "12 hosts" and the 14 rows Proxmox lists reconcile.
            "templates": templates,
            "self_guest": self_guest,
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
            # Sums to `unknown`; see UNKNOWN_REASON_BUCKETS.
            "unknown_by_reason": unknown_by_reason,
        },
        "artifacts": artifacts_by_status,
        "tasks": tasks_by_status,
        "addresses": {"conflicts": address_conflicts},
        "agents": {
            "known": agents_known,
            "connected": agents_connected,
            "enrolled": agents_enrolled,
            "offline_since": offline_since,
            "offline_alarm": offline_alarm,
        },
        # Deliberately NOT counting metric rows or distinct series here: the
        # metrics table is the one table that grows without an operator doing
        # anything, and a COUNT(DISTINCT ...) over it would put a full scan on
        # the page every operator opens first. alert_state has one row per
        # (rule, host) and is cheap.
        "metrics": {
            # The SAME predicate `list_firing` uses (#714): the count on the
            # Overview and the list behind it must not disagree about what is
            # firing, and a disabled rule's alert is not firing.
            "firing_alerts": (
                await db.fetchone(
                    "SELECT COUNT(*) c FROM alert_state s JOIN alert_rules r "
                    "ON r.id = s.rule_id WHERE s.firing_since IS NOT NULL AND r.enabled = 1"
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
            # A rule the evaluator has not looked at yet (`last_eval_at` NULL -
            # created or seeded since boot; the first pass comes a minute after
            # it) is not known to watch nothing. Settings already says "not
            # evaluated yet" for it; counting it here put a false "watches no
            # host" line on the Overview after every upgrade that seeds a rule.
            "rules_watching_nothing": (
                await db.fetchone(
                    "SELECT COUNT(*) c FROM alert_rules "
                    "WHERE enabled = 1 AND last_eval_at IS NOT NULL "
                    "AND COALESCE(hosts_matched, 0) = 0"
                )
            )["c"],
            # The `nodata` share of the above (#660). Its candidates are enrolled
            # hosts, not hosts with samples, so "matches no host" has a different
            # cause and a different fix - no matching host has an agent - and the
            # Overview must not tell the operator to check a metric name it has
            # not got.
            "nodata_rules_watching_nothing": (
                await db.fetchone(
                    "SELECT COUNT(*) c FROM alert_rules "
                    "WHERE enabled = 1 AND last_eval_at IS NOT NULL "
                    "AND COALESCE(hosts_matched, 0) = 0 AND kind = 'nodata'"
                )
            )["c"],
            # Resolved, not read off boot Settings: the number the overview
            # states is the horizon actually in force (#553 C2).
            "retention_days": await effective("metrics_retention_days", get_settings()),
        },
    }
