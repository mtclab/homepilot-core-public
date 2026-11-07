"""Registry-walking fuzz gate for the MCP dispatcher (#696, the #635 class).

WHAT THIS FORBIDS: an MCP tool call whose arguments are missing, wrong-typed or
out of bounds escaping the dispatcher as a raw exception. The mcp 2.x low-level
server does no input-schema validation, and anything ``_on_call_tool`` does not
turn into a result reaches the transport's generic handler, which answers the
caller "Internal server error". The 2026-09 audit counted 46 such escapes across
36 tools on a real bootstrap: a ``KeyError`` for every missing required
argument, ``TypeError``/``AttributeError`` for wrong-typed ones,
``RuntimeError("Proxmox not configured")``, ``VaultError`` ... - every one a
question the product knew the answer to and told the caller nothing about.

It also forbids the quieter half: inputs the REST twin refuses being accepted
over MCP and doing something else - a negative ``limit`` (unbounded in SQLite),
``bulk_host_action(action="nuke")`` answering OK, a ``host_ids`` string iterated
per character, a ``-1`` quota silently clamped to a hard block, an alert rule on
a metric no agent emits, a record_fact of an undefined kind.

HOW: the gate walks ``_TOOL_DEFINITIONS`` - every tool, including ones added
after this file - and derives the bad inputs from each tool's own inputSchema,
then drives them through ``_on_call_tool`` over a context built by the REAL
stdio ``_bootstrap()`` (a default install: no Proxmox, no vault secrets, no
agents). Nothing is mocked between the call and the handler.

Teeth: remove the schema validation from ``_handle_tool`` or narrow the domain
error mapping in ``_on_call_tool`` back to ``ValueError`` and this fails,
listing every tool/variant that escapes or is wrongly accepted.
"""

from __future__ import annotations

import os
import shutil
import socket
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from homepilot.mcp import server as mcp_server


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture
async def real_ctx(monkeypatch) -> AsyncIterator[dict[str, Any]]:
    """The context the stdio MCP server really builds, on a fresh default install.

    Under $HOME rather than tmp_path: create_app_state refuses an artifacts dir
    under /tmp as a protected path (same arrangement as test_metrics_journey).
    ``_env_file=None`` keeps a developer's own install config out of the run.
    """
    from homepilot.config import Settings

    hp_dir = tempfile.mkdtemp(dir=str(Path.home()), prefix=".hp-test-mcpfuzz-")
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        data_dir=hp_dir,
        artifacts_dir=os.path.join(hp_dir, "artifacts"),
        agent_hub_port=_free_port(),
        proxmox_host="",
    )
    monkeypatch.setattr(mcp_server, "get_settings", lambda: settings)
    ctx = await mcp_server._bootstrap()
    saved = dict(mcp_server._server_context.items())
    mcp_server._server_context.clear()
    mcp_server._server_context.update(ctx)
    try:
        yield ctx
    finally:
        mcp_server._server_context.clear()
        mcp_server._server_context.update(saved)
        db = ctx.get("database")
        if db is not None:
            await db.close()
        shutil.rmtree(hp_dir, ignore_errors=True)


def _bogus_for(prop: dict[str, Any]) -> Any:
    """A well-typed value naming nothing that exists."""
    types = prop.get("type")
    t = types[0] if isinstance(types, list) else types
    if "enum" in prop:
        return prop["enum"][0]
    if t in ("integer", "number"):
        low = prop.get("minimum", prop.get("exclusiveMinimum", 0))
        return max(int(low) + 1, 999_999) if "maximum" not in prop else prop["maximum"]
    if t == "array":
        return ["nope-999"]
    if t == "object":
        return {}
    if t == "boolean":
        return True
    return "nope-999"


def _wrong_for(prop: dict[str, Any]) -> Any:
    """A value of a type the schema does not allow."""
    types = prop.get("type")
    allowed = set(types) if isinstance(types, list) else {types}
    if not allowed or allowed == {None}:
        return None  # untyped property: nothing is "wrong"
    if allowed & {"integer", "number"}:
        return "not-a-number"
    if allowed & {"string"}:
        return 12345
    if allowed & {"array", "object", "boolean"}:
        return "nope"
    return None


def _variants(tool: dict[str, Any]) -> list[tuple[str, dict[str, Any], bool]]:
    """(label, arguments, must_be_refused) derived from the tool's own schema."""
    schema = tool["inputSchema"]
    props: dict[str, Any] = schema.get("properties", {})
    required: list[str] = schema.get("required") or []
    out: list[tuple[str, dict[str, Any], bool]] = [("empty", {}, bool(required))]

    bogus = {k: _bogus_for(props.get(k, {})) for k in required}
    if required:
        # Well-typed arguments naming nothing: refused or answered, never a crash.
        out.append(("unknown-ids", bogus, False))
    for key, prop in props.items():
        wrong = _wrong_for(prop)
        if wrong is None:
            continue
        out.append((f"wrong-type:{key}", {**bogus, key: wrong}, True))
    return out


