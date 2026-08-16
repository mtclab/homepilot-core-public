"""App-layer replay-protection tests for the Agent Hub (#362 slice 3).

Drives the shipped socket handler with a real Repository so per-agent tokens are
minted, then reconnects with the minted token to exercise register-frame
freshness (nonce+ts) and per-frame seq+MAC enforcement in both directions.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
import struct
import time

import pytest

from homepilot.agent_hub.registry import AgentRegistry
from homepilot.agent_hub.replay import (
    ReplaySession,
    canonical_bytes,
    compute_mac,
)
from homepilot.agent_hub.server import (
    HEADER_LEN,
    HUB_FEATURES,
    PROTOCOL_VERSION,
    AgentHubServer,
    _encode,
)

AUTH = "shared-secret"

# Cross-language contract: agent/go/replay_test.go pins the SAME canonical string
# and hex for the SAME input + key. The vector includes '<' '>' '&' (must stay
# RAW) and a "mac" key (must be EXCLUDED).
PINNED_KEY = b"per-agent-token-key"
PINNED_FRAME = {
    "action": "exec",
    "command": "echo a > b && cat < c",
    "request_id": "r-1",
    "seq": 1,
    "mac": "EXCLUDED",
}
PINNED_CANON = b'{"action":"exec","command":"echo a > b && cat < c","request_id":"r-1","seq":1}'
PINNED_HEX = "f5b7f7351c289d5f2fe9af6e42f0a362f343b65871c8b9b6e8a2e9fa3897a6a1"


async def _recv(reader: asyncio.StreamReader) -> dict:
    hdr = await reader.readexactly(HEADER_LEN)
    (length,) = struct.unpack("!I", hdr)
    body = await reader.readexactly(length)
    return json.loads(body)


class _Hub:
    def __init__(self, srv: AgentHubServer, port: int) -> None:
        self.srv = srv
        self.port = port
        self._writers: list[asyncio.StreamWriter] = []

    async def connect(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        self._writers.append(writer)
        return reader, writer

    async def aclose(self) -> None:
        for w in self._writers:
            w.close()
            with contextlib.suppress(Exception):
                await w.wait_closed()
        await self.srv.stop()


@contextlib.asynccontextmanager
async def _repo_hub(tmp_path):
    from homepilot.db.connection import Database
    from homepilot.db.migrations import run_migrations
    from homepilot.db.repository import Repository

    db = Database(str(tmp_path / "hub.db"))
    await db.connect()
    await run_migrations(db)
    repo = Repository(db)
    reg = AgentRegistry(repo=repo)
    srv = AgentHubServer(host="127.0.0.1", port=0, auth_token=AUTH, registry=reg)
    await srv.start()
    assert srv._server is not None
    port = srv._server.sockets[0].getsockname()[1]
    hub = _Hub(srv, port)
    try:
        yield hub
    finally:
        await hub.aclose()
        await db.close()


async def _enroll_get_token(hub, agent_id: str, hostname: str) -> str:
    """Enroll with the shared token and return the minted per-agent token."""
    reader, writer = await hub.connect()
    writer.write(
        _encode(
            {
                "action": "register",
                "auth_token": AUTH,
                "agent_id": agent_id,
                "hostname": hostname,
                "request_id": "enroll",
                "v": PROTOCOL_VERSION,
            }
        )
    )
    await writer.drain()
    ack = await _recv(reader)
    minted = ack["auth_token"]
    writer.close()
    await writer.wait_closed()
    await asyncio.sleep(0.05)
    return minted


async def _register_replay(
    hub,
    agent_id: str,
    hostname: str,
    token: str,
    *,
    nonce: str | None = None,
    ts: int | None = None,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, dict]:
    reader, writer = await hub.connect()
    frame = {
        "action": "register",
        "auth_token": token,
        "agent_id": agent_id,
        "hostname": hostname,
        "request_id": "reg",
        "v": PROTOCOL_VERSION,
        "replay": 1,
        "nonce": nonce if nonce is not None else secrets.token_hex(16),
        "ts": ts if ts is not None else int(time.time()),
    }
    writer.write(_encode(frame))
    await writer.drain()
    ack = await _recv(reader)
    return reader, writer, ack


def _stamp(key: bytes, frame: dict, seq: int) -> dict:
    out = dict(frame)
    out["seq"] = seq
    out["mac"] = compute_mac(key, out)
    return out


# --------------------------------------------------------------------------
# (g) canonicalization contract
# --------------------------------------------------------------------------


class TestCanonicalization:
    def test_pinned_vector_matches_go(self):
        """Revert-check: change the escaping in replay._canon_str (e.g. HTML-escape
        '<') and either the canonical string or the hex diverges from Go's."""
        assert canonical_bytes(PINNED_FRAME) == PINNED_CANON
        assert compute_mac(PINNED_KEY, PINNED_FRAME) == PINNED_HEX

    def test_mac_field_excluded(self):
        with_mac = {"a": 1, "mac": "whatever"}
        without = {"a": 1}
        assert canonical_bytes(with_mac) == canonical_bytes(without)


