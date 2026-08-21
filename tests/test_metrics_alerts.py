"""Duration-condition alerting (#458 S5).

The point of `for_seconds` is that a SPIKE must not page anyone and a condition
that really persists must. Both directions are asserted here, against real
stored samples and the real evaluator, plus the recovery half - an alerting
system that only reports breakage trains people to ignore it.

Teeth: delete the ``covered`` term in ``metrics.alerts.condition_holds`` (so a
breach that began inside the window counts) and
``test_a_single_spike_does_not_fire_a_five_minute_rule`` goes red.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.metrics.alerts import AlertEvaluator
from homepilot.metrics.repository import MetricsRepository

NOW = 1_800_000_000
FIVE_MINUTES = 300


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


class _Bus:
    """Captures what emit_event was asked to deliver."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, event_type: str, payload: dict[str, Any], repo: Any = None) -> None:
        self.events.append((event_type, dict(payload)))

    def names(self) -> list[str]:
        return [name for name, _ in self.events]


@pytest.fixture
def bus(monkeypatch) -> _Bus:
    from homepilot.metrics import alerts as alerts_mod

    captured = _Bus()
    monkeypatch.setattr(alerts_mod, "emit_event", captured)
    return captured


def _evaluator(repo: MetricsRepository, now: int = NOW) -> AlertEvaluator:
    return AlertEvaluator(repo, now=lambda: float(now))


async def _seed(
    repo: MetricsRepository,
    values: list[tuple[int, float]],
    hostname: str = "web01",
    metric: str = "load.1m",
) -> None:
    """Seed samples given as (seconds BEFORE now, value)."""
    await repo.insert_samples(
        hostname, "agent-1", [(metric, NOW - offset, value) for offset, value in values]
    )


async def _rule(repo: MetricsRepository, for_seconds: int = FIVE_MINUTES) -> dict[str, Any]:
    return await repo.create_rule(
        name="load too high",
        metric="load.1m",
        comparison="gt",
        threshold=4.0,
        for_seconds=for_seconds,
        host_filter="web01",
    )


class TestDurationCondition:
    async def test_a_single_spike_does_not_fire_a_five_minute_rule(self, repo, bus):
        """Ten minutes of calm with ONE breaching sample at the end. The value is
        over the threshold RIGHT NOW, so a rule without a duration condition
        would page - this one must not."""
        calm = [(offset, 1.0) for offset in range(600, 60, -60)]
        await _seed(repo, [*calm, (0, 9.9)])
        await _rule(repo)

        result = await _evaluator(repo).run()

        assert result.details["fired"] == 0, "a single spike fired a five-minute rule"
        assert bus.names() == []
        firing = await repo.list_firing()
        assert firing == []

    async def test_a_breach_that_persists_for_the_whole_duration_fires(self, repo, bus):
        """The same rule, the same threshold - but the condition has held for the
        full five minutes."""
        calm = [(offset, 1.0) for offset in range(900, 600, -60)]
        breach = [(offset, 9.9) for offset in range(540, -1, -60)]
        await _seed(repo, [*calm, *breach])
        rule = await _rule(repo)

        result = await _evaluator(repo).run()

        assert result.details["fired"] == 1, result.details
        assert bus.names() == ["alert_firing"]
        payload = bus.events[0][1]
        assert payload["hostname"] == "web01"
        assert payload["metric"] == "load.1m"
        assert payload["threshold"] == 4.0
        assert payload["for_seconds"] == FIVE_MINUTES
        assert payload["value"] == 9.9

        firing = await repo.list_firing()
        assert len(firing) == 1
        assert firing[0]["rule_id"] == rule["id"]

    async def test_a_breach_that_only_just_started_does_not_fire_yet(self, repo, bus):
        """Two minutes into a five-minute rule: really breaching, not yet long
        enough. This is the case a naive "is it over the line now" check gets
        wrong in the other direction from the spike."""
        calm = [(offset, 1.0) for offset in range(900, 120, -60)]
        breach = [(offset, 9.9) for offset in range(120, -1, -60)]
        await _seed(repo, [*calm, *breach])
        await _rule(repo)

        assert (await _evaluator(repo).run()).details["fired"] == 0
        assert bus.names() == []

    async def test_a_breach_interrupted_by_one_good_sample_restarts_the_clock(self, repo, bus):
        """Eight minutes of breaching with ONE healthy sample four minutes ago.
        The condition has NOT held continuously, so it must not fire."""
        early = [(offset, 9.9) for offset in range(600, 240, -60)]
        recovered = [(240, 1.0)]
        late = [(offset, 9.9) for offset in range(180, -1, -60)]
        await _seed(repo, [*early, *recovered, *late])
        await _rule(repo)

        assert (await _evaluator(repo).run()).details["fired"] == 0
        assert bus.names() == []

    async def test_one_sample_from_a_slow_reporting_agent_is_not_five_minutes(self, repo, bus):
        """An agent on a five-minute cadence produces ONE breaching sample that
        sits comfortably inside a five-minute window. The window says "the breach
        started before the window opened"; the elapsed time says one sample.
        One sample is not a duration.

        Teeth: drop the ``held`` term in ``metrics.alerts.condition_holds`` and
        this fires on a single point."""
        await _seed(repo, [(850, 1.0), (550, 1.0), (250, 9.9)])
        await _rule(repo)

        result = await _evaluator(repo).run()

        assert result.details["fired"] == 0, "a single sample was accepted as a five-minute breach"
        assert bus.names() == []

    async def test_a_forward_clock_jump_cannot_manufacture_a_duration(self, repo, bus):
        """A host whose clock jumps forward mid-breach has two samples that LOOK
        five minutes apart while ten real seconds passed. Duration alone is
        fooled by that; the window check is what is not.

        Teeth: drop the ``covered`` term in ``metrics.alerts.condition_holds``
        and this fires on ten seconds of breach."""
        calm = [(offset, 1.0) for offset in range(900, 10, -60)]
        await _seed(repo, calm)
        # The breach: a real sample ten seconds ago, then the clock jumps +300.
        await repo.insert_samples(
            "web01",
            "agent-1",
            [("load.1m", NOW - 10, 9.9), ("load.1m", NOW + 290, 9.9)],
        )
        await _rule(repo)

        result = await _evaluator(repo).run()

        assert result.details["fired"] == 0, "a clock jump was accepted as five minutes of breach"
        assert bus.names() == []

    async def test_a_host_that_has_only_just_started_reporting_does_not_fire(self, repo, bus):
        """A fresh agent whose first sample breaches has no history to have held
        anything for five minutes. Without the coverage check this fires
        instantly on enrolment - the classic false page."""
        await _seed(repo, [(30, 9.9), (0, 9.9)])
        await _rule(repo)

        assert (await _evaluator(repo).run()).details["fired"] == 0
        assert bus.names() == []


