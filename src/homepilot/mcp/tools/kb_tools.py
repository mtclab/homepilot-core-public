"""Knowledge base tools: search and record facts."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

from homepilot.artifacts.lifecycle import ArtifactLifecycle

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "search_kb",
        "description": (
            "Search the knowledge base using vector + keyword search. "
            "Returns matching notes, policies, and decisions."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query string",
                },
                "kind": {
                    "type": ["string", "null"],
                    "description": "Filter by kind: note, policy, or decision. Null for all.",
                },
            },
            "required": ["query"],
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "results": {"type": "array", "items": {"type": "object"}},
                "total": {"type": "integer"},
            },
            "required": ["results", "total"],
        },
    },
    {
        "name": "record_fact",
        "description": (
            "Write a note, policy, or decision to the knowledge base. "
            "Non-mutating: auto-applied without approval."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "Host, service, or network this fact relates to",
                },
                "kind": {
                    "type": "string",
                    "description": "note, policy, or decision",
                },
                "content": {
                    "type": "string",
                    "description": "Fact content",
                },
                "supersedes": {
                    "type": ["string", "null"],
                    "description": "ID of prior fact this supersedes, or null",
                },
            },
            "required": ["target", "kind", "content"],
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "status": {"type": "string"},
                "kind": {"type": "string"},
            },
            "required": ["id", "status", "kind"],
        },
    },
]


async def handle_search_kb(arguments: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    query = arguments["query"]
    kind = arguments.get("kind")
    results = await ctx["kb_service"].search(query, kind=kind, limit=20)
    return {"results": results, "total": len(results)}


def _mcp_caller_id() -> str:
    """Return a stable identity string for the current MCP caller."""
    from mcp.server.auth.middleware.auth_context import get_access_token

    token = get_access_token()
    if token is not None:
        return token.client_id or "mcp-http"
    from homepilot.mcp.server import _mcp_caller_id_var

    return _mcp_caller_id_var.get()


async def handle_record_fact(arguments: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    lifecycle: ArtifactLifecycle = ctx["lifecycle"]
    target = arguments["target"]
    kind = arguments["kind"]
    content = arguments["content"]
    supersedes = arguments.get("supersedes")

    date_prefix = datetime.now(UTC).strftime("%Y-%m-%d")
    slug = target.lower().replace(".", "-").replace(" ", "-")[:30]
    fact_id = f"{date_prefix}-kb-{slug}"
    content_hash = hashlib.sha256(content.encode()).hexdigest()[:6]
    fact_id = f"{fact_id}-{content_hash}"

    spec = {
        "id": fact_id,
        "kind": "kb-note",
        "intent": f"{kind}: {content[:180]}",
        "body": content,
        "note_kind": kind,
        "target": {"kind": "global", "host": target},
        "produced_by": {"session": "mcp", "agent": "mcp-tool", "user": _mcp_caller_id()},
    }
    if supersedes:
        spec["supersedes"] = [supersedes]

    artifact_id = await lifecycle.propose(spec)
    return {"id": artifact_id, "status": "applied", "kind": kind}
