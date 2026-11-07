from __future__ import annotations

import operator
import re
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from fnmatch import fnmatchcase
from typing import Any

from ..db.connection import Database

# Upper bound on the points ONE series query returns. A 7-day window at the
# agent's default 60s cadence is ~10k points; a browser cannot draw them and a
# JSON response should not carry them, so the API returns the most recent
# MAX_SERIES_POINTS and says `truncated: true` rather than silently thinning
# the data (a downsample the caller did not ask for is a lie about the series).
MAX_SERIES_POINTS = 2000

# A metric name is a storage key and an alert-rule join key: keep it to the
# shape the agent emits (`disk.free_gb`, `load.1m`) so a malformed frame cannot
# fill the table with junk series.
METRIC_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)*$")
MAX_METRIC_NAME_LEN = 64

# Exactly what a shipped hp-agent emits, in the order agent/go/metrics.go
# builds them. This is the metric vocabulary of the whole product: a rule on
# anything else can never fire, and every surface that offers an EXAMPLE metric
# must take it from here. `agent/go/metrics.go` is the source of truth and
# tests/test_metrics_vocabulary.py fails if the two drift.
AGENT_METRICS: tuple[str, ...] = (
    "cpu.count",
    "disk.total_gb",
    "disk.free_gb",
    "memory.total_gb",
    "memory.free_gb",
    "load.1m",
    "load.5m",
    "load.15m",
)

VALID_COMPARISONS = ("gt", "gte", "lt", "lte")

# What kind of condition a rule is (#660).
#
# * ``threshold`` - a metric crossing a value for ``for_seconds``. A host that
#   stops reporting drops out of its candidate set by design: no data is not a
#   breach.
# * ``nodata`` - an ENROLLED host that has reported nothing at all for
#   ``for_seconds``. Its candidate set is the inventory's enrolled hosts, not
#   the metrics table, which is the only way silence can be noticed. It has no
#   metric, comparison or threshold of its own; they are stored as the fixed
#   placeholders below so the columns stay NOT NULL and nothing can mistake
#   them for a real condition.
KIND_THRESHOLD = "threshold"
KIND_NODATA = "nodata"
RULE_KINDS = (KIND_THRESHOLD, KIND_NODATA)
NODATA_METRIC = "*"
_NODATA_COMPARISON = "gt"
_NODATA_THRESHOLD = 0.0

# The shortest silence a nodata rule may wait for. The agent reports every 60 s
# by default; anything under two cadences would fire on one late frame, and an
# alert that cries wolf on a jittery link is the alert people mute.
NODATA_MIN_SECONDS = 120

# Comparison name -> SQL operator. A fixed table, never interpolated from caller
# input: the name is looked up here and a miss raises, so no rule can put text
# into a query.
_SQL_COMPARISONS = {"gt": ">", "gte": ">=", "lt": "<", "lte": "<="}

