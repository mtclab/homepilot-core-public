"""ADR-004 S5's claim, tested rather than restated (#648 tranche 5).

    "Nothing installs, imports or configures; monitoring is part of the
    product."

    "Every new feature is now judged by 'what must an operator do by hand for
    this to work?', where the acceptable answer is 'nothing'."

Collection satisfied that from the first release. ALERTING did not: a fresh
install had zero rules, the Overview's ``firing_alerts: 0`` read as "all well"
when it meant "nothing is being looked at", the checklist called the install
complete without ever mentioning monitoring, and the startup self-check's
consequence line for a missing events webhook named only "artifact and task
events" - the one sentence in the product that had to say alerts go nowhere.

Teeth: delete the ``seed_default_alert_rules`` call from ``main.py`` and the
seeding tests go red; drop ``rules_enabled`` from the dashboard payload and the
summary tests go red; put "Artifact and task events" back in the self-check and
the wording test goes red.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.db.repository import Repository
from homepilot.metrics.defaults import (
    DEFAULT_ALERT_RULES,
    HOST_WENT_DARK_RULE,
    NODATA_SEED_MARKER_KEY,
    SEED_MARKER_KEY,
    seed_default_alert_rules,
)
from homepilot.metrics.repository import AGENT_METRICS, MetricsRepository

# The threshold floors plus "Host went dark" (#660).
ALL_DEFAULTS = len(DEFAULT_ALERT_RULES) + 1


@pytest.fixture
async def db(tmp_path: Path):
    database = Database(str(tmp_path / "hp.db"))
    await database.connect()
    await run_migrations(database)
    yield database
    await database.close()


@pytest.fixture
def repo(db) -> Repository:
    return Repository(db)


@pytest.fixture
def metrics(db) -> MetricsRepository:
    return MetricsRepository(db)


class TestAFreshInstallWatchesSomething:
    async def test_the_defaults_are_created_on_first_boot(self, repo, metrics):
        created = await seed_default_alert_rules(repo, metrics)

        assert created == ALL_DEFAULTS
        rules = await metrics.list_rules(enabled_only=True)
        assert len(rules) == ALL_DEFAULTS, (
            "a fresh install must WATCH something, not merely store samples - ADR-004 corollary 2"
        )

    async def test_they_are_not_created_twice(self, repo, metrics):
        await seed_default_alert_rules(repo, metrics)
        assert await seed_default_alert_rules(repo, metrics) == 0
        assert len(await metrics.list_rules()) == ALL_DEFAULTS

    async def test_a_deleted_default_stays_deleted(self, repo, metrics):
        """The marker guards on "has this ever been seeded", not on "are there
        any rules" - otherwise a default an operator deliberately removed comes
        back on the next restart, which is worse than never having it."""
        await seed_default_alert_rules(repo, metrics)
        for rule in await metrics.list_rules():
            await metrics.delete_rule(rule["id"])

        assert await seed_default_alert_rules(repo, metrics) == 0
        assert await metrics.list_rules() == []

    async def test_every_default_is_on_a_metric_the_fleet_reports(self):
        """A seeded rule that can never fire would be worse than none: it makes
        the Overview look watched when it is not."""
        for spec in DEFAULT_ALERT_RULES:
            assert spec["metric"] in AGENT_METRICS, spec
            assert spec["host_filter"] == "*"
            assert spec["for_seconds"] > 0, "a default with no duration would page on a blip"

    async def test_an_upgrade_never_adds_threshold_rules_to_an_existing_policy(self, repo, metrics):
        """An operator with five tuned rules did not ask for two more threshold
        floors. The absence of rules is what that fixes; an existing policy is
        not it. "Host went dark" IS added: no existing policy could express it,
        so none can have chosen to go without it (#660)."""
        await metrics.create_rule(
            name="mine", metric="load.1m", comparison="gt", threshold=4.0, for_seconds=300
        )

        assert await seed_default_alert_rules(repo, metrics) == 1
        assert [r["name"] for r in await metrics.list_rules()] == [
            "mine",
            HOST_WENT_DARK_RULE["name"],
        ]
        assert await repo.get_setting(SEED_MARKER_KEY) is not None, (
            "the question must not be re-asked on every boot"
        )
        assert await repo.get_setting(NODATA_SEED_MARKER_KEY) is not None

    async def test_host_went_dark_is_a_five_minute_nodata_rule_on_every_host(self, repo, metrics):
        await seed_default_alert_rules(repo, metrics)
        dark = [r for r in await metrics.list_rules() if r["kind"] == "nodata"]
        assert len(dark) == 1
        assert dark[0]["for_seconds"] == 300
        assert dark[0]["host_filter"] == "*"
        assert dark[0]["enabled"] == 1

    async def test_an_install_that_already_has_a_nodata_rule_gets_no_second(self, repo, metrics):
        await metrics.create_rule(
            name="my dark rule",
            kind="nodata",
            metric="*",
            comparison="gt",
            threshold=0,
            for_seconds=600,
        )
        await seed_default_alert_rules(repo, metrics)
        assert [r["name"] for r in await metrics.list_rules() if r["kind"] == "nodata"] == [
            "my dark rule"
        ]

    async def test_a_seeding_failure_does_not_stop_the_instance_booting(self, repo, metrics):
        """A control plane that will not start because it could not write a
        convenience rule is a worse outcome than starting without it."""

        class Broken:
            async def list_rules(self, **kwargs):
                return []

            async def create_rule(self, **kwargs):
                raise RuntimeError("disk full")

        assert await seed_default_alert_rules(repo, Broken()) == 0
        assert await repo.get_setting(SEED_MARKER_KEY) is None, (
            "the marker must not be written when nothing was created, or the "
            "next boot would never retry"
        )

    def test_the_lifespan_actually_awaits_it(self):
        """Structural, not textual: an import of the seeder with no call to it
        would satisfy a substring check and leave a fresh install watching
        nothing. This looks for the awaited CALL, inside the lifespan."""
        import ast

        source = (Path(__file__).resolve().parents[1] / "src" / "homepilot" / "main.py").read_text()
        tree = ast.parse(source)
        lifespans = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "lifespan"
        ]
        assert lifespans, "main.py no longer defines an async lifespan"
        calls = [
            node
            for fn in lifespans
            for node in ast.walk(fn)
            if isinstance(node, ast.Await)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "seed_default_alert_rules"
        ]
        assert calls, (
            "the lifespan never awaits seed_default_alert_rules, so a fresh "
            "install watches nothing again (ADR-004 corollary 2)"
        )
        assert len(calls[0].value.args) == 2, "the seeder needs both repositories"


