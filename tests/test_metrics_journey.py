"""THE journey for native metrics (#458 S5, ADR-004).

Not "the frame was accepted". The whole path, end to end, on the shipped
artifacts: the REAL `hp-agent` binary built from `agent/go` connects to a REAL
hub started from ``create_app_state`` on a default config, sends metric frames,
and then

  1. the query API returns a series for that host,
  2. an alert rule with a duration condition fires through the real
     ``emit_event`` notification path,
  3. the payload the API returns is the one the UI knows how to draw.

Step 3 is checked against ``web/src/lib/series.fixture.json`` — the same file
``web/src/lib/seriesContract.test.ts`` feeds through the real sparkline geometry.
Break the API's shape and this fails; stop drawing that shape and the vitest
half fails.

Skipped when no Go toolchain is present (set ``HP_GO_BIN`` to point at one).
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from .go_toolchain import GO as _GO
from .go_toolchain import GO_SKIP_REASON

REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT_SRC = REPO_ROOT / "agent" / "go"
UI_SERIES_FIXTURE = REPO_ROOT / "web" / "src" / "lib" / "series.fixture.json"

# Applied to the binary-driving class only: the fixture-contract test below runs
# everywhere, so a drifted UI contract is caught even without a Go toolchain.
_needs_go = pytest.mark.skipif(not _GO, reason=GO_SKIP_REASON)


# Built once per session by tests/conftest.py, shared with the TLS journey.
@pytest.fixture
def agent_binary(hp_agent_binary: str) -> str:
    return hp_agent_binary


@pytest.fixture
def hp_dir() -> Iterator[str]:
    path = tempfile.mkdtemp(dir=str(Path.home()), prefix=".hp-test-metrics-")
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


async def _hub_on(hp_dir: str) -> tuple[Any, Any]:
    """A hub started exactly the way a default install starts one."""
    import socket

    from homepilot.app_state import create_app_state
    from homepilot.config import Settings

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])

    settings = Settings(
        data_dir=hp_dir,
        artifacts_dir=os.path.join(hp_dir, "artifacts"),
        agent_hub_port=port,
    )
    state = await create_app_state(settings)
    await state.agent_hub.start()
    return state, settings


def _spawn_agent(binary: str, conf: Path, settings: Any, pin: str) -> subprocess.Popen[str]:
    conf.mkdir(parents=True, exist_ok=True)
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(conf),
        "HP_AGENT_HUB_HOST": "127.0.0.1",
        "HP_AGENT_HUB_PORT": str(settings.agent_hub_port),
        "HP_AGENT_AUTH_TOKEN": settings.agent_hub_auth_token,
        "HP_AGENT_TOKEN_FILE": str(conf / "agent.token"),
        "HP_AGENT_ID_FILE": str(conf / "agent.id"),
        "HP_AGENT_TLS": "true",
        "HP_AGENT_TLS_PIN": pin,
        "HP_AGENT_HEARTBEAT_INTERVAL": "5",
        # A test cannot wait out the 60s production cadence.
        "HP_AGENT_METRICS_INTERVAL": "2",
    }
    return subprocess.Popen(
        [binary], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )


async def _wait_for_samples(state: Any, timeout: float) -> list[dict[str, Any]]:
    """Wait until the hub has STORED samples — not until a frame was seen."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        rows = await state.database.fetchall(
            "SELECT hostname, metric, ts, value FROM metrics ORDER BY ts"
        )
        if rows:
            return rows
        await asyncio.sleep(0.2)
    return []


async def _shutdown(state: Any, proc: subprocess.Popen[str] | None) -> str:
    output = ""
    if proc is not None:
        proc.terminate()
        try:
            output = proc.communicate(timeout=10)[0] or ""
        except subprocess.TimeoutExpired:
            proc.kill()
            output = proc.communicate(timeout=10)[0] or ""
    await state.agent_hub.stop()
    await state.database.close()
    return output


