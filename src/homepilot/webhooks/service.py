"""Webhook configurations: the ONE implementation the API, MCP and CLI share (#700).

Webhook configs lived only in a CLI that opened the database directly, so on a
running install - where the backend holds the database - they could not be
configured at all. These functions are what `/admin/webhooks` and the MCP
webhook tools call; the CLI goes through the API.

The URL is treated as a credential: endpoints commonly carry their secret in
the path or query. Everything this module returns or logs shows only
scheme://host:port; the full URL is echoed exactly once, to the admin who
created it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from ..common import redact_endpoint
from .catalog import (
    EVENT_TYPES,
    TEST_EVENT,
    WILDCARD,
    validate_event_types,
    validate_webhook_url,
)


def public_view(row: dict[str, Any]) -> dict[str, Any]:
    """A stored config as every reader sees it: no URL path, no secret."""
    try:
        events = json.loads(row.get("event_types") or "[]")
    except (TypeError, ValueError):
        events = []
    return {
        "id": int(row["id"]),
        "endpoint": redact_endpoint(str(row.get("url") or "")),
        "event_types": events if isinstance(events, list) else [],
        "enabled": bool(row.get("enabled", 1)),
        "max_retries": int(row.get("max_retries") or 0),
        "has_secret": bool(row.get("secret")),
        "created_at": row.get("created_at"),
    }


async def list_webhooks(repo: Any) -> list[dict[str, Any]]:
    return [public_view(r) for r in await repo.list_webhook_configs()]


async def listing(repo: Any) -> dict[str, Any]:
    """The list answer every surface gives: the configs, and what they can subscribe to."""
    items = await list_webhooks(repo)
    return {
        "items": items,
        "total": len(items),
        "event_types": sorted(EVENT_TYPES),
        "wildcard": WILDCARD,
    }


async def create_webhook(
    repo: Any,
    *,
    url: str,
    event_types: list[str],
    secret: str | None,
    max_retries: int,
    actor: str,
    source: str,
) -> dict[str, Any]:
    """Validate, store and audit one config. Raises ValueError naming the problem."""
    clean_url = validate_webhook_url(url)
    events = validate_event_types(event_types)
    if not 0 <= int(max_retries) <= 10:
        raise ValueError("max_retries must be between 0 and 10")
    config_id = await repo.create_webhook_config(
        url=clean_url, event_types=events, secret=secret or None, max_retries=int(max_retries)
    )
    row = await repo.get_webhook_config(config_id)
    view = public_view(row or {"id": config_id, "url": clean_url})
    await repo.log_audit(
        user_id=actor,
        source=source,
        action="webhook_created",
        details_json=json.dumps(
            {"id": config_id, "endpoint": view["endpoint"], "event_types": events}
        ),
    )
    return view


async def delete_webhook(repo: Any, webhook_id: int, *, actor: str, source: str) -> bool:
    row = await repo.get_webhook_config(webhook_id)
    if row is None:
        return False
    deleted = bool(await repo.delete_webhook_config(webhook_id))
    if deleted:
        await repo.log_audit(
            user_id=actor,
            source=source,
            action="webhook_deleted",
            details_json=json.dumps(
                {"id": webhook_id, "endpoint": redact_endpoint(str(row.get("url") or ""))}
            ),
        )
    return deleted


async def send_test_event(repo: Any, webhook_id: int, *, actor: str, source: str) -> dict[str, Any]:
    """Send one test event, ONE attempt, and say whether the endpoint accepted it.

    One attempt rather than the config's retry schedule: this answers "does
    delivery work right now", and the retry back-off would hold the caller for
    half a minute to say the same thing. Raises LookupError for an unknown id.
    """
    from ..events import deliver_with_retry

    row = await repo.get_webhook_config(webhook_id)
    if row is None:
        raise LookupError(f"webhook {webhook_id} not found")
    payload = {
        "event": TEST_EVENT,
        "message": f"Test event for webhook #{webhook_id}",
        "timestamp": datetime.now(UTC).isoformat(),
    }
    delivered = await deliver_with_retry(
        url=str(row["url"]), payload=payload, secret=row.get("secret"), max_retries=0
    )
    endpoint = redact_endpoint(str(row["url"]))
    await repo.log_audit(
        user_id=actor,
        source=source,
        action="webhook_tested",
        details_json=json.dumps({"id": webhook_id, "endpoint": endpoint, "delivered": delivered}),
    )
    return {"id": webhook_id, "endpoint": endpoint, "event": TEST_EVENT, "delivered": delivered}
