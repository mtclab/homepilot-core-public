"""The Overview's attention rules that need the server's knowledge (3.12).

Owner, on prod 3.11.0: "the overview is a bit noisy" - 14 attention lines, 3 of
them decisions. Most of the noise came from rules the browser could not apply
well because it saw only a slice of the data: eight failed runs that later runs
had already superseded (it read the 50 most recent tasks and said so in a
plumbing note), seven separate "gone" lines, and no way to put a line off.

So the rules that need the whole task table, the hub registry or the snooze
table live here, and BOTH surfaces read them from ``build_summary``: the
console's Overview and an assistant's ``get_dashboard_summary`` cannot disagree
about what needs the operator. Wording and layout stay in the web.

Each line this module returns carries a stable ``key`` and a ``severity``; a
line an operator put off ("Not now, 7 days") is moved to ``put_off`` while its
snooze holds. Keys of the lines the browser still builds from other reads
(alerts, drift, enrolment requests...) follow the same snooze rule there
(``web/src/lib/attention.ts`` ``snoozeHolds``, which MUST equal ``snooze_holds``
below).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

# A failed run is news for a day. Past that it is still in Records, but it is
# no longer something the operator is asked about on the first screen.
FAILED_RUN_WINDOW = timedelta(hours=24)
# How far back the footer counts the failed runs it left out ("8 failed runs
# ... are in Records"). Older ones are simply history.
LEFT_OUT_WINDOW = timedelta(days=7)
# A run still pending or running after this is stuck, not busy; after an hour
# it is the more serious kind of stuck.
STUCK_AFTER = timedelta(minutes=15)
STUCK_CRITICAL_AFTER = timedelta(hours=1)
# "Not now, 7 days."
SNOOZE_DAYS = 7

SEVERITIES = ("notice", "warning", "critical")
_RANK = {name: i for i, name in enumerate(SEVERITIES)}

# Which later successes settle a failure: a failed apply is settled by the apply
# or replay that then worked, or by revoking the change outright. Any other
# action only by itself. (Moved here from the Overview's browser code.)
_SETTLED_BY: dict[str, tuple[str, ...]] = {
    "apply": ("apply", "replay", "revoke"),
    "replay": ("apply", "replay", "revoke"),
}

GONE_KEY = "gone:machines"


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def snooze_holds(snooze: dict[str, Any] | None, severity: str, now: datetime) -> bool:
    """Does this snooze still hide a line of this severity?

    Until its `until`, and only while the line is no MORE serious than when it
    was put off: a warning put off for a week that turns critical shows at once.
    The same rule as ``snoozeHolds`` in web/src/lib/attention.ts.
    """
    if not snooze:
        return False
    until = _parse(snooze.get("until"))
    if until is None or now >= until:
        return False
    return _RANK.get(severity, len(SEVERITIES)) <= _RANK.get(str(snooze.get("severity")), -1)


async def active_snoozes(db: Any, now: datetime) -> dict[str, dict[str, Any]]:
    """Every snooze whose time has not run out, by key."""
    rows = await db.fetchall(
        "SELECT key, severity, until, snoozed_by, snoozed_at FROM attention_snoozes "
        "WHERE until > ? ORDER BY until",
        (_iso(now),),
    )
    return {
        str(r["key"]): {
            "key": str(r["key"]),
            "severity": str(r["severity"]),
            "until": str(r["until"]),
            "by": str(r["snoozed_by"]),
            "at": str(r["snoozed_at"]),
        }
        for r in rows
    }


async def snooze(
    repo: Any, key: str, severity: str, actor: str, days: int = SNOOZE_DAYS
) -> dict[str, Any]:
    """Put one line off for `days`, journaled. Snoozing again restarts the week."""
    if severity not in _RANK:
        raise ValueError(f"severity must be one of {', '.join(SEVERITIES)}")
    now = datetime.now(UTC)
    row = {
        "key": key,
        "severity": severity,
        "until": _iso(now + timedelta(days=days)),
        "by": actor,
        "at": _iso(now),
    }
    await repo.db.execute(
        "INSERT INTO attention_snoozes (key, severity, until, snoozed_by, snoozed_at) "
        "VALUES (?, ?, ?, ?, ?) ON CONFLICT(key) DO UPDATE SET severity = excluded.severity, "
        "until = excluded.until, snoozed_by = excluded.snoozed_by, "
        "snoozed_at = excluded.snoozed_at",
        (key, severity, row["until"], actor, row["at"]),
    )
    await repo.db.conn.commit()
    await repo.log_audit(
        user_id=actor,
        source="api",
        action="attention_snoozed",
        details_json=json.dumps({"key": key, "severity": severity, "until": row["until"]}),
    )
    return row


async def unsnooze(repo: Any, key: str, actor: str) -> bool:
    """Bring a put-off line back now. False when nothing was put off under `key`."""
    cursor = await repo.db.execute("DELETE FROM attention_snoozes WHERE key = ?", (key,))
    await repo.db.conn.commit()
    if not cursor.rowcount:
        return False
    await repo.log_audit(
        user_id=actor,
        source="api",
        action="attention_unsnoozed",
        details_json=json.dumps({"key": key}),
    )
    return True


def _task_host(task: dict[str, Any]) -> dict[str, Any]:
    """What an artifactless task (an agent install) recorded about its host."""
    try:
        parsed = json.loads(task.get("result_json") or "null")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _subject(task: dict[str, Any]) -> str | None:
    """The thing a task acted on, or None when it cannot be told."""
    if task.get("artifact_id"):
        return f"artifact:{task['artifact_id']}"
    host_id = _task_host(task).get("host_id")
    return f"host:{host_id}" if host_id else None


def _settled(
    task: dict[str, Any], successes: list[dict[str, Any]], live_host_ids: set[str]
) -> bool:
    """A failure something later made moot.

    A later task on the same subject succeeded, or - for an agent install - the
    host's agent is connected now, which is the very thing the install was for.
    A failure whose subject cannot be told is never settled by guesswork.
    """
    subject = _subject(task)
    if subject is None:
        return False
    actions = _SETTLED_BY.get(str(task["action"]), (str(task["action"]),))
    failed_at = str(task["created_at"])
    if any(
        o["action"] in actions and _subject(o) == subject and str(o["created_at"]) > failed_at
        for o in successes
    ):
        return True
    if task["action"] == "install_agent":
        return str(_task_host(task).get("host_id") or "") in live_host_ids
    return False


def _run_line(task: dict[str, Any], key: str, severity: str) -> dict[str, Any]:
    host = _task_host(task)
    return {
        "key": key,
        "severity": severity,
        "task_id": task["id"],
        "action": task["action"],
        "status": task["status"],
        "artifact_id": task.get("artifact_id"),
        "host_id": host.get("host_id"),
        "hostname": host.get("hostname"),
        "error": task.get("error"),
        "created_at": task["created_at"],
        "finished_at": task.get("finished_at"),
    }


async def _runs(
    db: Any, now: datetime, live_host_ids: set[str]
) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    """(failed run lines, what was left out, stuck run lines) over the whole table."""
    since = _iso(now - LEFT_OUT_WINDOW)
    failed = [
        dict(r)
        for r in await db.fetchall(
            "SELECT * FROM tasks WHERE status = 'failed' AND created_at >= ? "
            "ORDER BY created_at DESC",
            (since,),
        )
    ]
    successes = (
        [
            dict(r)
            for r in await db.fetchall(
                "SELECT id, artifact_id, action, created_at, result_json FROM tasks "
                "WHERE status = 'succeeded' AND created_at >= ?",
                (since,),
            )
        ]
        if failed
        else []
    )
    news_from = _iso(now - FAILED_RUN_WINDOW)
    lines: list[dict[str, Any]] = []
    superseded = older = 0
    for task in failed:
        if _settled(task, successes, live_host_ids):
            superseded += 1
        elif str(task.get("finished_at") or task["created_at"]) < news_from:
            older += 1
        else:
            lines.append(_run_line(task, f"task:{task['id']}", "critical"))
    left_out = {"superseded": superseded, "older": older, "since": since}

    stuck_before = _iso(now - STUCK_AFTER)
    critical_before = _iso(now - STUCK_CRITICAL_AFTER)
    stuck = [
        _run_line(
            dict(r),
            f"stuck:{r['id']}",
            "critical" if str(r["created_at"]) < critical_before else "warning",
        )
        for r in await db.fetchall(
            "SELECT * FROM tasks WHERE status IN ('pending', 'running') AND created_at < ? "
            "ORDER BY created_at",
            (stuck_before,),
        )
    ]
    return lines, left_out, stuck


async def _gone(db: Any) -> list[dict[str, Any]]:
    """Machines Proxmox no longer reports, with the Hosts page's membership rule.

    MUST equal ``attentionReason(h) === 'gone'`` (web/src/lib/hostGroups.ts): a
    template, or a guest nobody adopted (pending / ignored), is not news.
    """
    rows = await db.fetchall(
        "SELECT id, hostname, absent_since FROM hosts WHERE absent_since IS NOT NULL "
        "AND is_template = 0 AND COALESCE(import_state, '') NOT IN ('pending', 'ignored') "
        "ORDER BY absent_since, hostname"
    )
    return [
        {"id": str(r["id"]), "hostname": str(r["hostname"]), "absent_since": r["absent_since"]}
        for r in rows
    ]


async def outdated_agents(registry: Any, repo: Any) -> list[dict[str, Any]]:
    """Connected agents older than this control plane, or reporting no version.

    The ONE list behind GET /agents/upgrades and the Overview's "N agents are
    older" line, so the line's count is the page's count. ``host_id`` is the
    inventory host Upgrade reinstalls through (None when HomePilot cannot).
    """
    from ..agent_hub.join_requests import install_host_id
    from ..agent_hub.version_skew import control_plane_version, is_behind

    if registry is None or not hasattr(registry, "list_connected"):
        return []
    control = control_plane_version()
    out: list[dict[str, Any]] = []
    for agent in registry.list_connected():
        info = agent.get("system_info") or {}
        version = info.get("agent_version") if isinstance(info, dict) else None
        behind = is_behind(version, control)
        if behind is False:
            continue
        hostname = str(agent.get("hostname") or "")
        out.append(
            {
                "agent_id": agent.get("agent_id"),
                "hostname": hostname,
                "agent_version": version or None,
                "control_version": control,
                "behind": behind,
                "host_id": await install_host_id(repo, hostname) if repo is not None else None,
            }
        )
    return out


async def build_attention(
    repo: Any, registry: Any, live_agent_ids: set[str], now: datetime | None = None
) -> dict[str, Any]:
    """The server's share of the Overview's "Needs you", snoozes applied."""
    from ..agent_hub.version_skew import control_plane_version

    db = repo.db
    now = now or datetime.now(UTC)
    live_host_ids = {
        str(r["id"])
        for r in await db.fetchall("SELECT id, agent_id FROM hosts WHERE agent_id IS NOT NULL")
        if r["agent_id"] in live_agent_ids
    }
    failed, left_out, stuck = await _runs(db, now, live_host_ids)
    gone_hosts = await _gone(db)
    agents = await outdated_agents(registry, repo)
    control = control_plane_version()

    snoozes = await active_snoozes(db, now)
    put_off: list[dict[str, Any]] = []

    def keep(kind: str, line: dict[str, Any]) -> bool:
        held = snoozes.get(line["key"])
        if snooze_holds(held, line["severity"], now):
            put_off.append({"kind": kind, "line": line, "snooze": held})
            return False
        return True

    gone_line = (
        {"key": GONE_KEY, "severity": "warning", "hosts": gone_hosts} if gone_hosts else None
    )
    agents_line = (
        {
            "key": f"agents:behind:{control}",
            "severity": "warning",
            "control_version": control,
            "agents": agents,
        }
        if agents
        else None
    )
    return {
        "failed_runs": [line for line in failed if keep("failed_run", line)],
        "left_out": left_out,
        "stuck_runs": [line for line in stuck if keep("stuck_run", line)],
        "gone": gone_line if gone_line and keep("gone", gone_line) else None,
        "agents_behind": agents_line
        if agents_line and keep("agents_behind", agents_line)
        else None,
        "put_off": put_off,
        "snoozes": list(snoozes.values()),
        "checked_at": _iso(now),
    }
