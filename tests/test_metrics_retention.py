"""Retention actually deletes (#458 S5).

Retention is the ONLY thing bounding the metrics table - the slice ships no
rollups on purpose (ADR-004: measure a real week first). A pruner that runs and
deletes nothing would therefore be an unbounded table with a reassuring log line,
so the assertion here is on the ROWS, not on the pruner returning success.

Also measures the real storage cost of the default window, so the "do we need
rollups" decision later is made on numbers rather than on a guess.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.metrics.repository import MetricsRepository
from homepilot.metrics.retention import SECONDS_PER_DAY, MetricsPruner

NOW = 1_800_000_000

# What one agent reports at the shipped defaults.
METRICS_PER_SAMPLE = 8
SAMPLES_PER_DAY = 24 * 60  # one per minute


@pytest.fixture
async def metrics_db(tmp_path: Path):
    db = Database(str(tmp_path / "metrics.db"))
    await db.connect()
    await run_migrations(db)
    yield db
    await db.close()


@pytest.fixture
def repo(metrics_db) -> MetricsRepository:
    return MetricsRepository(metrics_db)


class TestRetention:
    async def test_eight_day_old_samples_are_pruned_and_six_day_old_ones_survive(self, repo):
        await repo.insert_samples(
            "web01",
            "agent-1",
            [
                ("load.1m", NOW - 8 * SECONDS_PER_DAY, 8.0),
                ("load.1m", NOW - 7 * SECONDS_PER_DAY - 60, 7.0),
                ("load.1m", NOW - 6 * SECONDS_PER_DAY, 6.0),
                ("load.1m", NOW - 60, 1.0),
            ],
        )

        result = await MetricsPruner(repo, retention_days=7, now=lambda: float(NOW)).run()

        assert result.details["deleted"] == 2, result.details
        remaining = await repo.db.fetchall("SELECT ts FROM metrics ORDER BY ts")
        kept = [int(r["ts"]) for r in remaining]
        assert NOW - 6 * SECONDS_PER_DAY in kept, "a six-day-old sample was pruned"
        assert NOW - 60 in kept
        assert NOW - 8 * SECONDS_PER_DAY not in kept, "an eight-day-old sample survived"

    async def test_retention_days_is_configurable(self, repo):
        await repo.insert_samples(
            "web01",
            "agent-1",
            [("load.1m", NOW - 2 * SECONDS_PER_DAY, 1.0), ("load.1m", NOW - 60, 1.0)],
        )

        result = await MetricsPruner(repo, retention_days=1, now=lambda: float(NOW)).run()

        assert result.details["deleted"] == 1
        assert result.details["retention_days"] == 1

    async def test_a_pruner_with_nothing_to_do_is_not_an_error(self, repo):
        await repo.insert_samples("web01", "agent-1", [("load.1m", NOW - 60, 1.0)])
        result = await MetricsPruner(repo, retention_days=7, now=lambda: float(NOW)).run()
        assert result.success is True
        assert result.details["deleted"] == 0

    async def test_a_zero_retention_is_clamped_to_one_day(self, repo):
        """A misconfigured 0 must not mean "delete everything the moment it
        arrives" - the pruner floors at one day rather than emptying the table."""
        pruner = MetricsPruner(repo, retention_days=0, now=lambda: float(NOW))
        assert pruner.cutoff_ts == NOW - SECONDS_PER_DAY


class TestMeasuredStorageCost:
    """The number behind "should we build rollups". Measured, not estimated:
    seven days of one agent's real output at the shipped defaults, then the
    database file's own size."""

    async def test_seven_days_of_one_agent_fits_the_documented_budget(self, repo, metrics_db):
        metrics = [
            "cpu.count",
            "disk.total_gb",
            "disk.free_gb",
            "memory.total_gb",
            "memory.free_gb",
            "load.1m",
            "load.5m",
            "load.15m",
        ]
        assert len(metrics) == METRICS_PER_SAMPLE
        start = NOW - 7 * SECONDS_PER_DAY
        for day in range(7):
            batch: list[tuple[str, int, float]] = []
            for i in range(SAMPLES_PER_DAY):
                ts = start + day * SECONDS_PER_DAY + i * 60
                for n, metric in enumerate(metrics):
                    batch.append((metric, ts, 0.5 + n + (i % 7) / 10))
            await repo.insert_samples("web01", "agent-1", batch)

        rows = (await metrics_db.fetchone("SELECT COUNT(*) c FROM metrics"))["c"]
        assert rows == 7 * SAMPLES_PER_DAY * METRICS_PER_SAMPLE == 80_640

        page_size = (await metrics_db.fetchone("PRAGMA page_size"))["page_size"]
        used = await metrics_db.fetchall(
            "SELECT SUM(pgsize) AS bytes FROM dbstat WHERE name IN ('metrics', 'idx_metrics_ts')"
        )
        measured = int(used[0]["bytes"]) if used and used[0]["bytes"] else 0
        per_row = measured / rows

        # MEASURED on this schema, 2026-08-20: 80,640 rows occupy 6.26 MB across
        # the WITHOUT ROWID table (3.81 MB) and idx_metrics_ts (2.46 MB) - 77.7
        # bytes per row, so ~63 MB for ten hosts and ~313 MB for fifty at the
        # 7-day default. That is the number the "do rollups earn their
        # complexity" decision should be made on. The bounds below are loose
        # enough not to be flaky and tight enough that a row growing by half
        # again fails here rather than on someone's disk.
        assert 0 < per_row < 100, f"{per_row:.1f} bytes/row (page_size={page_size})"
        assert measured < 8 * 1024 * 1024, f"7 days of one agent used {measured} bytes"

    async def test_measurement_matches_the_documented_default(self):
        """The README/ARCHITECTURE figure is derived from these constants, so a
        change to the default cadence has to come here too."""
        assert 7 * SAMPLES_PER_DAY * METRICS_PER_SAMPLE == 80_640
        assert time.gmtime(NOW).tm_year > 2020
