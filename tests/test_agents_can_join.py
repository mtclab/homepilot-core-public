"""Agents can join (lane A3, review 05/3): a refused agent is SEEN, a genuine one
is not banned for waiting, and a pending bootstrap token is listed.

The holes, measured on main before this change:

  * an agent installed while the enrolment window was shut was refused with
    "not accepting new hosts right now" and retried every 30 s for days - and
    the refusal lived only in the host's journal and the audit trail.
    ``record_agent_error`` (correctly) writes nothing for a hostname the fleet
    has never seen, so no page, API or MCP tool said a host was waiting;
  * every refusal fed the per-peer ban, so the same honest agent was banned for
    60 s after every 5 refusals - it held the REAL shared token, it was not
    guessing anything - and opening the window did not let it in until the ban
    ran out;
  * a bootstrap token that was minted and never used was invisible.

What these gates hold:

  * a refused register leaves one bounded ``enrolment_attempts`` row per
    hostname + peer (count, last seen, reason) and NEVER the token it presented;
  * the read API and the MCP tool list it, and a valid-token "window closed"
    refusal is marked ``wants_to_join``;
  * window-closed refusals do not count toward the peer ban; wrong tokens still
    do;
  * opening the window lets the host in on its next retry, and joining clears
    its row;
  * minted-unused bootstrap tokens are listed with who minted them and expiry.

The goal is proven with the REAL ``hp-agent`` binary (``TestTheRealAgent``).

Teeth (each proven by reverting the fix and watching the named test fail):
  * call ``_record_auth_failure`` for every denial again (drop the
    ``counts_toward_ban`` check in ``AgentHubServer._handle_agent``) ->
    ``test_a_genuine_agent_knocking_is_never_banned`` (the 6th knock gets no
    answer) and the real-binary ``test_a_refused_agent_is_seen_waits_unbanned_
    and_joins_when_the_window_opens`` fail;
  * drop the ``_record_attempt`` call -> every "is seen" test fails;
  * drop the ``_clear_attempts`` call -> ``test_joining_clears_the_knock`` fails;
  * make ``Denial.counts_toward_ban`` return False -> the bad-token ban tests
    fail.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from homepilot.agent_hub import enrolment_window, join_requests
from homepilot.agent_hub import server as hub_server_module
from homepilot.agent_hub.audit import AuditLog
from homepilot.agent_hub.registry import AgentRegistry
from homepilot.agent_hub.server import (
    ENROLMENT_WINDOW_CLOSED_ERROR,
    MAX_AUTH_FAILURES,
    AgentHubServer,
    _encode,
)
from homepilot.agent_hub.tokens import BootstrapTokenStore, hash_token
from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.db.repository import Repository

pytestmark = pytest.mark.asyncio

AUTH = "shared-fleet-token-for-the-join-gates"  # pragma: allowlist secret - test fixture
WRONG = "a-guessed-token-that-matches-nothing"  # pragma: allowlist secret - test fixture


# ── A real hub on a real socket ──────────────────────────────────────────────


class _Hub:
    def __init__(self, server: AgentHubServer, port: int, repo: Repository, db: Database):
        self.server = server
        self.port = port
        self.repo = repo
        self.db = db
        self._writers: list[asyncio.StreamWriter] = []

    async def knock(self, hostname: str, token: str, agent_id: str = "") -> dict | None:
        """One register handshake. Returns the hub's reply, or ``None`` when the
        hub hung up without answering - which is what a BANNED peer gets."""
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        self._writers.append(writer)
        writer.write(
            _encode(
                {
                    "action": "register",
                    "auth_token": token,
                    "agent_id": agent_id or f"id-{hostname}",
                    "hostname": hostname,
                    "request_id": f"reg-{hostname}",
                }
            )
        )
        await writer.drain()
        try:
            header = await asyncio.wait_for(reader.readexactly(4), timeout=5)
        except (asyncio.IncompleteReadError, ConnectionResetError):
            return None
        body = await asyncio.wait_for(reader.readexactly(int.from_bytes(header, "big")), 5)
        return dict(json.loads(body.decode()))

    async def dump(self) -> str:
        """Every row of the database as SQL text - the place a stored token
        would show up, whatever column or table it ended up in."""
        lines = []
        async with self.db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ) as cursor:
            tables = [r[0] for r in await cursor.fetchall()]
        for table in tables:
            async with self.db.conn.execute(f"SELECT * FROM {table}") as cursor:  # nosec B608
                lines.extend(repr(tuple(r)) for r in await cursor.fetchall())
        return "\n".join(lines)

    async def aclose(self) -> None:
        for writer in self._writers:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
        await self.server.stop()


@pytest.fixture
async def hub(tmp_path: Path):
    db = Database(str(tmp_path / "hub.db"))
    await db.connect()
    await run_migrations(db)
    repo = Repository(db)
    registry = AgentRegistry(repo=repo)
    registry.audit_log = AuditLog(repo=repo)
    server = AgentHubServer(
        host="127.0.0.1",
        port=0,
        auth_token=AUTH,
        registry=registry,
        token_store=BootstrapTokenStore(db=db),
    )
    await server.start()
    assert server._server is not None
    port = int(server._server.sockets[0].getsockname()[1])
    hub = _Hub(server, port, repo, db)
    # One agent already enrolled, so the install is not "fresh" and the window
    # rule applies.
    await repo.set_agent_credential("incumbent", "already-here", hash_token("incumbent-token"))
    try:
        yield hub
    finally:
        await hub.aclose()
        await db.close()


def _only(rows: list[dict], hostname: str) -> dict:
    mine = [r for r in rows if r["hostname"] == hostname]
    assert len(mine) == 1, f"expected one row for {hostname}, got {rows}"
    return mine[0]


# ── The ban ──────────────────────────────────────────────────────────────────


class TestTheBanIsForGuessers:
    async def test_a_genuine_agent_knocking_is_never_banned(self, hub: _Hub):
        """Twice the ban threshold of window-closed refusals, and every one is
        still ANSWERED with the reason - then the window opens and the very next
        knock is let in. Before the fix the 6th knock got a silent hang-up and
        the host stayed locked out for a minute after the window opened."""
        knocks = MAX_AUTH_FAILURES * 2
        for n in range(knocks):
            reply = await hub.knock("new-box", AUTH)
            assert reply is not None, f"knock {n + 1} was banned: a valid token is not a guess"
            assert reply.get("error") == ENROLMENT_WINDOW_CLOSED_ERROR, reply

        await enrolment_window.open_window(hub.repo, 5)
        ack = await hub.knock("new-box", AUTH)
        assert ack is not None and ack.get("action") == "register_ack", ack

    async def test_a_wrong_token_is_still_banned(self, hub: _Hub):
        for _ in range(MAX_AUTH_FAILURES):
            reply = await hub.knock("guesser", WRONG)
            assert reply is not None and reply.get("error") == "invalid auth_token", reply
        assert await hub.knock("guesser", WRONG) is None, (
            "a peer that failed MAX_AUTH_FAILURES credential checks was not banned"
        )

    async def test_window_refusals_do_not_launder_a_guessing_streak(self, hub: _Hub):
        """Interleaving genuine-token knocks must neither reset nor pad the
        guess count: the bad tokens alone still reach the ban."""
        for _ in range(MAX_AUTH_FAILURES - 1):
            assert await hub.knock("mixed", WRONG) is not None
            assert await hub.knock("mixed", AUTH) is not None
        assert await hub.knock("mixed", WRONG) is not None  # the 5th bad one
        assert await hub.knock("mixed", AUTH) is None, "the guessing peer was not banned"


# ── Seen ─────────────────────────────────────────────────────────────────────


class TestARefusedAgentIsSeen:
    async def test_a_knock_is_recorded_with_count_reason_and_peer(self, hub: _Hub):
        for _ in range(3):
            await hub.knock("new-box", AUTH)

        state = await join_requests.payload(hub.repo)
        row = _only(state["attempts"], "new-box")
        assert row["attempts"] == 3
        assert row["peer"] == "127.0.0.1"
        assert row["reason_code"] == "window_closed"
        assert row["wants_to_join"] is True
        assert "enrolment window" in row["reason"]
        assert row["first_seen"] <= row["last_seen"]
        assert state["summary"]["wants_to_join"] == 1
        assert state["window"]["open"] is False

    async def test_a_wrong_token_is_seen_but_does_not_want_to_join(self, hub: _Hub):
        await hub.knock("typo-box", WRONG)
        state = await join_requests.payload(hub.repo)
        row = _only(state["attempts"], "typo-box")
        assert row["reason_code"] == "bad_token"
        assert row["wants_to_join"] is False, (
            "opening the window would not let a wrong token in; offering it would lie"
        )
        assert state["summary"] == {
            "wants_to_join": 0,
            "refused": 1,
            "pending_tokens": 0,
            "expired_unused_tokens": 0,
        }

    async def test_no_presented_token_is_ever_stored(self, hub: _Hub):
        """Neither the shared token (valid, refused for the window) nor a wrong
        one may land anywhere in the database because of a refusal."""
        await hub.knock("new-box", AUTH)
        await hub.knock("typo-box", WRONG)
        await asyncio.sleep(0.1)  # the audit mirror is fire-and-forget

        dumped = await hub.dump()
        assert "new-box" in dumped and "typo-box" in dumped  # the dump sees the rows
        for secret in (AUTH, WRONG):
            assert secret not in dumped, "a presented token was written to the database"
            assert hash_token(secret) not in dumped, "a presented token's hash was stored"

    async def test_joining_clears_the_knock(self, hub: _Hub):
        await hub.knock("new-box", AUTH)
        await enrolment_window.open_window(hub.repo, 5)
        ack = await hub.knock("new-box", AUTH)
        assert ack is not None and ack.get("action") == "register_ack"

        state = await join_requests.payload(hub.repo)
        assert [r for r in state["attempts"] if r["hostname"] == "new-box"] == [], (
            "a host that joined is still listed as wanting to join"
        )


# ── Bounded ──────────────────────────────────────────────────────────────────


class TestTheRecordIsBounded:
    async def test_a_flood_cannot_grow_it_or_evict_a_genuine_host(self, hub: _Hub, monkeypatch):
        # The genuine knock is the OLDEST row, so plain oldest-first eviction
        # would drop it first; only the wrong-token-first order keeps it.
        an_hour_ago = datetime.now(UTC) - timedelta(hours=1)
        monkeypatch.setattr(join_requests, "_utcnow", lambda: an_hour_ago)
        await join_requests.record_attempt(
            hub.repo,
            hostname="the-real-one",
            peer="192.0.2.10",
            code="window_closed",
            reason="no window",
        )
        monkeypatch.setattr(join_requests, "_utcnow", lambda: datetime.now(UTC))
        for i in range(join_requests.MAX_ATTEMPT_ROWS * 2):
            await join_requests.record_attempt(
                hub.repo,
                hostname=f"flood-{i}",
                peer=f"198.51.100.{i % 250}",
                code="bad_token",
                reason="guess",
            )
        rows = await hub.repo.list_enrolment_attempts("")
        assert len(rows) == join_requests.MAX_ATTEMPT_ROWS
        assert any(r["hostname"] == "the-real-one" for r in rows), (
            "a wrong-token flood evicted the host that is genuinely waiting"
        )

    async def test_old_knocks_age_out(self, hub: _Hub, monkeypatch):
        await hub.knock("long-gone", AUTH)
        later = datetime.now(UTC) + join_requests.ATTEMPT_MAX_AGE + timedelta(hours=1)
        monkeypatch.setattr(join_requests, "_utcnow", lambda: later)

        state = await join_requests.payload(hub.repo)
        assert state["attempts"] == [], "a week-old knock is still reported"
        # ...and the next write actually deletes it, not just hides it.
        await join_requests.record_attempt(
            hub.repo, hostname="fresh", peer="192.0.2.5", code="bad_token", reason="x"
        )
        assert [r["hostname"] for r in await hub.repo.list_enrolment_attempts("")] == ["fresh"]

    async def test_peer_supplied_fields_are_capped_and_cleaned(self, hub: _Hub):
        await join_requests.record_attempt(
            hub.repo,
            hostname="x" * 5000 + "\n\x1b[31mforged",
            peer="192.0.2.7",
            code="bad_token",
            reason="r",
            agent_id="y" * 5000,
        )
        row = (await hub.repo.list_enrolment_attempts(""))[0]
        assert len(row["hostname"]) <= 253
        assert "\n" not in row["hostname"] and "\x1b" not in row["hostname"]
        assert len(row["agent_id"]) <= 128


# ── Pending bootstrap tokens ─────────────────────────────────────────────────


class TestPendingTokensAreListed:
    async def test_minted_unused_then_used(self, hub: _Hub):
        token = await hub.server._token_store.create(minted_by="operator (token:abcd1234)")

        state = await join_requests.payload(hub.repo)
        assert state["summary"]["pending_tokens"] == 1
        listed = state["pending_tokens"][0]
        assert listed["minted_by"] == "operator (token:abcd1234)"
        assert listed["expired"] is False and listed["seconds_remaining"] > 0
        assert listed["expires_at"] and listed["created_at"]
        assert token not in json.dumps(state), "the listing carries the token itself"

        ack = await hub.knock("bootstrapped", token)
        assert ack is not None and ack.get("action") == "register_ack"
        state = await join_requests.payload(hub.repo)
        assert state["pending_tokens"] == [], "a used token is still listed as pending"

    async def test_an_expired_unused_token_says_so(self, hub: _Hub):
        await hub.server._token_store.create(minted_by="someone")
        await hub.db.execute("UPDATE bootstrap_tokens SET expires_at = 1")
        await hub.db.conn.commit()

        state = await join_requests.payload(hub.repo)
        assert state["pending_tokens"][0]["expired"] is True
        assert state["summary"] == {
            "wants_to_join": 0,
            "refused": 0,
            "pending_tokens": 0,
            "expired_unused_tokens": 1,
        }


# ── The API ──────────────────────────────────────────────────────────────────


async def _api_client(repo: Repository, registry, monkeypatch):
    """The real agents router over ASGI, with an admin and a read token."""
    import homepilot.app_state as app_state
    from homepilot.agent_hub.router import router as agents_router
    from homepilot.auth.tokens import generate_api_token

    user_id = await repo.create_user("admin", "admin@example.com")
    admin, a_prefix, a_hash = generate_api_token()
    await repo.create_api_token(
        user_id=user_id, token_type="api", prefix=a_prefix, hash=a_hash, scope="*"
    )
    reader, r_prefix, r_hash = generate_api_token()
    await repo.create_api_token(
        user_id=user_id, token_type="api", prefix=r_prefix, hash=r_hash, scope="read"
    )
    app = FastAPI()
    app.include_router(agents_router)
    app.state.repo = repo
    app.state.agent_registry = registry
    monkeypatch.setattr(app_state, "_agent_registry", registry, raising=False)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test"), admin, reader


@pytest.fixture
async def api(hub: _Hub, monkeypatch):
    from homepilot.config import get_settings

    monkeypatch.setenv("HP_COOKIE_SECURE", "false")
    get_settings.cache_clear()
    hub.server.registry.hub_server = hub.server
    client, admin, reader = await _api_client(hub.repo, hub.server.registry, monkeypatch)
    async with client:
        yield client, admin, reader
    get_settings.cache_clear()


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class TestTheReadApi:
    async def test_a_read_token_sees_who_is_knocking(self, hub: _Hub, api):
        client, _admin, reader = api
        await hub.knock("new-box", AUTH)

        resp = await client.get("/agents/enrolment-requests", headers=_bearer(reader))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert _only(body["attempts"], "new-box")["wants_to_join"] is True
        assert body["summary"]["wants_to_join"] == 1
        assert AUTH not in resp.text

    async def test_unauthenticated_is_refused(self, api):
        client, _admin, _reader = api
        resp = await client.get("/agents/enrolment-requests")
        assert resp.status_code in (401, 403)

    async def test_a_minted_bootstrap_is_pending_with_the_minter(self, hub: _Hub, api):
        client, admin, reader = api
        minted = await client.post("/agents/bootstrap", headers=_bearer(admin))
        assert minted.status_code == 200, minted.text
        token = minted.json()["bootstrap_token"]

        resp = await client.get("/agents/enrolment-requests", headers=_bearer(reader))
        pending = resp.json()["pending_tokens"]
        assert len(pending) == 1
        assert pending[0]["minted_by"] and "admin" in pending[0]["minted_by"]
        assert token not in resp.text


# ── The journey, on the shipped path ─────────────────────────────────────────


async def _wait_for(predicate, timeout: float, step: float = 0.25):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        value = await predicate()
        if value:
            return value
        await asyncio.sleep(step)
    return await predicate()


class TestTheRealAgent:
    """The real ``hp-agent`` binary against a hub started the way an install
    starts one (the TLS-journey rig), driven through the real HTTP routes.

    The ban threshold is lowered to 2 for these two journeys only, so the
    agent's own backoff (1, 2, 4, 8 s) crosses it within seconds instead of the
    half-minute five knocks take; the rule under test is unchanged - which
    refusals count - and the hub-level gates above run at the real threshold.
    """

    async def test_a_refused_agent_is_seen_waits_unbanned_and_joins_when_the_window_opens(
        self, hp_agent_binary: str, tmp_path: Path, monkeypatch
    ):
        from .test_agent_hub_tls_journey import _hub_on, _shutdown, _spawn_agent

        monkeypatch.setattr(hub_server_module, "MAX_AUTH_FAILURES", 2)
        hp_dir = tempfile.mkdtemp(dir=str(Path.home()), prefix=".hp-test-join-")
        state, settings = await _hub_on(hp_dir)
        proc = None
        try:
            repo = Repository(state.database)
            await repo.set_agent_credential("incumbent", "some-other-host", hash_token("t"))
            client, admin, reader = await _api_client(repo, state.agent_registry, monkeypatch)
            async with client:

                async def knocks() -> dict | None:
                    resp = await client.get("/agents/enrolment-requests", headers=_bearer(reader))
                    assert resp.status_code == 200, resp.text
                    waiting = [a for a in resp.json()["attempts"] if a["wants_to_join"]]
                    return waiting[0] if waiting else None

                pin = "sha256:" + state.agent_hub.cert_fingerprint
                proc = _spawn_agent(hp_agent_binary, tmp_path / "newcomer", settings, pin)

                # Seen: within one retry it is on the API as wanting to join.
                first = await _wait_for(knocks, timeout=10)
                assert first, "a refused agent never showed up as wanting to join"
                assert first["reason_code"] == "window_closed"

                # Not banned: it keeps getting ANSWERED past the ban threshold.
                # Banned connections are hung up before the register is read,
                # so a banned agent's count would freeze at the threshold.
                async def past_threshold() -> dict | None:
                    row = await knocks()
                    return row if row and row["attempts"] > 2 else None

                assert await _wait_for(past_threshold, timeout=15), (
                    "the agent's knocks stopped being heard at the ban threshold: a "
                    "valid shared token refused for want of a window was banned"
                )
                assert not state.agent_hub._auth_bans, state.agent_hub._auth_bans
                assert not state.agent_registry.list_connected()

                # Let it in: one operator action, nothing touched on the host.
                opened = await client.post(
                    "/agents/enrolment-window", json={"minutes": 5}, headers=_bearer(admin)
                )
                assert opened.status_code == 200, opened.text

                async def joined() -> list:
                    return list(state.agent_registry.list_connected())

                assert await _wait_for(joined, timeout=30), (
                    "the window opened and the agent still did not join"
                )
                assert await _wait_for(lambda: _none(knocks), timeout=5), (
                    "the agent joined and is still listed as wanting to join"
                )
        finally:
            log = await _shutdown(state, proc)
            shutil.rmtree(hp_dir, ignore_errors=True)
        assert "not accepting new hosts right now" in log, log[-2000:]

    async def test_an_agent_with_a_wrong_token_is_seen_and_banned(
        self, hp_agent_binary: str, tmp_path: Path, monkeypatch
    ):
        from .test_agent_hub_tls_journey import _hub_on, _shutdown, _spawn_agent

        monkeypatch.setattr(hub_server_module, "MAX_AUTH_FAILURES", 2)
        hp_dir = tempfile.mkdtemp(dir=str(Path.home()), prefix=".hp-test-join-bad-")
        state, settings = await _hub_on(hp_dir)
        proc = None
        try:
            repo = Repository(state.database)
            await repo.set_agent_credential("incumbent", "some-other-host", hash_token("t"))
            settings.agent_hub_auth_token = WRONG  # what the spawned agent presents
            pin = "sha256:" + state.agent_hub.cert_fingerprint
            proc = _spawn_agent(hp_agent_binary, tmp_path / "guesser", settings, pin)

            async def banned() -> bool:
                return bool(state.agent_hub._auth_bans)

            assert await _wait_for(banned, timeout=15), "a wrong-token agent was never banned"
            rows = await repo.list_enrolment_attempts("")
            assert rows and rows[0]["reason_code"] == "bad_token", rows
            assert not state.agent_registry.list_connected()
            async with state.database.conn.execute("SELECT * FROM enrolment_attempts") as cursor:
                stored = repr([tuple(r) for r in await cursor.fetchall()])
            assert WRONG not in stored
        finally:
            await _shutdown(state, proc)
            shutil.rmtree(hp_dir, ignore_errors=True)


async def _none(fetch) -> bool:
    return await fetch() is None
