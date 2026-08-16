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

    async def test_exec_hub_fails_no_agent(self):
        hub = MagicMock()
        agent = MagicMock()
        agent.agent_id = "agent-001"
        hub.registry.get_by_hostname.return_value = None
        adapter = AgentAdapter(hub_server=hub)
        with pytest.raises(AgentAdapterError, match="no agent connected"):
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


class TestAgentAdapterProvision:
    """#397 phase-B1: install_package / manage_service / write_config dispatch the
    right action+params to the hub and return the parsed {changed, detail}."""

    @staticmethod
    def _adapter_with_hub(send_return):
        hub = MagicMock()
        agent = MagicMock()
        agent.agent_id = "agent-001"
        hub.registry.get_by_hostname.return_value = agent
        hub.send_action = AsyncMock(return_value=send_return)
        return AgentAdapter(hub_server=hub), hub

    async def test_install_package_dispatches(self):
        adapter, hub = self._adapter_with_hub({"changed": True, "detail": "nginx installed"})
        result = await adapter.install_package("web1", "nginx")
        assert result == {"changed": True, "detail": "nginx installed"}
        hub.send_action.assert_called_once_with("agent-001", "install_package", {"name": "nginx"})

    async def test_manage_service_dispatches(self):
        adapter, hub = self._adapter_with_hub({"changed": False, "detail": "nginx already active"})
        result = await adapter.manage_service("web1", "nginx", "started")
        assert result == {"changed": False, "detail": "nginx already active"}
        hub.send_action.assert_called_once_with(
            "agent-001", "manage_service", {"name": "nginx", "state": "started"}
        )

    async def test_write_config_dispatches(self):
        adapter, hub = self._adapter_with_hub({"changed": True, "detail": "written"})
        result = await adapter.write_config("web1", "/etc/nginx/app.conf", "server {}", mode="0640")
        assert result == {"changed": True, "detail": "written"}
        hub.send_action.assert_called_once_with(
            "agent-001",
            "write_config",
            {"path": "/etc/nginx/app.conf", "content": "server {}", "mode": "0640"},
        )

    async def test_write_config_defaults_mode(self):
        adapter, hub = self._adapter_with_hub({"changed": True, "detail": "written"})
        await adapter.write_config("web1", "/etc/nginx/app.conf", "x")
        _, _, params = hub.send_action.call_args.args
        assert params["mode"] == "0644"

    async def test_provision_no_agent(self):
        hub = MagicMock()
        hub.registry.get_by_hostname.return_value = None
        adapter = AgentAdapter(hub_server=hub)
        with pytest.raises(AgentAdapterError, match="no agent connected"):
            await adapter.install_package("unknown", "nginx")

    async def test_provision_pve_node_blocked(self):
        adapter = AgentAdapter(pve_nodes=["pve1"])
        with pytest.raises(GuestHostError, match="PVE node"):
            await adapter.manage_service("pve1", "nginx", "started")


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
        adapter = AgentAdapter(hub_server=hub)
        assert await adapter.test_connection("unknown") is False
