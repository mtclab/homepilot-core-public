"""The agent tools must SAY what happened (#648).

Only `ValueError` was translated into a tool error, so every domain answer the
agent surface produces - `AgentAdapterError` ("no agent connected for web01")
and `AgentCommandError` ("path not in allowed read prefixes", "file is 30395000
bytes; the agent hub accepts at most 524288 per reply") - fell through to the
transport's generic handler and reached the operator as `Internal server error`.

Found live on dev 2026-08-29: reading a large file, and reading anything from a
host with no agent, both answered `Internal server error`. The product knew
exactly what had happened in both cases.
"""

from __future__ import annotations

from typing import Any

import pytest
from mcp import types

from homepilot.adapters.agent import AgentAdapter, AgentAdapterError
from homepilot.agent_hub.server import AgentCommandError
from homepilot.mcp.server import _on_call_tool, _server_context


class _RaisingHub:
    """A hub whose registry knows the host but whose calls fail the way a real
    one does."""

    def __init__(self, exc: BaseException):
        self._exc = exc

        class _Agent:
            agent_id = "agent-1"

        class _Registry:
            def get_by_hostname(self, _host: str) -> Any:
                return _Agent()

        self.registry = _Registry()

    async def send_command(self, *_a: Any, **_k: Any) -> dict[str, Any]:
        raise self._exc

    async def send_read_file(self, *_a: Any, **_k: Any) -> dict[str, Any]:
        raise self._exc


async def _call(tool: str, arguments: dict[str, Any], adapter: AgentAdapter) -> Any:
    await _server_context.async_update({"agent_adapter": adapter})
    try:
        return await _on_call_tool(
            None, types.CallToolRequestParams(name=tool, arguments=arguments)
        )
    finally:
        await _server_context.async_update({"agent_adapter": None})


class TestAgentToolErrorsAreAnswers:
    @pytest.mark.parametrize(
        ("tool", "arguments"),
        [
            ("exec_on_guest_readonly", {"host": "web01", "command": "hostname"}),
            ("read_file_on_guest", {"host": "web01", "path": "/etc/hostname"}),
        ],
    )
    async def test_no_agent_connected_is_reported_verbatim(
        self, tool: str, arguments: dict[str, Any]
    ):
        result = await _call(tool, arguments, AgentAdapter(hub_server=None))

        assert result.is_error is True
        assert "no agent connected for web01" in result.content[0].text

    async def test_a_refusal_from_the_agent_reaches_the_caller(self):
        """AgentCommandError carries the agent's own sentence - the size refusal,
        the allowlist refusal, "file not found"."""
        refusal = AgentCommandError(
            "file is 30395000 bytes; the agent hub accepts at most 524288 per reply"
        )
        adapter = AgentAdapter(hub_server=_RaisingHub(refusal))

        result = await _call(
            "read_file_on_guest", {"host": "web01", "path": "/var/log/syslog"}, adapter
        )

        assert result.is_error is True
        assert "accepts at most" in result.content[0].text

    async def test_a_transport_failure_names_the_host(self):
        adapter = AgentAdapter(hub_server=_RaisingHub(ConnectionError("agent gone: hub hung up")))

        result = await _call(
            "exec_on_guest_readonly", {"host": "web01", "command": "hostname"}, adapter
        )

        assert result.is_error is True
        text = result.content[0].text
        assert "web01" in text
        assert "hub hung up" in text
        assert isinstance(AgentAdapterError("x"), Exception)
