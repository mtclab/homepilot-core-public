"""Notification channel tools (#660): see where alerts go, and check one delivers.

The same `Notifier` calls `/admin/notifications/channels` makes, at the admin
tier its routes carry.

Creating, disabling and deleting a channel are NOT here, on purpose: creating
one takes the topic URL - a credential - as input, which would land in the
assistant's transcript, and disabling or deleting one would let the assistant
silence the channel that wakes the operator. Those stay console actions.
"""

from __future__ import annotations

from typing import Any

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "list_notification_channels",
        "description": (
            "The native alert channels (ntfy): id, kind, name, the endpoint as "
            "scheme://host:port (never the topic, which is the credential), whether it "
            "is enabled, and the outcome of its LAST delivery - state ok / failing / "
            "untested / disabled, last_ok_at, last_error, last_error_at. A channel that "
            "is 'untested' has never proven it delivers. Read-only."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "outputSchema": {
            "type": "object",
            "properties": {
                "items": {"type": "array", "items": {"type": "object"}},
                "total": {"type": "integer"},
            },
            "required": ["items", "total"],
        },
    },
    {
        "name": "test_notification_channel",
        "description": (
            "Send ONE test message to a notification channel and report whether the "
            "server accepted it (`delivered`, and `error` when not). A single attempt, "
            "no retries. It WILL buzz the operator's phone - use it to check delivery, "
            "not as a heartbeat."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "channel_id": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "From list_notification_channels",
                }
            },
            "required": ["channel_id"],
            "additionalProperties": False,
        },
    },
]


def _notifier(ctx: dict[str, Any]) -> Any:
    notifier = ctx.get("notifier")
    if notifier is None:
        raise RuntimeError("Notification channels are not available")
    return notifier


async def handle_list_notification_channels(
    _arguments: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    items = await _notifier(ctx).list_channels()
    return {"items": items, "total": len(items)}


async def handle_test_notification_channel(
    arguments: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    channel_id = int(arguments["channel_id"])
    try:
        result: dict[str, Any] = await _notifier(ctx).send_test(channel_id)
    except LookupError:
        raise ValueError(f"Notification channel {channel_id} not found") from None
    repo = ctx.get("repo")
    if repo is not None:
        import json

        await repo.log_audit(
            user_id=str(ctx.get("_mcp_caller_id") or "mcp"),
            source="mcp",
            action="notification_channel_tested",
            details_json=json.dumps(
                {"id": channel_id, "endpoint": result["endpoint"], "delivered": result["delivered"]}
            ),
        )
    return result