class TestTheOverviewCanTellAllWellFromNotWatched:
    async def _summary(self, db, repo, metrics):
        from homepilot.dashboard.service import build_summary

        return await build_summary(repo)

    async def test_no_rules_is_reported_as_no_rules_not_as_zero_alerts(self, db, repo, metrics):
        summary = await self._summary(db, repo, metrics)
        assert summary["metrics"]["firing_alerts"] == 0
        assert summary["metrics"]["rules_enabled"] == 0, (
            "firing_alerts: 0 alone cannot distinguish a healthy fleet from an "
            "unwatched one, and it sits on the first screen an operator sees"
        )

    async def test_an_inert_rule_is_counted_as_watching_nothing(self, db, repo, metrics):
        rule = await metrics.create_rule(
            name="db disks",
            metric="disk.free_gb",
            comparison="lt",
            threshold=1.0,
            for_seconds=300,
            host_filter="db-*",
        )
        await metrics.record_rule_coverage(rule["id"], 0)
        live = await metrics.create_rule(
            name="fleet disks",
            metric="disk.free_gb",
            comparison="lt",
            threshold=1.0,
            for_seconds=300,
        )
        await metrics.record_rule_coverage(live["id"], 3)

        summary = await self._summary(db, repo, metrics)
        assert summary["metrics"]["rules_enabled"] == 2
        assert summary["metrics"]["rules_watching_nothing"] == 1
        assert summary["metrics"]["nodata_rules_watching_nothing"] == 0

    async def test_a_went_dark_rule_watching_nothing_is_counted_apart(self, db, repo, metrics):
        """Release 3.7.0 cross-lane fix: a nodata rule matches no host because no
        matching host has an agent, not because of a metric name - the Overview
        words it differently, so the summary has to say which rules they are."""
        dark = await metrics.create_rule(
            name="Host went dark",
            kind="nodata",
            metric="*",
            comparison="gt",
            threshold=0.0,
            for_seconds=300,
        )
        await metrics.record_rule_coverage(dark["id"], 0)
        typo = await metrics.create_rule(
            name="typo", metric="disk.fre_gb", comparison="lt", threshold=1.0, for_seconds=300
        )
        await metrics.record_rule_coverage(typo["id"], 0)

        summary = await self._summary(db, repo, metrics)
        assert summary["metrics"]["rules_watching_nothing"] == 2
        assert summary["metrics"]["nodata_rules_watching_nothing"] == 1

    async def test_a_silenced_rule_is_not_counted_as_watching(self, db, repo, metrics):
        rule = await metrics.create_rule(
            name="r", metric="load.1m", comparison="gt", threshold=4.0, for_seconds=300
        )
        await metrics.set_rule_enabled(rule["id"], False)

        summary = await self._summary(db, repo, metrics)
        assert summary["metrics"]["rules_enabled"] == 0
        assert summary["metrics"]["rules_watching_nothing"] == 0


