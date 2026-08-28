"""Monitoring read tools (MCP read parity, wave 1).

Alert rules, firing alerts, and host metrics - the same MetricsRepository calls
GET /monitoring/* makes, so a chart in the console and an answer from the
assistant come from one store.

Creating, silencing and deleting alert rules are NOT here: they change what the
system will wake an operator for, which belongs to the mutation wave.
"""

from __future__ import annotations

import time
from typing import Any

from homepilot.mcp.tools.host_param import host_arg, host_properties, with_host_warning
from homepilot.metrics.repository import MAX_SERIES_POINTS, VALID_COMPARISONS
from homepilot.metrics.router import MAX_WINDOW_HOURS

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "list_alert_rules",
        "description": (
            "Every configured alert rule: name, metric, comparison, threshold, the "
            "for_seconds duration the condition must hold, the host_filter it applies "
            "to, and whether it is enabled. Read-only - it cannot add, silence or "
            "delete a rule."
        ),
        "inputSchema": {"type": "object", "properties": {}},
        "outputSchema": {
            "type": "object",
            "properties": {
                "items": {"type": "array", "items": {"type": "object"}},
                "total": {"type": "integer"},
            },
            "required": ["items", "total"],
        },
    },
    {
        "name": "get_monitoring_alerts",
        "description": (
            "Alerts firing RIGHT NOW, each joined to the rule that raised it and the "
            "host it fired for. An empty list means nothing is currently breaching a "
            "rule - it does not mean no rules exist (use list_alert_rules for that). "
            "Read-only."
        ),
        "inputSchema": {"type": "object", "properties": {}},
        "outputSchema": {
            "type": "object",
            "properties": {
                "items": {"type": "array", "items": {"type": "object"}},
                "total": {"type": "integer"},
            },
            "required": ["items", "total"],
        },
    },
    {
        "name": "get_host_metrics",
        "description": (
            "The newest value of every metric one host has reported (cpu, memory, "
            "disk, load and whatever else its agent sends), as a list of "
            "{metric, ts, value}. Reads stored samples - it does not touch the host, "
            "so a host whose agent is offline returns its last known values, not "
            "fresh ones, and an unknown host returns an empty list."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                **host_properties("The host's name"),
            },
            "required": ["host"],
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "hostname": {"type": "string"},
                "metrics": {"type": "array", "items": {"type": "object"}},
            },
            "required": ["hostname", "metrics"],
        },
    },
    {
        "name": "get_host_metrics_series",
        "description": (
            "One host and one metric over a time window, oldest point first. Every "
            "point returned is a sample the host actually reported - nothing is "
            f"averaged or thinned. At most {MAX_SERIES_POINTS} points come back; when "
            "the window holds more, `truncated` is true and the OLDEST points are the "
            f"ones left out. The window may be up to {MAX_WINDOW_HOURS} hours, and is "
            "answered only from samples retention has not yet pruned."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                **host_properties("The host's name"),
                "metric": {
                    "type": "string",
                    "description": 'Metric name, e.g. "cpu.percent" or "disk.free_gb"',
                },
                "hours": {
                    "type": "number",
                    "description": f"Window size in hours (default 1, max {MAX_WINDOW_HOURS})",
                },
                "limit": {
                    "type": "integer",
                    "description": f"Maximum points to return (max {MAX_SERIES_POINTS})",
                },
            },
            "required": ["host", "metric"],
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "hostname": {"type": "string"},
                "metric": {"type": "string"},
                "since": {"type": "integer"},
                "points": {"type": "array", "items": {"type": "object"}},
                "truncated": {"type": "boolean"},
                "max_points": {"type": "integer"},
            },
            "required": ["hostname", "metric", "points", "truncated"],
        },
    },
    # ── Admin tier (MCP<->API parity, wave 3). POST/PATCH/DELETE /monitoring/rules
    # are all API require_scope("admin") - changing what the fleet wakes an
    # operator for - so these need an admin MCP token. ──────────────────────────
    {
        "name": "create_alert_rule",
        "description": (
            "Create an alert rule: fire when `metric` on hosts matching `host_filter` "
            "is `comparison` (gt/gte/lt/lte) `threshold` for at least `for_seconds` "
            "(0 = fire on the first breaching sample). Returns the stored rule. Admin "
            "only."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Human-readable rule name"},
                "metric": {"type": "string", "description": "Metric key, e.g. cpu.percent"},
                "comparison": {"type": "string", "description": "One of gt, gte, lt, lte"},
                "threshold": {"type": "number"},
                "for_seconds": {
                    "type": "integer",
                    "description": "Seconds the condition must hold (0-86400, default 300)",
                },
                "host_filter": {
                    "type": "string",
                    "description": "Hostname glob the rule applies to (default '*')",
                },
                "enabled": {"type": "boolean", "description": "Default true"},
            },
            "required": ["name", "metric", "comparison", "threshold"],
        },
        "outputSchema": {"type": "object", "properties": {"id": {"type": "string"}}},
    },
    {
        "name": "update_alert_rule",
        "description": (
            "Retune an existing alert rule by id, in place. Every field except "
            "rule_id is optional and only the ones you pass change - the rest are "
            "left as they were. Pass `enabled` to silence or re-enable it; pass "
            "`threshold`, `comparison`, `metric`, `for_seconds`, `host_filter` or "
            "`name` to change the condition without deleting and recreating the rule "
            "(which would drop its firing state). Returns the updated rule; an "
            "unknown id is an error. Admin only."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "rule_id": {"type": "string", "description": "The rule's id"},
                "name": {"type": "string", "description": "Human-readable rule name"},
                "metric": {"type": "string", "description": "Metric key, e.g. cpu.percent"},
                "comparison": {"type": "string", "description": "One of gt, gte, lt, lte"},
                "threshold": {"type": "number"},
                "for_seconds": {
                    "type": "integer",
                    "description": "Seconds the condition must hold (0-86400)",
                },
                "host_filter": {
                    "type": "string",
                    "description": "Hostname glob the rule applies to",
                },
                "enabled": {"type": "boolean", "description": "New enabled state"},
            },
            "required": ["rule_id"],
        },
        "outputSchema": {"type": "object", "properties": {"id": {"type": "string"}}},
    },
    {
        "name": "delete_alert_rule",
        "description": (
            "Permanently delete an alert rule by id. Returns whether it was removed; an "
            "unknown id is an error. Admin only."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "rule_id": {"type": "string", "description": "The rule's id"},
            },
            "required": ["rule_id"],
        },
        "outputSchema": {
            "type": "object",
            "properties": {"id": {"type": "string"}, "deleted": {"type": "boolean"}},
            "required": ["id", "deleted"],
        },
    },
]


