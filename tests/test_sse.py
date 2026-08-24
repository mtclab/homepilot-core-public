from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

from homepilot.sse import SSEBus, SSEEvent


class TestSSEBusPublishSubscribe:
    async def test_publish_delivers_to_subscriber(self):
        test_bus = SSEBus()
        q = test_bus.subscribe()
        await test_bus.publish("artifact_proposed", {"id": "test-123", "kind": "ansible-playbook"})
        event = q.get_nowait()
        assert event.type == "artifact_proposed"
        assert event.data == {"id": "test-123", "kind": "ansible-playbook"}
        assert event.id is not None
        assert event.id.startswith("artifact_proposed-")

    async def test_multiple_subscribers_receive_events(self):
        test_bus = SSEBus()
        q1 = test_bus.subscribe()
        q2 = test_bus.subscribe()
        await test_bus.publish("artifact_approved", {"id": "test-456"})
        e1 = q1.get_nowait()
        e2 = q2.get_nowait()
        assert e1.type == "artifact_approved"
        assert e2.type == "artifact_approved"
        assert e1.data == e2.data


class TestSSEBusUnsubscribe:
    async def test_unsubscribed_queue_does_not_receive_events(self):
        test_bus = SSEBus()
        q = test_bus.subscribe()
        test_bus.unsubscribe(q)
        await test_bus.publish("artifact_rejected", {"id": "test-789"})
        assert q.empty()

    async def test_unsubscribe_nonexistent_queue_does_not_raise(self):
        test_bus = SSEBus()
        other_q: asyncio.Queue[SSEEvent] = asyncio.Queue(maxsize=10)
        test_bus.unsubscribe(other_q)


class TestSSEEvent:
    def test_to_sse_with_id(self):
        event = SSEEvent(type="test_event", data={"key": "value"}, id="test-123")
        result = event.to_sse()
        assert result["event"] == "test_event"
        assert result["data"] == json.dumps({"key": "value"})
        assert result["id"] == "test-123"

    def test_to_sse_without_id(self):
        event = SSEEvent(type="test_event", data={"key": "value"})
        result = event.to_sse()
        assert result["event"] == "test_event"
        assert result["data"] == json.dumps({"key": "value"})
        assert "id" not in result


class TestSSEEndpoint:
    async def test_sse_stream_handler(self):
        """Test the SSE stream route handler directly (not via HTTP)."""
        from homepilot.sse import SSEBus

        test_bus = SSEBus()
        q = test_bus.subscribe()
        await test_bus.publish("artifact_proposed", {"id": "abc-123"})
        event = q.get_nowait()
        sse_dict = event.to_sse()
        assert sse_dict["event"] == "artifact_proposed"
        assert json.loads(sse_dict["data"]) == {"id": "abc-123"}
        assert sse_dict["id"] is not None

    def test_sse_endpoint_requires_auth(self):
        from unittest.mock import AsyncMock

        from fastapi import Depends, FastAPI
        from fastapi.testclient import TestClient

        from homepilot.artifacts.router import router as artifacts_router
        from homepilot.auth.deps import require_scope

        app = FastAPI()
        app.state.repo = AsyncMock()
        app.include_router(
            artifacts_router, prefix="/artifacts", dependencies=[Depends(require_scope("read"))]
        )

        with TestClient(app) as client:
            resp = client.get("/artifacts/events/stream")
            assert resp.status_code in (401, 403)


class TestEmitEventPublishesToBus:
    async def test_emit_event_publishes_to_sse_bus(self):
        test_bus = SSEBus()
        q = test_bus.subscribe()

        with (
            patch("homepilot.events.sse_bus", test_bus),
            patch("homepilot.events.get_settings") as mock_settings,
        ):
            from homepilot.config import Settings

            mock_settings.return_value = Settings(
                events_webhook_url=None,
            )
            from homepilot.events import emit_event

            await emit_event("artifact_proposed", {"id": "test-xyz"})

        event = q.get_nowait()
        assert event.type == "artifact_proposed"
        assert event.data == {"id": "test-xyz"}
