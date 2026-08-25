"""The shared fleet token only enrols a STRANGER inside an operator's window (#537).

The hole: the shared hub token minted per-agent credentials for any hostname the
hub had never seen, for ever. A token that leaked once (a stale `.env`, a shell
history, a screenshot of the UI panel) let anyone add machines to the fleet -
and a fleet member gets fleet-root exec and file access. Revoking the credential
of a machine you did not know existed is not a workflow anybody has.

The rule these gates hold the hub to:

  * a FRESH install (no agents at all) still enrols with the shared token and
    nothing else - zero-touch rollout keeps its promise (#458);
  * once the fleet has a member, a shared-token register for a hostname the
    install has never seen is REFUSED unless an operator has a window open;
  * a refusal says WHY, on the wire and in the audit trail, and does NOT create
    an agent row for the stranger (the #491 invariant: a refused connection must
    not grow the table it was refused from);
  * one-shot bootstrap tokens and per-agent reconnects are untouched;
  * a window EXPIRES - the same register that worked a minute ago is refused
    once the expiry passes, with no background job involved.

Teeth (each proven by reverting the enforcement):
  * make ``AgentHubServer._enrolment_allowed`` return ``None`` unconditionally and
    ``test_closed_window_refuses_a_stranger`` fails on the ack assertion (the
    stranger is enrolled and handed a credential), together with the expiry,
    audit-row and journey gates;
  * drop the ``count_agents() == 0`` exemption and
    ``test_fresh_install_enrols_with_no_window`` fails - the zero-touch install
    it protects stops working.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import struct
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from homepilot.agent_hub import enrolment_window
from homepilot.agent_hub.audit import AuditLog
from homepilot.agent_hub.registry import AgentRegistry
from homepilot.agent_hub.server import (
    ENROLMENT_WINDOW_CLOSED_ERROR,
    GENERIC_AUTH_ERROR,
    AgentHubServer,
    _encode,
)
from homepilot.agent_hub.tokens import hash_token
from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.db.repository import Repository

pytestmark = pytest.mark.asyncio

AUTH = "shared-fleet-token-for-the-window-gates"


# ── A real hub on a real socket ──────────────────────────────────────────────


async def _recv(reader: asyncio.StreamReader) -> dict:
    header = await asyncio.wait_for(reader.readexactly(4), timeout=5)
    length = struct.unpack("!I", header)[0]
    body = await asyncio.wait_for(reader.readexactly(length), timeout=5)
    return dict(json.loads(body.decode()))


class _Hub:
    def __init__(self, server: AgentHubServer, port: int, repo: Repository) -> None:
        self.server = server
        self.port = port
        self.repo = repo
        self._writers: list[asyncio.StreamWriter] = []

    async def register(self, agent_id: str, hostname: str, token: str) -> dict:
        """One full register handshake against the shipped handler; returns the
        hub's reply (a register_ack, or the refusal)."""
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        self._writers.append(writer)
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
        return await _recv(reader)

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
    server = AgentHubServer(host="127.0.0.1", port=0, auth_token=AUTH, registry=registry)
    await server.start()
    assert server._server is not None
    port = int(server._server.sockets[0].getsockname()[1])
    hub = _Hub(server, port, repo)
    try:
        yield hub
    finally:
        await hub.aclose()
        await db.close()


async def _seed_fleet_member(repo: Repository, hostname: str = "already-here") -> str:
    """One agent already enrolled, so the install is no longer 'fresh'."""
    token = "per-agent-token-of-the-incumbent"
    await repo.set_agent_credential("incumbent", hostname, hash_token(token))
    return token


def _freeze(monkeypatch, offset: timedelta) -> None:
    """Move the enrolment window's clock, the way the rest of the suite fakes
    time - never by sleeping through a real 15-minute window."""
    moment = datetime.now(UTC) + offset
    monkeypatch.setattr(enrolment_window, "_utcnow", lambda: moment)


# ── The rule ─────────────────────────────────────────────────────────────────