# --------------------------------------------------------------------------
# Per-frame seq + MAC enforcement (established connection)
# --------------------------------------------------------------------------


class TestReplayFrameEnforcement:
    async def test_well_formed_sequence_passes(self, tmp_path):
        """(c) A replay-enabled connection round-trips correctly stamped frames,
        and the hub's replies carry a valid seq+MAC in the reverse direction."""
        async with _repo_hub(tmp_path) as hub:
            minted = await _enroll_get_token(hub, "a1", "host1")
            key = minted.encode("utf-8")
            reader, writer, ack = await _register_replay(hub, "a1", "host1", minted)
            assert ack["action"] == "register_ack"

            client_view = ReplaySession(key)  # verifies the hub's outbound frames
            for seq in (1, 2, 3):
                writer.write(
                    _encode(_stamp(key, {"action": "heartbeat", "request_id": f"h{seq}"}, seq))
                )
                await writer.drain()
                reply = await _recv(reader)
                assert reply["action"] == "heartbeat_ack"
                # The hub stamped its reply with a monotonic seq + valid MAC.
                assert reply["seq"] == seq
                client_view.verify(reply)  # raises if the MAC/seq is wrong

    async def test_duplicate_seq_rejected_and_closed(self, tmp_path):
        """(a) A replayed frame (a seq the hub already consumed) is rejected and
        the connection is closed. Revert-check: remove the replay_session.verify()
        call in _handle_agent and the duplicate is accepted -> the socket stays
        open and this fails."""
        async with _repo_hub(tmp_path) as hub:
            minted = await _enroll_get_token(hub, "a1", "host1")
            key = minted.encode("utf-8")
            reader, writer, ack = await _register_replay(hub, "a1", "host1", minted)
            assert ack["action"] == "register_ack"

            writer.write(_encode(_stamp(key, {"action": "heartbeat", "request_id": "h1"}, 1)))
            await writer.drain()
            assert (await _recv(reader))["action"] == "heartbeat_ack"

            # Replay seq=1 (the hub now expects 2): fail-closed.
            writer.write(_encode(_stamp(key, {"action": "heartbeat", "request_id": "h1"}, 1)))
            await writer.drain()
            with pytest.raises((asyncio.IncompleteReadError, ConnectionError)):
                await _recv(reader)

    async def test_bad_mac_rejected_and_closed(self, tmp_path):
        """(b) A frame with a valid seq but a forged MAC is rejected + closed.
        Revert-check: as above, dropping the verify() call accepts it."""
        async with _repo_hub(tmp_path) as hub:
            minted = await _enroll_get_token(hub, "a1", "host1")
            reader, writer, ack = await _register_replay(hub, "a1", "host1", minted)
            assert ack["action"] == "register_ack"

            forged = {"action": "heartbeat", "request_id": "h1", "seq": 1, "mac": "00" * 32}
            writer.write(_encode(forged))
            await writer.drain()
            with pytest.raises((asyncio.IncompleteReadError, ConnectionError)):
                await _recv(reader)


