"""A webhook URL never reaches the logs (#690).

A Slack or Discord webhook URL is the credential: whoever holds it can post into
the channel. Every delivery failure path logged `url=%s` raw - and two of them
attached a traceback whose exception text can carry the URL as well.

Each test drives a real failure through the real delivery function and asserts
on everything the log captured, record text and exception text alike. Teeth: put
`url` back into any of those log calls (or `exc_info=True` back on the exception
paths) and the secret path segment shows up in the captured logs.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from homepilot.config import Settings
from homepilot.events import deliver_with_retry, emit_event
from homepilot.webhooks.send import describe_failure, send_webhook

SECRET_PATH = "/services/T0000/B0000/not-a-real-webhook-secret"  # pragma: allowlist secret
URL = f"https://hooks.example.com{SECRET_PATH}"


def _client(post: AsyncMock) -> AsyncMock:
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.post = post
    return client


def _everything_logged(caplog: pytest.LogCaptureFixture) -> str:
    parts = []
    for record in caplog.records:
        parts.append(record.getMessage())
        if record.exc_info:
            parts.append(logging.Formatter().formatException(record.exc_info))
        if record.exc_text:
            parts.append(record.exc_text)
    return "\n".join(parts)


def _refused() -> AsyncMock:
    # The exception text names the URL, as an httpx error can.
    return AsyncMock(side_effect=httpx.ConnectError(f"cannot reach {URL}"))


def _rejected() -> AsyncMock:
    return AsyncMock(return_value=httpx.Response(500, text="no"))


@pytest.mark.parametrize("post", [_refused, _rejected], ids=["exception", "http-500"])
async def test_deliver_with_retry_logs_only_the_host(post, caplog) -> None:
    with (
        caplog.at_level(logging.DEBUG),
        patch("homepilot.events.httpx.AsyncClient", return_value=_client(post())),
    ):
        ok = await deliver_with_retry(URL, {"event": "t"}, secret=None, max_retries=0)
    assert ok is False
    logged = _everything_logged(caplog)
    assert "hooks.example.com" in logged, "the failure should still name the endpoint host"
    assert SECRET_PATH not in logged


@pytest.mark.parametrize("post", [_refused, _rejected], ids=["exception", "http-500"])
async def test_emit_event_logs_only_the_host(post, caplog) -> None:
    sender = post()
    with (
        caplog.at_level(logging.DEBUG),
        patch("homepilot.events.get_settings", return_value=Settings(events_webhook_url=URL)),
        patch("homepilot.events.effective", AsyncMock(return_value=URL)),
        patch("homepilot.events.httpx.AsyncClient", return_value=_client(sender)),
    ):
        await emit_event("artifact_proposed", {"id": "x"})
    assert sender.await_count == 1, "the delivery was never attempted - nothing was tested"
    assert SECRET_PATH not in _everything_logged(caplog)


@pytest.mark.parametrize("post", [_refused, _rejected], ids=["exception", "http-500"])
async def test_send_webhook_logs_only_the_host(post, caplog) -> None:
    sender = post()
    with (
        caplog.at_level(logging.DEBUG),
        patch("homepilot.config.get_settings", return_value=Settings(events_webhook_url=URL)),
        patch("homepilot.app_settings.effective", AsyncMock(return_value=URL)),
        patch("homepilot.webhooks.send.httpx.AsyncClient", return_value=_client(sender)),
    ):
        await send_webhook("artifact_proposed", {"id": "x", "kind": "kb-note"})
    assert sender.await_count == 1, "the delivery was never attempted - nothing was tested"
    assert SECRET_PATH not in _everything_logged(caplog)


def test_describe_failure_cuts_the_url_out_of_the_exception_text() -> None:
    text = describe_failure(httpx.ConnectError(f"cannot reach {URL}/"), URL)
    assert "ConnectError" in text
    assert SECRET_PATH not in text
