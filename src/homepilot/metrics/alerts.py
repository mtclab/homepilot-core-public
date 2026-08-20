from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from ..events import emit_event
from ..reconciler.base import Reconciler, ReconcilerResult
from .repository import MetricsRepository

logger = logging.getLogger(__name__)

# How far after the window opened the breach may have started before we refuse
# to claim it held for the whole duration. Without it, a host whose first-ever
# sample happens to breach would look like an uninterrupted breach with one
# point and fire a five-minute rule instantly - the exact behaviour for_seconds
# exists to prevent. Sized at 1.5x the agent's default 60s cadence.
WINDOW_COVERAGE_SLACK_SECONDS = 90

# How far BEFORE the duration window the breach search reaches. A rule whose
# for_seconds is shorter than the sample cadence would otherwise look at a
# window holding no samples at all and could never fire; the extra reach also
# lets a breach that began before the window be recognised as one breach rather
# than as a fresh one.
WINDOW_LOOKBACK_SECONDS = 300

# A rule only STARTS firing on data this fresh. An agent that stopped reporting
# is a connectivity problem, not a threshold breach, and inventing an alert from
# stale numbers is how an alerting system loses its credibility.
SAMPLE_FRESHNESS_SECONDS = 300

# Event names, matching the existing snake_case bus vocabulary
# (artifact_applied, artifact_drifted, ...). Delivery is emit_event's: SSE bus,
# the configured events webhook, and every matching webhook config.
EVENT_FIRING = "alert_firing"
EVENT_RESOLVED = "alert_resolved"


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).timestamp()
    except ValueError:
        return None


def condition_holds(
    breach_start: int | None,
    newest_ts: int,
    window_start: int,
    for_seconds: int,
) -> bool:
    """Whether a breach satisfies a duration condition.

    Two things must both be true:

    * the breach has been running for at least ``for_seconds`` (newest sample
      minus the breach's start) - this is what a spike, and a host that has only
      just started breaching, fail; and
    * it started at or before the window opened, within the coverage slack.

    The second looks redundant, and for a sane clock it is: a duration measured
    between two stored timestamps already implies the breach began before the
    window. It earns its place when a host's clock JUMPS forward mid-breach -
    the two samples then look ``for_seconds`` apart while barely any real time
    passed, and only the window check notices. See
    ``test_a_forward_clock_jump_cannot_manufacture_a_duration``.
    """
    if breach_start is None:
        return False
    held = (newest_ts - breach_start) >= for_seconds
    covered = breach_start <= window_start + WINDOW_COVERAGE_SLACK_SECONDS
    return held and covered


class AlertEvaluator(Reconciler):
    """Evaluates every enabled rule over the stored window and notifies on the
    two transitions that matter: a rule that STARTS firing and one that stops.

    Both go out through ``emit_event`` - the same SSE + webhook machinery
    artifacts already use. There is no second notifier."""

    def __init__(
        self,
        metrics_repo: MetricsRepository,
        repo: Any = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._metrics = metrics_repo
        self._repo = repo
        self._now = now

    async def run(self) -> ReconcilerResult:
        fired = 0
        resolved = 0
        evaluated = 0
        rules = await self._metrics.list_rules(enabled_only=True)
        for rule in rules:
            for hostname in await self._hosts_for(rule):
                evaluated += 1
                transition = await self._evaluate(rule, hostname)
                if transition == "firing":
                    fired += 1
                elif transition == "resolved":
                    resolved += 1
        return ReconcilerResult(
            name="alert_evaluator",
            success=True,
            details={
                "rules": len(rules),
                "evaluated": evaluated,
                "fired": fired,
                "resolved": resolved,
            },
        )

    async def _hosts_for(self, rule: dict[str, Any]) -> list[str]:
        """Hosts this rule applies to.

        A ``*`` filter means every host that has recently reported the metric; an
        exact hostname means that host only. Hosts that never reported the metric
        are not evaluated at all - "no data" is not a breach."""
        window = int(rule["for_seconds"]) + SAMPLE_FRESHNESS_SECONDS
        since = int(self._now() - window)
        reporting = await self._metrics.hosts_reporting(str(rule["metric"]), since)
        host_filter = str(rule["host_filter"] or "*")
        if host_filter == "*":
            return reporting
        return [h for h in reporting if h == host_filter]

    async def _evaluate(self, rule: dict[str, Any], hostname: str) -> str | None:
        now = self._now()
        for_seconds = int(rule["for_seconds"])
        window_start = int(now - for_seconds)
        window = await self._metrics.breach_window(
            hostname,
            str(rule["metric"]),
            since_ts=window_start - WINDOW_LOOKBACK_SECONDS,
            comparison=str(rule["comparison"]),
            threshold=float(rule["threshold"]),
        )
        state = await self._metrics.get_alert_state(str(rule["id"]), hostname)
        firing_since = state.get("firing_since") if state else None

        if window is None:
            # The host has never reported this metric: nothing to assert either
            # way. An already firing alert stays firing - silently resolving on
            # missing data is how an outage comes to look like a recovery.
            return None

        value = float(window["newest_value"])
        newest_ts = int(window["newest_ts"])
        fresh = (now - newest_ts) <= SAMPLE_FRESHNESS_SECONDS
        start = window["breach_start"]

        if start is None:
            # The newest sample is within limits.
            if firing_since and fresh:
                await self._metrics.set_alert_state(str(rule["id"]), hostname, None, value)
                await self._notify_resolved(rule, hostname, value, firing_since)
                return "resolved"
            await self._metrics.set_alert_state(str(rule["id"]), hostname, firing_since, value)
            return None

        if firing_since:
            # Already firing and still breaching: refresh the value, notify
            # nothing. An alert that re-announced itself every minute is one
            # people mute.
            await self._metrics.set_alert_state(str(rule["id"]), hostname, firing_since, value)
            return None
        if fresh and condition_holds(int(start), newest_ts, window_start, for_seconds):
            since = _iso(int(start))
            await self._metrics.set_alert_state(str(rule["id"]), hostname, since, value)
            await self._notify_firing(rule, hostname, value, since)
            return "firing"
        await self._metrics.set_alert_state(str(rule["id"]), hostname, None, value)
        return None

    def _payload(self, rule: dict[str, Any], hostname: str, value: float) -> dict[str, Any]:
        return {
            "rule_id": str(rule["id"]),
            "rule_name": str(rule["name"]),
            "hostname": hostname,
            "metric": str(rule["metric"]),
            "comparison": str(rule["comparison"]),
            "threshold": float(rule["threshold"]),
            "for_seconds": int(rule["for_seconds"]),
            "value": value,
        }

    async def _notify_firing(
        self, rule: dict[str, Any], hostname: str, value: float, since: str
    ) -> None:
        payload = {**self._payload(rule, hostname, value), "since": since}
        logger.warning(
            "alert firing: %s on %s (%s %s %s, value %s, for %ss)",
            rule["name"],
            hostname,
            rule["metric"],
            rule["comparison"],
            rule["threshold"],
            value,
            rule["for_seconds"],
        )
        await emit_event(EVENT_FIRING, payload, repo=self._repo)

    async def _notify_resolved(
        self, rule: dict[str, Any], hostname: str, value: float, firing_since: str
    ) -> None:
        started = _parse_iso(firing_since)
        payload = {
            **self._payload(rule, hostname, value),
            "since": firing_since,
            "resolved_at": _iso(self._now()),
            "duration_seconds": int(self._now() - started) if started else None,
        }
        logger.info("alert resolved: %s on %s (value %s)", rule["name"], hostname, value)
        await emit_event(EVENT_RESOLVED, payload, repo=self._repo)
