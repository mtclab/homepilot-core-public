"""Lane A2 "nobody was told" (#660, review 03 steps 1-3), tested on the GOAL.

Not "the evaluator returned fired: 1". The operator's outcome, end to end
through the real repositories, the real evaluator, the real notifier and the
real routes, with the ntfy server stood in for at the HTTP TRANSPORT layer
(``httpx.MockTransport``) so the request a phone would receive is asserted:

* a host whose agent stops reporting produces a FIRING entry in the alert
  history (``GET /monitoring/alert-events``) and an ntfy POST saying it went
  dark; when it reports again a RESOLVED entry with its duration and a second
  POST follow;
* a fire that resolves leaves a record (the latch alone left none);
* a channel's topic URL lives in the vault, never in the database;
* "Send test" and the last-delivery state are what the self-check reports.

Teeth (each proven by reverting the fix, recorded in the PR):
* drop the ``kind == nodata`` branch in ``AlertEvaluator.run`` -> the dark-host
  tests go red (the host is never a candidate);
* drop ``record_alert_event`` from ``_notify_firing``/``_notify_resolved`` ->
  the history tests go red;
* drop ``_deliver`` from the evaluator -> the ntfy tests go red;
* drop ``_watching_since`` from the silence measure -> the restart test goes red.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.db.repository import Repository
from homepilot.metrics.alerts import AlertEvaluator
from homepilot.metrics.repository import MetricsRepository
from homepilot.notifications import ChannelStoreError, Notifier

# Example values only (RFC 2606 / RFC 5737): never a real ntfy server or topic.
TOPIC_URL = "https://ntfy.example.com/hp-test-topic-7f3a"
NOW = 1_800_000_000


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeVault:
    """The VaultManager surface the notifier uses, in memory."""

    def __init__(self, locked: bool = False) -> None:
        self.secrets: dict[str, dict[str, Any]] = {}
        self.locked = locked

    async def store_secret(self, name: str, value: dict[str, Any]) -> None:
        if self.locked:
            raise RuntimeError("vault locked")
        self.secrets[name] = dict(value)

    async def get_secret(self, name: str) -> dict[str, Any]:
        if self.locked:
            raise RuntimeError("vault locked")
        return dict(self.secrets[name])

    async def delete_secret(self, name: str) -> bool:
        return self.secrets.pop(name, None) is not None


class NtfyStub:
    """Stands in for an ntfy server at the HTTP transport layer."""

    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, json={"id": "x"})

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def bodies(self) -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.requests]


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


@pytest.fixture
def ntfy_server() -> NtfyStub:
    return NtfyStub()


@pytest.fixture
def vault() -> FakeVault:
    return FakeVault()


@pytest.fixture
def notifier(db, vault, metrics, ntfy_server) -> Notifier:
    return Notifier(
        db, vault, metrics_repo=metrics, transport=ntfy_server.transport, retry_delays=()
    )


@pytest.fixture(autouse=True)
def quiet_bus(monkeypatch):
    """emit_event's SSE/webhook half is tested elsewhere; capture it here."""
    from homepilot.metrics import alerts as alerts_mod

    seen: list[tuple[str, dict[str, Any]]] = []

    async def _emit(event_type: str, payload: dict[str, Any], repo: Any = None) -> None:
        seen.append((event_type, dict(payload)))

    monkeypatch.setattr(alerts_mod, "emit_event", _emit)
    return seen


async def _enrol(repo: Repository, hostname: str, heard: float, agent_id: str | None = None):
    """An enrolled host whose agent was last heard at ``heard``."""
    agent_id = agent_id or f"agent-{hostname}"
    await repo.upsert_agent(agent_id, hostname)
    await repo.db.execute(
        "UPDATE agents SET last_heartbeat = ?, connected_at = ?, first_seen = ? WHERE agent_id = ?",
        (_iso(heard), _iso(heard), _iso(heard), agent_id),
    )
    await repo.db.conn.commit()
    return agent_id


