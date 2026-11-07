"""Native notification channels - the ONE implementation the API and the
evaluator share (#660, review 03 step 3).

Before this, nothing left the box unless an operator built a webhook, a router
and a chat bot by hand. A channel is one row plus one vault entry:

* the row (``notification_channels``) holds what may be SHOWN: kind, name,
  ``endpoint`` as scheme://host[:port], and the outcome of the last delivery;
* the vault entry (``secret_ref``) holds what must not be: the topic URL and an
  optional access token. A locked or absent vault refuses the channel rather
  than storing the URL in the clear.

Channels receive the two alert transitions (``alert_firing``,
``alert_resolved``) and the "Send test" message. Delivery runs in the
background so a slow ntfy server cannot hold up the evaluator, and every
attempt is recorded on the channel (last ok / last error) and on the alert
event (``delivered_to``), so "was anyone told?" has an answer in the product.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Any

import httpx

from ..common import redact_endpoint
from . import ntfy

logger = logging.getLogger(__name__)

KIND_NTFY = "ntfy"
CHANNEL_KINDS = (KIND_NTFY,)
# The events a channel is sent. Alerts only: artifact events keep the webhook
# path (they carry approval codes, which do not belong on a phone lock screen).
CHANNEL_EVENTS = ("alert_firing", "alert_resolved")
TEST_EVENT = "notification_test"

MAX_NAME_LENGTH = 80
# Delivery is retried, briefly: an alert that was not delivered because one
# packet was lost is a worse failure than one that arrives five seconds late.
RETRY_DELAYS: tuple[float, ...] = (1.0, 5.0)
SEND_TIMEOUT_SECONDS = 5.0


class ChannelStoreError(RuntimeError):
    """The vault cannot hold a channel's URL right now."""


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def secret_ref_for(channel_id: int) -> str:
    return f"notify-channel-{int(channel_id)}"


def public_view(row: dict[str, Any]) -> dict[str, Any]:
    """A channel as every reader sees it: never the URL path, never the token."""
    return {
        "id": int(row["id"]),
        "kind": str(row["kind"]),
        "name": str(row["name"]),
        "endpoint": str(row["endpoint"]),
        "has_token": bool(row.get("has_token")),
        "enabled": bool(row.get("enabled", 1)),
        "created_at": row.get("created_at"),
        "last_attempt_at": row.get("last_attempt_at"),
        "last_ok_at": row.get("last_ok_at"),
        "last_error": row.get("last_error"),
        "last_error_at": row.get("last_error_at"),
        "state": delivery_state(row),
    }


def delivery_state(row: dict[str, Any]) -> str:
    """``ok`` / ``failing`` / ``untested`` / ``disabled`` from the last attempt."""
    if not row.get("enabled", 1):
        return "disabled"
    # The LAST attempt's own outcome, not a comparison of timestamps: a test
    # that succeeds and a real alert that fails in the same second must read
    # as failing.
    result = row.get("last_result")
    if result == "ok":
        return "ok"
    if result == "failed":
        return "failing"
    return "untested"


