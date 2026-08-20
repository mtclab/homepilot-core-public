"""Hub-side metric ingest, and the ONE state path (#458 S5, #430).

Two things are gated here:

* what the hub does with a ``metrics`` frame - stores the good samples, counts
  the bad ones back, and never lets a malformed frame invent a series;
* that there is exactly ONE live state channel. ``report_state`` was a handler
  no agent ever sent, which is why the Agents view rendered an empty state and
  a heartbeat frozen at registration. Two half-working channels is worse than
  one, so the dead one is gone and the metrics frame carries state.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import struct
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from homepilot.agent_hub.server import (
    HEADER_LEN,
    MAX_SAMPLES_PER_FRAME,
    AgentHubServer,
    _encode,
    parse_metric_sample,
)
from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.db.repository import Repository
from homepilot.metrics.repository import MetricsRepository

AUTH = "shared-secret"
# The hub validates a sample's clock against the REAL wall clock (a point dated
# next year would sit in the table until retention caught up), so the fixtures
# have to be anchored to it rather than to a pinned constant.
NOW = int(time.time())


async def _recv(reader: asyncio.StreamReader) -> dict:
    hdr = await reader.readexactly(HEADER_LEN)
    (length,) = struct.unpack("!I", hdr)
    return json.loads(await reader.readexactly(length))


@pytest.fixture
async def wired(tmp_path: Path):
    """A loopback hub with real persistence behind it."""
    from homepilot.agent_hub.registry import AgentRegistry

    db = Database(str(tmp_path / "hub.db"))
    await db.connect()
    await run_migrations(db)
    metrics_repo = MetricsRepository(db)
    registry = AgentRegistry(repo=Repository(db), metrics_repo=metrics_repo)
    srv = AgentHubServer(host="127.0.0.1", port=0, auth_token=AUTH, registry=registry)
    await srv.start()
    assert srv._server is not None
    port = srv._server.sockets[0].getsockname()[1]
    writers: list[asyncio.StreamWriter] = []

    async def connect() -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writers.append(writer)
        writer.write(
            _encode(
                {
                    "action": "register",
                    "auth_token": AUTH,
                    "agent_id": "agent-1",
                    "hostname": "web01",
                    "request_id": "reg-1",
                }
            )
        )
        await writer.drain()
        assert (await _recv(reader))["action"] == "register_ack"
        return reader, writer

    yield SimpleNamespace(srv=srv, db=db, metrics=metrics_repo, connect=connect)
    for w in writers:
        w.close()
        with contextlib.suppress(Exception):
            await w.wait_closed()
    await srv.stop()
    await db.close()


class TestMetricsFrameIngest:
    async def test_a_frame_is_stored_and_acked_with_its_counts(self, wired):
        reader, writer = await wired.connect()

        writer.write(
            _encode(
                {
                    "action": "metrics",
                    "request_id": "m-1",
                    "samples": [
                        {"metric": "load.1m", "value": 0.42, "clock": NOW},
                        {"metric": "disk.free_gb", "value": 91.5, "clock": NOW},
                    ],
                }
            )
        )
        await writer.drain()
        ack = await _recv(reader)

        assert ack["action"] == "metrics_ack"
        assert ack["request_id"] == "m-1"
        assert ack["accepted"] == 2
        assert ack["rejected"] == 0

        # Stored, not merely acked.
        points, _ = await wired.metrics.series("web01", "load.1m", since_ts=NOW - 60)
        assert points == [{"ts": NOW, "value": 0.42}]

    async def test_malformed_samples_are_counted_back_and_the_good_ones_kept(self, wired):
        reader, writer = await wired.connect()

        writer.write(
            _encode(
                {
                    "action": "metrics",
                    "request_id": "m-2",
                    "samples": [
                        {"metric": "load.1m", "value": 1.0, "clock": NOW},
                        {"metric": "DROP TABLE metrics", "value": 1.0, "clock": NOW},
                        {"metric": "load.5m", "value": "not a number", "clock": NOW},
                        {"metric": "load.5m", "value": 1.0, "clock": "yesterday"},
                        "not even an object",
                    ],
                }
            )
        )
        await writer.drain()
        ack = await _recv(reader)

        assert ack["accepted"] == 1
        assert ack["rejected"] == 4
        assert [m["metric"] for m in await wired.metrics.latest("web01")] == ["load.1m"]

    async def test_a_frame_without_a_sample_list_is_acked_not_fatal(self, wired):
        reader, writer = await wired.connect()
        writer.write(_encode({"action": "metrics", "request_id": "m-3", "samples": "nope"}))
        await writer.drain()
        ack = await _recv(reader)
        assert ack["action"] == "metrics_ack"
        assert ack["accepted"] == 0

        # The connection survives: a bad frame is a per-request problem.
        writer.write(_encode({"action": "heartbeat", "request_id": "hb-1"}))
        await writer.drain()
        assert (await _recv(reader))["action"] == "heartbeat_ack"

    async def test_a_resent_batch_does_not_duplicate_points(self, wired):
        """The agent re-sends anything the hub did not ack, so the same sample
        can legitimately arrive twice. (hostname, metric, ts) is its identity."""
        reader, writer = await wired.connect()
        frame = {
            "action": "metrics",
            "request_id": "m-4",
            "samples": [{"metric": "load.1m", "value": 0.42, "clock": NOW}],
        }
        for _ in range(3):
            writer.write(_encode(frame))
            await writer.drain()
            await _recv(reader)

        rows = await wired.db.fetchall("SELECT ts FROM metrics WHERE metric = 'load.1m'")
        assert len(rows) == 1

    async def test_an_oversized_frame_is_capped(self, wired):
        reader, writer = await wired.connect()
        samples = [
            {"metric": "load.1m", "value": 1.0, "clock": NOW - i}
            for i in range(MAX_SAMPLES_PER_FRAME + 10)
        ]
        writer.write(_encode({"action": "metrics", "request_id": "m-5", "samples": samples}))
        await writer.drain()
        ack = await _recv(reader)
        assert ack["accepted"] == MAX_SAMPLES_PER_FRAME
        assert ack["rejected"] == 10


class TestSampleValidation:
    @pytest.mark.parametrize(
        "sample",
        [
            {"metric": "Load.1M", "value": 1.0, "clock": NOW},  # uppercase
            {"metric": "load 1m", "value": 1.0, "clock": NOW},  # space
            {"metric": "../etc/passwd", "value": 1.0, "clock": NOW},
            {"metric": "x" * 65, "value": 1.0, "clock": NOW},
            {"metric": "load.1m", "value": True, "clock": NOW},  # bool is not a number
            {"metric": "load.1m", "value": float("nan"), "clock": NOW},
            {"metric": "load.1m", "value": float("inf"), "clock": NOW},
            {"metric": "load.1m", "value": 1.0, "clock": NOW + 3600},  # too far ahead
            {"metric": "load.1m", "value": 1.0, "clock": NOW - 30 * 86400},  # too old
            {"metric": "load.1m", "value": 1.0},  # no clock
            {"value": 1.0, "clock": NOW},  # no metric
        ],
    )
    def test_rejected(self, sample):
        assert parse_metric_sample(sample, float(NOW)) is None

    def test_accepted(self):
        assert parse_metric_sample(
            {"metric": "disk.free_gb", "value": 91, "clock": NOW}, float(NOW)
        ) == ("disk.free_gb", NOW, 91.0)


class TestExactlyOneStatePath:
    async def test_the_metrics_frame_moves_the_persisted_heartbeat_and_state(self, wired):
        """#430: the persisted heartbeat used to freeze at registration because
        the only thing that touched it was an action no agent sent."""
        reader, writer = await wired.connect()
        before = await wired.db.fetchone(
            "SELECT state, last_heartbeat FROM agents WHERE agent_id = ?", ("agent-1",)
        )
        assert json.loads(before["state"]) == {}

        writer.write(
            _encode(
                {
                    "action": "metrics",
                    "request_id": "m-6",
                    "samples": [
                        {"metric": "load.1m", "value": 0.42, "clock": NOW},
                        {"metric": "load.1m", "value": 0.99, "clock": NOW + 60},
                    ],
                }
            )
        )
        await writer.drain()
        await _recv(reader)
        # The persist is a background task on the registry.
        for _ in range(50):
            after = await wired.db.fetchone(
                "SELECT state, last_heartbeat FROM agents WHERE agent_id = ?", ("agent-1",)
            )
            if json.loads(after["state"]):
                break
            await asyncio.sleep(0.02)

        state = json.loads(after["state"])
        assert state["load.1m"] == 0.99, "the state kept the older sample"
        assert wired.srv.registry.get("agent-1").state["load.1m"] == 0.99

    async def test_report_state_is_gone_and_answers_as_an_unknown_action(self, wired):
        reader, writer = await wired.connect()
        writer.write(
            _encode({"action": "report_state", "request_id": "s-1", "state": {"anything": 1}})
        )
        await writer.drain()
        resp = await _recv(reader)
        assert "unknown action" in resp.get("error", ""), resp
        assert wired.srv.registry.get("agent-1").state == {}

    def test_no_second_state_channel_survives_anywhere_in_the_tree(self):
        """The whole point of the decision: ONE live state path. A future
        re-introduction of the dead handler fails here rather than quietly
        splitting state across two channels again."""
        root = Path(__file__).resolve().parents[1]
        # The ACTION LITERALS, not the words: the code that removed the channel
        # is allowed to explain why it did.
        dead = ('"report_state"', '"state_ack"')
        offenders = []
        for path in list((root / "src").rglob("*.py")) + list((root / "agent" / "go").glob("*.go")):
            text = path.read_text(encoding="utf-8", errors="ignore")
            if any(literal in text for literal in dead):
                offenders.append(str(path.relative_to(root)))
        assert not offenders, f"the removed state channel came back in: {offenders}"

    def test_update_state_has_exactly_one_caller(self):
        """update_state persists the heartbeat, so a second caller would be a
        second cadence and a second source of truth."""
        root = Path(__file__).resolve().parents[1] / "src"
        callers = [
            str(path)
            for path in root.rglob("*.py")
            if "update_state(" in path.read_text(encoding="utf-8") and path.name != "registry.py"
        ]
        assert len(callers) == 1, f"expected only the metrics ingest to call it, got {callers}"
        assert callers[0].endswith("agent_hub/server.py")
