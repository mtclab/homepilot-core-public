from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from ..auth.deps import require_scope
from .repository import (
    KIND_NODATA,
    KIND_THRESHOLD,
    MAX_SERIES_POINTS,
    NODATA_METRIC,
    RULE_KINDS,
    VALID_COMPARISONS,
    MetricsRepository,
)

# NOT mounted at /metrics: that path is the PUBLIC Prometheus exposition
# endpoint (main.py), and hanging an authenticated subtree off the same
# prefix invites both a scrape-config mistake and a wrong assumption about
# what is public here.
router = APIRouter(prefix="/monitoring", tags=["monitoring"])

_read = Depends(require_scope("read"))
_admin = Depends(require_scope("admin"))

# Widest window a single series request may ask for. Matches the maximum
# retention an operator is likely to configure; anything longer is a report, not
# a chart, and would be answered from data the pruner has already removed.
MAX_WINDOW_HOURS = 24 * 30

# Widest window the alert history may be asked for: the default operational
# retention (`retention_days`, 90). Older rows are pruned anyway.
MAX_HISTORY_HOURS = 24 * 90
MAX_HISTORY_ITEMS = 500


def _repo(request: Request) -> MetricsRepository:
    repo = getattr(request.app.state, "metrics_repo", None)
    if not isinstance(repo, MetricsRepository):
        raise HTTPException(status_code=503, detail="Metrics storage not available")
    return repo


class AlertRuleIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    # `threshold` (a metric crossing a value) or `nodata` (an enrolled host that
    # has reported nothing for for_seconds). A nodata rule takes no metric,
    # comparison or threshold.
    kind: str = KIND_THRESHOLD
    metric: str = Field(default=NODATA_METRIC, min_length=1, max_length=64)
    comparison: str = "gt"
    threshold: float = 0.0
    # The duration condition. 0 means "fire on the first breaching sample";
    # anything above it requires the condition to hold for that whole span.
    for_seconds: int = Field(default=300, ge=0, le=86400)
    host_filter: str = Field(default="*", max_length=253)
    enabled: bool = True


@router.get("/hosts/{hostname}/series", dependencies=[_read])
async def host_series(
    request: Request,
    hostname: str,
    metric: str = Query(..., min_length=1, max_length=64),
    hours: float = Query(1.0, gt=0, le=MAX_WINDOW_HOURS),
    limit: int = Query(MAX_SERIES_POINTS, ge=1, le=MAX_SERIES_POINTS),
) -> dict[str, Any]:
    """One host + one metric over a window, oldest point first.

    At most ``limit`` points (hard ceiling ``MAX_SERIES_POINTS`` = 2000) are
    returned, newest-first-wins: when a window holds more, ``truncated`` is true
    and the OLDEST points are the ones left out. Nothing is averaged or thinned -
    every point returned is a sample the host actually reported."""
    since = int(time.time() - hours * 3600)
    points, truncated = await _repo(request).series(hostname, metric, since, limit=limit)
    return {
        "hostname": hostname,
        "metric": metric,
        "since": since,
        "points": points,
        "truncated": truncated,
        "max_points": limit,
    }


@router.get("/hosts/{hostname}/latest", dependencies=[_read])
async def host_latest(request: Request, hostname: str) -> dict[str, Any]:
    """The newest value of every metric this host has reported."""
    return {"hostname": hostname, "metrics": await _repo(request).latest(hostname)}


@router.get("/rules", dependencies=[_read])
async def list_rules(request: Request) -> dict[str, Any]:
    rules = await _repo(request).list_rules()
    return {"items": rules, "total": len(rules)}


def rule_kind_problem(kind: str, fields: set[str]) -> str | None:
    """Why a create request does not make sense for its kind, or None.

    Shared by the route and the MCP tool so both refuse the same things."""
    if kind not in RULE_KINDS:
        return f"kind must be one of {', '.join(RULE_KINDS)}"
    if kind == KIND_NODATA:
        given = sorted(fields & {"metric", "comparison", "threshold"})
        if given:
            return (
                f"a nodata rule takes no {', '.join(given)}: it fires when an enrolled "
                "host has reported nothing at all for for_seconds"
            )
    elif "metric" not in fields or "threshold" not in fields or "comparison" not in fields:
        return "a threshold rule needs metric, comparison and threshold"
    return None