class TestRecovery:
    async def test_recovery_fires_when_the_condition_clears(self, repo, bus):
        calm = [(offset, 1.0) for offset in range(1200, 600, -60)]
        breach = [(offset, 9.9) for offset in range(540, -1, -60)]
        await _seed(repo, [*calm, *breach])
        await _rule(repo)
        assert (await _evaluator(repo).run()).details["fired"] == 1

        # The host comes back within limits.
        await repo.insert_samples("web01", "agent-1", [("load.1m", NOW + 60, 0.9)])
        result = await _evaluator(repo, now=NOW + 60).run()

        assert result.details["resolved"] == 1, result.details
        assert bus.names() == ["alert_firing", "alert_resolved"]
        resolved = bus.events[1][1]
        assert resolved["hostname"] == "web01"
        assert resolved["value"] == 0.9
        assert resolved["duration_seconds"] is not None
        assert await repo.list_firing() == []

    async def test_a_firing_alert_notifies_once_not_every_evaluation(self, repo, bus):
        calm = [(offset, 1.0) for offset in range(1200, 600, -60)]
        breach = [(offset, 9.9) for offset in range(540, -1, -60)]
        await _seed(repo, [*calm, *breach])
        await _rule(repo)

        await _evaluator(repo).run()
        await repo.insert_samples("web01", "agent-1", [("load.1m", NOW + 60, 9.9)])
        await _evaluator(repo, now=NOW + 60).run()

        assert bus.names() == ["alert_firing"], "the alert re-announced itself"

    async def test_a_stale_series_does_not_resolve_an_alert_by_itself(self, repo, bus):
        """An agent that goes silent while breaching has not recovered. Turning
        missing data into a recovery is how an outage looks like a fix."""
        calm = [(offset, 1.0) for offset in range(1200, 600, -60)]
        breach = [(offset, 9.9) for offset in range(540, -1, -60)]
        await _seed(repo, [*calm, *breach])
        await _rule(repo)
        await _evaluator(repo).run()

        # An hour later, nothing new has arrived.
        result = await _evaluator(repo, now=NOW + 3600).run()

        assert result.details["resolved"] == 0
        assert bus.names() == ["alert_firing"]
        assert len(await repo.list_firing()) == 1


class TestRuleScope:
    async def test_a_host_filter_evaluates_only_that_host(self, repo, bus):
        breach = [(offset, 9.9) for offset in range(900, -1, -60)]
        await _seed(repo, breach, hostname="web01")
        await _seed(repo, breach, hostname="db01")
        await _rule(repo)  # host_filter="web01"

        result = await _evaluator(repo).run()

        assert result.details["evaluated"] == 1
        assert [e[1]["hostname"] for e in bus.events] == ["web01"]

    async def test_a_wildcard_rule_fires_per_host(self, repo, bus):
        breach = [(offset, 9.9) for offset in range(900, -1, -60)]
        await _seed(repo, breach, hostname="web01")
        await _seed(repo, breach, hostname="db01")
        await repo.create_rule(
            name="fleet load",
            metric="load.1m",
            comparison="gt",
            threshold=4.0,
            for_seconds=FIVE_MINUTES,
            host_filter="*",
        )

        result = await _evaluator(repo).run()

        assert result.details["fired"] == 2
        assert sorted(e[1]["hostname"] for e in bus.events) == ["db01", "web01"]

    async def test_a_disabled_rule_is_not_evaluated(self, repo, bus):
        breach = [(offset, 9.9) for offset in range(900, -1, -60)]
        await _seed(repo, breach)
        rule = await _rule(repo)
        await repo.set_rule_enabled(rule["id"], False)

        assert (await _evaluator(repo).run()).details["evaluated"] == 0
        assert bus.names() == []

    async def test_deleting_a_rule_clears_its_firing_state(self, repo, bus):
        calm = [(offset, 1.0) for offset in range(1200, 600, -60)]
        breach = [(offset, 9.9) for offset in range(540, -1, -60)]
        await _seed(repo, [*calm, *breach])
        rule = await _rule(repo)
        await _evaluator(repo).run()
        assert len(await repo.list_firing()) == 1

        assert await repo.delete_rule(rule["id"]) is True
        assert await repo.list_firing() == []

    async def test_an_invalid_comparison_is_refused_at_the_storage_layer(self, repo):
        with pytest.raises(ValueError, match="Invalid comparison"):
            await repo.create_rule(
                name="nonsense", metric="load.1m", comparison="approximately", threshold=1.0
            )
