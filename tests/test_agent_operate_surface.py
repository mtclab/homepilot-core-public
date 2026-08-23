"""The fleet can explain itself: version, refusal reasons, revoke that bites (#430).

Before this, a disconnected agent could not say anything about why. Every refusal
- a revoked credential, a token presented from the wrong host, a replayed
register, an identity already claimed, a banned peer - was `logger.warning` and
nothing else, so a revoked agent, a duplicate-identity clash and a powered-off
box were pixel-identical grey dots. There was no agent version anywhere (the
release workflow's `-X main.version` pointed at a symbol package main did not
have, so Go silently ignored it and every released binary was unversioned).
Revoking left the live channel open, and the hub connection is long-lived by
design - so a compromised agent kept a fleet-root exec/write channel.

These gates assert what an OPERATOR gets: a reason they can read on the host that
is missing, a version they can hunt an upgrade with, and a revoke that ends the
channel now rather than "at the next reconnect, which may be never".
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import struct
import subprocess
from pathlib import Path
from typing import Any

import pytest

from homepilot.agent_hub.registry import AgentRegistry
from homepilot.agent_hub.server import AgentHubServer
from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.db.repository import Repository

pytestmark = pytest.mark.asyncio

REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT_SRC = REPO_ROOT / "agent" / "go"
_GO = os.environ.get("HP_GO_BIN") or shutil.which("go") or ""
_needs_go = pytest.mark.skipif(not _GO, reason="Go toolchain not available")

AUTH = "shared-secret-for-tests"
HEADER_LEN = 4


def _encode(msg: dict[str, Any]) -> bytes:
    body = json.dumps(msg).encode()
    return struct.pack("!I", len(body)) + body


async def _recv(reader: asyncio.StreamReader) -> dict[str, Any]:
    hdr = await reader.readexactly(HEADER_LEN)
    (length,) = struct.unpack("!I", hdr)
    return dict(json.loads(await reader.readexactly(length)))


@contextlib.asynccontextmanager
async def _hub(tmp_path: Path):
    """A loopback hub backed by a REAL database, so what it records is checkable."""
    db = Database(str(tmp_path / "homepilot.db"))
    await db.connect()
    await run_migrations(db)
    repo = Repository(db)
    registry = AgentRegistry(repo=repo)
    srv = AgentHubServer(host="127.0.0.1", port=0, auth_token=AUTH, registry=registry)
    await srv.start()
    assert srv._server is not None
    port = srv._server.sockets[0].getsockname()[1]
    writers: list[asyncio.StreamWriter] = []

    async def register(agent_id: str, hostname: str, token: str = AUTH):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writers.append(writer)
        writer.write(
            _encode(
                {
                    "action": "register",
                    "auth_token": token,
                    "agent_id": agent_id,
                    "hostname": hostname,
                    "request_id": f"reg-{agent_id}",
                }
            )
        )
        await writer.drain()
        return reader, writer

    try:
        yield srv, repo, register
    finally:
        for w in writers:
            w.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(w.wait_closed(), timeout=5)
        await srv.stop()
        await db.close()


async def _wait_for(predicate, timeout: float = 5.0):
    """Agent persistence is fire-and-forget; wait for the write to land."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        value = await predicate()
        if value:
            return value
        await asyncio.sleep(0.05)
    return None


class TestPersistenceCannotOutliveItsDatabase:
    """A fire-and-forget write still in aiosqlite's queue when the database is
    closed leaves that close waiting - forever, in the worst case. That is how
    the whole suite hung inside a fixture teardown and took `make gate` with it
    (#496)."""

    async def test_stopping_the_hub_drains_outstanding_writes(self, tmp_path: Path):
        async with _hub(tmp_path) as (srv, _repo, register):
            reader, writer = await register("agent-drain", "box-drain")
            await _recv(reader)
            writer.close()

            await srv.stop()

            assert not [t for t in srv.registry._persist_tasks if not t.done()], (
                "the hub stopped with persistence writes still in flight"
            )

    async def test_closing_a_database_never_hangs(self, tmp_path: Path):
        """The safety net: a close that cannot complete must become a logged
        warning, not a wedged process."""
        db = Database(str(tmp_path / "stuck.db"))
        await db.connect()
        real_close = db._connection.close

        async def _never_finishes() -> None:
            await asyncio.sleep(3600)

        db._connection.close = _never_finishes  # type: ignore[method-assign]
        try:
            await asyncio.wait_for(db.close(), timeout=Database._CLOSE_TIMEOUT_SECONDS + 5)
        finally:
            db._connection = None
            with contextlib.suppress(Exception):
                await real_close()


