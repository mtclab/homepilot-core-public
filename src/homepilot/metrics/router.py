from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from ..auth.deps import require_scope
from .repository import MAX_SERIES_POINTS, VALID_COMPARISONS, MetricsRepository

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


def _repo(request: Request) -> MetricsRepository:
    repo = getattr(request.app.state, "metrics_repo", None)
    if not isinstance(repo, MetricsRepository):
        raise HTTPException(status_code=503, detail="Metrics storage not available")
    return repo


class AlertRuleIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    metric: str = Field(min_length=1, max_length=64)
    comparison: str
    threshold: float
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


@router.post("/rules", dependencies=[_admin], status_code=201)
async def create_rule(request: Request, body: AlertRuleIn) -> dict[str, Any]:
    if body.comparison not in VALID_COMPARISONS:
        raise HTTPException(
            status_code=422,
            detail=f"comparison must be one of {', '.join(VALID_COMPARISONS)}",
        )
    return await _repo(request).create_rule(
        name=body.name,
        metric=body.metric,
        comparison=body.comparison,
        threshold=body.threshold,
        for_seconds=body.for_seconds,
        host_filter=body.host_filter,
        enabled=body.enabled,
    )


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
    rule = await repo.update_rule(rule_id, **fields)
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