class ChannelStore:
    """SQL for ``notification_channels``. Values are always bound parameters."""

    def __init__(self, db: Any) -> None:
        self.db = db

    async def list(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM notification_channels"
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY id"
        return [dict(r) for r in await self.db.fetchall(sql)]

    async def get(self, channel_id: int) -> dict[str, Any] | None:
        row = await self.db.fetchone(
            "SELECT * FROM notification_channels WHERE id = ?", (int(channel_id),)
        )
        return dict(row) if row else None

    async def insert(self, *, kind: str, name: str, endpoint: str, has_token: bool) -> int:
        cursor = await self.db.execute(
            """INSERT INTO notification_channels
                 (kind, name, endpoint, secret_ref, has_token, enabled, created_at)
               VALUES (?, ?, ?, '', ?, 1, ?)""",
            (kind, name, endpoint, int(has_token), _now()),
        )
        channel_id = int(cursor.lastrowid or 0)
        await self.db.execute(
            "UPDATE notification_channels SET secret_ref = ? WHERE id = ?",
            (secret_ref_for(channel_id), channel_id),
        )
        await self.db.conn.commit()
        return channel_id

    async def delete(self, channel_id: int) -> bool:
        cursor = await self.db.execute(
            "DELETE FROM notification_channels WHERE id = ?", (int(channel_id),)
        )
        await self.db.conn.commit()
        return (cursor.rowcount or 0) > 0

    async def set_enabled(self, channel_id: int, enabled: bool) -> bool:
        cursor = await self.db.execute(
            "UPDATE notification_channels SET enabled = ? WHERE id = ?",
            (int(enabled), int(channel_id)),
        )
        await self.db.conn.commit()
        return (cursor.rowcount or 0) > 0

    async def record_attempt(self, channel_id: int, ok: bool, error: str | None) -> None:
        at = _now()
        if ok:
            await self.db.execute(
                "UPDATE notification_channels SET last_attempt_at = ?, last_ok_at = ?, "
                "last_result = 'ok' WHERE id = ?",
                (at, at, int(channel_id)),
            )
        else:
            await self.db.execute(
                "UPDATE notification_channels SET last_attempt_at = ?, last_error = ?, "
                "last_error_at = ?, last_result = 'failed' WHERE id = ?",
                (at, (error or "delivery failed")[:300], at, int(channel_id)),
            )
        await self.db.conn.commit()


def _describe_error(exc: BaseException) -> str:
    """An exception fit to store and show: its class and text with no URL in it.

    httpx puts the request URL - the credential - into some exception texts, so
    anything that looks like one is cut out wholesale."""
    import re

    text = re.sub(r"https?://\S+", "<url>", str(exc))
    return f"{type(exc).__name__}: {text}"[:300]


class Notifier:
    """Delivers alert transitions to every enabled channel.

    ``transport`` is an ``httpx`` transport, injectable so a test can stand in
    for the ntfy server at the HTTP layer and assert the real request."""

    def __init__(
        self,
        db: Any,
        vault: Any,
        metrics_repo: Any = None,
        transport: httpx.AsyncBaseTransport | None = None,
        retry_delays: tuple[float, ...] = RETRY_DELAYS,
    ) -> None:
        self.store = ChannelStore(db)
        self._vault = vault
        self._metrics = metrics_repo
        self._transport = transport
        self._retry_delays = retry_delays
        # Held, not merely created: asyncio keeps only weak references to tasks
        # and a delivery collected mid-flight is an alert nobody gets (#431).
        self._in_flight: set[asyncio.Task[Any]] = set()

    # ── Configuration ────────────────────────────────────────────────────────
    async def create_channel(
        self, *, kind: str, name: str, url: str, token: str | None
    ) -> dict[str, Any]:
        """Validate, store the secret in the vault, then the row. ValueError on bad
        input; ChannelStoreError when the vault cannot take the secret."""
        if kind not in CHANNEL_KINDS:
            raise ValueError(f"unknown channel kind {kind!r}; known: {', '.join(CHANNEL_KINDS)}")
        clean_name = (name or "").strip()
        if not clean_name or len(clean_name) > MAX_NAME_LENGTH:
            raise ValueError(f"the name must be 1-{MAX_NAME_LENGTH} characters")
        ntfy.split_topic_url(url)  # raises ValueError naming the problem
        clean_token = (token or "").strip() or None
        if clean_token and (len(clean_token) > ntfy.MAX_TOKEN_LENGTH or " " in clean_token):
            raise ValueError("the access token must be one word of at most 512 characters")
        if self._vault is None:
            raise ChannelStoreError(
                "There is no vault on this install, so a channel's URL cannot be stored "
                "safely. The topic URL is a credential and is never kept in the database."
            )
        channel_id = await self.store.insert(
            kind=kind,
            name=clean_name,
            endpoint=redact_endpoint(url.strip()),
            has_token=bool(clean_token),
        )
        try:
            await self._vault.store_secret(
                secret_ref_for(channel_id), {"url": url.strip(), "token": clean_token or ""}
            )
        except Exception as exc:
            # No row without its secret: a channel that can never send would be
            # listed as configured and fail on the first real alert.
            await self.store.delete(channel_id)
            raise ChannelStoreError(
                f"The vault refused the channel's URL ({type(exc).__name__}); nothing was "
                "saved. If the vault is locked, unlock it and try again."
            ) from exc
        row = await self.store.get(channel_id)
        return public_view(row or {})

    async def delete_channel(self, channel_id: int) -> bool:
        row = await self.store.get(channel_id)
        if row is None:
            return False
        deleted = await self.store.delete(channel_id)
        if self._vault is not None:
            try:
                await self._vault.delete_secret(str(row["secret_ref"]))
            except Exception:
                logger.warning(
                    "notification channel %s deleted, but its vault entry %s could not be removed",
                    channel_id,
                    row["secret_ref"],
                    exc_info=True,
                )
        return deleted

    async def list_channels(self) -> list[dict[str, Any]]:
        return [public_view(r) for r in await self.store.list()]

    # ── Delivery ─────────────────────────────────────────────────────────────
    async def _secret(self, row: dict[str, Any]) -> dict[str, Any] | None:
        if self._vault is None:
            return None
        try:
            secret = await self._vault.get_secret(str(row["secret_ref"]))
        except Exception:
            logger.warning(
                "notification channel %s (%s): its URL could not be read from the vault",
                row["id"],
                row["endpoint"],
            )
            return None
        return secret if isinstance(secret, dict) and secret.get("url") else None

    async def _send_once(
        self, row: dict[str, Any], *, title: str, message: str, priority: int, tags: list[str]
    ) -> tuple[bool, str | None]:
        secret = await self._secret(row)
        if secret is None:
            return False, "the channel's URL could not be read from the vault"
        try:
            publish_url, topic = ntfy.split_topic_url(str(secret["url"]))
        except ValueError as exc:
            return False, f"stored URL is not usable: {exc}"
        body = ntfy.build_message(
            topic=topic, title=title, message=message, priority=priority, tags=tags
        )
        try:
            async with httpx.AsyncClient(
                timeout=SEND_TIMEOUT_SECONDS, transport=self._transport
            ) as client:
                resp = await client.post(
                    publish_url,
                    content=json.dumps(body).encode(),
                    headers=ntfy.headers_for(str(secret.get("token") or "") or None),
                )
        except (httpx.HTTPError, OSError) as exc:
            return False, _describe_error(exc)
        if resp.status_code >= 400:
            return False, f"the server answered HTTP {resp.status_code}"
        return True, None

    async def _deliver(
        self,
        row: dict[str, Any],
        *,
        title: str,
        message: str,
        priority: int,
        tags: list[str],
        retry: bool,
    ) -> tuple[bool, str | None]:
        delays = self._retry_delays if retry else ()
        ok, error = await self._send_once(
            row, title=title, message=message, priority=priority, tags=tags
        )
        for delay in delays:
            if ok:
                break
            await asyncio.sleep(delay)
            ok, error = await self._send_once(
                row, title=title, message=message, priority=priority, tags=tags
            )
        await self.store.record_attempt(int(row["id"]), ok, error)
        if not ok:
            logger.warning(
                "notification channel %s (%s) did not accept a message: %s",
                row["id"],
                row["endpoint"],
                error,
            )
        return ok, error

    async def send_test(self, channel_id: int) -> dict[str, Any]:
        """One attempt, no retries: this answers "does delivery work right now".
        Raises LookupError for an unknown id."""
        row = await self.store.get(channel_id)
        if row is None:
            raise LookupError(f"notification channel {channel_id} not found")
        ok, error = await self._deliver(
            row,
            title="HomePilot test notification",
            message=(
                f"Test from HomePilot for channel '{row['name']}'. If you can read this, "
                "firing and resolved alerts will reach you here."
            ),
            priority=ntfy.PRIORITY_TEST,
            tags=["homepilot", "test"],
            retry=False,
        )
        fresh = await self.store.get(channel_id)
        return {
            "id": int(channel_id),
            "endpoint": row["endpoint"],
            "delivered": ok,
            "error": error,
            "channel": public_view(fresh or row),
        }

    async def dispatch(
        self, event_type: str, payload: dict[str, Any], *, event_id: int | None = None
    ) -> None:
        """Queue one alert transition for every enabled channel and return.

        The sends run in the background; ``drain()`` waits for them."""
        if event_type not in CHANNEL_EVENTS:
            return
        channels = await self.store.list(enabled_only=True)
        if not channels:
            if event_id is not None and self._metrics is not None:
                await self._metrics.set_alert_event_delivery(event_id, [])
            return
        task = asyncio.create_task(self._fan_out(channels, event_type, payload, event_id))
        self._in_flight.add(task)
        task.add_done_callback(self._in_flight.discard)

    async def _fan_out(
        self,
        channels: list[dict[str, Any]],
        event_type: str,
        payload: dict[str, Any],
        event_id: int | None,
    ) -> None:
        firing = event_type == "alert_firing"
        host = payload.get("hostname", "?")
        rule = payload.get("rule_name", "?")
        title = f"{'Alert' if firing else 'Resolved'}: {rule} on {host}"
        message = str(payload.get("message") or title)
        priority = ntfy.PRIORITY_FIRING if firing else ntfy.PRIORITY_RESOLVED
        tags = ["homepilot", "firing" if firing else "resolved"]
        results = await asyncio.gather(
            *(
                self._deliver(
                    row, title=title, message=message, priority=priority, tags=tags, retry=True
                )
                for row in channels
            ),
            return_exceptions=True,
        )
        delivered = [
            str(row["name"])
            for row, result in zip(channels, results, strict=True)
            if isinstance(result, tuple) and result[0]
        ]
        if event_id is not None and self._metrics is not None:
            await self._metrics.set_alert_event_delivery(event_id, delivered)

    async def drain(self) -> None:
        """Wait for every queued delivery (tests, and a clean shutdown)."""
        while self._in_flight:
            await asyncio.gather(*list(self._in_flight), return_exceptions=True)

    async def summary(self) -> list[dict[str, Any]]:
        """Where alerts go, for read-scope surfaces: no endpoint, no URL."""
        return [
            {
                "id": int(r["id"]),
                "kind": str(r["kind"]),
                "name": str(r["name"]),
                "enabled": bool(r["enabled"]),
                "state": delivery_state(r),
                "last_ok_at": r.get("last_ok_at"),
                "last_error_at": r.get("last_error_at"),
            }
            for r in await self.store.list()
        ]