class TestTheOverviewReadsWhatTheEvaluatorStamped:
    """Release 3.7.0 dev walk: right after the upgrade started, the Overview said
    the freshly seeded "Host went dark" rule watched no host while the dev agent
    was enrolled and connected.

    The rule had simply not been evaluated yet (the evaluator's first pass comes
    a minute after boot), and the summary counted ``hosts_matched IS NULL`` as
    zero - while Settings -> Monitoring, reading the same row, said "not
    evaluated yet". Unknown is not "watching nothing". These read the summary
    the way the dashboard route does (``build_summary`` over a repository), and
    the post-evaluation reads go through a SECOND connection to the same file, so
    a coverage stamp left uncommitted would read as missing too."""

    async def test_a_rule_not_evaluated_yet_is_not_said_to_watch_nothing(self, repo, metrics):
        from homepilot.dashboard.service import build_summary

        assert await seed_default_alert_rules(repo, metrics) == ALL_DEFAULTS

        summary = await build_summary(repo)
        assert summary["metrics"]["rules_enabled"] == ALL_DEFAULTS
        assert summary["metrics"]["rules_watching_nothing"] == 0
        assert summary["metrics"]["nodata_rules_watching_nothing"] == 0

    async def test_after_one_evaluation_a_fresh_reader_sees_what_it_matched(
        self, tmp_path, db, repo, metrics
    ):
        import time

        from homepilot.dashboard.service import build_summary
        from homepilot.metrics.alerts import AlertEvaluator

        await seed_default_alert_rules(repo, metrics)
        # An enrolled host whose agent was just heard (registering links it into
        # the inventory, as the hub does).
        await repo.upsert_agent("agent-dev01", "dev01")

        result = await AlertEvaluator(metrics, repo=repo, now=time.time).run()
        assert result.details["watching_nothing"] == 2  # no samples yet for the floors

        reader = Database(db.db_path)
        await reader.connect()
        try:
            fresh = MetricsRepository(reader)
            rules = {r["name"]: r for r in await fresh.list_rules()}
            dark = rules[HOST_WENT_DARK_RULE["name"]]
            assert dark["last_eval_at"] is not None, "the coverage stamp was never committed"
            assert dark["hosts_matched"] == 1
            summary = await build_summary(Repository(reader))
        finally:
            await reader.close()
        assert summary["metrics"]["nodata_rules_watching_nothing"] == 0
        # The two threshold floors were evaluated and match no reporting host: those
        # ARE watching nothing, and still say so.
        assert summary["metrics"]["rules_watching_nothing"] == 2


class TestTheSelfCheckNamesAlerts:
    def test_a_missing_events_webhook_says_alerts_go_nowhere(self):
        from homepilot.selfcheck import _events_webhook_subsystem

        class _S:
            events_webhook_url = ""

        sub = _events_webhook_subsystem(_S())
        assert "ALERT" in sub.off.upper(), (
            "the one sentence that tells an operator nothing is forwarded says "
            f"only: {sub.off!r}. alert_firing rides this exact channel."
        )

    def test_a_broken_events_webhook_says_alerts_are_lost(self):
        from homepilot.selfcheck import _events_webhook_subsystem

        class _S:
            events_webhook_url = "https://example.invalid/hook"

        sub = _events_webhook_subsystem(_S())
        assert "ALERT" in sub.broken.upper()


class TestAForgottenHostTakesItsAlertsWithIt:
    async def test_deleting_a_host_clears_its_firing_alerts(self, db, repo, metrics):
        """A latched alert on a host that is gone can never clear on its own:
        the evaluator only ever looks at hosts that reported recently, so a
        forgotten host is never evaluated again and the Overview counts a
        machine that is no longer in inventory - forever."""
        host = await repo.create_host(hostname="gone01", host_type="qemu", ip_address="10.0.0.9")
        rule = await metrics.create_rule(
            name="r", metric="load.1m", comparison="gt", threshold=4.0, for_seconds=0
        )
        await metrics.set_alert_state(rule["id"], "gone01", "2026-01-01T00:00:00Z", 9.9)
        assert len(await metrics.list_firing()) == 1

        await repo.delete_host(host)

        assert await metrics.list_firing() == [], (
            "the forgotten host is still firing, and nothing will ever resolve it"
        )

    async def test_forgetting_one_host_does_not_clear_another(self, db, repo, metrics):
        keep = await repo.create_host(hostname="keep01", host_type="qemu", ip_address="10.0.0.1")
        drop = await repo.create_host(hostname="drop01", host_type="qemu", ip_address="10.0.0.2")
        rule = await metrics.create_rule(
            name="r", metric="load.1m", comparison="gt", threshold=4.0, for_seconds=0
        )
        await metrics.set_alert_state(rule["id"], "keep01", "2026-01-01T00:00:00Z", 9.9)
        await metrics.set_alert_state(rule["id"], "drop01", "2026-01-01T00:00:00Z", 9.9)

        await repo.delete_host(drop)

        assert [a["hostname"] for a in await metrics.list_firing()] == ["keep01"]
        assert await repo.get_host(keep) is not None
