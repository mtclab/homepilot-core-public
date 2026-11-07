from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from fnmatch import fnmatchcase
from typing import Any

from ..events import emit_event
from ..reconciler.base import Reconciler, ReconcilerResult
from .repository import KIND_NODATA, MetricsRepository

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


def _humanise(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 90:
        return f"{seconds} s"
    if seconds < 5400:
        return f"{seconds // 60} min"
    if seconds < 172800:
        return f"{seconds // 3600} h"
    return f"{seconds // 86400} d"


# Why a nodata alert resolved without the host speaking again.
REASON_STOPPED = "the hypervisor reports the guest stopped"
REASON_UNENROLLED = "its agent was revoked or forgotten, so it is no longer watched"


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
    artifacts already use - and, when a ``notifier`` is wired, to the native
    notification channels (ntfy). Each transition is also appended to
    ``alert_events`` first, so a fire that resolves leaves a record (#660)."""

    def __init__(
        self,
        metrics_repo: MetricsRepository,
        repo: Any = None,
        now: Callable[[], float] = time.time,
        notifier: Any = None,
    ) -> None:
        self._metrics = metrics_repo
        self._repo = repo
        self._now = now
        self._notifier = notifier
        # Silence is only measured over time THIS evaluator has been watching.
        # After the control plane itself was down for ten minutes, every agent
        # looks ten minutes quiet at the first evaluation - and a "went dark"
        # page for the whole fleet on every restart would be a lie about the
        # hosts that is really a fact about HomePilot.
        self._watching_since = now()

    async def run(self) -> ReconcilerResult:
        fired = 0
        resolved = 0
        evaluated = 0
        watching_nothing = 0
        rules = await self._metrics.list_rules(enabled_only=True)
        for rule in rules:
            if str(rule.get("kind") or "") == KIND_NODATA:
                matched, transitions = await self._run_nodata(rule)
                await self._metrics.record_rule_coverage(str(rule["id"]), matched)
                evaluated += matched
                if not matched:
                    watching_nothing += 1
                fired += transitions.count("firing")
                resolved += transitions.count("resolved")
                continue
            hosts = await self._hosts_for(rule)
            # Recorded every cycle, zero included: a rule that matches no host
            # is enabled, listed and guarding nothing, and this is the only
            # place that knows it (#648 tranche 5).
            await self._metrics.record_rule_coverage(str(rule["id"]), len(hosts))
            if not hosts:
                watching_nothing += 1
                logger.info(
                    "alert rule %r (%s on %r) matched no reporting host this cycle",
                    rule["name"],
                    rule["metric"],
                    str(rule["host_filter"] or "*"),
                )
            for hostname in hosts:
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
                # Surfaced next to the others because a run of "rules: 4,
                # evaluated: 0" is the state an operator most needs to see and
                # the one the old details line was silent about.
                "watching_nothing": watching_nothing,
            },
        )

    async def _run_nodata(self, rule: dict[str, Any]) -> tuple[int, list[str]]:
        """Evaluate one ``nodata`` rule over every enrolled host it matches.

        Returns ``(hosts matched, transitions)``. The candidate set is the
        inventory's enrolled hosts (``enrolled_hosts``), never the metrics
        table, so a host that has gone quiet is still looked at - which is the
        whole point. Hosts that never had an agent are not candidates: that is
        missing coverage, not an outage, and paging for it would bury the real
        "went dark" under thirteen permanent ones (review 03, Q5)."""
        host_filter = str(rule["host_filter"] or "*")
        candidates = [
            h
            for h in await self._metrics.enrolled_hosts()
            if host_filter == "*" or fnmatchcase(str(h["hostname"]), host_filter)
        ]
        # One verdict per hostname: two inventory rows can name one machine.
        by_host: dict[str, dict[str, Any]] = {}
        for host in candidates:
            name = str(host["hostname"])
            if name not in by_host or (_heard_at(host) or 0) > (_heard_at(by_host[name]) or 0):
                by_host[name] = host
        transitions: list[str] = []
        for hostname, host in by_host.items():
            transition = await self._evaluate_nodata(rule, hostname, host)
            if transition:
                transitions.append(transition)
        # A dark host that has since LEFT the candidate set (its agent revoked
        # or forgotten) would otherwise stay latched forever: nothing revisits
        # it, and the Overview would count a machine nobody watches as firing
        # (the #714 class). It resolves, and the history says why.
        for row in await self._metrics.firing_for_rule(str(rule["id"])):
            hostname = str(row["hostname"])
            if hostname in by_host:
                continue
            if host_filter != "*" and not fnmatchcase(hostname, host_filter):
                continue  # out of scope by filter: update_rule already dropped it
            await self._metrics.set_alert_state(str(rule["id"]), hostname, None, None)
            await self._notify_resolved(
                rule,
                hostname,
                float(row["last_value"] or 0),
                str(row["firing_since"]),
                reason=REASON_UNENROLLED,
            )
            transitions.append("resolved")
        return len(by_host), transitions

    async def _evaluate_nodata(
        self, rule: dict[str, Any], hostname: str, host: dict[str, Any]
    ) -> str | None:
        now = self._now()
        rule_id = str(rule["id"])
        state = await self._metrics.get_alert_state(rule_id, hostname)
        firing_since = state.get("firing_since") if state else None
        heard = _heard_at(host)
        if heard is None:
            # Enrolled but never connected: nothing has been heard to stop.
            return None
        silent = now - max(heard, self._watching_since)

        if str(host.get("pve_status") or "") == "stopped":
            # Somebody stopped the guest. Its agent is silent because the
            # machine is off, which the hypervisor already says - that is not a
            # host going dark, and paging for every stopped dev VM is how this
            # rule would get deleted on day one. A dark host that is then
            # stopped resolves, with the reason recorded, rather than staying
            # latched forever (the #714 class).
            if firing_since:
                await self._metrics.set_alert_state(rule_id, hostname, None, silent)
                await self._notify_resolved(
                    rule, hostname, silent, firing_since, reason=REASON_STOPPED
                )
                return "resolved"
            await self._metrics.set_alert_state(rule_id, hostname, None, silent)
            return None

        if silent >= int(rule["for_seconds"]):
            if firing_since:
                await self._metrics.set_alert_state(rule_id, hostname, firing_since, silent)
                return None
            # "Since" is when the host was last HEARD, not when we noticed: the
            # operator's question is "how long has it been dark".
            since = _iso(heard)
            await self._metrics.set_alert_state(rule_id, hostname, since, silent)
            await self._notify_firing(rule, hostname, silent, since)
            return "firing"

        if firing_since:
            await self._metrics.set_alert_state(rule_id, hostname, None, silent)
            await self._notify_resolved(rule, hostname, silent, firing_since)
            return "resolved"
        await self._metrics.set_alert_state(rule_id, hostname, None, silent)
        return None

    async def _hosts_for(self, rule: dict[str, Any]) -> list[str]:
        """Hosts this rule applies to.

        ``host_filter`` is a GLOB, which is what the API docs, the MCP tool
        description and the console's own placeholder have always said it was -
        it simply was not one. It was compared with ``==``, so ``web-*`` matched
        no host, the rule was skipped on every cycle, and nothing anywhere said
        so; only ``*`` and an exact hostname ever worked (#648 tranche 5).
        ``fnmatchcase`` subsumes both: ``*`` matches every host and a bare
        hostname has no wildcards, so it still matches only itself.

        Hosts that never reported the metric are not evaluated at all - "no
        data" is not a breach. That is deliberate, and it is also why a rule can
        match nothing; the caller records the count either way."""
        window = int(rule["for_seconds"]) + SAMPLE_FRESHNESS_SECONDS
        since = int(self._now() - window)
        reporting = await self._metrics.hosts_reporting(str(rule["metric"]), since)
        host_filter = str(rule["host_filter"] or "*")
        if host_filter == "*":
            return reporting
        return [h for h in reporting if fnmatchcase(h, host_filter)]

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
            "kind": str(rule.get("kind") or "threshold"),
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
        payload["message"] = describe_alert(EVENT_FIRING, payload)
        logger.warning("alert firing: %s", payload["message"])
        event_id = await self._metrics.record_alert_event(
            rule=rule,
            hostname=hostname,
            event="firing",
            value=value,
            since=since,
            at=_iso(self._now()),
        )
        payload["alert_event_id"] = event_id
        await emit_event(EVENT_FIRING, payload, repo=self._repo)
        await self._deliver(EVENT_FIRING, payload, event_id)

    async def _notify_resolved(
        self,
        rule: dict[str, Any],
        hostname: str,
        value: float,
        firing_since: str,
        reason: str | None = None,
    ) -> None:
        started = _parse_iso(firing_since)
        duration = int(self._now() - started) if started else None
        payload = {
            **self._payload(rule, hostname, value),
            "since": firing_since,
            "resolved_at": _iso(self._now()),
            "duration_seconds": duration,
            "reason": reason,
        }
        payload["message"] = describe_alert(EVENT_RESOLVED, payload)
        logger.info("alert resolved: %s", payload["message"])
        event_id = await self._metrics.record_alert_event(
            rule=rule,
            hostname=hostname,
            event="resolved",
            value=value,
            since=firing_since,
            at=_iso(self._now()),
            duration_seconds=duration,
            reason=reason,
        )
        payload["alert_event_id"] = event_id
        await emit_event(EVENT_RESOLVED, payload, repo=self._repo)
        await self._deliver(EVENT_RESOLVED, payload, event_id)

    async def _deliver(self, event_type: str, payload: dict[str, Any], event_id: int) -> None:
        """Hand the transition to the native channels. Never raises: a broken
        channel must not stop the next rule from being evaluated."""
        if self._notifier is None:
            return
        try:
            await self._notifier.dispatch(event_type, payload, event_id=event_id)
        except Exception:
            logger.warning(
                "could not hand %s to the notification channels", event_type, exc_info=True
            )