# Out-of-bounds inputs the REST twin refuses. Every one MUST be refused over MCP.
_BOUNDS: dict[str, list[tuple[str, dict[str, Any]]]] = {
    "list_kb": [("neg-limit", {"limit": -5}), ("huge-limit", {"limit": 10**9})],
    "get_audit_log": [("neg-limit", {"limit": -1}), ("neg-offset", {"offset": -1})],
    "get_fleet_drift": [("neg-limit", {"limit": -1}), ("neg-offset", {"offset": -2})],
    "list_tasks": [("neg-limit", {"limit": -1}), ("neg-offset", {"offset": -3})],
    "get_agent_audit": [("neg-limit", {"limit": -1})],
    "bulk_host_action": [
        ("unknown-action", {"action": "nuke", "host_ids": ["1"]}),
        ("ids-as-string", {"action": "adopt", "host_ids": "1"}),
    ],
    "set_guest_quota": [
        ("negative-axis", {"cn": "guest-x", "max_vms": -1}),
        ("blank-cn", {"cn": "  "}),
    ],
    "delete_guest_quota": [("blank-cn", {"cn": ""})],
    "record_fact": [("unknown-kind", {"target": "h", "kind": "zzz", "content": "c"})],
    "create_alert_rule": [
        (
            "unknown-metric",
            {"name": "n", "metric": "nonexistent.metric", "comparison": "gt", "threshold": 1},
        ),
        ("bad-comparison", {"name": "n", "metric": "load.1m", "comparison": "zz", "threshold": 1}),
        (
            "neg-for",
            {
                "name": "n",
                "metric": "load.1m",
                "comparison": "gt",
                "threshold": 1,
                "for_seconds": -1,
            },
        ),
    ],
    "update_alert_rule": [("unknown-metric", {"rule_id": "r", "metric": "cpu.percent"})],
    "open_enrolment_window": [("neg", {"minutes": -5}), ("huge", {"minutes": 10**9})],
    "get_host_metrics_series": [("neg-hours", {"host": "h", "metric": "load.1m", "hours": -1})],
    "propose_artifact": [("spec-not-json", {"spec": "{not json"}), ("spec-list", {"spec": "[]"})],
}


async def _call(name: str, arguments: Any) -> Any:
    return await mcp_server._on_call_tool(None, SimpleNamespace(name=name, arguments=arguments))


def _text(result: Any) -> str:
    return result.content[0].text if result.content else ""


async def test_no_tool_input_escapes_the_dispatcher(real_ctx) -> None:
    token = mcp_server._mcp_token_scope_var.set("admin")
    escapes: list[str] = []
    accepted: list[str] = []
    try:
        for tool in mcp_server._TOOL_DEFINITIONS:
            name = tool["name"]
            if name in mcp_server._MCP_FORBIDDEN_TOOLS:
                continue
            cases = _variants(tool) + [(lbl, a, True) for lbl, a in _BOUNDS.get(name, [])]
            for label, arguments, must_refuse in cases:
                try:
                    result = await _call(name, arguments)
                except Exception as exc:
                    escapes.append(f"{name} [{label}] -> {type(exc).__name__}: {exc}"[:240])
                    continue
                if must_refuse and not result.is_error:
                    accepted.append(f"{name} [{label}] answered OK: {_text(result)[:160]}")
                if result.is_error and "Internal server error" in _text(result):
                    escapes.append(f"{name} [{label}] -> generic internal error")
    finally:
        mcp_server._mcp_token_scope_var.reset(token)

    assert not escapes, (
        f"{len(escapes)} MCP input(s) escaped the dispatcher (the caller would see "
        "'Internal server error'):\n  " + "\n  ".join(escapes)
    )
    assert not accepted, (
        f"{len(accepted)} invalid MCP input(s) were ACCEPTED - the REST twin refuses "
        "them:\n  " + "\n  ".join(accepted)
    )


async def test_the_refusal_names_the_argument(real_ctx) -> None:
    """A refusal a model can act on: it says WHICH argument and what was wrong."""
    result = await _call("get_host", {})
    assert result.is_error
    assert "host_id" in _text(result)

    result = await _call("list_tasks", {"limit": -1})
    assert result.is_error
    assert "limit" in _text(result)


async def test_valid_calls_are_not_refused_by_validation(real_ctx) -> None:
    """The honest other arm: validation must not reject what the schema allows.

    ``hostname`` is the deprecated alias of ``host`` (#608) and must keep working
    even though only ``host`` is in ``required``."""
    for name, arguments in [
        ("list_kb", {"limit": 5, "offset": 0}),
        ("list_tasks", {}),
        ("get_host_metrics", {"hostname": "web01"}),
        ("get_host_metrics", {"host": "web01"}),
        ("query_inventory", {"filter": None}),
        ("get_kb_doc", {"doc_id": 1}),
    ]:
        result = await _call(name, arguments)
        assert "Invalid arguments" not in _text(result), (name, arguments, _text(result))
