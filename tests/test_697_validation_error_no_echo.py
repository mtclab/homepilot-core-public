"""A validation error names the field, never the value - on every MCP tool (#697).

#628 fixed `rejoin_tailnet` and the HTTP 422 handler, and the same leak stayed
in five other tools: `provision_guest`, `create_guest_template`, `add_host`,
`update_host` and `bulk_host_action` built their error from ``str(exc)`` of a
pydantic ValidationError, which carries ``input_value=<whatever was sent>``. A
malformed ``tailscale_auth_key`` came straight back into the MCP transcript.

The fix is one formatter (`homepilot.common.format_validation_error`, loc + msg
only) used everywhere a ValidationError becomes text. This registry test feeds a
secret-shaped bad value into EVERY argument of EVERY tool the server lists and
asserts the value never comes back - so a new tool that stringifies a
ValidationError fails here without anyone having to remember to add it.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import BaseModel, Field, ValidationError

from homepilot.mcp.server import _TOOL_DEFINITIONS, _handle_tool

# Shaped like a real tailnet auth key, and invalid for every pattern/type that
# might check it (the trailing "!!" breaks any key/hostname/identifier regex).
MARK = "tskey-auth-kQ7SECRETSHAPEDzz0123456789abcdef!!"
# What every NON-secret argument gets. A tool may legitimately say "host
# 'IDENT-...' not found" - echoing an identifier it was asked about is not the
# defect - but pydantic's `input_value=` is never acceptable, for any argument.
IDENT = "IDENT-not-a-secret-!!"

# Arguments that carry a credential. Their value must never come back, in any
# form, from any error.
_SECRET_ARG = re.compile(r"(^|_)(auth_key|authorized_key|token|secret|password|passphrase|code)$")
_SECRET_ARGS_BY_TOOL = {
    # A settings override value can itself be a credential.
    ("set_setting_override", "value"),
    ("probe_setting_override", "value"),
}


def _is_secret(tool: str, arg: str) -> bool:
    return bool(_SECRET_ARG.search(arg)) or (tool, arg) in _SECRET_ARGS_BY_TOOL


def _bad_value(schema: dict[str, Any], marker: str) -> Any:
    kind = schema.get("type")
    if kind == "array":
        return [marker]
    if kind == "object":
        return {"k": marker}
    # strings get the marker; integer/number/boolean get it too - the wrong
    # TYPE, which is exactly what makes pydantic include input_value.
    return marker


def _arguments(tool: dict[str, Any]) -> dict[str, Any]:
    props = (tool.get("inputSchema") or {}).get("properties") or {}
    return {
        name: _bad_value(schema or {}, MARK if _is_secret(tool["name"], name) else IDENT)
        for name, schema in props.items()
    }


class _AnyCtx(dict):  # type: ignore[type-arg]
    """A context that has every service, each a mock, so a handler gets as far
    as its own validation instead of stopping at "not configured"."""

    def get(self, key: Any, default: Any = None) -> Any:
        if key in self:
            return super().get(key)
        return self._stub(key)

    def __getitem__(self, key: Any) -> Any:
        if key in self:
            return super().__getitem__(key)
        return self._stub(key)

    def _stub(self, key: Any) -> Any:
        mock = AsyncMock(name=str(key))
        # Attribute probes like `service.proxmox is None` must see a service.
        mock.proxmox = MagicMock(name="proxmox")
        super().__setitem__(key, mock)
        return mock


def _text(result: Any) -> str:
    if isinstance(result, dict):
        return json.dumps(result, default=str)
    return " ".join(getattr(item, "text", str(item)) for item in (result or []))


@pytest.mark.parametrize("tool", _TOOL_DEFINITIONS, ids=lambda t: t["name"])
async def test_no_tool_echoes_a_rejected_value(tool: dict[str, Any]) -> None:
    ctx = _AnyCtx({"_mcp_token_scope": "admin", "_mcp_caller_id": "mcp-test"})
    try:
        out = _text(await asyncio.wait_for(_handle_tool(tool["name"], _arguments(tool), ctx), 10))
    except TimeoutError:
        pytest.fail(f"{tool['name']} hung on bad input")
    except Exception as exc:  # the error text IS what reaches the transcript
        out = str(exc)
    assert "input_value" not in out, f"{tool['name']} leaked a pydantic input echo: {out[:300]}"
    assert MARK not in out, f"{tool['name']} echoed a credential argument: {out[:300]}"


def test_secret_arguments_are_actually_exercised() -> None:
    """Teeth for the MARK assertion: the registry does put the credential
    marker into the arguments the reported leak went through."""
    by_tool = {t["name"]: _arguments(t) for t in _TOOL_DEFINITIONS}
    assert by_tool["provision_guest"]["tailscale_auth_key"] == MARK
    assert by_tool["rejoin_tailnet"]["auth_key"] == MARK


async def test_bulk_host_action_does_not_echo_a_bad_host_id() -> None:
    """The one known leaker the generic arguments above do not drive into
    pydantic (every id is a valid string): a malformed id list."""
    ctx = _AnyCtx({"_mcp_token_scope": "admin", "_mcp_caller_id": "mcp-test"})
    with pytest.raises(ValueError) as info:
        await _handle_tool(
            "bulk_host_action", {"action": "ignore", "host_ids": [{"secret": MARK}]}, ctx
        )
    assert MARK not in str(info.value) and "input_value" not in str(info.value)
    assert "host_ids" in str(info.value), "the error no longer names the field"


def test_the_registry_covers_the_known_leakers() -> None:
    names = {t["name"] for t in _TOOL_DEFINITIONS}
    for leaker in (
        "provision_guest",
        "create_guest_template",
        "add_host",
        "update_host",
        "bulk_host_action",
    ):
        assert leaker in names


class _Secretish(BaseModel):
    tailscale_auth_key: str = Field(pattern=r"^tskey-[a-z]+-[A-Za-z0-9]+$")
    port: int


class TestTheFormatter:
    def _error(self) -> ValidationError:
        with pytest.raises(ValidationError) as info:
            _Secretish(tailscale_auth_key=MARK, port=MARK)  # type: ignore[arg-type]
        return info.value

    def test_names_every_field_and_no_value(self) -> None:
        from homepilot.common import format_validation_error

        text = format_validation_error(self._error())
        assert MARK not in text
        assert "input_value" not in text
        assert "tailscale_auth_key" in text and "port" in text

    def test_the_raw_error_does_echo(self) -> None:
        """Teeth: the thing the formatter replaces really does leak."""
        assert MARK in str(self._error())