async def _heard(repo: Repository, agent_id: str, at: float) -> None:
    await repo.db.execute(
        "UPDATE agents SET last_heartbeat = ? WHERE agent_id = ?", (_iso(at), agent_id)
    )
    await repo.db.conn.commit()


async def _dark_rule(metrics: MetricsRepository, for_seconds: int = 300) -> dict[str, Any]:
    return await metrics.create_rule(
        name="Host went dark",
        kind="nodata",
        metric="*",
        comparison="gt",
        threshold=0,
        for_seconds=for_seconds,
    )


class Clock:
    def __init__(self, t: float) -> None:
        self.t = float(t)

    def __call__(self) -> float:
        return self.t


def _api(metrics: MetricsRepository, notifier: Notifier, repo: Repository | None = None):
    """The REAL monitoring + notifications routers, auth stubbed to admin."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from homepilot.auth.deps import require_token
    from homepilot.metrics.router import router as metrics_router
    from homepilot.notifications.router import router as notifications_router

    app = FastAPI()
    app.include_router(metrics_router)
    app.include_router(notifications_router)
    app.state.metrics_repo = metrics
    app.state.notifier = notifier
    app.state.repo = repo
    app.dependency_overrides[require_token] = lambda: {
        "user_id": "1",
        "token_id": "1",
        "scope": "admin",
        "role": "admin",
        "display_name": "admin",
    }
    return TestClient(app)


class TestAHostThatGoesDarkIsHeard:
    async def test_dark_host_fires_is_listed_and_pushed_then_resolves(
        self, repo, metrics, notifier, ntfy_server, quiet_bus
    ):
        """THE journey: silence -> firing in history + ntfy push; voice -> resolved."""
        clock = Clock(NOW)
        agent = await _enrol(repo, "web01", heard=NOW)
        await _dark_rule(metrics)
        await notifier.create_channel(kind="ntfy", name="phone", url=TOPIC_URL, token="tk_example")
        evaluator = AlertEvaluator(metrics, repo=repo, now=clock, notifier=notifier)

        # Four minutes of silence: not yet dark.
        clock.t = NOW + 240
        await evaluator.run()
        await notifier.drain()
        assert ntfy_server.requests == []

        # Six minutes: dark.
        clock.t = NOW + 360
        result = await evaluator.run()
        await notifier.drain()
        assert result.details["fired"] == 1, result.details

        # The phone got it: the real request, at the transport.
        assert len(ntfy_server.requests) == 1
        sent = ntfy_server.requests[0]
        assert str(sent.url) == "https://ntfy.example.com/"
        assert sent.headers["authorization"] == "Bearer tk_example"
        body = ntfy_server.bodies()[0]
        assert body["topic"] == "hp-test-topic-7f3a"
        assert body["title"] == "Alert: Host went dark on web01"
        assert "web01 went dark" in body["message"] and "6 min" in body["message"]
        assert body["priority"] == 4

        # The Overview's list reads it from the API.
        client = _api(metrics, notifier)
        history = client.get("/monitoring/alert-events?hours=24").json()
        assert [(e["hostname"], e["event"]) for e in history["items"]] == [("web01", "firing")]
        firing = history["items"][0]
        assert firing["rule_kind"] == "nodata"
        assert firing["since"] == _iso(NOW), "since must be when the host was last HEARD"
        assert firing["delivered_to"] == ["phone"]
        assert client.get("/monitoring/alerts").json()["items"][0]["hostname"] == "web01"
        assert [n for n, _ in quiet_bus] == ["alert_firing"]

        # Still dark a minute later: no second push.
        clock.t = NOW + 420
        await evaluator.run()
        await notifier.drain()
        assert len(ntfy_server.requests) == 1

        # The agent speaks again.
        clock.t = NOW + 600
        await _heard(repo, agent, NOW + 590)
        result = await evaluator.run()
        await notifier.drain()
        assert result.details["resolved"] == 1

        assert len(ntfy_server.requests) == 2
        resolved_body = ntfy_server.bodies()[1]
        assert resolved_body["title"] == "Resolved: Host went dark on web01"
        assert "reporting again after 10 min" in resolved_body["message"]
        assert resolved_body["priority"] == 3

        history = client.get("/monitoring/alert-events?hours=24").json()
        assert [e["event"] for e in history["items"]] == ["resolved", "firing"]
        resolved = history["items"][0]
        assert resolved["duration_seconds"] == 600
        assert resolved["delivered_to"] == ["phone"]
        assert client.get("/monitoring/alerts").json()["total"] == 0
        assert history["channels"][0]["state"] == "ok"
        assert "url" not in json.dumps(history["channels"]).lower()

    async def test_the_history_survives_a_restart(self, db, repo, metrics, notifier, tmp_path):
        """Durable: the record is in the database, not in a process."""
        clock = Clock(NOW)
        await _enrol(repo, "web01", heard=NOW)
        await _dark_rule(metrics)
        evaluator = AlertEvaluator(metrics, repo=repo, now=clock)
        clock.t = NOW + 400
        await evaluator.run()

        await db.close()
        reopened = Database(db.db_path)
        await reopened.connect()
        try:
            rows = await MetricsRepository(reopened).list_alert_events(_iso(NOW - 3600))
            assert [(r["hostname"], r["event"]) for r in rows] == [("web01", "firing")]
        finally:
            await reopened.close()
            await db.connect()

    async def test_a_host_that_never_had_an_agent_is_not_dark(self, repo, metrics):
        """No agent ever = a coverage gap, shown elsewhere; never a page."""
        await repo.create_host(hostname="bare01", host_type="qemu", ip_address="192.0.2.10")
        rule = await _dark_rule(metrics)
        result = await AlertEvaluator(metrics, now=Clock(NOW + 99999)).run()
        assert result.details["fired"] == 0
        assert (await metrics.get_rule(rule["id"]))["hosts_matched"] == 0

    async def test_a_revoked_agent_is_not_watched(self, repo, metrics):
        agent = await _enrol(repo, "old01", heard=NOW)
        await repo.db.execute(
            "UPDATE agents SET credential_hash = 'h' WHERE agent_id = ?", (agent,)
        )
        assert await repo.revoke_agent_credential(agent)
        await _dark_rule(metrics)
        result = await AlertEvaluator(metrics, now=Clock(NOW)).run()
        assert result.details["evaluated"] == 0

    async def test_control_plane_downtime_is_not_the_hosts_silence(self, repo, metrics):
        """Every agent looks quiet right after HomePilot itself was down."""
        await _enrol(repo, "web01", heard=NOW - 3600)
        await _dark_rule(metrics)
        clock = Clock(NOW)
        evaluator = AlertEvaluator(metrics, now=clock)
        assert (await evaluator.run()).details["fired"] == 0, (
            "a whole fleet went 'dark' because the control plane restarted"
        )
        # Still silent five minutes into OUR watch: now it is the host.
        clock.t = NOW + 301
        assert (await evaluator.run()).details["fired"] == 1

    async def test_a_stopped_guest_is_not_dark_and_a_dark_one_resolves_when_stopped(
        self, repo, metrics
    ):
        await _enrol(repo, "dev01", heard=NOW)
        host = await repo.db.fetchone("SELECT id FROM hosts WHERE hostname = 'dev01'")
        await _dark_rule(metrics)
        clock = Clock(NOW + 400)
        evaluator = AlertEvaluator(metrics, now=Clock(NOW - 10))
        evaluator._now = clock  # watched since before the silence
        assert (await evaluator.run()).details["fired"] == 1

        await repo.db.execute("UPDATE hosts SET pve_status = 'stopped' WHERE id = ?", (host["id"],))
        await repo.db.conn.commit()
        clock.t = NOW + 460
        assert (await evaluator.run()).details["resolved"] == 1
        events = await metrics.list_alert_events(_iso(NOW - 60))
        assert events[0]["event"] == "resolved"
        assert events[0]["reason"] == "the hypervisor reports the guest stopped"

        clock.t = NOW + 9999
        assert (await evaluator.run()).details["fired"] == 0, "a stopped guest paged"

    async def test_a_dark_host_whose_agent_is_revoked_resolves_instead_of_latching(
        self, repo, metrics
    ):
        """The #714 class: a firing row nothing revisits stays firing forever."""
        agent = await _enrol(repo, "web01", heard=NOW)
        await _dark_rule(metrics)
        evaluator = AlertEvaluator(metrics, now=Clock(NOW - 1))
        evaluator._now = Clock(NOW + 400)
        assert (await evaluator.run()).details["fired"] == 1

        await repo.db.execute(
            "UPDATE agents SET credential_hash = 'h' WHERE agent_id = ?", (agent,)
        )
        assert await repo.revoke_agent_credential(agent)
        assert (await evaluator.run()).details["resolved"] == 1
        assert await metrics.list_firing() == []
        events = await metrics.list_alert_events(_iso(NOW - 60))
        assert events[0]["reason"].startswith("its agent was revoked or forgotten")

    async def test_a_glob_filter_scopes_the_dark_rule(self, repo, metrics):
        await _enrol(repo, "web01", heard=NOW)
        await _enrol(repo, "db01", heard=NOW)
        await metrics.create_rule(
            name="web dark",
            kind="nodata",
            metric="*",
            comparison="gt",
            threshold=0,
            for_seconds=300,
            host_filter="web-*",
        )
        await metrics.create_rule(
            name="web dark 2",
            kind="nodata",
            metric="*",
            comparison="gt",
            threshold=0,
            for_seconds=300,
            host_filter="web*",
        )
        evaluator = AlertEvaluator(metrics, now=Clock(NOW - 1))
        evaluator._now = Clock(NOW + 400)
        result = await evaluator.run()
        assert result.details["fired"] == 1
        assert [a["hostname"] for a in await metrics.list_firing()] == ["web01"]


