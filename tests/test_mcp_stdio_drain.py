"""Tests for MCP stdio transport drain fix — issue #76.

When stdin closes (read stream EOF), srv.run() returns. Any in-flight async
tool responses must be allowed to flush to stdout before stdio_server() tears
down the write stream. The fix adds a finally-block drain inside the
stdio_server context so pending writes complete before the stream closes.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.integration


@asynccontextmanager
async def _fake_stdio():
    yield AsyncMock(), AsyncMock()


def _make_srv(side_effect=None) -> MagicMock:
    srv = MagicMock()
    srv.create_initialization_options.return_value = {}
    srv.run = (
        AsyncMock(return_value=None) if side_effect is None else AsyncMock(side_effect=side_effect)
    )
    return srv


class TestStdioServerDrain:
    """run_server() keeps the write stream alive briefly after srv.run() returns."""

    async def test_run_server_completes_normally(self):
        srv = _make_srv()
        with (
            patch("homepilot.mcp.server.stdio_server", _fake_stdio),
            patch("homepilot.mcp.server._bootstrap", AsyncMock(return_value={})),
            patch("homepilot.mcp.server.create_server", return_value=srv),
            patch("asyncio.sleep", AsyncMock()),
        ):
            import homepilot.mcp.server as mcp_server

            await mcp_server.run_server()

        srv.run.assert_awaited_once()

    async def test_drain_sleep_called_after_srv_run(self):
        """asyncio.sleep(0.5) executes inside the stdio_server context after srv.run()."""
        sleep_calls: list[float] = []

        async def tracking_sleep(delay: float) -> None:
            sleep_calls.append(delay)

        srv = _make_srv()
        with (
            patch("homepilot.mcp.server.stdio_server", _fake_stdio),
            patch("homepilot.mcp.server._bootstrap", AsyncMock(return_value={})),
            patch("homepilot.mcp.server.create_server", return_value=srv),
            patch("asyncio.sleep", tracking_sleep),
        ):
            import homepilot.mcp.server as mcp_server

            await mcp_server.run_server()

        assert 0.5 in sleep_calls, f"Expected 0.5s drain sleep, got: {sleep_calls}"

    async def test_drain_swallows_cancelled_error(self):
        """CancelledError from the drain is suppressed — run_server() still returns normally."""

        async def cancelled_sleep(delay: float) -> None:
            raise asyncio.CancelledError()

        srv = _make_srv()
        with (
            patch("homepilot.mcp.server.stdio_server", _fake_stdio),
            patch("homepilot.mcp.server._bootstrap", AsyncMock(return_value={})),
            patch("homepilot.mcp.server.create_server", return_value=srv),
            patch("asyncio.sleep", cancelled_sleep),
        ):
            import homepilot.mcp.server as mcp_server

            # Must NOT raise — drain exceptions are suppressed
            await mcp_server.run_server()

    async def test_drain_called_when_srv_run_raises(self):
        """finally drain executes even when srv.run() raises."""
        sleep_calls: list[float] = []

        async def tracking_sleep(delay: float) -> None:
            sleep_calls.append(delay)

        srv = _make_srv(side_effect=RuntimeError("transport closed"))
        with (
            patch("homepilot.mcp.server.stdio_server", _fake_stdio),
            patch("homepilot.mcp.server._bootstrap", AsyncMock(return_value={})),
            patch("homepilot.mcp.server.create_server", return_value=srv),
            patch("asyncio.sleep", tracking_sleep),
        ):
            import homepilot.mcp.server as mcp_server

            with pytest.raises(RuntimeError, match="transport closed"):
                await mcp_server.run_server()

        assert 0.5 in sleep_calls, "Drain must execute even when srv.run() raises"

    async def test_resources_closed_after_drain(self):
        """Database is closed after run_server() drains and exits."""
        fake_db = AsyncMock()
        fake_db.close = AsyncMock()

        srv = _make_srv()
        with (
            patch("homepilot.mcp.server.stdio_server", _fake_stdio),
            patch("homepilot.mcp.server._bootstrap", AsyncMock(return_value={"database": fake_db})),
            patch("homepilot.mcp.server.create_server", return_value=srv),
            patch("asyncio.sleep", AsyncMock()),
        ):
            import homepilot.mcp.server as mcp_server

            await mcp_server.run_server()

        fake_db.close.assert_awaited_once()
