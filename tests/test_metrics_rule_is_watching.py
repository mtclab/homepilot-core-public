"""A rule that can never fire must not look like a rule standing guard (#648 t5).

The alerting design had two ways to accept a rule and then never evaluate it,
and no way at all to notice:

* ``host_filter`` is called a glob by the API docs, by the MCP tool schema and
  by the console's own help text. It was compared with ``==``. ``web-*`` matched
  no host - the rule was enabled, listed and skipped every cycle.
* the metric name is never checked against anything, and the MCP tool schema's
  own example was ``cpu.percent`` - a metric no HomePilot agent has ever
  emitted. A rule on it is accepted and never matches.

Either way the operator sees an enabled rule and no alerts, which is exactly
what a healthy fleet looks like. Reproduced live on dev 3.6.16 before the fix:
three enabled rules, ``evaluated: 1``.

Teeth:

* revert ``_hosts_for`` to ``h == host_filter`` and the glob tests go red;
* drop ``record_rule_coverage`` from ``AlertEvaluator.run`` and every coverage
  test goes red;
* put ``cpu.percent`` back in a monitoring tool description and the vocabulary
  test goes red.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.metrics.alerts import AlertEvaluator
from homepilot.metrics.repository import AGENT_METRICS, MetricsRepository

NOW = 1_800_000_000
REPO_ROOT = Path(__file__).resolve().parents[1]


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


@pytest.fixture(autouse=True)
def _silent_bus(monkeypatch):
    from homepilot.metrics import alerts as alerts_mod

    async def _noop(event_type: str, payload: dict[str, Any], repo: Any = None) -> None:
        return None

    monkeypatch.setattr(alerts_mod, "emit_event", _noop)


def _evaluator(repo: MetricsRepository) -> AlertEvaluator:
    return AlertEvaluator(repo, now=lambda: float(NOW))


async def _breaching(repo: MetricsRepository, *hostnames: str, metric: str = "load.1m") -> None:
    for hostname in hostnames:
        await repo.insert_samples(
            hostname, "agent-1", [(metric, NOW - offset, 9.9) for offset in range(900, -1, -60)]
        )


class TestTheFilterIsAGlob:
    """What the docs, the MCP schema and the console all say it is."""

    async def test_a_prefix_glob_matches_the_hosts_it_names(self, repo):
        await _breaching(repo, "web01", "web02", "db01")
        await repo.create_rule(
            name="web load",
            metric="load.1m",
            comparison="gt",
            threshold=4.0,
            for_seconds=300,
            host_filter="web*",
        )

        result = await _evaluator(repo).run()

        assert result.details["evaluated"] == 2, (
            "a 'web*' rule did not evaluate web01 and web02 - host_filter is "
            "documented as a glob on every surface that offers it"
        )
        assert sorted(a["hostname"] for a in await repo.list_firing()) == ["web01", "web02"]

    async def test_a_bare_hostname_still_means_that_host_alone(self, repo):
        """The glob must not widen an exact filter: 'web01' has no wildcards."""
        await _breaching(repo, "web01", "web011", "db01")
        await repo.create_rule(
            name="one host",
            metric="load.1m",
            comparison="gt",
            threshold=4.0,
            for_seconds=300,
            host_filter="web01",
        )

        result = await _evaluator(repo).run()

        assert result.details["evaluated"] == 1
        assert [a["hostname"] for a in await repo.list_firing()] == ["web01"]

    async def test_star_still_means_every_reporting_host(self, repo):
        await _breaching(repo, "web01", "db01")
        await repo.create_rule(
            name="fleet",
            metric="load.1m",
            comparison="gt",
            threshold=4.0,
            for_seconds=300,
            host_filter="*",
        )

        assert (await _evaluator(repo).run()).details["evaluated"] == 2


class TestARuleSaysWhatItIsWatching:
    async def test_a_rule_matching_no_host_reports_zero_not_silence(self, repo):
        """The headline: enabled, listed, and guarding nothing."""
        await _breaching(repo, "web01")
        inert = await repo.create_rule(
            name="db disks",
            metric="load.1m",
            comparison="gt",
            threshold=4.0,
            for_seconds=300,
            host_filter="db-*",
        )
        live = await repo.create_rule(
            name="web load",
            metric="load.1m",
            comparison="gt",
            threshold=4.0,
            for_seconds=300,
            host_filter="*",
        )

        result = await _evaluator(repo).run()

        assert result.details["watching_nothing"] == 1, result.details
        stored = {r["id"]: r for r in await repo.list_rules()}
        assert stored[inert["id"]]["hosts_matched"] == 0
        assert stored[inert["id"]]["last_eval_at"] is not None, (
            "the rule WAS evaluated - it matched nothing, which is the thing to report"
        )
        assert stored[live["id"]]["hosts_matched"] == 1

    async def test_a_rule_on_a_metric_nobody_reports_reports_zero(self, repo):
        """The other road to an inert rule, and the one an assistant took: the
        MCP tool schema used to offer `cpu.percent` as the example."""
        await _breaching(repo, "web01")
        rule = await repo.create_rule(
            name="cpu",
            metric="cpu.percent",
            comparison="gt",
            threshold=90.0,
            for_seconds=300,
            host_filter="*",
        )

        result = await _evaluator(repo).run()

        assert result.details["watching_nothing"] == 1
        assert (await repo.get_rule(rule["id"]))["hosts_matched"] == 0

    async def test_a_rule_never_evaluated_says_so_rather_than_zero(self, repo):
        rule = await repo.create_rule(
            name="new", metric="load.1m", comparison="gt", threshold=4.0, for_seconds=300
        )
        stored = await repo.get_rule(rule["id"])
        assert stored["last_eval_at"] is None
        assert stored["hosts_matched"] is None

    async def test_coverage_bookkeeping_does_not_touch_updated_at(self, repo):
        """`updated_at` means "an operator changed this rule"; an evaluation is
        not a change, and moving it would make every rule look freshly edited."""
        await _breaching(repo, "web01")
        rule = await repo.create_rule(
            name="r", metric="load.1m", comparison="gt", threshold=4.0, for_seconds=300
        )
        await _evaluator(repo).run()
        assert (await repo.get_rule(rule["id"]))["updated_at"] == rule["updated_at"]


class TestTheVocabularyIsOne:
    """Every surface that names an example metric names a real one.

    ``agent/go/metrics.go`` is the source of truth: it is the only thing that
    decides what a series is called. Add a metric there and forget the Python
    half, and this fails."""

    def test_agent_metrics_matches_the_go_agent(self):
        source = (REPO_ROOT / "agent" / "go" / "metrics.go").read_text()
        body = source.split("func systemInfoToSamples", 1)[1].split("\n}\n", 1)[0]
        emitted = tuple(re.findall(r'add\("([a-z0-9_.]+)"', body))
        assert emitted, "could not read the metric names out of systemInfoToSamples"
        assert emitted == AGENT_METRICS, (
            f"the Go agent emits {emitted} but metrics.repository.AGENT_METRICS says {AGENT_METRICS} - "
            "every alert rule and every tool description is written against that "
            "list, so they cannot drift"
        )

    def test_no_monitoring_tool_offers_a_metric_the_fleet_cannot_report(self):
        from homepilot.mcp.tools.monitoring_tools import TOOL_DEFINITIONS

        blob = repr(TOOL_DEFINITIONS)
        # Anything shaped like a metric key, mentioned anywhere in the schemas.
        mentioned = set(re.findall(r"\b((?:cpu|disk|memory|load)\.[a-z0-9_]+)\b", blob))
        assert mentioned, "the tool schemas name no metric at all - they should"
        unknown = sorted(mentioned - set(AGENT_METRICS))
        assert not unknown, (
            f"MCP monitoring tools offer metric(s) no agent emits: {unknown}. A rule "
            "created on one is accepted, enabled and never evaluated."
        )

    def test_the_console_only_offers_real_metrics(self):
        source = (
            REPO_ROOT / "web" / "src" / "lib" / "components" / "AlertRules.svelte"
        ).read_text()
        listed = re.search(r"RULE_METRICS = \[(.*?)\]", source, re.S)
        assert listed, "AlertRules.svelte no longer declares RULE_METRICS"
        offered = set(re.findall(r"'([a-z0-9_.]+)'", listed.group(1)))
        assert offered <= set(AGENT_METRICS), (
            f"the console offers metric(s) no agent emits: {sorted(offered - set(AGENT_METRICS))}"
        )
