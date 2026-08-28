"""One name for "which machine" across the MCP tool surface (#608).

The surface had grown two names for the same argument: five tools took `host`,
four took `hostname`. An assistant that had just run `exec_on_host(host=...)`
called `get_host_metrics(host=...)` next and got a KeyError - a wasted turn,
every turn, and nothing in the schemas said which tool wanted which.

`host` won on the count (5 v 4) and the four moved; `hostname` stays accepted
as a deprecated alias so callers written against the old surface keep working.

Two gates:

* **The registry gate** - `TestEveryHostAddressedToolTakesHost` walks the REAL
  tool registry, not a list written here, and demands that every tool naming a
  machine expose `host`, never require `hostname`, and mark the alias
  deprecated where it offers one. Teeth: rename any of these parameters back to
  a bare `hostname` and it fails naming the tool.
* **The alias gate** - `TestTheDeprecatedAliasStillWorks` drives each tool
  through `_handle_tool` TWICE, once per name, against the same fakes, and
  asserts both calls reach the same machine. Teeth: drop the alias out of
  `host_arg` and every old-name call fails.
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.db.repository import Repository
from homepilot.mcp.server import _TOOL_DEFINITIONS, _handle_tool
from homepilot.mcp.tools.host_param import (
    DEPRECATED_HOST_PARAM,
    HOST_PARAM,
)
from homepilot.metrics.repository import MetricsRepository

pytestmark = pytest.mark.asyncio

HOSTNAME = "guest-01"
AGENT_ID = "agent-1"


def _host_addressed_tools() -> list[dict[str, Any]]:
    """Every tool that names a machine, read off the REAL registry.

    "Names a machine" is deliberately mechanical - the property is called `host`
    or `hostname` - so a tool cannot slip past the gate by being described
    differently. Anything addressing a machine by an id (`host_id`, `agent_id`)
    is a different kind of argument and is not in scope.
    """
    found = []
    for tool in _TOOL_DEFINITIONS:
        props = (tool.get("inputSchema") or {}).get("properties") or {}
        if HOST_PARAM in props or DEPRECATED_HOST_PARAM in props:
            found.append(tool)
    return found


class TestEveryHostAddressedToolTakesHost:
    async def test_the_walk_actually_finds_the_surface(self) -> None:
        """Guard the guard: a gate that iterates an empty set proves nothing."""
        names = {t["name"] for t in _host_addressed_tools()}
        # The nine that existed when #608 was fixed - five `host`, four `hostname`.
        assert {
            "read_file_on_guest",
            "exec_on_guest_readonly",
            "check_host_reachable",
            "exec_on_host",
            "write_file_on_host",
            "add_host",
            "get_agent",
            "get_host_metrics",
            "get_host_metrics_series",
        } <= names

    @pytest.mark.parametrize("tool", _host_addressed_tools(), ids=lambda t: str(t["name"]))
    async def test_it_exposes_the_standard_name(self, tool: dict[str, Any]) -> None:
        props = (tool.get("inputSchema") or {}).get("properties") or {}
        assert HOST_PARAM in props, (
            f"{tool['name']} names a machine with `{DEPRECATED_HOST_PARAM}` and no "
            f"`{HOST_PARAM}`; #608 standardised on `{HOST_PARAM}` across the surface"
        )

    @pytest.mark.parametrize("tool", _host_addressed_tools(), ids=lambda t: str(t["name"]))
    async def test_the_old_name_is_never_required(self, tool: dict[str, Any]) -> None:
        """The alias is a leniency, not a second way to be correct: a schema that
        REQUIRES `hostname` tells every new caller to use the deprecated name."""
        required = (tool.get("inputSchema") or {}).get("required") or []
        assert DEPRECATED_HOST_PARAM not in required, (
            f"{tool['name']} requires the deprecated `{DEPRECATED_HOST_PARAM}`"
        )
        props = (tool.get("inputSchema") or {}).get("properties") or {}
        if DEPRECATED_HOST_PARAM in props and required and HOST_PARAM not in required:
            pytest.fail(
                f"{tool['name']} has required arguments but `{HOST_PARAM}` is not one "
                "of them, so nothing tells a caller which name is the standard"
            )

    @pytest.mark.parametrize("tool", _host_addressed_tools(), ids=lambda t: str(t["name"]))
    async def test_the_alias_says_it_is_deprecated(self, tool: dict[str, Any]) -> None:
        """An accepted-but-undocumented alias is how a surface grows two names
        again: a caller reading the schema must be told which one to use."""
        props = (tool.get("inputSchema") or {}).get("properties") or {}
        alias = props.get(DEPRECATED_HOST_PARAM)
        if alias is None:
            return
        description = str(alias.get("description") or "").lower()
        assert "deprecated" in description and f"`{HOST_PARAM}`" in description, (
            f"{tool['name']} accepts `{DEPRECATED_HOST_PARAM}` without saying in the "
            f"schema that it is deprecated in favour of `{HOST_PARAM}`"
        )


class _FakeHub:
    def __init__(self) -> None:
        self.registry = MagicMock()
        agent = SimpleNamespace(agent_id=AGENT_ID, hostname=HOSTNAME)
        self.registry.get_by_hostname = MagicMock(
            side_effect=lambda h: agent if h == HOSTNAME else None
        )
        self.send_command = AsyncMock(return_value={"exit_code": 0, "stdout": "ok", "stderr": ""})
        self.send_write_file = AsyncMock(return_value={"written": True})
        self.send_read_file = AsyncMock(return_value={"content": "hello"})


def _adapter() -> Any:
    from homepilot.adapters.agent import AgentAdapter

    return AgentAdapter(hub_server=_FakeHub(), pve_nodes=["pve1"])


class _FakeAgentRegistry:
    def __init__(self) -> None:
        self.agent = SimpleNamespace(
            agent_id=AGENT_ID,
            hostname=HOSTNAME,
            system_info={"agent_version": "1.2.3"},
            state={},
            connected_at=SimpleNamespace(isoformat=lambda: "2026-01-01T00:00:00"),
            last_heartbeat=SimpleNamespace(isoformat=lambda: "2026-01-01T00:01:00"),
        )
        self.asked: list[str] = []

    def get_by_hostname(self, name: str) -> Any:
        self.asked.append(name)
        return self.agent if name == HOSTNAME else None

    def get(self, agent_id: str) -> Any:
        return self.agent if agent_id == AGENT_ID else None


@pytest.fixture
async def estate(tmp_path: Path):
    db = Database(str(tmp_path / "host-param.db"))
    await db.connect()
    await run_migrations(db)
    repo = Repository(db)
    metrics = MetricsRepository(db)
    await metrics.insert_samples(HOSTNAME, AGENT_ID, [("cpu.percent", int(time.time()) - 5, 12.5)])
    try:
        yield SimpleNamespace(db=db, repo=repo, metrics=metrics)
    finally:
        await db.close()


@pytest.fixture
def ctx(estate):
    return {
        "repo": estate.repo,
        "database": estate.db,
        "metrics_repo": estate.metrics,
        "_mcp_token_scope": "admin",
        "_mcp_caller_id": "host-param-tester",
    }


def _reached(result: Any) -> str:
    """The machine a result says it reached, whichever key it uses."""
    if isinstance(result, dict):
        for key in ("host", "hostname"):
            if key in result:
                return str(result[key])
    raise AssertionError(f"no host in {result!r}")


class TestTheDeprecatedAliasStillWorks:
    """The journey, per tool: the SAME machine is reached under either name.

    Asserted on where the call landed - the agent the adapter resolved, the row
    the repository wrote, the metrics that came back - never on "it returned
    without raising".
    """

    async def _both_ways(
        self, tool: str, extra: dict[str, Any], ctx_extra: dict[str, Any], ctx: dict[str, Any]
    ) -> tuple[Any, Any]:
        modern = await _handle_tool(tool, {HOST_PARAM: HOSTNAME, **extra}, {**ctx, **ctx_extra})
        legacy = await _handle_tool(
            tool, {DEPRECATED_HOST_PARAM: HOSTNAME, **extra}, {**ctx, **ctx_extra}
        )
        return modern, legacy

    async def test_exec_on_host(self, ctx) -> None:
        adapter = _adapter()
        modern, legacy = await self._both_ways(
            "exec_on_host", {"command": "uptime"}, {"agent_adapter": adapter}, ctx
        )
        assert modern["stdout"] == legacy["stdout"] == "ok"
        # Both calls resolved the SAME agent, so both addressed the same machine.
        assert adapter._hub.send_command.await_count == 2
        assert all(call.args[0] == AGENT_ID for call in adapter._hub.send_command.await_args_list)
        assert legacy["warning"] and "deprecated" in legacy["warning"].lower()
        assert "warning" not in modern

    async def test_write_file_on_host(self, ctx) -> None:
        adapter = _adapter()
        modern, legacy = await self._both_ways(
            "write_file_on_host",
            {"path": "/etc/homepilot/agent.env", "content": "X=1"},
            {"agent_adapter": adapter},
            ctx,
        )
        assert modern["changed"] is True and legacy["changed"] is True
        assert adapter._hub.send_write_file.await_count == 2
        assert all(
            call.args[0] == AGENT_ID for call in adapter._hub.send_write_file.await_args_list
        )
        assert legacy["warning"]

    async def test_exec_on_guest_readonly(self, ctx) -> None:
        adapter = _adapter()
        modern, legacy = await self._both_ways(
            "exec_on_guest_readonly", {"command": "uptime"}, {"agent_adapter": adapter}, ctx
        )
        assert modern["exit_code"] == legacy["exit_code"] == 0
        assert adapter._hub.send_command.await_count == 2
        assert legacy["warning"]

    async def test_read_file_on_guest(self, ctx) -> None:
        adapter = _adapter()
        modern, legacy = await self._both_ways(
            "read_file_on_guest", {"path": "/etc/hostname"}, {"agent_adapter": adapter}, ctx
        )
        # This one answers with the FILE, so there is no warning to carry - and
        # nothing extra may be spliced into the content either.
        assert modern[0].text == legacy[0].text == "hello"
        assert adapter._hub.send_read_file.await_count == 2

    async def test_check_host_reachable(self, ctx) -> None:
        registry = _FakeAgentRegistry()
        modern, legacy = await self._both_ways(
            "check_host_reachable", {}, {"agent_registry": registry}, ctx
        )
        assert modern["reachable"] is True and legacy["reachable"] is True
        assert registry.asked == [HOSTNAME, HOSTNAME]
        assert _reached(modern) == _reached(legacy) == HOSTNAME
        assert legacy["warning"]

    async def test_get_agent(self, ctx) -> None:
        registry = _FakeAgentRegistry()
        modern, legacy = await self._both_ways("get_agent", {}, {"agent_registry": registry}, ctx)
        assert modern["agent_id"] == legacy["agent_id"] == AGENT_ID
        assert registry.asked == [HOSTNAME, HOSTNAME]
        assert legacy["warning"]

    async def test_get_host_metrics(self, ctx) -> None:
        modern, legacy = await self._both_ways("get_host_metrics", {}, {}, ctx)
        assert [m["metric"] for m in modern["metrics"]] == ["cpu.percent"]
        assert modern["metrics"] == legacy["metrics"]
        assert _reached(modern) == HOSTNAME
        assert legacy["warning"]

    async def test_get_host_metrics_series(self, ctx) -> None:
        modern, legacy = await self._both_ways(
            "get_host_metrics_series", {"metric": "cpu.percent"}, {}, ctx
        )
        assert modern["points"] and modern["points"] == legacy["points"]
        assert legacy["warning"]

    async def test_add_host(self, ctx) -> None:
        """The created ROW carries the name, whichever parameter delivered it."""
        first = await _handle_tool("add_host", {HOST_PARAM: "nas-01"}, ctx)
        second = await _handle_tool("add_host", {DEPRECATED_HOST_PARAM: "nas-02"}, ctx)

        rows = await ctx["repo"].db.fetchall("SELECT hostname FROM hosts ORDER BY hostname")
        names = [r["hostname"] for r in rows]
        assert "nas-01" in names and "nas-02" in names
        assert first["hostname"] == "nas-01" and second["hostname"] == "nas-02"
        assert second["warning"]

    async def test_host_wins_when_a_caller_sends_both(self, ctx) -> None:
        """Preferring the deprecated name would make the migration a lie."""
        registry = _FakeAgentRegistry()
        out = await _handle_tool(
            "check_host_reachable",
            {HOST_PARAM: HOSTNAME, DEPRECATED_HOST_PARAM: "somewhere-else"},
            {**ctx, "agent_registry": registry},
        )
        assert registry.asked == [HOSTNAME]
        assert out["reachable"] is True

    async def test_neither_name_is_refused_by_name(self, ctx) -> None:
        with pytest.raises(ValueError, match="`host` is required"):
            await _handle_tool(
                "check_host_reachable", {}, {**ctx, "agent_registry": _FakeAgentRegistry()}
            )
