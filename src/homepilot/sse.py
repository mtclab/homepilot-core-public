from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

logger = logging.getLogger(__name__)


@dataclass
class SSEEvent:
    type: str
    data: dict[str, object]
    id: str | None = None

    def to_sse(self) -> dict[str, str]:
        payload = {"event": self.type, "data": json.dumps(self.data)}
        if self.id is not None:
            payload["id"] = self.id
        return payload


class SSEBus:
    """Simple pub/sub event bus for SSE streaming."""

    MAX_SUBSCRIBERS = 50

    def __init__(self, max_subscribers: int = MAX_SUBSCRIBERS) -> None:
        self._subscribers: list[asyncio.Queue[SSEEvent]] = []
        self._max_subscribers = max_subscribers

    async def publish(self, event_type: str, data: dict[str, object]) -> None:
        event = SSEEvent(
            type=event_type,
            data=data,
            id=f"{event_type}-{datetime.now(UTC).strftime('%Y%m%d%H%M%S%f')}",
        )
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning("SSE subscriber queue full, dropping event")

    def subscribe(self) -> asyncio.Queue[SSEEvent] | None:
        if len(self._subscribers) >= self._max_subscribers:
            logger.warning("SSE subscriber limit reached (%d), rejecting", self._max_subscribers)
            return None
        q: asyncio.Queue[SSEEvent] = asyncio.Queue(maxsize=100)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[SSEEvent]) -> None:
        with contextlib.suppress(ValueError):
            self._subscribers.remove(q)


bus = SSEBus()