class TestAThresholdFireLeavesARecord:
    async def test_fire_and_resolve_are_both_in_the_history(
        self, repo, metrics, notifier, ntfy_server
    ):
        await notifier.create_channel(kind="ntfy", name="phone", url=TOPIC_URL, token=None)
        await metrics.create_rule(
            name="load too high",
            metric="load.1m",
            comparison="gt",
            threshold=4.0,
            for_seconds=0,
            host_filter="web01",
        )
        await metrics.insert_samples("web01", "a1", [("load.1m", NOW - 30, 9.0)])
        await AlertEvaluator(metrics, now=Clock(NOW), notifier=notifier).run()
        await metrics.insert_samples("web01", "a1", [("load.1m", NOW + 30, 1.0)])
        await AlertEvaluator(metrics, now=Clock(NOW + 60), notifier=notifier).run()
        await notifier.drain()

        events = await metrics.list_alert_events(_iso(NOW - 3600))
        assert [(e["event"], e["value"]) for e in events] == [("resolved", 1.0), ("firing", 9.0)]
        assert events[0]["rule_name"] == "load too high"
        assert events[0]["duration_seconds"] == 90
        assert "Authorization" not in ntfy_server.requests[0].headers
        assert "load.1m is 9, above 4" in ntfy_server.bodies()[0]["message"]

    async def test_a_deleted_rule_keeps_its_history(self, metrics):
        rule = await metrics.create_rule(
            name="gone", metric="load.1m", comparison="gt", threshold=4.0, for_seconds=0
        )
        await metrics.insert_samples("web01", "a1", [("load.1m", NOW - 30, 9.0)])
        await AlertEvaluator(metrics, now=Clock(NOW)).run()
        await metrics.delete_rule(rule["id"])
        events = await metrics.list_alert_events(_iso(NOW - 3600))
        assert [e["rule_name"] for e in events] == ["gone"]

    async def test_with_no_channel_the_event_says_nobody_was_told(self, metrics, notifier):
        await metrics.create_rule(
            name="r", metric="load.1m", comparison="gt", threshold=4.0, for_seconds=0
        )
        await metrics.insert_samples("web01", "a1", [("load.1m", NOW - 30, 9.0)])
        await AlertEvaluator(metrics, now=Clock(NOW), notifier=notifier).run()
        await notifier.drain()
        events = await metrics.list_alert_events(_iso(NOW - 3600))
        assert events[0]["delivered_to"] == []

    async def test_the_history_is_in_the_order_things_happened_not_clock_order(self, metrics):
        """Found by the shipped-binary journey: ordering by `at` put a recovery
        BEFORE its fire when the wall clock stepped back between them."""
        rule = await metrics.create_rule(
            name="r", metric="load.1m", comparison="gt", threshold=4.0, for_seconds=0
        )
        await metrics.record_alert_event(
            rule=rule, hostname="h", event="firing", value=9.0, since=None, at=_iso(NOW + 100)
        )
        await metrics.record_alert_event(
            rule=rule, hostname="h", event="resolved", value=1.0, since=None, at=_iso(NOW)
        )
        events = await metrics.list_alert_events(_iso(NOW - 60))
        assert [e["event"] for e in events] == ["resolved", "firing"]

    async def test_the_history_is_pruned_on_the_operational_horizon(self, repo, metrics):
        rule = await metrics.create_rule(
            name="r", metric="load.1m", comparison="gt", threshold=4.0, for_seconds=0
        )
        await metrics.record_alert_event(
            rule=rule,
            hostname="web01",
            event="firing",
            value=9.0,
            since=None,
            at="2000-01-01T00:00:00Z",
        )
        from homepilot.reconciler.retention import RetentionReconciler

        result = await RetentionReconciler(repo, retention_days=90).run()
        assert result.success
        assert await metrics.list_alert_events("1999-01-01T00:00:00Z") == []