def _heard_at(host: dict[str, Any]) -> float | None:
    """When this enrolled host was last heard from, as epoch seconds."""
    for key in ("last_heartbeat", "connected_at", "first_seen"):
        parsed = _parse_iso(host.get(key))
        if parsed is not None:
            return parsed
    return None


_COMPARISON_WORDS = {"gt": "above", "gte": "at or above", "lt": "below", "lte": "at or below"}


def describe_alert(event_type: str, payload: dict[str, Any]) -> str:
    """One plain sentence for an alert transition.

    Built HERE, once, so the phone, the n8n flow and the log say the same thing
    and none of them has to know what a ``nodata`` rule is."""
    host = payload.get("hostname", "?")
    rule = payload.get("rule_name", "?")
    value = payload.get("value")
    if payload.get("kind") == KIND_NODATA:
        if event_type == EVENT_FIRING:
            return f"{host} went dark: nothing heard for {_humanise(float(value or 0))} ({rule})"
        duration = payload.get("duration_seconds")
        dark_for = f" after {_humanise(duration)}" if duration is not None else ""
        if payload.get("reason"):
            return f"{host} is no longer counted as dark{dark_for}: {payload['reason']} ({rule})"
        return f"{host} is reporting again{dark_for} ({rule})"
    metric = payload.get("metric", "?")
    words = _COMPARISON_WORDS.get(str(payload.get("comparison")), str(payload.get("comparison")))
    shown = f"{float(value):g}" if isinstance(value, int | float) else str(value)
    if event_type == EVENT_FIRING:
        return (
            f"{rule} on {host}: {metric} is {shown}, {words} {payload.get('threshold'):g} "
            f"for {_humanise(int(payload.get('for_seconds') or 0))}"
        )
    duration = payload.get("duration_seconds")
    lasted = f" after {_humanise(duration)}" if duration is not None else ""
    return f"{rule} on {host} resolved{lasted}: {metric} is {shown}"
