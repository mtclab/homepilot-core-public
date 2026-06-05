from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from homepilot.adapters.agent import AgentAdapter, AgentAdapterError, GuestHostError


class TestAgentAdapterResolve:
    def test_resolve_agent_id_found(self):
        hub = MagicMock()
        agent = MagicMock()
        agent.agent_id = "agent-001"
        hub.registry.get_by_hostname.return_value = agent
        adapter = AgentAdapter(hub_server=hub)
        assert adapter._resolve_agent_id("web1") == "agent-001"

    def test_resolve_agent_id_not_found(self):
        hub = MagicMock()
        hub.registry.get_by_hostname.return_value = None
        adapter = AgentAdapter(hub_server=hub)
        assert adapter._resolve_agent_id("web1") is None

    def test_resolve_agent_id_no_hub(self):
        adapter = AgentAdapter(hub_server=None)
        assert adapter._resolve_agent_id("web1") is None


class TestAgentAdapterExec:
    async def test_exec_via_hub(self):
        hub = MagicMock()
        agent = MagicMock()
        agent.agent_id = "agent-001"
        hub.registry.get_by_hostname.return_value = agent
        hub.send_command = AsyncMock(return_value={"exit_code": 0, "stdout": "web1", "stderr": ""})
        adapter = AgentAdapter(hub_server=hub)
        rc, out, _err = await adapter.exec("web1", "hostname")
        assert rc == 0
        assert out == "web1"
        hub.send_command.assert_called_once_with("agent-001", "hostname", 30)

    async def test_exec_hub_fails_fallback_no_ssh(self):
        hub = MagicMock()
        agent = MagicMock()
        agent.agent_id = "agent-001"
        hub.registry.get_by_hostname.return_value = None
        adapter = AgentAdapter(hub_server=hub, jump_client=None)
        with pytest.raises(AgentAdapterError, match="no agent or SSH"):
            await adapter.exec("unknown", "hostname")

    async def test_exec_pve_node_blocked(self):
        adapter = AgentAdapter(pve_nodes=["pve1"])
        with pytest.raises(GuestHostError, match="PVE node"):
            await adapter.exec("pve1", "hostname")


class TestAgentAdapterReadFile:
    async def test_read_file_via_hub(self):
        hub = MagicMock()
        agent = MagicMock()
        agent.agent_id = "agent-001"
        hub.registry.get_by_hostname.return_value = agent
        hub.send_read_file = AsyncMock(return_value={"content": "hello world"})
        adapter = AgentAdapter(hub_server=hub)
        content = await adapter.read_file("web1", "/etc/hostname")
        assert content == "hello world"
        hub.send_read_file.assert_called_once_with("agent-001", "/etc/hostname")


class TestAgentAdapterWriteFile:
    async def test_write_file_via_hub(self):
        hub = MagicMock()
        agent = MagicMock()
        agent.agent_id = "agent-001"
        hub.registry.get_by_hostname.return_value = agent
        hub.send_write_file = AsyncMock(return_value={})
        hub.send_read_file = AsyncMock(side_effect=AgentAdapterError("not found"))
        adapter = AgentAdapter(hub_server=hub)
        result = await adapter.write_file("web1", "/etc/test", "content")
        assert result["changed"] is True
        hub.send_write_file.assert_called_once_with("agent-001", "/etc/test", "content")


class TestAgentAdapterConnection:
    async def test_connection_agent_connected(self):
        hub = MagicMock()
        agent = MagicMock()
        agent.agent_id = "agent-001"
        hub.registry.get_by_hostname.return_value = agent
        adapter = AgentAdapter(hub_server=hub)
        assert await adapter.test_connection("web1") is True

    async def test_connection_no_agent_no_ssh(self):
        hub = MagicMock()
        hub.registry.get_by_hostname.return_value = None
        adapter = AgentAdapter(hub_server=hub, jump_client=None)
        assert await adapter.test_connection("unknown") is False
