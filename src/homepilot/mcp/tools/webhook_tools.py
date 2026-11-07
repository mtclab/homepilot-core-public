"""Webhook tools (#700): see the configured endpoints, and check one delivers.

The same `homepilot.webhooks.service` calls `/admin/webhooks` makes, at the
admin tier its routes carry.

Creating and deleting a webhook are NOT here, on purpose. The
`artifact_proposed` event carries the approval code to the configured endpoints
(#681): a tool that adds an endpoint would let the assistant send itself the
codes it is meant to receive only from a human, and a tool that removes one
would let it silence the channel that tells the operator something is waiting.
Both stay operator actions in the console and `hp webhook`.
"""

from __future__ import annotations

from typing import Any

from ...webhooks import service

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "list_webhooks",
        "description": (
            "The configured event webhooks: id, the endpoint as scheme://host:port "
            "(never its path or query, which commonly carry the endpoint's own "
            "credential), the event types it receives ('*' is every event), whether "
            "it is enabled, whether deliveries are HMAC-signed, and its retry count. "
            "Read-only; webhooks are added and removed by the operator."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "outputSchema": {
            "type": "object",
            "properties": {
                "items": {"type": "array", "items": {"type": "object"}},
                "total": {"type": "integer"},
                "event_types": {"type": "array", "items": {"type": "string"}},
                "wildcard": {"type": "string"},
            },
            "required": ["items", "total", "event_types"],
        },
    },
    {
        "name": "test_webhook",
        "description": (
            "Send ONE `webhook_test` event to a configured webhook and report whether "
            "the endpoint accepted it (`delivered`). A single attempt, no retries: it "
            "answers 'does delivery work right now'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "webhook_id": {"type": "integer", "minimum": 1, "description": "From list_webhooks"}
            },
            "required": ["webhook_id"],
            "additionalProperties": False,
        },
    },
]


async def handle_list_webhooks(_arguments: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    return await service.listing(ctx["repo"])


async def handle_test_webhook(arguments: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    webhook_id = int(arguments["webhook_id"])
    try:
        return await service.send_test_event(
            ctx["repo"],
            webhook_id,
            actor=str(ctx.get("_mcp_caller_id") or "mcp"),
            source="mcp",
        )
    except LookupError:
        raise ValueError(f"Webhook {webhook_id} not found") from None