class TestAShutdownAlwaysFinishes:
    """`stop()` must end the hub, not wait on the fleet to notice.

    Python 3.12's `Server.wait_closed()` returns only once every connection
    HANDLER has finished. Our handler sits in a 300s read waiting for the
    agent's next frame, so closing the listening socket and waiting is a wait of
    up to five minutes per connected agent - and an unbounded one when a peer
    never hangs up at all. That is a backend that ignores SIGTERM until Docker
    kills it, and in the suite it is the fixture teardown that hung and took
    `make gate` with it (#496).

    TEETH: reverting `_close_live_connections` (back to a bare
    `close()` + `await wait_closed()`) makes both of these hang until the
    surrounding `wait_for` fires, which is the failure they exist to forbid.
    """

    async def test_a_registered_agent_that_never_hangs_up_cannot_hold_the_shutdown(
        self, tmp_path: Path
    ):
        async with _hub(tmp_path) as (srv, _repo, register):
            reader, _writer = await register("agent-live", "box-live")
            await _recv(reader)  # register_ack: the handler is now in its read loop

            # No writer.close(): this is the agent that has not noticed, which is
            # exactly the case a shutdown may not depend on.
            await asyncio.wait_for(srv.stop(), timeout=15)

            assert not srv._connections, (
                "the hub stopped but is still tracking a live connection handler"
            )

    async def test_a_preauth_peer_that_sends_nothing_cannot_hold_the_shutdown(self, tmp_path: Path):
        """The same, one step earlier: a socket that connects and then says
        nothing sits in the pre-auth read. It is unauthenticated and unnamed, and
        it still gets a handler that `wait_closed()` waits on."""
        async with _hub(tmp_path) as (srv, _repo, _register):
            assert srv._server is not None
            port = srv._server.sockets[0].getsockname()[1]
            _reader, _writer = await asyncio.open_connection("127.0.0.1", port)
            await asyncio.sleep(0.05)  # let the handler start and block on read

            await asyncio.wait_for(srv.stop(), timeout=15)

            assert not srv._connections, (
                "the hub stopped but is still tracking a pre-auth connection handler"
            )


class TestARejectedAgentSaysWhy:
    async def test_a_revoked_agent_reports_its_revocation_not_a_grey_dot(self, tmp_path: Path):
        """The headline: an operator looking at the fleet list can tell a revoked
        agent from a powered-off one."""
        async with _hub(tmp_path) as (_srv, repo, register):
            # Enrol for real, then revoke, then let it come back the way an
            # already-installed agent does: with its per-agent credential.
            reader, writer = await register("agent-1", "box-1")
            ack = await _recv(reader)
            minted = ack.get("auth_token")
            assert minted, "the hub did not mint a per-agent credential"
            writer.close()
            await _wait_for(lambda: _row(repo, "agent-1"))

            await repo.revoke_agent_credential("agent-1")

            reader2, _w2 = await register("agent-1", "box-1", token=minted)
            resp = await _recv(reader2)
            assert "error" in resp

            row = await _wait_for(lambda: _last_error(repo, "agent-1"))
            assert row, "the hub refused the agent and recorded no reason at all"
            assert "revoked" in row.lower(), (
                f"the recorded reason does not name the revocation: {row!r}"
            )

    async def test_the_refusal_is_in_the_durable_audit_trail(self, tmp_path: Path):
        async with _hub(tmp_path) as (srv, _repo, register):
            reader, _w = await register("agent-2", "box-2", token="not-the-token")
            await _recv(reader)

            entries = await _wait_for(
                lambda: _audit(srv, action="register_rejected"),
            )
            assert entries, "a refused registration left no audit record"
            assert entries[0]["result"] == "blocked"

    async def test_the_reason_distinguishes_a_wrong_token_from_a_revocation(self, tmp_path: Path):
        """The point of the whole slice: distinct failures must not collapse into
        one indistinguishable 'invalid auth_token'."""
        async with _hub(tmp_path) as (_srv, repo, register):
            reader, writer = await register("agent-3", "box-3")
            ack = await _recv(reader)
            minted = ack["auth_token"]
            writer.close()
            await _wait_for(lambda: _row(repo, "agent-3"))

            # (a) a token that is simply wrong
            r1, _ = await register("agent-3", "box-3", token="garbage")
            await _recv(r1)
            wrong = await _wait_for(lambda: _last_error(repo, "agent-3"))

            # (b) the right token, presented from a different host
            r2, _ = await register("agent-3", "somewhere-else", token=minted)
            await _recv(r2)
            elsewhere = await _wait_for(
                lambda: _last_error_matching(repo, "agent-3", "presented from")
            )

            assert wrong and elsewhere
            assert wrong != elsewhere, (
                "a wrong token and a token replayed from another host produced the "
                "same reason - the operator still cannot tell them apart"
            )

    async def test_a_reconnected_agent_stops_showing_a_stale_reason(self, tmp_path: Path):
        """A host that was refused, fixed and is now connected must not keep
        displaying the refusal forever."""
        async with _hub(tmp_path) as (_srv, repo, register):
            # It has to be a KNOWN agent first: a refusal for a host the hub has
            # never seen has no row to write a reason onto (and must not create
            # one - the agents table is the credential store).
            r0, w0 = await register("agent-4", "box-4")
            await _recv(r0)
            await _wait_for(lambda: _row(repo, "agent-4"))
            w0.close()
            with contextlib.suppress(Exception):
                await w0.wait_closed()

            r1, _ = await register("agent-4", "box-4", token="garbage")
            await _recv(r1)
            assert await _wait_for(lambda: _last_error(repo, "agent-4")), (
                "fixture precondition: the refusal was not recorded"
            )

            # Now it registers properly again.
            r2, w2 = await register("agent-4", "box-4")
            await _recv(r2)

            cleared = await _wait_for(
                lambda: _row_is(repo, "agent-4", lambda row: row["last_error"] is None)
            )
            assert cleared, "a connected agent is still carrying an old failure reason"
            w2.close()