# --------------------------------------------------------------------------
# Register-frame freshness (nonce + ts)
# --------------------------------------------------------------------------


class TestRegisterFreshness:
    async def test_stale_timestamp_rejected(self, tmp_path):
        """(d) A register frame whose ts is outside the window is refused.
        Revert-check: remove the abs(now-ts) > REPLAY_TS_WINDOW check in
        _check_register_freshness and the stale register is accepted -> fails."""
        async with _repo_hub(tmp_path) as hub:
            minted = await _enroll_get_token(hub, "a1", "host1")
            _r, _w, ack = await _register_replay(
                hub, "a1", "host1", minted, ts=int(time.time()) - 100_000
            )
            assert "error" in ack
            assert "stale timestamp" in ack["error"]

    async def test_duplicate_nonce_rejected(self, tmp_path):
        """(e) Reusing a nonce for the same agent within the window is refused.
        Revert-check: remove the nonce_cache.check_and_record() gate and the
        replayed register authenticates -> fails."""
        async with _repo_hub(tmp_path) as hub:
            minted = await _enroll_get_token(hub, "a1", "host1")
            nonce = secrets.token_hex(16)
            _r1, w1, ack1 = await _register_replay(hub, "a1", "host1", minted, nonce=nonce)
            assert ack1["action"] == "register_ack"
            w1.close()
            await w1.wait_closed()
            await asyncio.sleep(0.05)

            _r2, _w2, ack2 = await _register_replay(hub, "a1", "host1", minted, nonce=nonce)
            assert "error" in ack2
            assert "duplicate nonce" in ack2["error"]


# --------------------------------------------------------------------------
# Back-compat: connections that do NOT negotiate replay are unaffected
# --------------------------------------------------------------------------


class TestBackCompatNoReplay:
    async def test_per_agent_reconnect_without_replay_flag_plain(self, tmp_path):
        """(f) A per-agent reconnect that does NOT send replay:1 works with plain
        frames — no seq/MAC required or added (old agents unaffected)."""
        async with _repo_hub(tmp_path) as hub:
            minted = await _enroll_get_token(hub, "a1", "host1")
            reader, writer = await hub.connect()
            writer.write(
                _encode(
                    {
                        "action": "register",
                        "auth_token": minted,
                        "agent_id": "a1",
                        "hostname": "host1",
                        "request_id": "reg",
                        "v": PROTOCOL_VERSION,
                    }
                )
            )
            await writer.drain()
            ack = await _recv(reader)
            assert ack["action"] == "register_ack"

            # A plain heartbeat (no seq/mac) round-trips and the reply is unframed.
            writer.write(_encode({"action": "heartbeat", "request_id": "hb"}))
            await writer.drain()
            reply = await _recv(reader)
            assert reply["action"] == "heartbeat_ack"
            assert "seq" not in reply
            assert "mac" not in reply

    async def test_enrollment_connection_is_exempt(self, tmp_path):
        """(f) An enrollment (shared-token) connection that sends replay:1 is NOT
        enforced — its auth kind is 'shared', which has no per-agent key yet. It
        registers and exchanges plain frames. Revert-check: enforce replay on the
        'shared' path and this plain heartbeat gets closed -> fails."""
        async with _repo_hub(tmp_path) as hub:
            reader, writer, ack = await _register_replay(hub, "e1", "hoste", AUTH)
            assert ack["action"] == "register_ack"
            # Enrollment minted a per-agent token; the connection stays unframed.
            writer.write(_encode({"action": "heartbeat", "request_id": "hb"}))
            await writer.drain()
            reply = await _recv(reader)
            assert reply["action"] == "heartbeat_ack"
            assert "seq" not in reply

    def test_hub_advertises_replay_feature(self):
        assert "replay-v1" in HUB_FEATURES
