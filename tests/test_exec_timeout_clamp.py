"""The hub never asks an agent for an unbounded exec (#720).

The exec timeout travelled from the API body / MCP arguments to the agent
unchanged, so one request could hold an agent worker for as long as the caller
liked (and before the agent's worker pool, wedge the agent outright). The hub now
clamps what it sends, and the API refuses an out-of-range value outright rather
than silently shortening it.

Teeth: drop the clamp in ``AgentHubServer.send_command`` and
``test_the_frame_carries_a_clamped_timeout`` fails with the raw value on the
wire; drop the Field bounds and the model tests fail.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import struct

import pytest
from pydantic import ValidationError

from homepilot.agent_hub.router import ExecRequest, HostExecRequest
from homepilot.agent_hub.server import (
    HEADER_LEN,
    MAX_EXEC_TIMEOUT_S,
    AgentHubServer,
    _encode,
)


async def _recv(reader: asyncio.StreamReader) -> dict:
    hdr = await asyncio.wait_for(reader.readexactly(HEADER_LEN), timeout=5)
    (length,) = struct.unpack("!I", hdr)
    return dict(json.loads(await asyncio.wait_for(reader.readexactly(length), timeout=5)))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("asked", "sent"),
    [(10**6, MAX_EXEC_TIMEOUT_S), (MAX_EXEC_TIMEOUT_S + 1, MAX_EXEC_TIMEOUT_S), (0, 1), (-7, 1)],
)
async def test_the_frame_carries_a_clamped_timeout(asked: int, sent: int) -> None:
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
                    "agent_id": "a-clamp",
                    "hostname": "clamp01",
                    "request_id": "reg-1",
                }
            )
        )
        await writer.drain()
        assert (await _recv(reader)).get("action") == "register_ack"

        call = asyncio.create_task(srv.send_command("a-clamp", "hostname", timeout=asked))
        frame = await _recv(reader)
        assert frame["action"] == "exec"
        assert frame["timeout"] == sent, frame
        writer.write(
            _encode(
                {
                    "action": "command_result",
                    "request_id": frame["request_id"],
                    "exit_code": 0,
                    "stdout": "clamp01\n",
                    "stderr": "",
                }
            )
        )
        await writer.drain()
        result = await asyncio.wait_for(call, timeout=10)
        assert result.get("exit_code") == 0
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(writer.wait_closed(), timeout=5)
        await srv.stop()


@pytest.mark.parametrize("model", [HostExecRequest, ExecRequest])
def test_the_api_refuses_an_out_of_range_timeout(model: type) -> None:
    fields = {"host": "h", "command": "hostname"} if model is HostExecRequest else {}
    fields["command"] = "hostname"
    for bad in (0, -1, MAX_EXEC_TIMEOUT_S + 1, 10**6):
        with pytest.raises(ValidationError):
            model(**fields, timeout=bad)
    assert model(**fields, timeout=MAX_EXEC_TIMEOUT_S).timeout == MAX_EXEC_TIMEOUT_S
    assert model(**fields).timeout == 30