class TestADisconnectIsRecordedWithItsReason:
    async def test_a_dropped_connection_records_how_it_ended(self, tmp_path: Path):
        async with _hub(tmp_path) as (_srv, repo, register):
            reader, writer = await register("agent-5", "box-5")
            await _recv(reader)
            await _wait_for(lambda: _row(repo, "agent-5"))

            writer.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(writer.wait_closed(), timeout=5)

            reason = await _wait_for(lambda: _last_error(repo, "agent-5"))
            assert reason, "a disconnected agent recorded nothing about how it ended"


class TestRevokeEndsTheChannelNow:
    async def test_revoking_closes_the_live_connection(self, tmp_path: Path):
        """A long-lived channel that outlives its credential is the #430 finding:
        a compromised agent kept fleet-root exec/write until it happened to
        reconnect, which may be never."""
        async with _hub(tmp_path) as (srv, _repo, register):
            reader, _writer = await register("agent-6", "box-6")
            await _recv(reader)
            assert srv.registry.get("agent-6") is not None

            closed = srv.registry.disconnect("agent-6", "credential revoked by an operator")

            assert closed is True, "revoking did not close the live channel"
            assert srv.registry.get("agent-6") is None
            # The agent's own socket sees the close, which is what actually ends
            # the exec/write channel.
            with contextlib.suppress(asyncio.IncompleteReadError, ConnectionError):
                await asyncio.wait_for(reader.read(1), timeout=5)
            assert reader.at_eof(), "the agent's end of the channel is still open"

    async def test_the_endpoint_reports_whether_a_channel_was_closed(self, tmp_path: Path):
        from fastapi import FastAPI
        from httpx import ASGITransport, AsyncClient

        from homepilot.agent_hub.router import router as agents_router
        from homepilot.auth.deps import require_token

        async with _hub(tmp_path) as (srv, repo, register):
            reader, _w = await register("agent-7", "box-7")
            await _recv(reader)

            app = FastAPI()
            app.include_router(agents_router)
            app.state.repo = repo
            app.state.agent_registry = srv.registry
            app.dependency_overrides[require_token] = lambda: {"scope": "*", "role": "admin"}
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post("/agents/agent-7/revoke")

            assert resp.status_code == 200, resp.text
            assert resp.json()["channel_closed"] is True, (
                "the API reported a revoke that left the channel open as a plain success"
            )


class TestTheAuditTrailCanBeNarrowed:
    async def test_filtering_by_action_returns_only_that_action(self, tmp_path: Path):
        """The repository has always implemented these filters and the route
        dropped them, so 'why did THIS host get turned away' meant reading 100
        mixed rows."""
        async with _hub(tmp_path) as (srv, _repo, register):
            r1, w1 = await register("agent-8", "box-8")
            await _recv(r1)
            r2, _ = await register("agent-9", "box-9", token="garbage")
            await _recv(r2)

            rejected = await _wait_for(lambda: _audit(srv, action="register_rejected"))
            assert rejected
            assert {e["action"] for e in rejected} == {"register_rejected"}, (
                "the action filter returned other actions too"
            )

            for_agent = await _wait_for(lambda: _audit(srv, agent_id="agent-8"))
            assert for_agent
            assert {e["agent_id"] for e in for_agent} == {"agent-8"}
            w1.close()