class TestSharedTokenEnrolment:
    async def test_fresh_install_enrols_with_no_window(self, hub: _Hub):
        """Zero agents = a first rollout: the shared token still enrols with no
        operator input at all (#458).

        Revert-check: delete the `count_agents() == 0` exemption in
        `_enrolment_allowed` and this fails - the ack carries the refusal and no
        credential is minted."""
        ack = await hub.register("a1", "first-host", AUTH)

        assert ack.get("action") == "register_ack", ack
        assert ack.get("auth_token"), "a first enrolment must still be handed a credential"
        assert (await hub.repo.get_agent_credential("a1")) is not None

    async def test_closed_window_refuses_a_stranger(self, hub: _Hub):
        """The fleet has a member and no window is open: a hostname this install
        has never seen is refused, told why, audited - and leaves no row behind.

        Revert-check: make `_enrolment_allowed` return None and every assertion
        below fails, starting with the ack (the stranger is enrolled)."""
        await _seed_fleet_member(hub.repo)
        before = await hub.repo.count_agents()

        ack = await hub.register("stranger", "not-our-host", AUTH)

        assert ack.get("action") != "register_ack", ack
        assert ack.get("error") == ENROLMENT_WINDOW_CLOSED_ERROR, ack
        assert "auth_token" not in ack, "a refused stranger was handed a credential"
        # The invariant from #491: a refusal must not grow the agents table.
        assert await hub.repo.count_agents() == before
        assert (await hub.repo.get_agent_credential("stranger")) is None
        assert hub.server.registry.get("stranger") is None

        await asyncio.sleep(0.1)  # the audit mirror is fire-and-forget
        rows = await hub.repo.query_agent_audit(action="register_rejected")
        assert rows, "the refusal left no audit row"
        assert "not-our-host" in (rows[0]["target"] or ""), rows[0]
        assert "enrolment window" in (rows[0]["target"] or ""), rows[0]

    async def test_bootstrap_token_still_enrols_a_stranger(self, hub: _Hub):
        """The sanctioned "add one host later" path is untouched: a one-shot
        bootstrap token enrols with the window shut."""
        await _seed_fleet_member(hub.repo)
        bootstrap = await hub.server._token_store.create()

        ack = await hub.register("newcomer", "brand-new-host", bootstrap)

        assert ack.get("action") == "register_ack", ack
        assert ack.get("auth_token"), "a bootstrap enrolment must still mint a credential"

    async def test_known_agent_reconnects_with_its_own_credential(self, hub: _Hub):
        """A steady-state reconnect authenticates as per-agent and never reaches
        the window check."""
        token = await _seed_fleet_member(hub.repo, "already-here")

        ack = await hub.register("incumbent", "already-here", token)

        assert ack.get("action") == "register_ack", ack
        # Nothing re-minted: it kept the credential it presented.
        assert "auth_token" not in ack

    async def test_open_window_lets_a_stranger_in_then_expiry_shuts_it(
        self, hub: _Hub, monkeypatch
    ):
        """Open -> the stranger enrols. Past the expiry -> the same register is
        refused again, with no background job having run.

        Revert-check: make `enrolment_window.status` ignore the expiry (always
        "open") and the second half fails - a 15-minute window would stay open
        for ever."""
        await _seed_fleet_member(hub.repo)
        await enrolment_window.open_window(hub.repo, minutes=15)

        ack = await hub.register("guest", "guest-host", AUTH)
        assert ack.get("action") == "register_ack", ack
        assert ack.get("auth_token")

        _freeze(monkeypatch, timedelta(minutes=16))
        later = await hub.register("guest2", "guest-host-2", AUTH)
        assert later.get("error") == ENROLMENT_WINDOW_CLOSED_ERROR, later

    async def test_a_wrong_token_still_says_nothing(self, hub: _Hub):
        """The window refusal is the ONLY refusal that explains itself: a bad
        token still gets the uninformative answer a guesser deserves."""
        await _seed_fleet_member(hub.repo)

        ack = await hub.register("guesser", "guessed-host", "not-the-token")

        assert ack.get("error") == GENERIC_AUTH_ERROR, ack


