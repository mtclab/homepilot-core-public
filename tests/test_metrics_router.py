"""The metrics query + alert-rule API (#458 S5).

Covers the two things a caller can be lied to about: the bound on a series (how
many points come back, and whether it SAYS that some were left out), and whether
a rule change actually took.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from homepilot.auth.deps import require_token
from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.metrics.repository import MAX_SERIES_POINTS, MetricsRepository
from homepilot.metrics.router import router as metrics_router

NOW = int(time.time())


def _admin_token() -> dict[str, str]:
    return {
        "user_id": "1",
        "token_id": "1",
        "scope": "admin",
        "role": "admin",
        "display_name": "admin",
    }


@pytest.fixture
async def api(tmp_path: Path):
    db = Database(str(tmp_path / "metrics.db"))
    await db.connect()
    await run_migrations(db)
    repo = MetricsRepository(db)
    app = FastAPI()
    app.include_router(metrics_router)
    app.state.metrics_repo = repo
    app.dependency_overrides[require_token] = _admin_token
    client = TestClient(app)
    yield client, repo
    app.dependency_overrides.clear()
    await db.close()


class TestSeries:
    async def test_returns_the_window_oldest_point_first(self, api):
        client, repo = api
        await repo.insert_samples(
            "web01",
            "agent-1",
            [("load.1m", NOW - 120, 1.0), ("load.1m", NOW - 60, 2.0), ("load.1m", NOW, 3.0)],
        )
        body = client.get("/monitoring/hosts/web01/series?metric=load.1m&hours=1").json()
        assert [p["value"] for p in body["points"]] == [1.0, 2.0, 3.0]
        assert body["truncated"] is False
        assert body["max_points"] == MAX_SERIES_POINTS

    async def test_a_window_with_more_points_than_the_bound_says_so(self, api):
        client, repo = api
        await repo.insert_samples(
            "web01", "agent-1", [("load.1m", NOW - i, float(i)) for i in range(1, 60)]
        )
        body = client.get("/monitoring/hosts/web01/series?metric=load.1m&hours=1&limit=10").json()
        assert len(body["points"]) == 10
        assert body["truncated"] is True, "the caller was not told points were left out"
        # The NEWEST points are kept: a chart that silently showed the oldest ten
        # would be describing the wrong hour.
        assert body["points"][-1]["ts"] == NOW - 1

    async def test_an_unknown_host_is_an_empty_series_not_an_error(self, api):
        client, _ = api
        body = client.get("/monitoring/hosts/nope/series?metric=load.1m").json()
        assert body["points"] == []

    async def test_the_limit_cannot_be_raised_above_the_hard_ceiling(self, api):
        client, _ = api
        resp = client.get(
            f"/monitoring/hosts/web01/series?metric=load.1m&limit={MAX_SERIES_POINTS + 1}"
        )
        assert resp.status_code == 422

    async def test_latest_is_the_newest_value_per_metric(self, api):
        client, repo = api
        await repo.insert_samples(
            "web01",
            "agent-1",
            [
                ("load.1m", NOW - 60, 1.0),
                ("load.1m", NOW, 9.0),
                ("disk.free_gb", NOW, 42.0),
            ],
        )
        body = client.get("/monitoring/hosts/web01/latest").json()
        values = {m["metric"]: m["value"] for m in body["metrics"]}
        assert values == {"load.1m": 9.0, "disk.free_gb": 42.0}


class TestAlertRuleApi:
    def _rule_body(self, **over):
        return {
            "name": "load high",
            "metric": "load.1m",
            "comparison": "gt",
            "threshold": 4.0,
            "for_seconds": 300,
            "host_filter": "*",
            **over,
        }

    async def test_create_list_silence_and_delete(self, api):
        client, _ = api
        created = client.post("/monitoring/rules", json=self._rule_body())
        assert created.status_code == 201, created.text
        rule_id = created.json()["id"]
        assert created.json()["for_seconds"] == 300

        listed = client.get("/monitoring/rules").json()
        assert listed["total"] == 1

        silenced = client.patch(f"/monitoring/rules/{rule_id}", json={"enabled": False})
        assert silenced.status_code == 200
        assert silenced.json()["enabled"] == 0
        # Silencing is not deleting: the rule is still there to re-enable.
        assert client.get("/monitoring/rules").json()["total"] == 1

        assert client.delete(f"/monitoring/rules/{rule_id}").status_code == 200
        assert client.get("/monitoring/rules").json()["total"] == 0

    async def test_an_unknown_rule_is_a_404_on_both_paths(self, api):
        client, _ = api
        assert client.delete("/monitoring/rules/nope").status_code == 404
        assert client.patch("/monitoring/rules/nope", json={"enabled": True}).status_code == 404

    async def test_an_invalid_comparison_is_refused(self, api):
        client, _ = api
        resp = client.post("/monitoring/rules", json=self._rule_body(comparison="roughly"))
        assert resp.status_code == 422

    async def test_a_duration_beyond_a_day_is_refused(self, api):
        client, _ = api
        resp = client.post("/monitoring/rules", json=self._rule_body(for_seconds=86401))
        assert resp.status_code == 422

    async def test_alerts_lists_what_is_firing_with_its_rule(self, api):
        client, repo = api
        rule = await repo.create_rule(
            name="load high", metric="load.1m", comparison="gt", threshold=4.0
        )
        await repo.set_alert_state(rule["id"], "web01", "2026-08-20T10:00:00Z", 9.9)

        body = client.get("/monitoring/alerts").json()
        assert body["total"] == 1
        item = body["items"][0]
        assert item["hostname"] == "web01"
        assert item["name"] == "load high"
        assert item["threshold"] == 4.0