@router.post("/rules", dependencies=[_admin], status_code=201)
async def create_rule(request: Request, body: AlertRuleIn) -> dict[str, Any]:
    problem = rule_kind_problem(body.kind, set(body.model_fields_set))
    if problem:
        raise HTTPException(status_code=422, detail=problem)
    if body.comparison not in VALID_COMPARISONS:
        raise HTTPException(
            status_code=422,
            detail=f"comparison must be one of {', '.join(VALID_COMPARISONS)}",
        )
    try:
        return await _repo(request).create_rule(
            name=body.name,
            metric=body.metric,
            comparison=body.comparison,
            threshold=body.threshold,
            for_seconds=body.for_seconds,
            host_filter=body.host_filter,
            enabled=body.enabled,
            kind=body.kind,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


class AlertRuleUpdate(BaseModel):
    """Retune a rule in place. Every field is optional and only the ones present
    in the request body change - so a PATCH with just `enabled` silences a rule
    (deleting is not the only way to quiet it for an afternoon), and a PATCH with
    `threshold` retunes it without a delete-and-recreate that would drop its
    firing state."""

    name: str | None = Field(default=None, min_length=1, max_length=120)
    metric: str | None = Field(default=None, min_length=1, max_length=64)
    comparison: str | None = None
    threshold: float | None = None
    # 0 means "fire on the first breaching sample"; above it, the condition must
    # hold for that whole span.
    for_seconds: int | None = Field(default=None, ge=0, le=86400)
    host_filter: str | None = Field(default=None, max_length=253)
    enabled: bool | None = None


@router.patch("/rules/{rule_id}", dependencies=[_admin])
async def update_rule(request: Request, rule_id: str, body: AlertRuleUpdate) -> dict[str, Any]:
    repo = _repo(request)
    # Only the fields the client actually sent - a missing field is "leave it",
    # not "set it to null". exclude_unset keeps a default of None from being
    # written over a real stored value.
    fields = body.model_dump(exclude_unset=True)
    if "comparison" in fields and fields["comparison"] not in VALID_COMPARISONS:
        raise HTTPException(
            status_code=422,
            detail=f"comparison must be one of {', '.join(VALID_COMPARISONS)}",
        )
    try:
        rule = await repo.update_rule(rule_id, **fields)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    if rule is None:
        raise HTTPException(status_code=404, detail=f"Alert rule not found: {rule_id}")
    return rule


@router.delete("/rules/{rule_id}", dependencies=[_admin])
async def delete_rule(request: Request, rule_id: str) -> dict[str, Any]:
    if not await _repo(request).delete_rule(rule_id):
        raise HTTPException(status_code=404, detail=f"Alert rule not found: {rule_id}")
    return {"id": rule_id, "deleted": True}


@router.get("/alerts", dependencies=[_read])
async def list_alerts(request: Request) -> dict[str, Any]:
    """Alerts currently firing, each joined to the rule that raised it."""
    items = await _repo(request).list_firing()
    return {"items": items, "total": len(items)}


async def alert_history(
    metrics_repo: MetricsRepository,
    notifier: Any,
    *,
    hours: float,
    hostname: str | None,
    limit: int,
) -> dict[str, Any]:
    """Alert transitions in a window, newest first, plus where alerts are sent.

    One implementation for GET /monitoring/alert-events and the MCP tool. The
    channel list is the read-scope summary (kind, name, last delivery state) -
    no endpoint, no URL - so a read token can see WHETHER anyone is told
    without learning where."""
    from datetime import UTC, datetime

    since_ts = time.time() - hours * 3600
    since = datetime.fromtimestamp(since_ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    items = await metrics_repo.list_alert_events(since, hostname=hostname or None, limit=limit)
    channels = await notifier.summary() if notifier is not None else []
    return {
        "items": items,
        "total": len(items),
        "since": since,
        "hours": hours,
        "hostname": hostname or None,
        "channels": channels,
    }


@router.get("/alert-events", dependencies=[_read])
async def list_alert_events(
    request: Request,
    hours: float = Query(24.0, gt=0, le=MAX_HISTORY_HOURS),
    hostname: str | None = Query(None, max_length=253),
    limit: int = Query(200, ge=1, le=MAX_HISTORY_ITEMS),
) -> dict[str, Any]:
    """What the alerting system has DONE: every fire and recovery in the window.

    `alert_state` only knows what is firing now; this is the record that
    answers "did anything fire last night?" (#660)."""
    return await alert_history(
        _repo(request),
        getattr(request.app.state, "notifier", None),
        hours=hours,
        hostname=hostname,
        limit=limit,
    )
