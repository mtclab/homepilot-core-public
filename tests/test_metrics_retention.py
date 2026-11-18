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
from homepilot.metrics.repository import AGENT_METRICS, MetricsRepository
from homepilot.metrics.retention import SECONDS_PER_DAY, MetricsPruner

NOW = 1_800_000_000

# What one agent reports at the shipped defaults (#661: cpu.count retired, the
# three ratios added - ten series).
METRICS_PER_SAMPLE = len(AGENT_METRICS)
SAMPLES_PER_DAY = 24 * 60  # one per minute

# Realistic identities (#661). The primary key repeats hostname and metric in
# every row and idx_metrics_ts repeats the whole key, and every row carries the
# agent id, so the bytes per row depend on how long those strings are. The gate
# measured 77.7 B/row with "web01" and "agent-1" while a real database measured
# 128.9: real agent ids are 36-character UUIDs, and real hostnames are longer
# than five characters.
REALISTIC_HOSTNAME = "docker-host-01"
REALISTIC_AGENT_ID = "6f1c2b9e-4a7d-4c3e-9b8a-2d5f0e1a7c44"


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
        metrics = list(AGENT_METRICS)
        assert len(metrics) == METRICS_PER_SAMPLE
        start = NOW - 7 * SECONDS_PER_DAY
        for day in range(7):
            batch: list[tuple[str, int, float]] = []
            for i in range(SAMPLES_PER_DAY):
                ts = start + day * SECONDS_PER_DAY + i * 60
                for n, metric in enumerate(metrics):
                    # Two-decimal fractional values, as the agent rounds them: a
                    # whole-number REAL is stored as an integer and would flatter
                    # the measurement.
                    batch.append((metric, ts, round(0.37 + n * 3.1 + (i % 97) / 7, 2)))
            await repo.insert_samples(REALISTIC_HOSTNAME, REALISTIC_AGENT_ID, batch)

        rows = (await metrics_db.fetchone("SELECT COUNT(*) c FROM metrics"))["c"]
        assert rows == 7 * SAMPLES_PER_DAY * METRICS_PER_SAMPLE == 100_800

        page_size = (await metrics_db.fetchone("PRAGMA page_size"))["page_size"]
        used = await metrics_db.fetchall(
            "SELECT SUM(pgsize) AS bytes FROM dbstat WHERE name IN ('metrics', 'idx_metrics_ts')"
        )
        measured = int(used[0]["bytes"]) if used and used[0]["bytes"] else 0
        per_row = measured / rows

        # MEASURED on this schema, 2026-11-18 (#661), with a UUID agent id and a
        # 14-character hostname: 100,800 rows (ten series) occupy 13.6 MB across
        # the WITHOUT ROWID table and idx_metrics_ts - 135 bytes per row, within
        # 5% of the 128.9 a real dev database measured. So ~13.6 MB per host per
        # week, ~136 MB for ten hosts and ~680 MB for fifty at the 7-day
        # default. The old figure here (77.7 B/row) came from "web01" and
        # "agent-1" and was ~66% optimistic. Bounds: loose enough not to be
        # flaky, tight enough that a row growing by a fifth fails here rather
        # than on someone's disk.
        assert per_row < 160, f"{per_row:.1f} bytes/row (page_size={page_size})"
        # A floor too: this number is what capacity is planned on, and a
        # measurement that comes back far under the real database's is
        # flattering itself (short synthetic identities, whole-number values).
        assert per_row > 110, (
            f"{per_row:.1f} bytes/row is well under the 128.9 a real database measured - "
            "is the fixture back to synthetic hostnames or agent ids?"
        )
        assert measured < 16 * 1024 * 1024, f"7 days of one agent used {measured} bytes"

    async def test_measurement_matches_the_documented_default(self):
        """The README/ARCHITECTURE figure is derived from these constants, so a
        change to the default cadence has to come here too."""
        assert 7 * SAMPLES_PER_DAY * METRICS_PER_SAMPLE == 100_800
        assert time.gmtime(NOW).tm_year > 2020
