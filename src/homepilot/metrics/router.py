from __future__ import annotations

import math
import time
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from ..auth.deps import require_scope
from .repository import (
    AGENT_METRICS,
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

# Widest window a single series request may ever ask for: the outer ceiling,
# whatever retention is configured. Anything longer is a report, not a chart.
#
# The window actually allowed is the SMALLER of this and the retention in force
# (``metrics_retention_days``, default 7 days): a window reaching past what the
# pruner keeps can only ever come back empty for the part beyond it, and is
# refused with a sentence rather than silently answered (#661).
MAX_WINDOW_HOURS = 24 * 30

# What a series request may ask for. ``raw`` (the default) returns samples
# exactly as reported, newest-first-wins under the point cap and says
# ``truncated`` when the window held more. ``auto`` lets the server summarise a
# window too long to return raw into fixed-width buckets of min/avg/max - and
# says that it did, and how wide the buckets are (#661).
RESOLUTION_RAW = "raw"
RESOLUTION_AUTO = "auto"

# The bucket widths ``auto`` chooses from, smallest first: the narrowest that
# fits the window into ``limit`` buckets wins. Round numbers on purpose - "each
# point is 10 minutes" is a sentence a person can read off a chart.
BUCKET_STEPS_SECONDS: tuple[int, ...] = (
    60,
    120,
    300,
    600,
    900,
    1800,
    3600,
    7200,
    10800,
    21600,
    43200,
    86400,
)


class SeriesWindowError(ValueError):
    """A series request that can only be answered with nothing, said in words."""


async def retention_days_in_force() -> int:
    """The metric retention in force, resolved rather than read off boot
    Settings (#553 C2), so a shortened horizon narrows the window at once."""
    from ..app_settings import effective
    from ..config import get_settings

    try:
        days = int(await effective("metrics_retention_days", get_settings()))
    except Exception:
        days = int(get_settings().metrics_retention_days)
    return max(1, days)


def window_problem(hours: float, retention_days: int) -> str | None:
    """Why a series window cannot be answered, in a sentence, or None.

    The ONE refusal for a window: the route used to bound ``hours`` in its
    ``Query`` too, so a window over the outer ceiling was refused with
    pydantic's list (``Input should be less than or equal to 720``) before this
    sentence was ever reached (3.14 walk). The route and MCP now leave every
    bound to this."""
    if not math.isfinite(hours) or hours <= 0:
        return f"A {hours:g}-hour window is not a window: ask for a number of hours greater than 0."
    if hours > retention_days * 24:
        return (
            f"A {hours:g}-hour window reaches past what is kept: metric samples are "
            f"kept for {retention_days} days ({retention_days * 24} hours) and older "
            f"ones are deleted, so ask for at most {retention_days * 24} hours."
        )
    if hours > MAX_WINDOW_HOURS:
        return (
            f"A {hours:g}-hour window is longer than one series request may ask for "
            f"({MAX_WINDOW_HOURS} hours), so ask for at most {MAX_WINDOW_HOURS} hours."
        )
    return None


def bucket_seconds_for(window_seconds: float, limit: int) -> int:
    """The narrowest round bucket that fits ``window_seconds`` into ``limit``.

    Epoch-aligned buckets over a window that does not start on a boundary
    touch one more bucket than the window divides into, so the window is
    divided by ``limit - 1``."""
    need = window_seconds / max(1, limit - 1)
    for step in BUCKET_STEPS_SECONDS:
        if step >= need:
            return step
    # Past a day per bucket: whole minutes, still enough to fit.
    return int(-(-need // 60) * 60)


async def series_answer(
    repo: MetricsRepository,
    hostname: str,
    metric: str,
    *,
    hours: float,
    limit: int = MAX_SERIES_POINTS,
    resolution: str = RESOLUTION_RAW,
) -> dict[str, Any]:
    """One host + one metric over a window. One implementation for
    GET /monitoring/hosts/{h}/series and the MCP tool.

    Raises ``SeriesWindowError`` for a window past the retention in force and
    ``ValueError`` for an unknown resolution.

    The payload always names what it is: ``resolution`` is what was RETURNED
    (``raw`` or ``bucketed``), ``bucket_seconds`` and ``aggregation`` say how a
    bucketed answer was made (both null for raw). ``auto`` returns raw when the
    whole window fits under ``limit`` - summarising data that fits would only
    lose detail - and buckets otherwise (a ``limit`` of 1 cannot hold a
    bucketed window and stays raw)."""
    if resolution not in (RESOLUTION_RAW, RESOLUTION_AUTO):
        raise ValueError(f"resolution must be {RESOLUTION_RAW} or {RESOLUTION_AUTO}")
    problem = window_problem(hours, await retention_days_in_force())
    if problem:
        raise SeriesWindowError(problem)
    since = int(time.time() - hours * 3600)
    answer: dict[str, Any] = {
        "hostname": hostname,
        "metric": metric,
        "since": since,
        "points": [],
        "truncated": False,
        "max_points": limit,
        "resolution": "raw",
        "bucket_seconds": None,
        "aggregation": None,
    }
    if (
        resolution == RESOLUTION_AUTO
        and limit >= 2
        and await repo.series_count(hostname, metric, since) > limit
    ):
        bucket = bucket_seconds_for(hours * 3600, limit)
        answer["points"] = await repo.series_buckets(hostname, metric, since, bucket)
        answer["resolution"] = "bucketed"
        answer["bucket_seconds"] = bucket
        # `value` is what a one-line chart draws; min/max say what it hides.
        answer["aggregation"] = {"value": "avg", "also": ["min", "max", "count"]}
        return answer
    points, truncated = await repo.series(hostname, metric, since, limit=limit)
    answer["points"] = points
    answer["truncated"] = truncated
    return answer


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
    metric: str = Field(
        default=NODATA_METRIC,
        min_length=1,
        max_length=64,
        description="The metric to watch. A current agent reports: "
        + ", ".join(AGENT_METRICS)
        + ". For 'over 90% full' use disk.used_pct with comparison gt and threshold 90.",
    )
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
    # No gt/le here: window_problem is the one refusal, in a sentence (a Query
    # bound answered first, with pydantic's list - 3.14 walk).
    hours: float = Query(
        1.0,
        description=(
            "Window in hours, greater than 0. At most the metric retention in force "
            f"(default 7 days = 168 hours) and never more than {MAX_WINDOW_HOURS}; a "
            "longer window is refused with 422 and a sentence saying how long it may be."
        ),
    ),
    limit: int = Query(MAX_SERIES_POINTS, ge=1, le=MAX_SERIES_POINTS),
    resolution: Literal["raw", "auto"] = Query(
        "raw",
        description=(
            "raw (default): every sample as reported, the newest `limit` of them, with "
            "`truncated` saying when older ones were left out. auto: when the window "
            "holds more than `limit` samples, the WHOLE window as fixed-width buckets "
            "of min/avg/max (`resolution: bucketed`, `bucket_seconds`, `aggregation`)."
        ),
    ),
) -> dict[str, Any]:
    """One host + one metric over a window, oldest point first.

    Raw (the default): at most ``limit`` points (hard ceiling
    ``MAX_SERIES_POINTS`` = 2000), newest-first-wins: when a window holds more,
    ``truncated`` is true and the OLDEST points are the ones left out. Nothing
    is averaged or thinned - every point is a sample the host reported.

    ``resolution=auto``: a window too long to return raw comes back whole, as
    buckets whose ``value`` is the average and which carry ``min``, ``max`` and
    ``count`` - and the payload says so (``resolution: bucketed``,
    ``bucket_seconds``, ``aggregation``). A downsample the caller did not ask
    for would be a lie about the series; one it asked for, and that names
    itself, is not."""
    try:
        return await series_answer(
            _repo(request), hostname, metric, hours=hours, limit=limit, resolution=resolution
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@router.get("/hosts/{hostname}/latest", dependencies=[_read])
async def host_latest(request: Request, hostname: str) -> dict[str, Any]:
    """The newest value of every metric this host has reported."""
    return {"hostname": hostname, "metrics": await _repo(request).latest(hostname)}


# A host counts as "reporting" when its newest sample of anything is this
# recent: the same staleness line the evaluator draws (alerts.py).
REPORTING_WINDOW_SECONDS = 300

# How many hostnames a coverage note names before it says "and N more".
_NAMES_IN_NOTE = 10


def _plural(n: int, one: str, many: str) -> str:
    return one if n == 1 else many


async def rules_with_coverage(
    repo: MetricsRepository, rules: list[dict[str, Any]], *, now: float | None = None
) -> list[dict[str, Any]]:
    """Each rule plus who it CANNOT see yet (#661).

    ``hosts_matched`` says how many hosts a rule looked at; on its own it cannot
    say why the number is below the fleet. A rule on a metric only a newer agent
    emits (``disk.used_pct``) matches the upgraded hosts and silently skips the
    rest - which is correct, and the operator needs to be told. So every
    threshold rule carries, read now:

    * ``hosts_without_metric`` - hosts in its filter that ARE reporting metrics
      but not this one (an older agent), and their names;
    * ``coverage_note`` - the plain sentence to show, or null when every
      reporting host in scope reports the metric.

    A host reporting nothing at all is not counted: that is a host gone dark,
    which the ``nodata`` rule is for, not an agent to upgrade."""
    from fnmatch import fnmatchcase

    since = int((time.time() if now is None else now) - REPORTING_WINDOW_SECONDS)
    reporting = await repo.hosts_reporting_any(since)
    by_metric: dict[str, set[str]] = {}
    out: list[dict[str, Any]] = []
    for rule in rules:
        item = dict(rule)
        item["hosts_without_metric"] = None
        item["hosts_without_metric_names"] = None
        item["coverage_note"] = None
        if str(item.get("kind") or KIND_THRESHOLD) == KIND_NODATA:
            out.append(item)
            continue
        metric = str(item["metric"])
        host_filter = str(item.get("host_filter") or "*")
        if metric not in by_metric:
            by_metric[metric] = set(await repo.hosts_reporting(metric, since))
        in_scope = [h for h in reporting if host_filter == "*" or fnmatchcase(h, host_filter)]
        missing = sorted(h for h in in_scope if h not in by_metric[metric])
        item["hosts_without_metric"] = len(missing)
        item["hosts_without_metric_names"] = missing
        if missing:
            item["coverage_note"] = _coverage_note(metric, missing)
        out.append(item)
    return out


def _coverage_note(metric: str, missing: list[str]) -> str:
    n = len(missing)
    named = ", ".join(missing[:_NAMES_IN_NOTE])
    if n > _NAMES_IN_NOTE:
        named += f" and {n - _NAMES_IN_NOTE} more"
    if metric in AGENT_METRICS:
        return (
            f"{n} {_plural(n, 'host does', 'hosts do')} not report {metric} yet - "
            f"upgrade {_plural(n, 'its agent', 'their agents')} ({named}). Until then "
            f"this rule does not watch {_plural(n, 'it', 'them')}."
        )
    return (
        f"{n} reporting {_plural(n, 'host does', 'hosts do')} not report {metric}, and "
        f"no current agent does - this rule cannot watch {_plural(n, 'it', 'them')} "
        f"({named}). The metrics a current agent reports: {', '.join(AGENT_METRICS)}."
    )


@router.get("/rules", dependencies=[_read])
async def list_rules(request: Request) -> dict[str, Any]:
    repo = _repo(request)
    rules = await rules_with_coverage(repo, await repo.list_rules())
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
    metric: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        description="The metric to watch. A current agent reports: " + ", ".join(AGENT_METRICS),
    )
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
