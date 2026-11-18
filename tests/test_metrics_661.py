"""A fleet-wide rule, a readable week, and a window that is honest (#661).

Three decisions, each measured on dev before it was made:

1. **The agent emits ratios.** ``disk.used_pct``, ``memory.used_pct`` and
   ``load.1m_per_core`` make "disk over 90%" a rule; an old agent does not
   report them, and a rule on one SAYS which hosts it cannot see yet instead of
   just showing a smaller ``hosts_matched``.
2. **``cpu.count`` is not sampled** (it was 12.5% of a real table). The hub still
   accepts it from old agents.
3. **``resolution=auto``** returns a whole 7-day window as min/avg/max buckets
   and names the aggregation; raw stays the default and still says when it
   truncated; a window past the retention in force is refused with a sentence.

The real-binary half (agent -> hub -> a percentage rule fires on the right host
only) is in tests/test_metrics_journey.py.

Teeth (each was reverted and watched fail):

* ``series_answer``: always take the raw branch -> the auto tests fail (the week
  comes back truncated, unnamed);
* drop the retention check in ``series_answer`` -> the over-retention tests fail;
* ``rules_with_coverage`` returning rules unchanged -> the coverage tests fail;
* ``cpu.count`` back in AGENT_METRICS -> the vocabulary tests fail.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from homepilot.app_settings import bind_resolver, bound_resolver
from homepilot.auth.deps import require_token
from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.metrics.alerts import AlertEvaluator
from homepilot.metrics.repository import (
    AGENT_METRICS,
    MAX_SERIES_POINTS,
    RETIRED_AGENT_METRICS,
    MetricsRepository,
)
from homepilot.metrics.router import (
    BUCKET_STEPS_SECONDS,
    bucket_seconds_for,
    rules_with_coverage,
)
from homepilot.metrics.router import router as metrics_router

WEEK = 7 * 86400


def _now() -> int:
    """Read per test, never at import: the series API and the coverage read use
    the wall clock, and a full suite runs for twenty minutes after import."""
    return int(time.time())


class _Retention:
    """A resolver that answers metrics_retention_days and nothing else."""

    def __init__(self, days: int) -> None:
        self.days = days

    async def value(self, key: str) -> Any:
        if key == "metrics_retention_days":
            return self.days
        from homepilot.config import get_settings

        return getattr(get_settings(), key)


@pytest.fixture(autouse=True)
def _silent_bus(monkeypatch):
    from homepilot.metrics import alerts as alerts_mod

    async def _noop(event_type: str, payload: dict[str, Any], repo: Any = None) -> None:
        return None

    monkeypatch.setattr(alerts_mod, "emit_event", _noop)


@pytest.fixture
def retention_days():
    """Bind a known retention for the test, restoring whatever was bound."""
    previous = bound_resolver()
    resolver = _Retention(7)
    bind_resolver(resolver)  # type: ignore[arg-type]
    yield resolver
    bind_resolver(previous)


@pytest.fixture
async def api(tmp_path: Path, retention_days):
    db = Database(str(tmp_path / "metrics.db"))
    await db.connect()
    await run_migrations(db)
    repo = MetricsRepository(db)
    app = FastAPI()
    app.include_router(metrics_router)
    app.state.metrics_repo = repo
    app.dependency_overrides[require_token] = lambda: {
        "user_id": "1",
        "token_id": "1",
        "scope": "admin",
        "role": "admin",
        "display_name": "admin",
    }
    yield TestClient(app), repo
    app.dependency_overrides.clear()
    await db.close()


async def _a_week_at_one_minute(repo: MetricsRepository, hostname: str = "web01") -> int:
    """Seven days of load.1m at the shipped 60 s cadence, ending now. The value
    is the minute index mod 10, so every 10-minute bucket aligned to the epoch
    holds 0..9 exactly once."""
    # Starts two minutes inside the window so the request's own `since`, read a
    # moment later, cannot cut the oldest sample off.
    now = _now()
    first = now - WEEK + 120
    rows = [("load.1m", ts, float((ts // 60) % 10)) for ts in range(first, now + 1, 60)]
    await repo.insert_samples(hostname, "agent-1", rows)
    return len(rows)


# ── 3. A readable week ──────────────────────────────────────────────────────


class TestAutoResolutionReturnsTheWholeWeek:
    async def test_raw_is_still_the_default_and_still_says_it_truncated(self, api):
        client, repo = api
        await _a_week_at_one_minute(repo)
        body = client.get("/monitoring/hosts/web01/series?metric=load.1m&hours=168").json()
        assert body["resolution"] == "raw"
        assert body["bucket_seconds"] is None and body["aggregation"] is None
        assert len(body["points"]) == MAX_SERIES_POINTS
        assert body["truncated"] is True, "raw dropped most of the week without saying so"
        covered_hours = (body["points"][-1]["ts"] - body["points"][0]["ts"]) / 3600
        assert covered_hours < 34, covered_hours  # the 21% the issue measured

    async def test_auto_returns_every_sample_of_the_week_as_named_buckets(self, api):
        client, repo = api
        total = await _a_week_at_one_minute(repo)
        body = client.get(
            "/monitoring/hosts/web01/series?metric=load.1m&hours=168&resolution=auto"
        ).json()

        assert body["resolution"] == "bucketed"
        assert body["truncated"] is False
        assert body["bucket_seconds"] == 600, body["bucket_seconds"]
        assert body["aggregation"] == {"value": "avg", "also": ["min", "max", "count"]}
        points = body["points"]
        assert len(points) <= MAX_SERIES_POINTS
        # The WHOLE window: every stored sample is summarised by some bucket,
        # and the buckets span the week, not its last 33 hours.
        assert sum(p["count"] for p in points) == total
        assert points[0]["ts"] <= body["since"] + 600
        assert points[-1]["ts"] >= _now() - 1200
        # A full bucket summarises 0..9: the aggregation is exactly what it says.
        full = next(p for p in points if p["count"] == 10)
        assert (full["min"], full["max"], full["value"]) == (0.0, 9.0, 4.5)
        assert full["ts"] % 600 == 0

    async def test_auto_on_a_window_that_fits_is_raw_and_says_so(self, api):
        client, repo = api
        now = _now()
        await repo.insert_samples(
            "web01", "agent-1", [("load.1m", now - 120, 1.0), ("load.1m", now - 60, 2.0)]
        )
        body = client.get(
            "/monitoring/hosts/web01/series?metric=load.1m&hours=168&resolution=auto"
        ).json()
        assert body["resolution"] == "raw"
        assert [p["value"] for p in body["points"]] == [1.0, 2.0]
        assert body["aggregation"] is None

    async def test_an_unknown_resolution_is_refused(self, api):
        client, _ = api
        resp = client.get("/monitoring/hosts/web01/series?metric=load.1m&resolution=hourly")
        assert resp.status_code == 422

    def test_bucket_width_is_the_narrowest_round_step_that_fits(self):
        assert bucket_seconds_for(3600, MAX_SERIES_POINTS) == 60
        assert bucket_seconds_for(WEEK, MAX_SERIES_POINTS) == 600
        assert bucket_seconds_for(30 * 86400, MAX_SERIES_POINTS) == 1800
        # However the window falls on the epoch grid, it fits in `limit`.
        for window in (3600, 86400, WEEK, 30 * 86400):
            for limit in (2, 50, 500, MAX_SERIES_POINTS):
                width = bucket_seconds_for(window, limit)
                assert window // width + 1 <= limit or width > BUCKET_STEPS_SECONDS[-1]


# ── 3. A window that can only return nothing is refused ─────────────────────


class TestTheWindowIsCappedAtRetention:
    async def test_the_retention_window_itself_is_answered(self, api):
        client, _ = api
        assert (
            client.get("/monitoring/hosts/web01/series?metric=load.1m&hours=168").status_code == 200
        )

    async def test_a_window_past_retention_is_refused_with_a_sentence(self, api):
        client, _ = api
        resp = client.get("/monitoring/hosts/web01/series?metric=load.1m&hours=169")
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert detail == (
            "A 169-hour window reaches past what is kept: metric samples are kept for 7 "
            "days (168 hours) and older ones are deleted, so ask for at most 168 hours."
        )

    async def test_the_cap_follows_the_retention_in_force(self, api, retention_days):
        client, _ = api
        retention_days.days = 3
        assert client.get("/monitoring/hosts/w/series?metric=load.1m&hours=72").status_code == 200
        resp = client.get("/monitoring/hosts/w/series?metric=load.1m&hours=73")
        assert resp.status_code == 422 and "kept for 3 days" in resp.json()["detail"]

    def test_a_long_retention_is_still_capped_per_request_and_says_which_limit(self):
        from homepilot.metrics.router import MAX_WINDOW_HOURS, window_problem

        assert window_problem(700, 60) is None
        assert window_problem(800, 60) == (
            f"A 800-hour window is longer than one series request may ask for "
            f"({MAX_WINDOW_HOURS} hours)."
        )
        assert "kept for 7 days" in (window_problem(169, 7) or "")

    async def test_mcp_refuses_the_same_window_with_the_same_sentence(self, api):
        from homepilot.mcp.tools.monitoring_tools import handle_get_host_metrics_series

        _, repo = api
        with pytest.raises(ValueError, match="kept for 7 days"):
            await handle_get_host_metrics_series(
                {"host": "web01", "metric": "load.1m", "hours": 169}, {"metrics_repo": repo}
            )

    async def test_mcp_auto_is_the_route_answer(self, api):
        from homepilot.mcp.tools.monitoring_tools import handle_get_host_metrics_series

        client, repo = api
        await _a_week_at_one_minute(repo)
        route = client.get(
            "/monitoring/hosts/web01/series?metric=load.1m&hours=168&resolution=auto"
        ).json()
        tool = await handle_get_host_metrics_series(
            {"host": "web01", "metric": "load.1m", "hours": 168, "resolution": "auto"},
            {"metrics_repo": repo},
        )
        for key in ("resolution", "bucket_seconds", "aggregation", "truncated"):
            assert tool[key] == route[key], key
        assert tool["points"] == route["points"]


# ── 1. A rule on a new metric says who it cannot see ────────────────────────


async def _fleet(repo: MetricsRepository) -> None:
    """new01 runs a current agent; old01 an agent from before #661; gone01 has
    been silent for an hour (not an upgrade problem - the nodata rule's)."""
    now = _now()
    await repo.insert_samples(
        "new01",
        "a-new",
        [("disk.used_pct", now - 60, 95.0), ("disk.free_gb", now - 60, 1.5)],
    )
    await repo.insert_samples(
        "new02",
        "a-new2",
        [("disk.used_pct", now - 60, 40.0), ("disk.free_gb", now - 60, 300.0)],
    )
    await repo.insert_samples(
        "old01", "a-old", [("disk.free_gb", now - 60, 2.0), ("cpu.count", now - 60, 2.0)]
    )
    await repo.insert_samples("gone01", "a-gone", [("disk.free_gb", now - 3600, 5.0)])


class TestARuleOnANewMetricSaysWhoItCannotSee:
    async def test_the_old_agent_is_named_with_a_sentence(self, api):
        client, repo = api
        await _fleet(repo)
        created = client.post(
            "/monitoring/rules",
            json={
                "name": "Disk over 90%",
                "metric": "disk.used_pct",
                "comparison": "gt",
                "threshold": 90,
                "for_seconds": 0,
            },
        )
        assert created.status_code == 201, created.text

        result = await AlertEvaluator(repo, now=time.time).run()
        # The percentage rule fires on the full host only.
        firing = client.get("/monitoring/alerts").json()["items"]
        assert [(a["hostname"], a["metric"]) for a in firing] == [("new01", "disk.used_pct")]
        assert result.details["fired"] == 1

        rule = client.get("/monitoring/rules").json()["items"][0]
        assert rule["hosts_matched"] == 2, "two upgraded hosts report the ratio"
        assert rule["hosts_without_metric"] == 1
        assert rule["hosts_without_metric_names"] == ["old01"]
        assert rule["coverage_note"] == (
            "1 host does not report disk.used_pct yet - upgrade its agent (old01). "
            "Until then this rule does not watch it."
        )

    async def test_a_rule_every_reporting_host_can_answer_has_no_note(self, api):
        client, repo = api
        await _fleet(repo)
        client.post(
            "/monitoring/rules",
            json={"name": "f", "metric": "disk.free_gb", "comparison": "lt", "threshold": 1},
        )
        rule = client.get("/monitoring/rules").json()["items"][0]
        assert rule["hosts_without_metric"] == 0
        assert rule["coverage_note"] is None

    async def test_the_host_filter_scopes_the_note(self, api):
        client, repo = api
        await _fleet(repo)
        client.post(
            "/monitoring/rules",
            json={
                "name": "new only",
                "metric": "disk.used_pct",
                "comparison": "gt",
                "threshold": 90,
                "host_filter": "new*",
            },
        )
        rule = client.get("/monitoring/rules").json()["items"][0]
        assert rule["hosts_without_metric"] == 0 and rule["coverage_note"] is None

    async def test_a_retired_metric_does_not_tell_anyone_to_upgrade(self, repo_only):
        repo = repo_only
        await _fleet(repo)
        rule = await repo.create_rule(
            name="cores", metric="cpu.count", comparison="lt", threshold=2
        )
        [item] = await rules_with_coverage(repo, [rule])
        assert item["hosts_without_metric_names"] == ["new01", "new02"]
        assert "upgrade" not in item["coverage_note"]
        assert "no current agent does" in item["coverage_note"]

    async def test_nodata_rules_carry_no_metric_coverage(self, repo_only):
        repo = repo_only
        await _fleet(repo)
        rule = await repo.create_rule(
            name="dark", metric="*", comparison="gt", threshold=0, kind="nodata", for_seconds=300
        )
        [item] = await rules_with_coverage(repo, [rule])
        assert item["hosts_without_metric"] is None and item["coverage_note"] is None

    async def test_mcp_lists_the_same_coverage_as_the_route(self, api):
        from homepilot.mcp.tools.monitoring_tools import handle_list_alert_rules

        client, repo = api
        await _fleet(repo)
        client.post(
            "/monitoring/rules",
            json={"name": "p", "metric": "disk.used_pct", "comparison": "gt", "threshold": 90},
        )
        route = client.get("/monitoring/rules").json()
        tool = await handle_list_alert_rules({}, {"metrics_repo": repo})
        assert tool == route
        assert tool["items"][0]["hosts_without_metric_names"] == ["old01"]


@pytest.fixture
async def repo_only(tmp_path: Path):
    db = Database(str(tmp_path / "m.db"))
    await db.connect()
    await run_migrations(db)
    yield MetricsRepository(db)
    await db.close()


# ── 1 + 2. The vocabulary every surface offers ──────────────────────────────


class TestTheVocabulary:
    def test_the_ratios_are_offered_and_cpu_count_is_not(self):
        for metric in ("disk.used_pct", "memory.used_pct", "load.1m_per_core"):
            assert metric in AGENT_METRICS
        assert "cpu.count" not in AGENT_METRICS
        assert "cpu.count" in RETIRED_AGENT_METRICS

    def test_mcp_rule_pickers_offer_the_ratios(self):
        from homepilot.mcp.tools.monitoring_tools import TOOL_DEFINITIONS

        for name in ("create_alert_rule", "update_alert_rule"):
            tool = next(t for t in TOOL_DEFINITIONS if t["name"] == name)
            enum = tool["inputSchema"]["properties"]["metric"]["enum"]
            assert "disk.used_pct" in enum and "cpu.count" not in enum, name

    def test_the_api_documents_the_vocabulary_for_the_cli(self):
        """`hp api monitoring create-rule --help` is generated from OpenAPI; the
        metric field's description is where a CLI user learns the names."""
        app = FastAPI()
        app.include_router(metrics_router)
        schema = app.openapi()["components"]["schemas"]["AlertRuleIn"]["properties"]["metric"]
        assert "disk.used_pct" in schema["description"]
        assert "cpu.count" not in schema["description"]
        series = app.openapi()["paths"]["/monitoring/hosts/{hostname}/series"]["get"]
        resolution = next(p for p in series["parameters"] if p["name"] == "resolution")
        assert set(resolution["schema"]["enum"]) == {"raw", "auto"}

    def test_the_console_offers_the_ratios(self):
        source = (
            Path(__file__).resolve().parents[1] / "web/src/lib/components/AlertRules.svelte"
        ).read_text()
        for metric in ("disk.used_pct", "memory.used_pct", "load.1m_per_core"):
            assert f"'{metric}'" in source, metric

    async def test_the_hub_still_accepts_cpu_count_from_an_old_agent(self):
        from homepilot.agent_hub.server import parse_metric_sample

        now = _now()
        assert parse_metric_sample({"metric": "cpu.count", "value": 2, "clock": now}, now) == (
            "cpu.count",
            now,
            2.0,
        )
        for metric in ("disk.used_pct", "memory.used_pct", "load.1m_per_core"):
            assert parse_metric_sample({"metric": metric, "value": 1.5, "clock": now}, now)
