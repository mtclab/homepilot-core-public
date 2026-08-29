"""What the agent-hub audit trail says happened (#648, defect class #642).

Two things it got wrong, both found by reading the live trail on dev:

* every REFUSED command was recorded as ``result: success``. A blocked exec is
  not an error frame - it comes back as an ordinary result with ``exit_code -1``
  and a ``command blocked:`` stderr - so the only test the trail applied ("was
  there an error key?") said yes-it-worked to `id`, to `sudo`, to every attempt
  at something the allowlist forbids. ``ResultType`` has had a ``blocked`` value
  the whole time; nothing ever produced it for an exec.
* every operation issued over MCP was recorded as ``caller: unknown``. #381
  persisted the trail "with caller attribution", but only the REST endpoints set
  the contextvar the log reads, so the interface this product is built around
  left no attribution at all. The trail could not answer "who ran this on my
  host".
"""

from __future__ import annotations

from typing import Any

import pytest
from mcp import types

from homepilot.agent_hub.audit import get_audit_caller, set_audit_caller
from homepilot.agent_hub.server import AgentCommandError, AgentHubServer


@pytest.fixture
def hub() -> AgentHubServer:
    srv = AgentHubServer(auth_token="t")
    srv.registry.register(agent_id="A", hostname="web01")
    return srv


def _last(hub: AgentHubServer) -> dict[str, Any]:
    return hub.registry.audit_log.query()[-1]


class TestARefusalIsNotASuccess:
    def test_a_blocked_command_is_recorded_as_blocked(self, hub: AgentHubServer):
        hub._finalize_result(
            "A",
            "exec",
            "id",
            {
                "exit_code": -1,
                "stdout": "",
                "stderr": "command blocked: privileged command not in allowlist: id",
            },
        )
        entry = _last(hub)
        assert entry["result"] == "blocked", "a command the agent refused is not a success"
        assert entry["command_or_path"] == "id"

    def test_a_normal_command_is_still_a_success(self, hub: AgentHubServer):
        hub._finalize_result(
            "A", "exec", "hostname", {"exit_code": 0, "stdout": "web01\n", "stderr": ""}
        )
        assert _last(hub)["result"] == "success"

    def test_a_nonzero_exit_from_a_real_command_is_still_a_success(self, hub: AgentHubServer):
        """The operation happened; the exit code carries the outcome. Only a
        refusal - where nothing ran at all - is `blocked`."""
        hub._finalize_result(
            "A",
            "exec",
            "systemctl is-active cron",
            {"exit_code": 3, "stdout": "inactive\n", "stderr": ""},
        )
        assert _last(hub)["result"] == "success"

    def test_an_error_frame_is_still_an_error(self, hub: AgentHubServer):
        with pytest.raises(AgentCommandError):
            hub._finalize_result("A", "read_file", "/etc/shadow", {"error": "access forbidden"})
        assert _last(hub)["result"] == "error"


class TestMcpCallsAreAttributed:
    async def test_an_mcp_call_names_its_credential_in_the_trail(self):
        from homepilot.mcp import server as mcp_mod

        set_audit_caller(None)
        token = mcp_mod._mcp_caller_id_var.set("mcp-api:abc12345")
        try:
            # Any tool will do: the attribution is set before dispatch, so a tool
            # that refuses on its arguments still proves the contextvar was set.
            await mcp_mod._on_call_tool(
                None,
                types.CallToolRequestParams(
                    name="approve_artifact", arguments={"artifact_id": "x"}
                ),
            )
            assert get_audit_caller() == "mcp-api:abc12345", (
                "an operation issued over MCP must be attributable in the fleet-root audit trail"
            )
        finally:
            mcp_mod._mcp_caller_id_var.reset(token)
            set_audit_caller(None)
