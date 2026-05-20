from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.db.repository import Repository


@pytest.fixture
async def db_repo(tmp_path):
    db_path = tmp_path / "test.db"
    db = Database(str(db_path))
    await db.connect()
    await run_migrations(db)
    repo = Repository(db)
    await db.conn.commit()
    yield db, repo
    await db.close()


class TestCreateWebhookConfig:
    async def test_create_webhook_config(self, db_repo):
        db, repo = db_repo
        config_id = await repo.create_webhook_config(
            url="https://example.com/hook",
            event_types=["artifact_proposed", "artifact_approved"],
            secret="mysecret",
            max_retries=5,
        )
        await db.conn.commit()
        assert config_id > 0

        config = await repo.get_webhook_config(config_id)
        assert config is not None
        assert config["url"] == "https://example.com/hook"
        assert json.loads(config["event_types"]) == ["artifact_approved", "artifact_proposed"]
        assert config["secret"] == "mysecret"
        assert config["enabled"] == 1
        assert config["max_retries"] == 5


class TestListWebhookConfigs:
    async def test_list_webhook_configs(self, db_repo):
        db, repo = db_repo
        await repo.create_webhook_config(url="https://a.com/hook", event_types=["*"], max_retries=3)
        await repo.create_webhook_config(
            url="https://b.com/hook", event_types=["artifact_proposed"], max_retries=5
        )
        await db.conn.commit()

        configs = await repo.list_webhook_configs()
        assert len(configs) == 2
        assert configs[0]["url"] == "https://a.com/hook"
        assert configs[1]["url"] == "https://b.com/hook"


class TestGetConfigsForEvent:
    async def test_get_configs_for_event(self, db_repo):
        db, repo = db_repo
        await repo.create_webhook_config(
            url="https:// wildcard.com/hook", event_types=["*"], max_retries=3
        )
        await repo.create_webhook_config(
            url="https://specific.com/hook",
            event_types=["artifact_proposed"],
            max_retries=3,
        )
        await repo.create_webhook_config(
            url="https://other.com/hook",
            event_types=["artifact_approved"],
            max_retries=3,
        )
        await db.conn.commit()

        matches = await repo.get_webhook_configs_for_event("artifact_proposed")
        urls = [c["url"] for c in matches]
        assert "https:// wildcard.com/hook" in urls
        assert "https://specific.com/hook" in urls
        assert "https://other.com/hook" not in urls

    async def test_get_configs_for_event_wildcard(self, db_repo):
        db, repo = db_repo
        await repo.create_webhook_config(
            url="https://stars.com/hook", event_types=["*"], max_retries=3
        )
        await db.conn.commit()

        matches = await repo.get_webhook_configs_for_event("anything_at_all")
        assert len(matches) == 1
        assert matches[0]["url"] == "https://stars.com/hook"

    async def test_get_configs_excludes_disabled(self, db_repo):
        db, repo = db_repo
        config_id = await repo.create_webhook_config(
            url="https://disabled.com/hook", event_types=["*"], max_retries=3
        )
        await db.conn.commit()
        await db.execute("UPDATE webhook_configs SET enabled = 0 WHERE id = ?", (config_id,))
        await db.conn.commit()

        matches = await repo.get_webhook_configs_for_event("any_event")
        assert len(matches) == 0


class TestDeleteWebhookConfig:
    async def test_delete_webhook_config(self, db_repo):
        db, repo = db_repo
        config_id = await repo.create_webhook_config(
            url="https://delete-me.com/hook", event_types=["*"], max_retries=3
        )
        await db.conn.commit()

        result = await repo.delete_webhook_config(config_id)
        await db.conn.commit()
        assert result is True

        config = await repo.get_webhook_config(config_id)
        assert config is None

    async def test_delete_nonexistent(self, db_repo):
        _db, repo = db_repo
        result = await repo.delete_webhook_config(9999)
        assert result is False


