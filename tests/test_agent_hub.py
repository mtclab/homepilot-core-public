from __future__ import annotations

import asyncio
import json
import struct

import pytest

from homepilot.agent_hub.audit import AuditLog
from homepilot.agent_hub.registry import AgentRegistry
from homepilot.agent_hub.server import HEADER_LEN, _encode


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

        adapter = AgentAdapter(hub_server=None, jump_client=None)
        with pytest.raises(AgentAdapterError, match="no agent or SSH"):
            asyncio.run(adapter.exec("host1", "hostname"))

    def test_readonly_command_validation(self):
        from homepilot.adapters.agent import AgentAdapter, ReadOnlyCommandError

        adapter = AgentAdapter(hub_server=None, jump_client=None)
        with pytest.raises(ReadOnlyCommandError):
            asyncio.run(adapter.exec_readonly("host1", "rm -rf /"))

    def test_guest_host_blocked(self):
        from homepilot.adapters.agent import AgentAdapter, GuestHostError

        adapter = AgentAdapter(hub_server=None, jump_client=None, pve_nodes=["pve1"])
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
