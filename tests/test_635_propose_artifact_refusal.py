"""#635: `propose_artifact` refuses an over-long intent BY NAME, over the transport.

Live on dev 3.6.10: an `intent` longer than 200 characters answered the MCP
caller with a bare "Internal server error"; the sentence naming the fix
("intent must be 1-200 chars") was only in a log the caller cannot read.

The handler-level guard already exists on this branch (artifact_tools maps
LifecycleError to a refusal) and test_lifecycle_truth_648 pins it with a mocked
lifecycle. That proves the call, not the goal. This drives the exact reported
journey: the REAL lifecycle and validator over the context the stdio server
builds, through the dispatcher (`_on_call_tool`) the caller actually talks to,
and asserts what the caller reads - the reason, as an error result - and that
nothing was created.

Two layers guard it on this branch: the handler's own except, and the
dispatcher's domain-error mapping (#696/#729, `mcp/validation.py`). Either
alone keeps this green, which is the point of asserting at the transport.
Teeth (verified): drop `LifecycleError` from BOTH and the error escapes the
dispatcher - the caller's "Internal server error" - and this fails.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from homepilot.mcp import server as mcp_server

from .test_mcp_input_fuzz_gate import real_ctx  # noqa: F401 - the shared fixture


def _spec(intent: str) -> dict[str, Any]:
    return {
        "id": "2026-11-02-intent-too-long",
        "kind": "proxmox-api-sequence",
        "intent": intent,
        "body": (
            "# s\n\n```yaml proxmox-api-spec\nsteps:\n"
            "  - id: read\n    method: GET\n    path: /nodes/pve/status\n```\n"
        ),
        "target": {"kind": "cluster"},
        "idempotence": "via-precheck",
        "produced_by": {"session": "s", "agent": "a", "user": "u"},
    }


async def _propose(spec: dict[str, Any]) -> Any:
    token = mcp_server._mcp_token_scope_var.set("admin")
    try:
        return await mcp_server._on_call_tool(
            None, SimpleNamespace(name="propose_artifact", arguments={"spec": json.dumps(spec)})
        )
    finally:
        mcp_server._mcp_token_scope_var.reset(token)


async def test_an_over_long_intent_is_refused_with_its_reason(real_ctx) -> None:  # noqa: F811
    result = await _propose(_spec("x" * 201))

    text = result.content[0].text if result.content else ""
    assert result.is_error, f"an invalid spec was accepted: {text}"
    assert "Internal server error" not in text
    assert "intent must be 1-200 chars" in text

    store = real_ctx["store"]
    assert not store.exists("2026-11-02-intent-too-long"), "a refused spec left an artifact"


async def test_a_200_char_intent_is_accepted(real_ctx) -> None:  # noqa: F811
    """The other arm: the boundary itself is legal, so the refusal is the rule."""
    result = await _propose(_spec("x" * 200))

    text = result.content[0].text if result.content else ""
    assert "intent must be" not in text, text