class TestWindowState:
    async def test_status_is_truthful_across_open_expire_close(self, tmp_path: Path, monkeypatch):
        db = Database(str(tmp_path / "w.db"))
        await db.connect()
        await run_migrations(db)
        repo = Repository(db)
        try:
            assert await enrolment_window.is_open(repo) is False

            opened = await enrolment_window.open_window(repo, minutes=15)
            assert opened["minutes"] == 15
            assert await enrolment_window.is_open(repo) is True

            _freeze(monkeypatch, timedelta(minutes=14))
            assert await enrolment_window.is_open(repo) is True
            _freeze(monkeypatch, timedelta(minutes=16))
            assert await enrolment_window.is_open(repo) is False

            monkeypatch.undo()
            await enrolment_window.open_window(repo, minutes=60)
            assert await enrolment_window.is_open(repo) is True
            await enrolment_window.close_window(repo)
            assert await enrolment_window.is_open(repo) is False
        finally:
            await db.close()

    async def test_length_is_capped_at_a_day(self, tmp_path: Path):
        db = Database(str(tmp_path / "cap.db"))
        await db.connect()
        await run_migrations(db)
        repo = Repository(db)
        try:
            result = await enrolment_window.open_window(repo, minutes=99999)
            assert result["minutes"] == enrolment_window.MAX_WINDOW_MINUTES
        finally:
            await db.close()

    async def test_an_unreadable_expiry_is_closed(self, tmp_path: Path):
        """Fail closed: a corrupted value must never read as 'open for ever'."""
        db = Database(str(tmp_path / "junk.db"))
        await db.connect()
        await run_migrations(db)
        repo = Repository(db)
        try:
            await repo.set_setting(enrolment_window.WINDOW_KEY, "not-a-timestamp")
            assert await enrolment_window.is_open(repo) is False
        finally:
            await db.close()


# ── The API ──────────────────────────────────────────────────────────────────


class _Registry:
    def __init__(self, repo: Repository) -> None:
        self.hub_server = MagicMock()
        self.hub_server.host = "hub.example"
        self.hub_server.port = 8443
        self.hub_server.tls = False
        self.hub_server.cert_sha256 = ""
        self.audit_log = AuditLog(repo=repo)

    def get(self, agent_id: str):
        return None


@pytest.fixture
async def api(tmp_path: Path, monkeypatch):
    import homepilot.app_state as app_state
    from homepilot.agent_hub.router import router as agents_router
    from homepilot.auth.tokens import generate_api_token
    from homepilot.config import get_settings

    monkeypatch.setenv("HP_COOKIE_SECURE", "false")
    get_settings.cache_clear()

    db = Database(str(tmp_path / "api.db"))
    await db.connect()
    await run_migrations(db)
    repo = Repository(db)

    user_id = await repo.create_user("admin", "admin@example.com")
    admin_token, prefix, token_hash = generate_api_token()
    await repo.create_api_token(
        user_id=user_id, token_type="api", prefix=prefix, hash=token_hash, scope="*"
    )
    reader_token, r_prefix, r_hash = generate_api_token()
    await repo.create_api_token(
        user_id=user_id, token_type="api", prefix=r_prefix, hash=r_hash, scope="read"
    )

    registry = _Registry(repo)
    monkeypatch.setattr(app_state, "_agent_registry", registry, raising=False)

    app = FastAPI()
    app.include_router(agents_router)
    app.state.repo = repo
    app.state.agent_registry = registry

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, admin_token, reader_token, repo
    await db.close()
    get_settings.cache_clear()


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class TestTheRealAgentIsTurnedAway:
    """The journey, on the shipped path: the real Go binary, the real installer
    credential (the shared token), a real hub - and a host that never joins.

    The harness is the existing TLS-journey one (a hub started exactly the way a
    default install starts one, plus a spawned `hp-agent`), so this costs a
    handful of lines rather than a second rig.
    """

    async def test_a_stranger_running_the_real_binary_never_joins(
        self, hp_agent_binary: str, tmp_path: Path
    ):
        """Revert-check: make `_enrolment_allowed` return None and this fails -
        the host appears in the fleet and the log carries no refusal."""
        import shutil
        import tempfile

        from .test_agent_hub_tls_journey import _hub_on, _shutdown, _spawn_agent, _wait_connected

        hp_dir = tempfile.mkdtemp(dir=str(Path.home()), prefix=".hp-test-window-")
        state, settings = await _hub_on(hp_dir)
        proc = None
        try:
            repo = Repository(state.database)
            # The fleet already has a member, so this install is not "fresh" -
            # and the window is shut, because nobody opened one.
            await _seed_fleet_member(repo, "some-other-host")
            before = await repo.count_agents()

            pin = "sha256:" + state.agent_hub.cert_fingerprint
            proc = _spawn_agent(hp_agent_binary, tmp_path / "stranger", settings, pin)

            assert not await _wait_connected(state, timeout=8), (
                "a host nobody added joined the fleet on a leaked shared token"
            )
            assert await repo.count_agents() == before, "a refused stranger grew the agents table"
        finally:
            log = await _shutdown(state, proc)
            shutil.rmtree(hp_dir, ignore_errors=True)
            assert "not accepting new hosts right now" in log, (
                "the agent never logged the hub's reason, so the operator on that "
                f"host has nothing to go on: {log[-2000:]}"
            )


