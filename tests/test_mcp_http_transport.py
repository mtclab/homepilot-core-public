"""Regression gates for the MCP HTTP transport + authorization (#382, #385).

These exercise the SHIPPED path: the real ``homepilot.main:app`` mounts the MCP
app and must drive its lifespan, otherwise the StreamableHTTPSessionManager never
starts and every ``POST /mcp/`` 500s with "Task group is not initialized" while
``/health`` still lies "ok". They also lock down the authorization/wiring gaps:
``approve_artifact`` must be unreachable over MCP, the HTTP tool context must carry
every key the tools reference, and the HTTP bind must refuse to start without a
token.

Revert-verification for each gate is recorded in the builder report.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import re
import shutil
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from unittest.mock import MagicMock, patch

import httpx
import pytest


class _LifespanManager:
    """Drive an ASGI app's lifespan protocol in the CURRENT (main) thread.

    Starlette's TestClient runs the lifespan in a worker thread, where
    ``loop.add_signal_handler`` (used by main.lifespan) raises. Driving the
    lifespan here keeps it on the pytest-asyncio main-thread loop.
    """

    def __init__(self, app: object) -> None:
        self.app = app
        self._up = asyncio.Event()
        self._down = asyncio.Event()
        self._queue: asyncio.Queue[dict] = asyncio.Queue()
        self._error: str | None = None
        self._task: asyncio.Task | None = None

    async def _send(self, message: dict) -> None:
        t = message["type"]
        if t == "lifespan.startup.complete":
            self._up.set()
        elif t == "lifespan.startup.failed":
            self._error = message.get("message", "startup failed")
            self._up.set()
        elif t == "lifespan.shutdown.complete":
            self._down.set()

    async def _receive(self) -> dict:
        return await self._queue.get()

    async def __aenter__(self) -> _LifespanManager:
        self._task = asyncio.create_task(self.app({"type": "lifespan"}, self._receive, self._send))
        await self._queue.put({"type": "lifespan.startup"})
        await self._up.wait()
        if self._error is not None:
            raise RuntimeError(f"lifespan startup failed: {self._error}")
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._queue.put({"type": "lifespan.shutdown"})
        await self._down.wait()
        if self._task is not None:
            await self._task


@asynccontextmanager
async def _booted_app(token: str) -> AsyncIterator[tuple[object, httpx.AsyncClient]]:
    """Boot the real ``homepilot.main:app`` through its full lifespan with a
    temp data dir under $HOME (the artifacts guard forbids /tmp), yielding the app
    and an ASGI httpx client. Boots exactly once per call — do NOT boot the shared
    ``main.app`` more than once per process (each boot appends another /mcp mount)."""
    data_dir = tempfile.mkdtemp(dir=os.path.expanduser("~"), prefix=".hp-test-mcp-")
    env = {
        "HP_DATA_DIR": data_dir,
        "HP_ARTIFACTS_DIR": os.path.join(data_dir, "artifacts"),
        "HP_MCP_TOKEN": token,
        "HP_SECRET_KEY": "test-secret-key-for-pytest-only-not-for-production",
    }
    from homepilot.config import get_settings

    with patch.dict(os.environ, env, clear=False):
        get_settings.cache_clear()
        from homepilot import main as main_mod

        try:
            async with _LifespanManager(main_mod.app):
                transport = httpx.ASGITransport(app=main_mod.app)
                async with httpx.AsyncClient(
                    transport=transport, base_url="http://testserver"
                ) as client:
                    yield main_mod.app, client
        finally:
            get_settings.cache_clear()
            shutil.rmtree(data_dir, ignore_errors=True)


_INITIALIZE_BODY = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "hp-test", "version": "1.0"},
    },
}


def _tool_context_keys() -> set[str]:
    """Every ``ctx[...]`` / ``ctx.get(...)`` key the MCP tool handlers reference,
    scanned from source so this gate stays live as tools are added."""
    from homepilot.mcp.tools import (
        artifact_tools,
        inventory_tools,
        kb_tools,
        system_tools,
    )

    pattern = re.compile(r'ctx(?:\[|\.get\()\s*["\']([a-zA-Z_]+)["\']')
    keys: set[str] = set()
    for module in (system_tools, artifact_tools, inventory_tools, kb_tools):
        keys |= set(pattern.findall(inspect.getsource(module)))
    # Per-request contextvars are injected at call time, not part of the app-built
    # server context, so they are not required to be pre-populated.
    return {k for k in keys if not k.startswith("_mcp")}


class TestHttpTransportLive:
    """Fix 1 (#382) + Fix 2b (#385): the mounted transport must actually run, the
    health check must tell the truth, and the tool context must be complete."""

    @pytest.mark.integration
    async def test_initialize_health_and_context_on_shipped_path(self):
        token = "live-mcp-token-382"
        async with _booted_app(token) as (_app, client):
            # --- Fix 1: initialize must NOT 500 ("Task group is not initialized").
            resp = await client.post(
                "/mcp/",
                json=_INITIALIZE_BODY,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json, text/event-stream",
                },
            )
            assert resp.status_code != 500, (
                f"MCP initialize 500'd — transport dead (#382): {resp.text[:300]}"
            )
            assert resp.status_code == 200, resp.text[:300]
            # A real MCP initialize result (SSE or JSON) carries these fields.
            assert "protocolVersion" in resp.text
            assert "serverInfo" in resp.text

            # --- Fix 1 health: /health must report mcp "ok" only because the
            # session manager is truly running (not merely because a route exists).
            health = await client.get("/health")
            assert health.json()["checks"]["mcp"] == "ok"

            # --- Fix 2b: the app-built HTTP context carries every key the tools read
            # (regression: agent_adapter + drift_reconciler were missing, so
            # read_file_on_guest / exec_on_guest_readonly / check_artifact_drift
            # always errored over HTTP).
            from homepilot.mcp.server import _server_context

            ctx = await _server_context.snapshot()
            required = _tool_context_keys()
            missing = required - set(ctx.keys())
            assert not missing, f"MCP tool context missing keys: {sorted(missing)}"
            # Explicitly pin the two that regressed.
            assert "agent_adapter" in ctx
            assert "drift_reconciler" in ctx


class TestHealthMcpTruthful:
    """Fix 1 health teeth, independent of the lifespan fix: a mounted-but-not-running
    session manager must read "error", never "ok". Uses a throwaway app so the
    shared ``main.app`` route table is never polluted."""

    def test_error_when_route_mounted_but_session_manager_not_running(self):
        from fastapi import FastAPI

        from homepilot.main import _mcp_health_status
        from homepilot.mcp import server as mcp_mod

        with patch.dict(os.environ, {"HP_MCP_TOKEN": "tok"}, clear=False):
            mock_srv = MagicMock()
            mock_srv.create_initialization_options = MagicMock(return_value={})
            # Built + MOUNTED (route exists) but its lifespan never driven, so
            # session_manager._task_group is None. The old route-existence check
            # returned "ok" here — that false positive is exactly the bug (#382).
            undriven = mcp_mod.create_http_app(mock_srv)

        tapp = FastAPI()
        tapp.mount("/mcp", undriven)
        tapp.state.mcp_app = undriven
        assert _mcp_health_status(tapp) == "error"

    def test_ok_when_session_manager_running(self):
        from types import SimpleNamespace

        from fastapi import FastAPI

        from homepilot.main import _mcp_health_status

        tapp = FastAPI()
        running = SimpleNamespace(_task_group=object())
        tapp.state.mcp_app = SimpleNamespace(state=SimpleNamespace(session_manager=running))
        assert _mcp_health_status(tapp) == "ok"

    def test_error_when_no_mcp_app(self):
        from fastapi import FastAPI

        from homepilot.main import _mcp_health_status

        assert _mcp_health_status(FastAPI()) == "error"


class TestApproveForbiddenOverMcp:
    """Fix 2a (#385): approve_artifact must be unreachable over the MCP transport."""

    async def test_approve_not_advertised_and_call_refused(self):
        from mcp import types

        from homepilot.mcp.server import _on_call_tool, _on_list_tools

        # mcp 2.x: tools are registered via the on_list_tools/on_call_tool
        # constructor handlers (the 1.x request_handlers dict is gone). Drive the
        # handlers directly.
        listed = await _on_list_tools(None, None)
        names = {t.name for t in listed.tools}
        assert "approve_artifact" not in names, "approve_artifact must not be advertised over MCP"
        # Sanity: the other mutating tools stay available (propose/record must work).
        assert "propose_artifact" in names

        result = await _on_call_tool(
            None,
            types.CallToolRequestParams(name="approve_artifact", arguments={"artifact_id": "x"}),
        )
        assert result.is_error is True
        assert "not available over the MCP transport" in result.content[0].text


class TestHttpBindStartupGuard:
    """Fix 2c (#385): the HTTP bind must hard-fail without HP_MCP_TOKEN."""

    async def test_run_server_http_refuses_without_token(self):
        from homepilot.mcp.server import run_server_http

        with (
            patch.dict(os.environ, {"HP_MCP_TOKEN": ""}, clear=False),
            pytest.raises(RuntimeError, match="without HP_MCP_TOKEN"),
        ):
            await run_server_http(host="127.0.0.1", port=0)