def _repo(ctx: dict[str, Any]) -> Any:
    repo = ctx.get("metrics_repo")
    if repo is None:
        raise RuntimeError("Metrics storage not available")
    return repo


async def handle_list_alert_rules(arguments: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    rules = await _repo(ctx).list_rules()
    return {"items": rules, "total": len(rules)}


async def handle_get_monitoring_alerts(
    arguments: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    items = await _repo(ctx).list_firing()
    return {"items": items, "total": len(items)}


async def handle_get_host_metrics(arguments: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    hostname, warning = host_arg(arguments)
    # The ANSWER keeps the `hostname` key: it mirrors GET /metrics/hosts/{hostname}
    # /latest, and #608 standardised the PARAMETER, not the payload.
    return with_host_warning(
        {"hostname": hostname, "metrics": await _repo(ctx).latest(hostname)}, warning
    )


async def handle_get_host_metrics_series(
    arguments: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    hostname, warning = host_arg(arguments)
    metric = str(arguments["metric"])

    # The route's Query(...) constraints are the product's limits, not FastAPI
    # trivia: a caller that asks for a 10-year window or 10 million points would
    # otherwise get a different (and much more expensive) answer over MCP than
    # over HTTP. Clamped here so both surfaces obey the same ceiling.
    raw_hours = arguments.get("hours")
    hours = 1.0 if raw_hours is None else float(raw_hours)
    if hours <= 0 or hours > MAX_WINDOW_HOURS:
        raise ValueError(f"hours must be greater than 0 and at most {MAX_WINDOW_HOURS}")

    raw_limit = arguments.get("limit")
    limit = MAX_SERIES_POINTS if raw_limit is None else int(raw_limit)
    if limit < 1 or limit > MAX_SERIES_POINTS:
        raise ValueError(f"limit must be between 1 and {MAX_SERIES_POINTS}")

    since = int(time.time() - hours * 3600)
    points, truncated = await _repo(ctx).series(hostname, metric, since, limit=limit)
    return with_host_warning(
        {
            "hostname": hostname,
            "metric": metric,
            "since": since,
            "points": points,
            "truncated": truncated,
            "max_points": limit,
        },
        warning,
    )


# ── Admin-tier handlers (wave 3). Call the SAME MetricsRepository methods the
# POST/PATCH/DELETE /monitoring/rules routes call. ────────────────────────────


async def handle_create_alert_rule(
    arguments: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    comparison = str(arguments["comparison"])
    if comparison not in VALID_COMPARISONS:
        raise ValueError(f"comparison must be one of {', '.join(VALID_COMPARISONS)}")
    result: dict[str, Any] = await _repo(ctx).create_rule(
        name=str(arguments["name"]),
        metric=str(arguments["metric"]),
        comparison=comparison,
        threshold=float(arguments["threshold"]),
        for_seconds=int(arguments.get("for_seconds", 300)),
        host_filter=str(arguments.get("host_filter") or "*"),
        enabled=bool(arguments.get("enabled", True)),
    )
    return result


async def handle_update_alert_rule(
    arguments: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    repo = _repo(ctx)
    rule_id = str(arguments["rule_id"])

    # Only the fields the caller actually passed - a missing field means "leave
    # it", never "set to null". No raw arguments["enabled"]: omitting it is a
    # valid partial update, not a KeyError (#593).
    fields: dict[str, Any] = {}
    if "name" in arguments:
        fields["name"] = str(arguments["name"])
    if "metric" in arguments:
        fields["metric"] = str(arguments["metric"])
    if "comparison" in arguments:
        comparison = str(arguments["comparison"])
        if comparison not in VALID_COMPARISONS:
            raise ValueError(f"comparison must be one of {', '.join(VALID_COMPARISONS)}")
        fields["comparison"] = comparison
    if "threshold" in arguments:
        fields["threshold"] = float(arguments["threshold"])
    if "for_seconds" in arguments:
        fields["for_seconds"] = int(arguments["for_seconds"])
    if "host_filter" in arguments:
        fields["host_filter"] = str(arguments["host_filter"] or "*")
    if "enabled" in arguments:
        fields["enabled"] = bool(arguments["enabled"])

    rule = await repo.update_rule(rule_id, **fields)
    if rule is None:
        raise ValueError(f"Alert rule not found: {rule_id}")
    result: dict[str, Any] = rule
    return result


async def handle_delete_alert_rule(
    arguments: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    rule_id = str(arguments["rule_id"])
    if not await _repo(ctx).delete_rule(rule_id):
        raise ValueError(f"Alert rule not found: {rule_id}")
    return {"id": rule_id, "deleted": True}
