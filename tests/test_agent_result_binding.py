"""A command result must answer the command that was ISSUED (#381).

``AgentRegistry.store_command_result`` matched a reply on ``request_id`` alone.
Nothing checked that the reply answered the action the hub sent under that id, so
the hub was a confused deputy: whatever arrived first with that id resolved the
future, and the caller read the payload as the result of *its* command. A reply
to a cheap ``read_file`` could satisfy the future a ``write_config`` was waiting
on, and the caller would report the write as done.

The binding is the issued action, recorded with the pending future and echoed
back by the agent as ``for_action``. A mismatch is dropped and audit-logged; the
connection is deliberately left up, and the caller's own timeout fires - "no
result arrived" is the truthful outcome.

Teeth: delete the ``verify_action`` branch from ``store_command_result`` and both
``test_a_reply_for_a_different_action_is_rejected`` and the end-to-end
``test_send_command_times_out_on_a_forged_reply`` fail with "DID NOT RAISE
<class 'TimeoutError'>" - the forged payload resolves the caller's future and is
returned as the result of a command it never answered.
"""

from __future__ import annotations

import asyncio

import pytest

from homepilot.agent_hub.registry import AgentRegistry

pytestmark = pytest.mark.asyncio

AGENT = "agent-under-test"


@pytest.fixture
def registry() -> AgentRegistry:
    reg = AgentRegistry()
    reg.register(agent_id=AGENT, hostname="host01")
    return reg


async def _issue(reg: AgentRegistry, request_id: str, action: str) -> asyncio.Task:
    """Start a caller waiting on ``request_id``, issued as ``action``."""
    task = asyncio.create_task(reg.wait_for_result(AGENT, request_id, action))
    await asyncio.sleep(0)  # let it register the future
    return task


class TestTheReplyMustAnswerTheIssuedAction:
    async def test_a_matching_reply_resolves_the_caller(self, registry: AgentRegistry):
        task = await _issue(registry, "req-1", "write_config")
        registry.store_command_result(
            AGENT,
            {"request_id": "req-1", "for_action": "write_config", "status": "ok"},
        )
        result = await asyncio.wait_for(task, timeout=1)
        assert result["status"] == "ok"

    async def test_a_reply_for_a_different_action_is_rejected(self, registry: AgentRegistry):
        """The confused-deputy vector: right id, wrong action."""
        task = await _issue(registry, "req-2", "write_config")

        forged = {
            "request_id": "req-2",
            "for_action": "read_file",
            "status": "ok",
            "changed": True,
            "detail": "config written",
        }
        registry.store_command_result(AGENT, forged)

        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(task), timeout=0.2)
        assert not task.done(), "the forged reply resolved the caller's future"
        task.cancel()

    async def test_the_drop_is_audit_logged(self, registry: AgentRegistry):
        task = await _issue(registry, "req-3", "exec")
        registry.store_command_result(
            AGENT, {"request_id": "req-3", "for_action": "read_file", "content": "x"}
        )
        blocked = [e for e in registry.audit_log.query() if e["result"] == "blocked"]
        assert blocked, "a dropped result must leave a trail"
        assert blocked[-1]["agent_id"] == AGENT
        assert blocked[-1]["action"] == "exec"
        task.cancel()

    async def test_the_connection_is_not_torn_down_by_a_mismatch(self, registry: AgentRegistry):
        """A bad frame drops the frame, not the channel: the next legitimate
        request on the same agent still works."""
        bad = await _issue(registry, "req-4", "exec")
        registry.store_command_result(AGENT, {"request_id": "req-4", "for_action": "write_file"})
        bad.cancel()

        good = await _issue(registry, "req-5", "exec")
        registry.store_command_result(
            AGENT, {"request_id": "req-5", "for_action": "exec", "exit_code": 0}
        )
        assert (await asyncio.wait_for(good, timeout=1))["exit_code"] == 0


class TestCompatibilityWithAgentsThatDoNotEcho:
    async def test_a_legacy_agent_without_for_action_is_still_served(self, registry: AgentRegistry):
        """An agent that never declared ``result_action`` in its register frame
        predates the echo. Refusing its replies would strand a fleet the moment
        the hub was upgraded ahead of it."""
        assert registry.get(AGENT).echoes_result_action is False
        task = await _issue(registry, "req-6", "exec")
        registry.store_command_result(AGENT, {"request_id": "req-6", "exit_code": 0})
        assert (await asyncio.wait_for(task, timeout=1))["exit_code"] == 0

    async def test_an_agent_that_declared_the_echo_is_held_to_it(self, registry: AgentRegistry):
        """Fail-closed for current agents: having declared it, omitting the echo
        is not a way back out of the check."""
        registry.get(AGENT).echoes_result_action = True
        task = await _issue(registry, "req-7", "exec")
        registry.store_command_result(AGENT, {"request_id": "req-7", "exit_code": 0})
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(task), timeout=0.2)
        task.cancel()

    async def test_a_hub_synthesised_error_still_reaches_the_caller(self, registry: AgentRegistry):
        """The oversize-frame path builds the error itself, so it carries no echo
        and must bypass the check - otherwise that caller times out instead of
        being told what went wrong."""
        registry.get(AGENT).echoes_result_action = True
        task = await _issue(registry, "req-8", "exec")
        registry.store_command_result(
            AGENT,
            {"request_id": "req-8", "error": "response too large"},
            verify_action=False,
        )
        assert (await asyncio.wait_for(task, timeout=1))["error"] == "response too large"


class TestTheWholeHubPathRefusesAForgedResult:
    """End-to-end over a real socket: the caller of ``send_command`` must not be
    handed a result that was produced for a different action."""

    async def test_send_command_times_out_on_a_forged_reply(self):
        import contextlib
        import json
        import struct

        from homepilot.agent_hub.server import HEADER_LEN, AgentHubServer, _encode

        async def recv(reader):
            hdr = await reader.readexactly(HEADER_LEN)
            (length,) = struct.unpack("!I", hdr)
            return json.loads(await reader.readexactly(length))

        srv = AgentHubServer(host="127.0.0.1", port=0, auth_token="shared-secret")
        await srv.start()
        assert srv._server is not None
        port = srv._server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            writer.write(
                _encode(
                    {
                        "action": "register",
                        "auth_token": "shared-secret",
                        "agent_id": "a-forger",
                        "hostname": "web01",
                        "request_id": "reg-1",
                        # This agent promises to echo the issued action back.
                        "result_action": 1,
                    }
                )
            )
            await writer.drain()
            await recv(reader)  # register_ack

            async def forge():
                frame = await recv(reader)
                assert frame["action"] == "exec"
                # Right request_id, wrong action: a read_file result dressed up
                # as the answer to the exec the hub is waiting on.
                writer.write(
                    _encode(
                        {
                            "action": "command_result",
                            "for_action": "read_file",
                            "request_id": frame["request_id"],
                            "exit_code": 0,
                            "stdout": "attacker-chosen output",
                            "stderr": "",
                        }
                    )
                )
                await writer.drain()

            forger = asyncio.create_task(forge())
            with pytest.raises(asyncio.TimeoutError):
                await srv.send_command("a-forger", "hostname", timeout=1)
            await forger
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(writer.wait_closed(), timeout=5)
            await srv.stop()
