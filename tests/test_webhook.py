from __future__ import annotations

import json
import logging
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from homepilot.artifacts.lifecycle import ArtifactLifecycle


def _make_spec(**overrides) -> dict:
    spec = {
        "id": "2025-01-01-test-webhook-abc123",
        "kind": "ansible-playbook",
        "intent": "Test webhook hook",
        "body": "---\n- name: test\n  hosts: all\n  tasks: []",
        "target": {"kind": "vm", "vmid": 100, "node": "pve1"},
        "idempotence": "via-precheck",
        "produced_by": {"session": "s1", "agent": "a1", "user": "u1"},
    }
    spec.update(overrides)
    return spec


@pytest.fixture
def mock_store(tmp_path):
    from unittest.mock import MagicMock

    import yaml

    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()
    store = MagicMock(spec=[])
    _storage: dict[str, tuple[dict, str]] = {}

    def _exists(id_str: str) -> bool:
        return id_str in _storage

    def _read(id_str: str) -> tuple[dict, str]:
        if id_str not in _storage:
            raise FileNotFoundError(id_str)
        return _storage[id_str]

    def _write(id_str: str, fm_yml: str, body: str, event: str):
        fm = yaml.safe_load(fm_yml)
        _storage[id_str] = (fm, body)
        p = artifacts_dir / f"{id_str}.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"---\n{fm_yml}---\n\n{body}", encoding="utf-8")

    store.exists = _exists
    store.read = _read
    store.write = _write
    store._storage = _storage
    return store


class TestWebhookOnPropose:
    async def test_webhook_called_on_propose(self, mock_store):
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_post = AsyncMock(return_value=mock_response)
        with (
            patch("homepilot.events.get_settings") as mock_settings,
            patch("homepilot.events.httpx.AsyncClient") as mock_client_cls,
        ):
            from homepilot.config import Settings

            mock_settings.return_value = Settings(
                secret_key="test",
                events_webhook_url="https://hooks.example.com/artifacts",
            )
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = mock_post
            mock_client_cls.return_value = mock_client

            lc = ArtifactLifecycle(store=mock_store)
            await lc.propose(_make_spec())

        mock_post.assert_called_once()
        call_args = mock_post.call_args
        body = json.loads(call_args[1]["content"])
        assert body["event"] == "artifact_proposed"
        assert body["id"] == "2025-01-01-test-webhook-abc123"
        assert body["kind"] == "ansible-playbook"

    async def test_webhook_failure_does_not_block_propose(self, mock_store):
        with (
            patch("homepilot.events.get_settings") as mock_settings,
            patch("homepilot.events.httpx.AsyncClient") as mock_client_cls,
        ):
            from homepilot.config import Settings

            mock_settings.return_value = Settings(
                secret_key="test",
                events_webhook_url="https://hooks.example.com/artifacts",
            )
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(side_effect=httpx.ConnectError("connection refused"))
            mock_client_cls.return_value = mock_client

            lc = ArtifactLifecycle(store=mock_store)
            aid = await lc.propose(_make_spec())

        assert aid == "2025-01-01-test-webhook-abc123"

    async def test_webhook_not_called_when_url_unset(self, mock_store):
        with (
            patch("homepilot.events.get_settings") as mock_settings,
            patch("homepilot.events.httpx.AsyncClient") as mock_client_cls,
        ):
            from homepilot.config import Settings

            mock_settings.return_value = Settings(
                secret_key="test",
                events_webhook_url=None,
            )

            lc = ArtifactLifecycle(store=mock_store)
            await lc.propose(_make_spec())

        mock_client_cls.assert_not_called()


class TestWebhookFailSoft:
    def _setup_mock_client(
        self, mock_client_cls, mock_settings, post_return=None, post_side_effect=None
    ):
        from homepilot.config import Settings

        mock_settings.return_value = Settings(
            secret_key="test",
            events_webhook_url="https://hooks.example.com/artifacts",
        )
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        if post_side_effect is not None:
            mock_client.post = AsyncMock(side_effect=post_side_effect)
        elif post_return is not None:
            mock_client.post = AsyncMock(return_value=post_return)
        mock_client_cls.return_value = mock_client
        return mock_client

    async def test_webhook_target_unreachable(self, mock_store, caplog):
        with (
            patch("homepilot.events.get_settings") as mock_settings,
            patch("homepilot.events.httpx.AsyncClient") as mock_client_cls,
        ):
            self._setup_mock_client(
                mock_client_cls,
                mock_settings,
                post_side_effect=httpx.ConnectError("connection refused"),
            )

            with caplog.at_level(logging.WARNING, logger="homepilot.events"):
                lc = ArtifactLifecycle(store=mock_store)
                aid = await lc.propose(_make_spec())

        assert aid == "2025-01-01-test-webhook-abc123"
        assert any("Webhook delivery failed" in r.message for r in caplog.records)

    async def test_webhook_target_timeout(self, mock_store):
        with (
            patch("homepilot.events.get_settings") as mock_settings,
            patch("homepilot.events.httpx.AsyncClient") as mock_client_cls,
        ):
            self._setup_mock_client(
                mock_client_cls, mock_settings, post_side_effect=httpx.TimeoutException("timed out")
            )

            lc = ArtifactLifecycle(store=mock_store)
            aid = await lc.propose(_make_spec())

        assert aid == "2025-01-01-test-webhook-abc123"

    async def test_webhook_returns_4xx(self, mock_store, caplog):
        with (
            patch("homepilot.events.get_settings") as mock_settings,
            patch("homepilot.events.httpx.AsyncClient") as mock_client_cls,
        ):
            self._setup_mock_client(mock_client_cls, mock_settings, post_return=httpx.Response(400))

            with caplog.at_level(logging.WARNING, logger="homepilot.events"):
                lc = ArtifactLifecycle(store=mock_store)
                aid = await lc.propose(_make_spec())

        assert aid == "2025-01-01-test-webhook-abc123"
        assert any("Webhook returned status=400" in r.message for r in caplog.records)

    async def test_webhook_returns_5xx(self, mock_store, caplog):
        with (
            patch("homepilot.events.get_settings") as mock_settings,
            patch("homepilot.events.httpx.AsyncClient") as mock_client_cls,
        ):
            self._setup_mock_client(mock_client_cls, mock_settings, post_return=httpx.Response(500))

            with caplog.at_level(logging.WARNING, logger="homepilot.events"):
                lc = ArtifactLifecycle(store=mock_store)
                aid = await lc.propose(_make_spec())

        assert aid == "2025-01-01-test-webhook-abc123"
        assert any("Webhook returned status=500" in r.message for r in caplog.records)