def _series_client(state: Any):
    """A test client over the REAL metrics router, auth stubbed out."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from homepilot.auth.deps import require_token
    from homepilot.metrics.router import router as metrics_router

    app = FastAPI()
    app.include_router(metrics_router)
    app.state.metrics_repo = state.metrics_repo
    app.dependency_overrides[require_token] = lambda: {
        "user_id": "1",
        "token_id": "1",
        "scope": "admin",
        "role": "admin",
        "display_name": "admin",
    }
    return TestClient(app)


@_needs_go
class TestNativeMetricsJourney:
    async def test_agent_metrics_reach_the_api_the_alerts_and_the_ui_shape(
        self, agent_binary: str, hp_dir: str, tmp_path: Path
    ):
        state, settings = await _hub_on(hp_dir)
        proc = None
        try:
            pin = "sha256:" + state.agent_hub.cert_fingerprint
            proc = _spawn_agent(agent_binary, tmp_path / "host-metrics", settings, pin)

            # ── 1. The agent's metrics are STORED, not merely accepted ────────
            rows = await _wait_for_samples(state, timeout=40)
            assert rows, "no metric sample was ever stored for the connected agent"
            hostname = rows[0]["hostname"]
            stored_metrics = {r["metric"] for r in rows}
            assert {"load.1m", "memory.free_gb", "disk.free_gb"} <= stored_metrics, (
                f"the agent reported only {sorted(stored_metrics)}"
            )

            # The metrics frame is also the live state channel (#430): the agent
            # record carries real values and a heartbeat that MOVED.
            agent_row = await state.database.fetchone(
                "SELECT state, last_heartbeat, connected_at FROM agents WHERE hostname = ?",
                (hostname,),
            )
            assert agent_row is not None
            assert json.loads(agent_row["state"]), "the agent's persisted state is still empty"
            assert agent_row["last_heartbeat"] >= agent_row["connected_at"]

            client = _series_client(state)

            # ── 2. The API returns that host's series ─────────────────────────
            resp = client.get(f"/monitoring/hosts/{hostname}/series?metric=load.1m&hours=1")
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["points"], "the API returned an empty series for a reporting host"
            assert body["hostname"] == hostname

            latest = client.get(f"/monitoring/hosts/{hostname}/latest").json()
            assert {m["metric"] for m in latest["metrics"]} >= stored_metrics

            # ── 3. The payload is the shape the UI draws ──────────────────────
            fixture = json.loads(UI_SERIES_FIXTURE.read_text())
            assert set(body) == set(fixture), (
                "the series payload no longer matches web/src/lib/series.fixture.json, "
                "which the UI's sparkline test draws from"
            )
            for key, sample in fixture.items():
                assert isinstance(body[key], type(sample)), f"{key} changed type"
            point = body["points"][0]
            assert set(point) == set(fixture["points"][0])

            # ── 4. A duration rule fires through the REAL notification path ───
            # The condition is one every host satisfies (load is never below a
            # huge number... it always is), so what is under test is the
            # duration + delivery, not the arithmetic.
            rule = client.post(
                "/monitoring/rules",
                json={
                    "name": "load below the moon",
                    "metric": "load.1m",
                    "comparison": "lt",
                    "threshold": 100000.0,
                    # 0 = fire on the first breaching sample: the DURATION
                    # semantics get their own dedicated gates in
                    # test_metrics_alerts.py; here the point is delivery.
                    "for_seconds": 0,
                    "host_filter": hostname,
                },
            )
            assert rule.status_code == 201, rule.text

            from homepilot.metrics.alerts import AlertEvaluator
            from homepilot.sse import bus

            queue = bus.subscribe()
            assert queue is not None
            try:
                result = await AlertEvaluator(state.metrics_repo, repo=state.repo).run()
                assert result.details["fired"] == 1, result.details
                events = [(queue.get_nowait()) for _ in range(queue.qsize())]
            finally:
                bus.unsubscribe(queue)

            assert any(e.type == "alert_firing" for e in events), (
                f"the alert never reached the event bus; saw {[e.type for e in events]}"
            )
            payload = next(e.data for e in events if e.type == "alert_firing")
            assert payload["hostname"] == hostname
            assert payload["metric"] == "load.1m"

            firing = client.get("/monitoring/alerts").json()
            assert firing["total"] == 1
            assert firing["items"][0]["hostname"] == hostname
        finally:
            log = await _shutdown(state, proc)
            assert "metrics enabled" in log, log

    async def test_a_dropped_connection_leaves_no_gap_in_the_series(
        self, agent_binary: str, hp_dir: str, tmp_path: Path
    ):
        """Drop the connection mid-stream and reconnect: the samples taken while
        the agent was disconnected must ARRIVE, not be lost.

        This is why the sampler runs for the process lifetime and the flusher per
        connection - a per-connection sampler would leave exactly the hole the
        buffer exists to prevent. The bound itself (oldest-first, logged) is
        gated in agent/go/metrics_test.go."""
        state, settings = await _hub_on(hp_dir)
        proc = None
        try:
            pin = "sha256:" + state.agent_hub.cert_fingerprint
            proc = _spawn_agent(agent_binary, tmp_path / "host-reconnect", settings, pin)
            rows = await _wait_for_samples(state, timeout=40)
            assert rows, "the agent never reported before the drop"
            hostname = rows[0]["hostname"]

            # Cut the socket from the hub side, mid-stream.
            agent_rec = next(iter(state.agent_registry._agents.values()))
            drop_at = int(time.time())
            agent_rec.writer.close()

            # Stay down for several sample intervals (2s in this test).
            await asyncio.sleep(7)
            gap_end = int(time.time())

            # The agent reconnects on its own backoff and flushes the backlog.
            deadline = asyncio.get_running_loop().time() + 40
            covered: list[dict[str, Any]] = []
            while asyncio.get_running_loop().time() < deadline:
                covered = await state.database.fetchall(
                    "SELECT ts FROM metrics WHERE hostname = ? AND metric = 'load.1m' "
                    "AND ts > ? AND ts <= ? ORDER BY ts",
                    (hostname, drop_at, gap_end),
                )
                if covered:
                    break
                await asyncio.sleep(0.5)

            assert covered, (
                "no sample taken during the disconnect ever arrived - the buffer "
                "did not cover the outage"
            )
        finally:
            log = await _shutdown(state, proc)
            assert "hub connection lost" in log, log


@_needs_go
class TestAHostThatGoesDarkReachesAPhone:
    """#660 on the shipped path: the REAL agent binary stops, the seeded
    "Host went dark" rule fires, the Alerts list shows it, an ntfy request is
    made (the server stood in for at the httpx transport), and when a real
    agent comes back the rule resolves with its duration and a second push."""

    async def test_a_silent_agent_fires_pushes_and_resolves(
        self, agent_binary: str, hp_dir: str, tmp_path: Path
    ):
        import httpx

        from homepilot.metrics.alerts import AlertEvaluator
        from homepilot.metrics.defaults import seed_default_alert_rules

        sent: list[dict[str, Any]] = []

        def ntfy(request: httpx.Request) -> httpx.Response:
            sent.append(json.loads(request.content))
            return httpx.Response(200, json={"id": "x"})

        state, settings = await _hub_on(hp_dir)
        proc = None
        try:
            # A default install seeds the dark rule; nobody types it in.
            await seed_default_alert_rules(state.repo, state.metrics_repo)
            assert any(r["kind"] == "nodata" for r in await state.metrics_repo.list_rules())

            notifier = state.notifier
            notifier._transport = httpx.MockTransport(ntfy)
            notifier._retry_delays = ()
            await notifier.create_channel(
                kind="ntfy", name="phone", url="https://ntfy.example.com/hp-journey", token=None
            )

            pin = "sha256:" + state.agent_hub.cert_fingerprint
            conf = tmp_path / "host-dark"
            proc = _spawn_agent(agent_binary, conf, settings, pin)
            rows = await _wait_for_samples(state, timeout=40)
            assert rows, "the agent never reported"
            hostname = rows[0]["hostname"]

            clock = [time.time()]
            evaluator = AlertEvaluator(
                state.metrics_repo, repo=state.repo, now=lambda: clock[0], notifier=notifier
            )
            assert (await evaluator.run()).details["fired"] == 0, "a live agent was called dark"

            # The agent dies. Six minutes pass on the evaluator's clock.
            proc.terminate()
            proc.communicate(timeout=10)
            proc = None
            clock[0] = time.time() + 360
            result = await evaluator.run()
            await notifier.drain()
            assert result.details["fired"] >= 1, result.details

            client = _series_client(state)
            client.app.state.notifier = notifier
            history = client.get("/monitoring/alert-events?hours=24").json()
            dark = [e for e in history["items"] if e["rule_kind"] == "nodata"]
            assert [(e["hostname"], e["event"]) for e in dark] == [(hostname, "firing")]
            assert dark[0]["delivered_to"] == ["phone"]
            assert any(m["title"] == f"Alert: Host went dark on {hostname}" for m in sent), sent

            # The same agent (same identity files) comes back.
            proc = _spawn_agent(agent_binary, conf, settings, pin)
            before = (
                await state.database.fetchone(
                    "SELECT last_heartbeat FROM agents WHERE hostname = ?", (hostname,)
                )
            )["last_heartbeat"]
            deadline = asyncio.get_running_loop().time() + 40
            while asyncio.get_running_loop().time() < deadline:
                row = await state.database.fetchone(
                    "SELECT last_heartbeat, connected FROM agents WHERE hostname = ?",
                    (hostname,),
                )
                if row["connected"] and row["last_heartbeat"] != before:
                    break
                await asyncio.sleep(0.5)
            clock[0] = time.time()
            result = await evaluator.run()
            await notifier.drain()
            assert result.details["resolved"] >= 1, result.details

            history = client.get("/monitoring/alert-events?hours=24").json()
            dark = [e for e in history["items"] if e["rule_kind"] == "nodata"]
            assert [e["event"] for e in dark] == ["resolved", "firing"]
            assert dark[0]["duration_seconds"] is not None
            assert any(m["title"] == f"Resolved: Host went dark on {hostname}" for m in sent)
        finally:
            await notifier.drain()
            await _shutdown(state, proc)


def test_ui_series_fixture_stays_in_sync_with_the_api_contract():
    """The fixture is a contract, so it may not drift into something the API
    could never return (a missing key, a point without a timestamp)."""
    fixture = json.loads(UI_SERIES_FIXTURE.read_text())
    assert set(fixture) == {
        "hostname",
        "metric",
        "since",
        "points",
        "truncated",
        "max_points",
    }
    assert fixture["points"], "an empty fixture would draw nothing and prove nothing"
    for point in fixture["points"]:
        assert set(point) == {"ts", "value"}
        assert isinstance(point["ts"], int)
        assert isinstance(point["value"], int | float)
    assert fixture["since"] < time.time() + 86400