@_needs_go
class TestTheAgentReportsItsVersion:
    """The release workflow has passed `-X main.version=<tag>` for a long time
    against a symbol that did not exist, and Go says nothing when `-X` misses.
    Only a build that stamps a value and then reads it back can catch that."""

    def _build(self, tmp_path: Path, stamp: str | None) -> str:
        out = tmp_path / "hp-agent"
        ldflags = f"-X main.version={stamp}" if stamp else ""
        cmd = [_GO, "build"]
        if ldflags:
            cmd += ["-ldflags", ldflags]
        cmd += ["-o", str(out), "."]
        proc = subprocess.run(
            cmd,
            cwd=str(AGENT_SRC),
            env={
                **os.environ,
                "CGO_ENABLED": "0",
                "GOCACHE": os.environ.get("GOCACHE") or str(tmp_path / "gocache"),
                "GOFLAGS": "-mod=mod",
            },
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert proc.returncode == 0, proc.stderr
        return str(out)

    async def test_the_release_ldflag_actually_stamps_the_binary(self, tmp_path: Path):
        binary = self._build(tmp_path, "t9.9.9-gate")

        proc = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=30)

        assert proc.returncode == 0, proc.stderr
        assert "t9.9.9-gate" in proc.stdout, (
            "-X main.version did not reach the binary: the release stamp is a no-op "
            f"again (got {proc.stdout!r})"
        )

    async def test_an_unstamped_build_says_dev_rather_than_nothing(self, tmp_path: Path):
        binary = self._build(tmp_path, None)

        proc = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=30)

        assert proc.returncode == 0, proc.stderr
        assert "dev" in proc.stdout

    async def test_the_version_reaches_the_fleet_list(self, tmp_path: Path):
        """The outcome an operator is buying: answering "which hosts still run
        the broken binary" from the fleet list instead of an SSH sweep."""
        binary = self._build(tmp_path, "t1.2.3-fleet")
        async with _hub(tmp_path) as (srv, _repo, _register):
            conf = tmp_path / "agent-home"
            conf.mkdir(parents=True, exist_ok=True)
            proc = subprocess.Popen(
                [binary],
                env={
                    "PATH": "/usr/bin:/bin",
                    "HOME": str(conf),
                    "HP_AGENT_HUB_HOST": "127.0.0.1",
                    "HP_AGENT_HUB_PORT": str(srv._server.sockets[0].getsockname()[1]),
                    "HP_AGENT_AUTH_TOKEN": AUTH,
                    "HP_AGENT_TOKEN_FILE": str(conf / "agent.token"),
                    "HP_AGENT_ID_FILE": str(conf / "agent.id"),
                    "HP_AGENT_TLS": "false",
                    "HP_AGENT_METRICS_ENABLED": "false",
                },
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            try:
                reported = await _wait_for(
                    lambda: _reported_version(srv),
                    timeout=30,
                )
                assert reported == "t1.2.3-fleet", (
                    f"the connected agent reported version {reported!r}; the fleet list "
                    "still cannot tell which binary a host runs"
                )
            finally:
                proc.terminate()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.communicate(timeout=10)


# ── small readers, kept out of the tests so the assertions stay readable ──────


async def _row(repo: Repository, agent_id: str) -> dict[str, Any] | None:
    for row in await repo.list_agents():
        if row["agent_id"] == agent_id:
            return row
    return None


async def _row_is(repo: Repository, agent_id: str, predicate) -> bool:
    row = await _row(repo, agent_id)
    return bool(row is not None and predicate(row))


async def _last_error(repo: Repository, agent_id: str) -> str | None:
    row = await _row(repo, agent_id)
    return row.get("last_error") if row else None


async def _last_error_matching(repo: Repository, agent_id: str, needle: str) -> str | None:
    value = await _last_error(repo, agent_id)
    return value if value and needle in value else None


async def _audit(
    srv: AgentHubServer, action: str | None = None, agent_id: str | None = None
) -> list[dict[str, Any]]:
    return await srv.registry.audit_log.query_persisted(limit=100, action=action, agent_id=agent_id)


async def _reported_version(srv: AgentHubServer) -> str | None:
    for a in srv.registry.list_connected():
        version = (a.get("system_info") or {}).get("agent_version")
        if version:
            return str(version)
    return None