class TestWebhookDeliveryWithRetry:
    async def test_delivery_with_retry_on_failure(self, db_repo):
        db, repo = db_repo
        from homepilot.events import deliver_with_retry

        config_id = await repo.create_webhook_config(
            url="https://flaky.com/hook", event_types=["*"], secret="testkey", max_retries=3
        )
        await db.conn.commit()

        delivery_id = await repo.create_webhook_delivery(
            webhook_id=config_id,
            event_type="test_event",
            payload='{"event":"test_event"}',
        )
        await db.conn.commit()

        with patch("homepilot.events.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(
                side_effect=[
                    httpx.ConnectError("fail1"),
                    httpx.ConnectError("fail2"),
                    AsyncMock(status_code=200),
                ]
            )
            mock_client_cls.return_value = mock_client

            with patch("homepilot.events.asyncio.sleep", new_callable=AsyncMock):
                await deliver_with_retry(
                    url="https://flaky.com/hook",
                    payload={"event": "test"},
                    secret="testkey",
                    max_retries=3,
                    delivery_id=delivery_id,
                    repo=repo,
                    db=db,
                )

        assert mock_client.post.call_count == 3

    async def test_delivery_records_success(self, db_repo):
        db, repo = db_repo
        from homepilot.events import deliver_with_retry

        config_id = await repo.create_webhook_config(
            url="https://ok.com/hook", event_types=["*"], max_retries=3
        )
        await db.conn.commit()

        delivery_id = await repo.create_webhook_delivery(
            webhook_id=config_id,
            event_type="test_event",
            payload='{"event":"test_event"}',
        )
        await db.conn.commit()

        with patch("homepilot.events.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(return_value=AsyncMock(status_code=200))
            mock_client_cls.return_value = mock_client

            await deliver_with_retry(
                url="https://ok.com/hook",
                payload={"event": "test"},
                secret=None,
                max_retries=3,
                delivery_id=delivery_id,
                repo=repo,
                db=db,
            )

        delivery = await db.fetchone(
            "SELECT * FROM webhook_deliveries WHERE id = ?", (delivery_id,)
        )
        assert delivery["status"] == "success"

    async def test_delivery_records_failure_after_retries(self, db_repo):
        db, repo = db_repo
        from homepilot.events import deliver_with_retry

        config_id = await repo.create_webhook_config(
            url="https://always-fail.com/hook", event_types=["*"], max_retries=2
        )
        await db.conn.commit()

        delivery_id = await repo.create_webhook_delivery(
            webhook_id=config_id,
            event_type="test_event",
            payload='{"event":"test_event"}',
        )
        await db.conn.commit()

        with patch("homepilot.events.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(side_effect=httpx.ConnectError("always fails"))
            mock_client_cls.return_value = mock_client

            with patch("homepilot.events.asyncio.sleep", new_callable=AsyncMock):
                await deliver_with_retry(
                    url="https://always-fail.com/hook",
                    payload={"event": "test"},
                    secret=None,
                    max_retries=2,
                    delivery_id=delivery_id,
                    repo=repo,
                    db=db,
                )

        delivery = await db.fetchone(
            "SELECT * FROM webhook_deliveries WHERE id = ?", (delivery_id,)
        )
        assert delivery["status"] == "failed"
        assert delivery["attempts"] >= 3


class TestHmacSignature:
    def test_sign_payload(self):
        from homepilot.events import sign_payload

        secret = "my-secret-key"
        payload = b'{"event": "test"}'
        sig = sign_payload(payload, secret)

        import hashlib
        import hmac as hmac_mod

        expected = hmac_mod.new(secret.encode(), payload, hashlib.sha256).hexdigest()
        assert sig == expected

    def test_sign_payload_different_keys(self):
        from homepilot.events import sign_payload

        payload = b'{"event": "test"}'
        sig1 = sign_payload(payload, "key1")
        sig2 = sign_payload(payload, "key2")
        assert sig1 != sig2

    async def test_signature_header_sent(self, db_repo):
        db, repo = db_repo
        from homepilot.events import deliver_with_retry

        _config_id = await repo.create_webhook_config(
            url="https://signed.com/hook",
            event_types=["*"],
            secret="signing-key",
            max_retries=3,
        )
        await db.conn.commit()

        captured_headers: dict[str, str] = {}

        async def fake_post(url, content=None, headers=None):
            if headers:
                captured_headers.update(headers)
            return AsyncMock(status_code=200)

        with patch("homepilot.events.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = fake_post
            mock_client_cls.return_value = mock_client

            await deliver_with_retry(
                url="https://signed.com/hook",
                payload={"event": "test"},
                secret="signing-key",
                max_retries=3,
            )

        assert "X-Webhook-Signature" in captured_headers
        from homepilot.events import sign_payload

        expected_sig = sign_payload(json.dumps({"event": "test"}).encode(), "signing-key")
        assert captured_headers["X-Webhook-Signature"] == expected_sig

    async def test_no_signature_without_secret(self, db_repo):
        _db, _repo = db_repo
        from homepilot.events import deliver_with_retry

        captured_headers: dict[str, str] = {}

        async def fake_post(url, content=None, headers=None):
            if headers:
                captured_headers.update(headers)
            return AsyncMock(status_code=200)

        with patch("homepilot.events.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = fake_post
            mock_client_cls.return_value = mock_client

            await deliver_with_retry(
                url="https://unsigned.com/hook",
                payload={"event": "test"},
                secret=None,
                max_retries=3,
            )

        assert "X-Webhook-Signature" not in captured_headers