class TestNodataRuleShape:
    async def test_a_nodata_rule_refuses_a_metric_or_threshold_over_http(self, metrics, notifier):
        client = _api(metrics, notifier)
        bad = client.post(
            "/monitoring/rules",
            json={"name": "x", "kind": "nodata", "metric": "load.1m", "for_seconds": 300},
        )
        assert bad.status_code == 422 and "takes no metric" in bad.text
        short = client.post(
            "/monitoring/rules", json={"name": "x", "kind": "nodata", "for_seconds": 60}
        )
        assert short.status_code == 422 and "120" in short.text
        ok = client.post(
            "/monitoring/rules", json={"name": "x", "kind": "nodata", "for_seconds": 300}
        )
        assert ok.status_code == 201, ok.text
        assert ok.json()["kind"] == "nodata"
        patch = client.patch(f"/monitoring/rules/{ok.json()['id']}", json={"threshold": 3})
        assert patch.status_code == 422

    async def test_a_threshold_rule_still_needs_its_condition(self, metrics, notifier):
        client = _api(metrics, notifier)
        resp = client.post("/monitoring/rules", json={"name": "x", "metric": "load.1m"})
        assert resp.status_code == 422


class TestChannels:
    async def test_the_topic_url_lives_in_the_vault_not_the_database(
        self, db, metrics, notifier, vault
    ):
        view = await notifier.create_channel(
            kind="ntfy", name="phone", url=TOPIC_URL, token="tk_example"
        )
        assert view["endpoint"] == "https://ntfy.example.com"
        assert vault.secrets[f"notify-channel-{view['id']}"]["url"] == TOPIC_URL
        await db.conn.commit()
        dump = "\n".join([line async for line in _iterdump(db)])
        assert "hp-test-topic-7f3a" not in dump and "tk_example" not in dump, (
            "the topic URL or token reached the database"
        )
        listed = json.dumps(await notifier.list_channels())
        assert "hp-test-topic-7f3a" not in listed and "tk_example" not in listed

    async def test_no_vault_or_a_locked_one_refuses_and_stores_nothing(self, db, metrics):
        for vault in (None, FakeVault(locked=True)):
            notifier = Notifier(db, vault, metrics_repo=metrics)
            with pytest.raises(ChannelStoreError):
                await notifier.create_channel(kind="ntfy", name="p", url=TOPIC_URL, token=None)
            assert await notifier.list_channels() == []

    @pytest.mark.parametrize(
        "url",
        [
            "ntfy.example.com/topic",
            "https://ntfy.example.com/",
            "https://ntfy.example.com/has space",
            "https://user:pw@ntfy.example.com/topic",
            "https://ntfy.example.com/topic?auth=x",
        ],
    )
    async def test_a_url_that_could_never_deliver_is_refused(self, notifier, url):
        with pytest.raises(ValueError):
            await notifier.create_channel(kind="ntfy", name="p", url=url, token=None)

    async def test_send_test_over_http_records_last_delivery(
        self, repo, metrics, notifier, ntfy_server
    ):
        client = _api(metrics, notifier, repo)
        made = client.post(
            "/admin/notifications/channels",
            json={"kind": "ntfy", "name": "phone", "url": TOPIC_URL},
        )
        assert made.status_code == 201, made.text
        assert TOPIC_URL not in made.text
        cid = made.json()["id"]
        assert made.json()["state"] == "untested"

        tested = client.post(f"/admin/notifications/channels/{cid}/test").json()
        assert tested["delivered"] is True
        assert ntfy_server.bodies()[0]["title"] == "HomePilot test notification"
        assert client.get("/admin/notifications/channels").json()["items"][0]["state"] == "ok"

        ntfy_server.status = 500
        tested = client.post(f"/admin/notifications/channels/{cid}/test").json()
        assert tested["delivered"] is False and "HTTP 500" in tested["error"]
        channel = client.get("/admin/notifications/channels").json()["items"][0]
        assert channel["state"] == "failing"
        assert TOPIC_URL not in json.dumps(channel)

        audit = await repo.db.fetchall(
            "SELECT action FROM audit_log WHERE action LIKE 'notification_channel_%'"
        )
        assert {r["action"] for r in audit} == {
            "notification_channel_created",
            "notification_channel_tested",
        }

        assert client.delete(f"/admin/notifications/channels/{cid}").json()["deleted"] is True
        assert client.get("/admin/notifications/channels").json()["total"] == 0

    async def test_a_transport_error_never_stores_the_url(self, db, metrics, vault):
        def boom(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError(f"cannot reach {request.url}")

        notifier = Notifier(db, vault, metrics_repo=metrics, transport=httpx.MockTransport(boom))
        view = await notifier.create_channel(kind="ntfy", name="p", url=TOPIC_URL, token=None)
        result = await notifier.send_test(view["id"])
        assert result["delivered"] is False
        assert "hp-test-topic" not in (result["error"] or "")
        assert "hp-test-topic" not in (result["channel"]["last_error"] or "")

    async def test_a_disabled_channel_is_not_sent_alerts(self, metrics, notifier, ntfy_server):
        view = await notifier.create_channel(kind="ntfy", name="p", url=TOPIC_URL, token=None)
        await notifier.store.set_enabled(view["id"], False)
        await notifier.dispatch("alert_firing", {"hostname": "h", "rule_name": "r"})
        await notifier.drain()
        assert ntfy_server.requests == []


class TestTheSelfCheckSaysWhetherAnyoneIsTold:
    async def _line(self, notifier) -> dict[str, Any]:
        from types import SimpleNamespace

        from homepilot.selfcheck import _notifications_subsystem, run_selfcheck

        report = await run_selfcheck([_notifications_subsystem(SimpleNamespace(notifier=notifier))])
        return report["subsystems"][0]

    async def test_no_channel_is_off_and_says_no_phone(self, notifier):
        line = await self._line(notifier)
        assert line["state"] == "off" and line["configured"] is False
        assert "no phone" in line["consequence"]

    async def test_untested_failing_ok(self, notifier, ntfy_server):
        view = await notifier.create_channel(kind="ntfy", name="phone", url=TOPIC_URL, token=None)
        assert (await self._line(notifier))["state"] == "unknown"
        await notifier.send_test(view["id"])
        ok = await self._line(notifier)
        assert ok["state"] == "ok" and "phone" in ok["consequence"]
        ntfy_server.status = 503
        await notifier.send_test(view["id"])
        assert (await self._line(notifier))["state"] == "unreachable"


async def _iterdump(db: Database):
    """Every row of every table as text (aiosqlite has iterdump)."""
    async for line in db.conn.iterdump():
        yield line
