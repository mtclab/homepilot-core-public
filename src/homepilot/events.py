from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from typing import Any

import httpx

from .config import get_settings
from .sse import bus as sse_bus
from .webhooks.send import sign_payload

logger = logging.getLogger(__name__)

RETRY_DELAYS: list[float] = [1.0, 5.0, 30.0]


async def deliver_with_retry(
    url: str,
    payload: dict[str, Any],
    secret: str | None,
    max_retries: int,
    delivery_id: int | None = None,
    repo: Any | None = None,
    db: Any | None = None,
) -> None:
    payload_bytes = json.dumps(payload).encode()
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if secret:
        headers["X-Webhook-Signature"] = sign_payload(payload_bytes, secret)

    attempts = 0
    for delay_index in range(max_retries + 1):
        attempts += 1
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(url, content=payload_bytes, headers=headers)
            if resp.status_code < 400:
                if repo and delivery_id is not None:
                    await repo.update_webhook_delivery(delivery_id, "success", attempts)
                    if db:
                        await db.conn.commit()
                return
            logger.warning(
                "Webhook returned status=%d for url=%s attempt=%d",
                resp.status_code,
                url,
                attempts,
            )
        except (httpx.HTTPError, httpx.TimeoutException, ConnectionError, OSError):
            logger.warning(
                "Webhook delivery failed for url=%s attempt=%d",
                url,
                attempts,
                exc_info=True,
            )

        if delay_index < max_retries and delay_index < len(RETRY_DELAYS):
            await asyncio.sleep(RETRY_DELAYS[delay_index])

    if repo and delivery_id is not None:
        await repo.update_webhook_delivery(delivery_id, "failed", attempts)
        if db:
            await db.conn.commit()
    logger.error(
        "Webhook delivery failed after %d attempts for url=%s",
        attempts,
        url,
    )


async def emit_event(
    event_type: str,
    payload: dict[str, object],
    repo: Any = None,
) -> None:
    await sse_bus.publish(event_type, dict(payload))

    settings = get_settings()
    url = settings.events_webhook_url
    if url:
        payload_data = {"event": event_type, **payload}
        payload_bytes = json.dumps(payload_data).encode()
        wh_headers: dict[str, str] = {"Content-Type": "application/json"}
        secret = settings.events_webhook_secret
        if secret:
            wh_headers["X-Webhook-Signature"] = sign_payload(payload_bytes, secret)
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(url, content=payload_bytes, headers=wh_headers)
                if resp.status_code >= 400:
                    logger.warning(
                        "Webhook returned status=%d for event=%s url=%s",
                        resp.status_code,
                        event_type,
                        url,
                    )
        except (httpx.HTTPError, httpx.TimeoutException, ConnectionError, OSError):
            logger.warning(
                "Webhook delivery failed for event=%s url=%s",
                event_type,
                url,
                exc_info=True,
            )

    if repo is not None:
        try:
            db_obj = repo.db
            configs = await repo.get_webhook_configs_for_event(event_type)
            for config in configs:
                wh_payload = {"event": event_type, **payload}
                delivery_id = await repo.create_webhook_delivery(
                    webhook_id=config["id"],
                    event_type=event_type,
                    payload=json.dumps(wh_payload),
                    status="pending",
                )
                await db_obj.conn.commit()
                task = asyncio.create_task(
                    deliver_with_retry(
                        url=config["url"],
                        payload=wh_payload,
                        secret=config.get("secret"),
                        max_retries=config.get("max_retries", 3),
                        delivery_id=delivery_id,
                        repo=repo,
                        db=db_obj,
                    )
                )
                _ = task  # prevent RUF006 warning
        except (
            httpx.HTTPError,
            httpx.TimeoutException,
            ConnectionError,
            OSError,
            sqlite3.OperationalError,
        ):
            logger.warning(
                "Webhook config delivery skipped for event=%s",
                event_type,
                exc_info=True,
            )