# The same comparisons in Python, for the in-process half of an evaluation. Both
# tables live here so the SQL and the Python can never mean different things.
COMPARISON_FUNCS: dict[str, Callable[[float, float], bool]] = {
    "gt": operator.gt,
    "gte": operator.ge,
    "lt": operator.lt,
    "lte": operator.le,
}


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class MetricsRepository:
    """Storage for metric samples, alert rules and per-(rule, host) alert state."""

    def __init__(self, db: Database):
        self.db = db

    # ── Samples ──────────────────────────────────────────────────────────────
    async def insert_samples(
        self,
        hostname: str,
        agent_id: str,
        samples: list[tuple[str, int, float]],
    ) -> int:
        """Insert a batch of ``(metric, ts, value)`` rows for one host.

        One executemany inside one transaction: a 60s cadence across a fleet is
        a write per host per minute, and per-row commits would be the dominant
        cost. INSERT OR REPLACE because (hostname, metric, ts) is the identity of
        a sample - an agent that re-sends an unacked batch must not create a
        second point at the same instant."""
        if not samples:
            return 0
        rows = [(hostname, metric, ts, value, agent_id) for metric, ts, value in samples]
        await self.db.conn.executemany(
            "INSERT OR REPLACE INTO metrics (hostname, metric, ts, value, agent_id) "
            "VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        await self.db.conn.commit()
        return len(rows)

    async def series(
        self,
        hostname: str,
        metric: str,
        since_ts: int,
        until_ts: int | None = None,
        limit: int = MAX_SERIES_POINTS,
    ) -> tuple[list[dict[str, Any]], bool]:
        """The most recent ``limit`` points of one series, oldest-first.

        Returns ``(points, truncated)``. The scan walks the primary key backwards
        over the (hostname, metric, ts) prefix, so it reads only this series'
        rows and needs no sort - one extra row is fetched purely to tell whether
        anything was left behind."""
        params: list[Any] = [hostname, metric, since_ts]
        sql = "SELECT ts, value FROM metrics WHERE hostname = ? AND metric = ? AND ts >= ?"
        if until_ts is not None:
            sql += " AND ts <= ?"
            params.append(until_ts)
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(limit + 1)
        rows = await self.db.fetchall(sql, tuple(params))
        truncated = len(rows) > limit
        if truncated:
            rows = rows[:limit]
        points = [{"ts": int(r["ts"]), "value": float(r["value"])} for r in reversed(rows)]
        return points, truncated

    async def latest(self, hostname: str) -> list[dict[str, Any]]:
        """The newest value of every metric this host has reported.

        ``value`` is a bare column beside ``MAX(ts)``: SQLite documents that such
        a column is taken from the row the min/max came from, so this is the
        newest sample rather than an arbitrary one."""
        rows = await self.db.fetchall(
            "SELECT metric, MAX(ts) AS ts, value FROM metrics WHERE hostname = ? GROUP BY metric",
            (hostname,),
        )
        return [
            {"metric": str(r["metric"]), "ts": int(r["ts"]), "value": float(r["value"])}
            for r in sorted(rows, key=lambda r: str(r["metric"]))
        ]

    async def hosts_reporting(self, metric: str, since_ts: int) -> list[str]:
        """Hosts that reported ``metric`` at or after ``since_ts`` - the candidate
        set an alert rule has to evaluate."""
        rows = await self.db.fetchall(
            "SELECT DISTINCT hostname FROM metrics WHERE metric = ? AND ts >= ? ORDER BY hostname",
            (metric, since_ts),
        )
        return [str(r["hostname"]) for r in rows]

    async def breach_window(
        self,
        hostname: str,
        metric: str,
        since_ts: int,
        comparison: str,
        threshold: float,
    ) -> dict[str, Any] | None:
        """Everything an alert rule needs about one host+metric, as aggregates.

        Returns ``None`` when the host has never reported the metric, else
        ``{newest_ts, newest_value, breach_start}`` where ``breach_start`` is the
        timestamp at which the CURRENT uninterrupted breach began (``None`` when
        the newest sample is within limits).

        Aggregates rather than "fetch the window and loop" on purpose: the series
        API caps how many points it will return, and a rule whose duration holds
        more samples than that cap would silently stop being able to fire. Each
        query here is a range over the (hostname, metric, ts) key prefix.
        """
        op = _SQL_COMPARISONS[comparison]
        newest = await self.db.fetchone(
            "SELECT ts, value FROM metrics WHERE hostname = ? AND metric = ? "
            "ORDER BY ts DESC LIMIT 1",
            (hostname, metric),
        )
        if newest is None:
            return None
        out: dict[str, Any] = {
            "newest_ts": int(newest["ts"]),
            "newest_value": float(newest["value"]),
            "breach_start": None,
        }
        if not COMPARISON_FUNCS[comparison](out["newest_value"], threshold):
            return out

        # The last sample that did NOT breach bounds the current breach.
        last_ok = await self.db.fetchone(
            f"SELECT MAX(ts) AS t FROM metrics WHERE hostname = ? AND metric = ? "
            f"AND ts >= ? AND NOT (value {op} ?)",
            (hostname, metric, since_ts, threshold),
        )
        floor_ts = since_ts if last_ok is None or last_ok["t"] is None else int(last_ok["t"]) + 1
        start = await self.db.fetchone(
            f"SELECT MIN(ts) AS t FROM metrics WHERE hostname = ? AND metric = ? "
            f"AND ts >= ? AND value {op} ?",
            (hostname, metric, floor_ts, threshold),
        )
        out["breach_start"] = None if start is None or start["t"] is None else int(start["t"])
        return out

    async def prune(self, older_than_ts: int) -> int:
        """Delete every sample older than ``older_than_ts``; returns the row count.

        Served by idx_metrics_ts - the primary key orders by hostname first, so
        an age-based delete would otherwise scan the whole table."""
        cursor = await self.db.execute("DELETE FROM metrics WHERE ts < ?", (older_than_ts,))
        deleted = cursor.rowcount or 0
        await self.db.conn.commit()
        return int(deleted)

    # ── Alert rules ──────────────────────────────────────────────────────────
    async def create_rule(
        self,
        name: str,
        metric: str,
        comparison: str,
        threshold: float,
        for_seconds: int = 300,
        host_filter: str = "*",
        enabled: bool = True,
        kind: str = KIND_THRESHOLD,
    ) -> dict[str, Any]:
        if kind not in RULE_KINDS:
            raise ValueError(f"Invalid rule kind {kind!r}. Must be one of {RULE_KINDS}")
        if kind == KIND_NODATA:
            if for_seconds < NODATA_MIN_SECONDS:
                raise ValueError(
                    f"a nodata rule must wait at least {NODATA_MIN_SECONDS} seconds: the "
                    "agent reports once a minute, so anything shorter fires on one late frame"
                )
            metric = NODATA_METRIC
            comparison = _NODATA_COMPARISON
            threshold = _NODATA_THRESHOLD
        if comparison not in VALID_COMPARISONS:
            raise ValueError(
                f"Invalid comparison {comparison!r}. Must be one of {VALID_COMPARISONS}"
            )
        if for_seconds < 0:
            raise ValueError("for_seconds must not be negative")
        rule_id = str(uuid.uuid4())
        ts = _now()
        await self.db.execute(
            """INSERT INTO alert_rules
                 (id, name, host_filter, metric, comparison, threshold, for_seconds,
                  enabled, created_at, updated_at, kind)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                rule_id,
                name,
                host_filter or "*",
                metric,
                comparison,
                float(threshold),
                int(for_seconds),
                int(enabled),
                ts,
                ts,
                kind,
            ),
        )
        await self.db.conn.commit()
        rule = await self.get_rule(rule_id)
        if rule is None:
            # A raise, not an assert: `python -O` strips asserts, and this one
            # narrows a type. Under it the function would return None from
            # something declared to return a dict, surfacing later as a confusing
            # TypeError instead of naming the row that vanished between the write
            # and the read (#481; same reasoning as #326).
            raise RuntimeError(f"alert rule {rule_id} disappeared immediately after being written")
        return rule

    async def get_rule(self, rule_id: str) -> dict[str, Any] | None:
        return await self.db.fetchone("SELECT * FROM alert_rules WHERE id = ?", (rule_id,))

    async def list_rules(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM alert_rules"
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY created_at"
        return await self.db.fetchall(sql)

    async def delete_rule(self, rule_id: str) -> bool:
        cursor = await self.db.execute("DELETE FROM alert_rules WHERE id = ?", (rule_id,))
        deleted = (cursor.rowcount or 0) > 0
        # The rule's state rows would otherwise outlive it and be reported as
        # firing by /alerts forever.
        await self.db.execute("DELETE FROM alert_state WHERE rule_id = ?", (rule_id,))
        await self.db.conn.commit()
        return deleted

    # Columns a rule can be retuned in place. `name` and the whole condition
    # (metric, comparison, threshold, for_seconds, host_filter) plus `enabled` -
    # everything create_rule sets except the immutable id and timestamps. An
    # operator who wants a different threshold must not have to delete and
    # recreate the rule (and lose its firing state with it).
    _UPDATABLE_COLUMNS = (
        "name",
        "metric",
        "comparison",
        "threshold",
        "for_seconds",
        "host_filter",
        "enabled",
    )

    async def update_rule(self, rule_id: str, **fields: Any) -> dict[str, Any] | None:
        """Update only the columns named in ``fields``; leave the rest untouched.

        Returns the updated rule, or None if no rule has that id. An empty
        ``fields`` is a no-op that still returns the (unchanged) rule, so a
        caller can PATCH with a subset - or nothing - without special-casing it."""
        unknown = set(fields) - set(self._UPDATABLE_COLUMNS)
        if unknown:
            raise ValueError(f"Cannot update unknown rule field(s): {', '.join(sorted(unknown))}")
        if "comparison" in fields and fields["comparison"] not in VALID_COMPARISONS:
            raise ValueError(
                f"Invalid comparison {fields['comparison']!r}. Must be one of {VALID_COMPARISONS}"
            )
        if "for_seconds" in fields and int(fields["for_seconds"]) < 0:
            raise ValueError("for_seconds must not be negative")

        if not fields:
            return await self.get_rule(rule_id)

        current = await self.get_rule(rule_id)
        if current is None:
            return None
        if str(current.get("kind") or KIND_THRESHOLD) == KIND_NODATA:
            # A nodata rule has no metric, comparison or threshold: accepting
            # one would store a condition that is never evaluated - the exact
            # "listed, enabled, guarding nothing" shape #648 removed.
            meaningless = sorted(set(fields) & {"metric", "comparison", "threshold"})
            if meaningless:
                raise ValueError(
                    f"a nodata rule has no {', '.join(meaningless)}: it fires when a host "
                    "reports nothing at all; change its duration or host filter instead"
                )
            if "for_seconds" in fields and int(fields["for_seconds"]) < NODATA_MIN_SECONDS:
                raise ValueError(f"a nodata rule must wait at least {NODATA_MIN_SECONDS} seconds")

        # Coerce to the stored column types so a JSON int threshold or a bool
        # enabled land the same way create_rule writes them.
        coercers: dict[str, Any] = {
            "threshold": float,
            "for_seconds": int,
            "enabled": lambda v: int(bool(v)),
        }
        assignments = []
        values: list[Any] = []
        for column in self._UPDATABLE_COLUMNS:
            if column in fields:
                assignments.append(f"{column} = ?")
                coerce = coercers.get(column, lambda v: v)
                values.append(coerce(fields[column]))
        assignments.append("updated_at = ?")
        values.append(_now())
        values.append(rule_id)

        # columns are the fixed _UPDATABLE_COLUMNS allowlist, never caller input;
        # every value is a bound ? parameter - no injection vector.
        query = f"UPDATE alert_rules SET {', '.join(assignments)} WHERE id = ?"  # nosec B608
        cursor = await self.db.execute(query, tuple(values))
        if (cursor.rowcount or 0) == 0:
            await self.db.conn.commit()
            return None
        await self._drop_unrevisited_state(rule_id, fields)
        await self.db.conn.commit()
        return await self.get_rule(rule_id)

    async def _drop_unrevisited_state(self, rule_id: str, fields: dict[str, Any]) -> None:
        """Clear alert state the evaluator will never look at again (#714).

        The evaluator only revisits (enabled rule, matching host) pairs, so a
        firing row it will not revisit stays firing FOREVER: the Overview
        counts it and the attention list names it, for a rule an operator has
        switched off or a host they have taken out of its scope. So:

        * disabling a rule drops all of its state - it is not watching;
        * changing the metric drops all of it - every row was a verdict about
          a different quantity;
        * narrowing the host filter drops the rows for hosts now outside it.

        A retuned threshold or duration keeps its state on purpose: the same
        hosts are still being watched, and the next evaluation re-decides
        them without losing when a real breach began.
        """
        if ("enabled" in fields and not fields["enabled"]) or "metric" in fields:
            await self.db.execute("DELETE FROM alert_state WHERE rule_id = ?", (rule_id,))
            return
        if "host_filter" in fields:
            host_filter = str(fields["host_filter"] or "*")
            if host_filter == "*":
                return
            rows = await self.db.fetchall(
                "SELECT hostname FROM alert_state WHERE rule_id = ?", (rule_id,)
            )
            outside = [
                str(r["hostname"]) for r in rows if not fnmatchcase(str(r["hostname"]), host_filter)
            ]
            for hostname in outside:
                await self.db.execute(
                    "DELETE FROM alert_state WHERE rule_id = ? AND hostname = ?",
                    (rule_id, hostname),
                )

    async def set_rule_enabled(self, rule_id: str, enabled: bool) -> bool:
        """Thin wrapper over update_rule for the enable/silence-only path."""
        return await self.update_rule(rule_id, enabled=enabled) is not None

    async def record_rule_coverage(self, rule_id: str, hosts_matched: int) -> None:
        """Stamp what the last evaluation of this rule actually looked at.

        Written every cycle, including - especially - when the answer is zero.
        A rule matching no host is the failure mode this exists to surface: it
        is enabled, it is listed, and it is guarding nothing, and before these
        columns the product had no way to say so. Not routed through
        ``update_rule``: this is bookkeeping about a rule, not a change to it,
        and it must not move ``updated_at``."""
        await self.db.execute(
            "UPDATE alert_rules SET last_eval_at = ?, hosts_matched = ? WHERE id = ?",
            (_now(), int(hosts_matched), rule_id),
        )
        await self.db.conn.commit()

    async def clear_alert_state_for_host(self, hostname: str) -> int:
        """Drop every alert-state row for one host; returns how many went.

        For a host being forgotten. Its state rows would otherwise outlive it
        AND never be revisited - the evaluator only ever looks at hosts that
        reported recently, so a host that is gone is never evaluated again and a
        latched ``firing_since`` stays latched forever. The Overview counts it,
        the attention list names a machine that is no longer in inventory, and
        the only way to clear it is to delete the rule (which stops watching
        every other host too)."""
        cursor = await self.db.execute("DELETE FROM alert_state WHERE hostname = ?", (hostname,))
        await self.db.conn.commit()
        return int(cursor.rowcount or 0)

    # ── Alert state ──────────────────────────────────────────────────────────
    async def get_alert_state(self, rule_id: str, hostname: str) -> dict[str, Any] | None:
        return await self.db.fetchone(
            "SELECT * FROM alert_state WHERE rule_id = ? AND hostname = ?",
            (rule_id, hostname),
        )

    async def set_alert_state(
        self,
        rule_id: str,
        hostname: str,
        firing_since: str | None,
        last_value: float | None,
    ) -> None:
        await self.db.execute(
            """INSERT INTO alert_state (rule_id, hostname, firing_since, last_value, last_eval)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(rule_id, hostname) DO UPDATE SET
                 firing_since=excluded.firing_since,
                 last_value=excluded.last_value,
                 last_eval=excluded.last_eval""",
            (rule_id, hostname, firing_since, last_value, _now()),
        )
        await self.db.conn.commit()

    async def list_firing(self) -> list[dict[str, Any]]:
        """Every alert currently firing, joined to the rule that raised it."""
        return await self.db.fetchall(
            """SELECT s.rule_id, s.hostname, s.firing_since, s.last_value, s.last_eval,
                      r.name, r.metric, r.comparison, r.threshold, r.for_seconds, r.kind
                 FROM alert_state s JOIN alert_rules r ON r.id = s.rule_id
                WHERE s.firing_since IS NOT NULL AND r.enabled = 1
                ORDER BY s.firing_since"""
        )

    async def firing_for_rule(self, rule_id: str) -> list[dict[str, Any]]:
        """This rule's latched (firing) rows, whatever host they are for."""
        return await self.db.fetchall(
            "SELECT hostname, firing_since, last_value FROM alert_state "
            "WHERE rule_id = ? AND firing_since IS NOT NULL",
            (rule_id,),
        )

    # ── Enrolled hosts (the nodata candidate set) ────────────────────────────
    async def enrolled_hosts(self) -> list[dict[str, Any]]:
        """Every host with an enrolled, unrevoked agent, and when it was last heard.

        The candidate set of a ``nodata`` rule (#660). Deliberately NOT "hosts
        that reported recently" - that is the threshold rules' candidate set, and
        it is precisely why a silent host could never alert: it had left the set
        before anyone looked.

        Joined through ``hosts.agent_id`` (the inventory's link to the agent it
        believes is that machine), so a reinstalled agent's stale row does not
        count twice and a host that never had an agent is not here at all: that
        is a coverage gap, not an outage. ``last_heartbeat`` moves on every
        heartbeat AND every metrics frame, so it is "last heard from" whether
        or not this agent samples metrics. ``pve_status`` lets the evaluator tell
        a VM somebody stopped from a VM that went dark."""
        rows = await self.db.fetchall(
            """SELECT a.hostname AS hostname, a.agent_id AS agent_id,
                      a.last_heartbeat AS last_heartbeat, a.connected_at AS connected_at,
                      a.first_seen AS first_seen, h.pve_status AS pve_status
                 FROM agents a JOIN hosts h ON h.agent_id = a.agent_id
                WHERE a.revoked_at IS NULL AND a.hostname IS NOT NULL AND a.hostname != ''
                ORDER BY a.hostname"""
        )
        return [dict(r) for r in rows]

    # ── Alert history (#660) ─────────────────────────────────────────────────
    async def record_alert_event(
        self,
        *,
        rule: dict[str, Any],
        hostname: str,
        event: str,
        value: float | None,
        since: str | None,
        at: str,
        duration_seconds: int | None = None,
        reason: str | None = None,
    ) -> int:
        """Append one transition to the history; returns its id.

        Called on the two transitions the evaluator already notifies on - never
        per evaluation - so it costs one insert per fire and one per recovery."""
        if event not in ("firing", "resolved"):
            raise ValueError(f"unknown alert event {event!r}")
        cursor = await self.db.execute(
            """INSERT INTO alert_events
                 (rule_id, rule_name, rule_kind, metric, hostname, event, value, since,
                  duration_seconds, reason, at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(rule["id"]),
                str(rule["name"]),
                str(rule.get("kind") or KIND_THRESHOLD),
                str(rule["metric"]),
                hostname,
                event,
                value,
                since,
                duration_seconds,
                reason,
                at,
            ),
        )
        await self.db.conn.commit()
        return int(cursor.lastrowid or 0)

    async def set_alert_event_delivery(self, event_id: int, delivered_to: list[str]) -> None:
        """Record which channels accepted this event (empty list = none did)."""
        import json

        await self.db.execute(
            "UPDATE alert_events SET delivered_to = ? WHERE id = ?",
            (json.dumps(sorted(delivered_to)), event_id),
        )
        await self.db.conn.commit()

    async def list_alert_events(
        self, since: str, hostname: str | None = None, limit: int = 200
    ) -> list[dict[str, Any]]:
        """Transitions at or after ``since`` (ISO), newest first."""
        import json

        sql = "SELECT * FROM alert_events WHERE at >= ?"
        params: list[Any] = [since]
        if hostname:
            sql += " AND hostname = ?"
            params.append(hostname)
        # Insertion order, not `at`: the table is append-only so the id IS the
        # order things happened in, while `at` is a wall-clock reading that an
        # NTP step can move backwards between a fire and its recovery.
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        rows = await self.db.fetchall(sql, tuple(params))
        out = []
        for r in rows:
            item = dict(r)
            raw = item.get("delivered_to")
            try:
                item["delivered_to"] = json.loads(raw) if raw else None
            except (TypeError, ValueError):
                item["delivered_to"] = None
            out.append(item)
        return out
