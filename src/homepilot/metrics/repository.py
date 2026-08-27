from __future__ import annotations

import operator
import re
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
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

VALID_COMPARISONS = ("gt", "gte", "lt", "lte")

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
    ) -> dict[str, Any]:
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
                  enabled, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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

        cursor = await self.db.execute(
            f"UPDATE alert_rules SET {', '.join(assignments)} WHERE id = ?",
            tuple(values),
        )
        await self.db.conn.commit()
        if (cursor.rowcount or 0) == 0:
            return None
        return await self.get_rule(rule_id)

    async def set_rule_enabled(self, rule_id: str, enabled: bool) -> bool:
        """Thin wrapper over update_rule for the enable/silence-only path."""
        return await self.update_rule(rule_id, enabled=enabled) is not None

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
                      r.name, r.metric, r.comparison, r.threshold, r.for_seconds
                 FROM alert_state s JOIN alert_rules r ON r.id = s.rule_id
                WHERE s.firing_since IS NOT NULL
                ORDER BY s.firing_since"""
        )