class TestWindowApi:
    async def test_open_status_close_round_trip(self, api):
        client, admin, _reader, repo = api

        closed = await client.get("/agents/enrolment-window", headers=_auth(admin))
        assert closed.status_code == 200, closed.text
        assert closed.json()["open"] is False
        assert closed.json()["fleet_empty"] is True

        await _seed_fleet_member(repo)
        opened = await client.post(
            "/agents/enrolment-window", json={"minutes": 30}, headers=_auth(admin)
        )
        assert opened.status_code == 200, opened.text
        body = opened.json()
        assert body["open"] is True
        assert body["expires_at"]
        assert body["fleet_empty"] is False
        assert 0 < body["seconds_remaining"] <= 30 * 60

        # GET reports the same truth the enforcement reads.
        status = await client.get("/agents/enrolment-window", headers=_auth(admin))
        assert status.json()["open"] is True
        assert await enrolment_window.is_open(repo) is True

        shut = await client.delete("/agents/enrolment-window", headers=_auth(admin))
        assert shut.status_code == 200, shut.text
        assert shut.json()["open"] is False
        assert await enrolment_window.is_open(repo) is False

    async def test_open_and_close_are_audited_with_the_caller(self, api):
        """Widening who may join the fleet is exactly what an audit trail is
        for: both edges name the operator who moved them."""
        client, admin, _reader, repo = api

        await client.post("/agents/enrolment-window", json={"minutes": 5}, headers=_auth(admin))
        await client.delete("/agents/enrolment-window", headers=_auth(admin))
        await asyncio.sleep(0.1)  # fire-and-forget mirror

        opened = await repo.query_agent_audit(action="enrolment_window_opened")
        closed = await repo.query_agent_audit(action="enrolment_window_closed")
        assert opened and closed, (opened, closed)
        assert "minutes=5" in (opened[0]["target"] or "")
        assert opened[0]["caller"] and opened[0]["caller"] != "unknown"
        assert closed[0]["caller"] == opened[0]["caller"]

    async def test_a_read_token_cannot_open_the_window(self, api):
        """Scope-enforced: opening or closing the door to new fleet members is an
        admin action, not something a read token can do. Reading the window state,
        however, was loosened to `read` in wave 3 (it exposes no secret)."""
        client, _admin, reader, repo = api

        for call in (
            client.post("/agents/enrolment-window", json={"minutes": 5}, headers=_auth(reader)),
            client.delete("/agents/enrolment-window", headers=_auth(reader)),
        ):
            resp = await call
            assert resp.status_code == 403, resp.text
        # The GET is a plain read now: a read token may see the window state.
        read = await client.get("/agents/enrolment-window", headers=_auth(reader))
        assert read.status_code == 200, read.text
        assert await enrolment_window.is_open(repo) is False

    async def test_unauthenticated_is_refused(self, api):
        client, _admin, _reader, _repo = api
        resp = await client.post("/agents/enrolment-window", json={"minutes": 5})
        assert resp.status_code in (401, 403), resp.text

    async def test_a_window_longer_than_a_day_is_refused(self, api):
        client, admin, _reader, repo = api
        resp = await client.post(
            "/agents/enrolment-window", json={"minutes": 60 * 25}, headers=_auth(admin)
        )
        assert resp.status_code == 422, resp.text
        assert await enrolment_window.is_open(repo) is False
