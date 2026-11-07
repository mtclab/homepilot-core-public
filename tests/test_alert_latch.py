"""THE GATE for #714: an alert the evaluator will never revisit must not stay firing.

The evaluator only looks at (enabled rule, host inside the rule's filter) pairs.
So a firing row outside that set was never re-decided and stayed firing forever:

* **disable a rule** - its alert went on being listed as firing, on the
  Overview count and in /alerts, even after the disk was fixed;
* **narrow its host filter** - the host taken out of scope stayed firing for
  good.

Asserted on what an operator sees - `list_firing` (what /alerts serves) and the
Overview's `firing_alerts` count, which must agree - not on the state table.
And the other half of the contract: a threshold retune KEEPS the firing state,
because the same hosts are still watched and a real breach's start matters.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from homepilot.dashboard.service import build_summary
from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.db.repository import Repository
from homepilot.metrics.alerts import AlertEvaluator
from homepilot.metrics.repository import MetricsRepository

pytestmark = pytest.mark.asyncio


class World:
    def __init__(self, db: Database) -> None:
        self.db = db
        self.metrics = MetricsRepository(db)
        self.repo = Repository(db)
        self.evaluator = AlertEvaluator(self.metrics)
        self.now = int(time.time())

    async def low_disk(self, *hosts: str) -> None:
        for host in hosts:
            await self.metrics.insert_samples(
                host, "agent", [("disk.free_gb", self.now - s, 0.5) for s in range(0, 900, 60)]
            )

    async def firing(self) -> list[str]:
        listed = sorted(r["hostname"] for r in await self.metrics.list_firing())
        counted = (await build_summary(self.repo))["metrics"]["firing_alerts"]
        assert counted == len(listed), (
            f"the Overview counts {counted} firing while /alerts lists {listed}"
        )
        return listed


@pytest.fixture
async def world(tmp_path: Path):
    db = Database(str(tmp_path / "m.db"))
    await db.connect()
    await run_migrations(db)
    try:
        yield World(db)
    finally:
        await db.close()


async def _firing_rule(w: World) -> dict:
    await w.low_disk("web-1", "db-1")
    rule = await w.metrics.create_rule("low disk", "disk.free_gb", "lt", 1.0, for_seconds=300)
    await w.evaluator.run()
    assert await w.firing() == ["db-1", "web-1"]
    return rule


async def test_disabling_a_firing_rule_stops_it_firing(world: World):
    rule = await _firing_rule(world)

    await world.metrics.set_rule_enabled(rule["id"], False)
    await world.evaluator.run()

    assert await world.firing() == [], "a DISABLED rule's alerts are still listed as firing"


async def test_re_enabling_starts_from_a_fresh_evaluation(world: World):
    rule = await _firing_rule(world)
    await world.metrics.set_rule_enabled(rule["id"], False)
    await world.metrics.set_rule_enabled(rule["id"], True)
    await world.evaluator.run()

    assert await world.firing() == ["db-1", "web-1"]


async def test_narrowing_the_host_filter_releases_the_host_taken_out_of_scope(world: World):
    rule = await _firing_rule(world)

    await world.metrics.update_rule(rule["id"], host_filter="db-*")
    await world.evaluator.run()

    assert await world.firing() == ["db-1"], "web-1 is outside the rule now, yet still firing"


async def test_changing_the_metric_drops_verdicts_about_the_old_one(world: World):
    rule = await _firing_rule(world)

    await world.metrics.update_rule(rule["id"], metric="mem.free_mb")
    await world.evaluator.run()

    assert await world.firing() == []


async def test_a_threshold_retune_keeps_the_breach_it_is_still_watching(world: World):
    rule = await _firing_rule(world)
    before = {r["hostname"]: r["firing_since"] for r in await world.metrics.list_firing()}

    await world.metrics.update_rule(rule["id"], threshold=2.0)
    await world.evaluator.run()

    after = {r["hostname"]: r["firing_since"] for r in await world.metrics.list_firing()}
    assert after == before, "a retune on the same hosts lost when the breach began"
