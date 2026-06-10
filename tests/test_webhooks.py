from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from homepilot.config import Settings
from homepilot.webhooks.send import build_artifact_payload, send_webhook, sign_payload


def _sample_frontmatter(**overrides):
    fm = {
        "id": "2025-01-01-test-webhook-abc123",
        "kind": "ansible-playbook",
        "intent": "Configure VM networking",
        "status": "proposed",
        "mutating": True,
        "produced_by": {"session": "s1", "agent": "coder", "user": "admin"},
        "target": {"kind": "vm", "vmid": 100, "node": "pve1"},
        "idempotence": "via-precheck",
    }
    fm.update(overrides)
    return fm


class TestBuildArtifactPayload:
    def test_basic_payload_fields(self):
        fm = _sample_frontmatter()
        payload = build_artifact_payload("artifact_proposed", fm)
        assert payload["event"] == "artifact_proposed"
        assert payload["id"] == "2025-01-01-test-webhook-abc123"
        assert payload["kind"] == "ansible-playbook"
        assert payload["intent"] == "Configure VM networking"
        assert payload["status"] == "proposed"
        assert payload["mutating"] is True
        assert payload["source"] == {"session": "s1", "agent": "coder", "user": "admin"}
        assert "timestamp" in payload
        assert payload["target"] == {"kind": "vm", "vmid": 100, "node": "pve1"}
        assert payload["idempotence"] == "via-precheck"

    def test_payload_without_optional_fields(self):
        fm = {"id": "2025-01-01-minimal", "kind": "shell-script", "status": "applied"}
        payload = build_artifact_payload("artifact_applied", fm)
        assert payload["event"] == "artifact_applied"
        assert "target" not in payload
        assert "idempotence" not in payload
        assert "superseded_by" not in payload
        assert "drift_summary" not in payload

    def test_payload_with_superseded_by(self):
        fm = _sample_frontmatter(status="superseded", superseded_by="2025-01-02-replacement-def456")
        payload = build_artifact_payload("artifact_superseded", fm)
        assert payload["superseded_by"] == "2025-01-02-replacement-def456"

    def test_payload_with_drift_summary(self):
        fm = _sample_frontmatter(drift_summary="Config file changed on host")
        payload = build_artifact_payload("artifact_drifted", fm)
        assert payload["drift_summary"] == "Config file changed on host"

    def test_payload_extra_fields(self):
        fm = _sample_frontmatter()
        payload = build_artifact_payload("artifact_proposed", fm, {"custom_field": "value"})
        assert payload["custom_field"] == "value"

    def test_payload_approved_by(self):
        fm = _sample_frontmatter(approved_by={"user": "admin", "at": "2025-01-01T12:00:00Z"})
        payload = build_artifact_payload("artifact_approved", fm)
        assert payload["approved_by"]["user"] == "admin"

    def test_payload_rejected_by(self):
        fm = _sample_frontmatter(rejected_by={"user": "admin", "at": "2025-01-01T12:00:00Z"})
        payload = build_artifact_payload("artifact_rejected", fm)
        assert payload["rejected_by"]["user"] == "admin"

    def test_payload_revoked_by(self):
        fm = _sample_frontmatter(
            revoked_by={"user": "admin", "at": "2025-01-01T12:00:00Z", "reason": "obsolete"}
        )
        payload = build_artifact_payload("artifact_revoked", fm)
        assert payload["revoked_by"]["reason"] == "obsolete"


class TestSignPayload:
    def test_sign_payload_matches_hmac_sha256(self):
        import hashlib
        import hmac as hmac_mod

        secret = "my-secret-key"
        payload_bytes = b'{"event": "test"}'
        sig = sign_payload(payload_bytes, secret)
        expected = hmac_mod.new(secret.encode(), payload_bytes, hashlib.sha256).hexdigest()
        assert sig == expected

    def test_sign_payload_different_keys(self):
        payload_bytes = b'{"event": "test"}'
        sig1 = sign_payload(payload_bytes, "key1")
        sig2 = sign_payload(payload_bytes, "key2")
        assert sig1 != sig2


class TestSendWebhook:
    async def test_send_webhook_posts_to_configured_url(self):
        mock_post = AsyncMock(return_value=httpx.Response(200, text="OK"))
        with (
            patch("homepilot.config.get_settings") as mock_settings,
            patch("homepilot.webhooks.send.httpx.AsyncClient") as mock_client_cls,
        ):
            mock_settings.return_value = Settings(
                secret_key="test",
                events_webhook_url="https://hooks.example.com/artifacts",
            )
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = mock_post
            mock_client_cls.return_value = mock_client

            fm = _sample_frontmatter()
            await send_webhook("artifact_proposed", fm)

        mock_post.assert_called_once()
        call_args = mock_post.call_args
        body = json.loads(call_args[1]["content"])
        assert body["event"] == "artifact_proposed"
        assert body["id"] == "2025-01-01-test-webhook-abc123"

    async def test_send_webhook_includes_hmac_signature_header(self):
        mock_post = AsyncMock(return_value=httpx.Response(200, text="OK"))
        with (
            patch("homepilot.config.get_settings") as mock_settings,
            patch("homepilot.webhooks.send.httpx.AsyncClient") as mock_client_cls,
        ):
            mock_settings.return_value = Settings(
                secret_key="test",
                events_webhook_url="https://hooks.example.com/artifacts",
                events_webhook_secret="my-secret-value",
            )
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = mock_post
            mock_client_cls.return_value = mock_client

            fm = _sample_frontmatter()
            await send_webhook("artifact_proposed", fm)

        call_args = mock_post.call_args
        body = call_args[1]["content"]
        expected_sig = sign_payload(body, "my-secret-value")
        assert call_args[1]["headers"]["X-Webhook-Signature"] == expected_sig

    async def test_send_webhook_no_signature_header_when_secret_unset(self):
        mock_post = AsyncMock(return_value=httpx.Response(200, text="OK"))
        with (
            patch("homepilot.config.get_settings") as mock_settings,
            patch("homepilot.webhooks.send.httpx.AsyncClient") as mock_client_cls,
        ):
            mock_settings.return_value = Settings(
                secret_key="test",
                events_webhook_url="https://hooks.example.com/artifacts",
                events_webhook_secret=None,
            )
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = mock_post
            mock_client_cls.return_value = mock_client

            fm = _sample_frontmatter()
            await send_webhook("artifact_proposed", fm)

        call_args = mock_post.call_args
        assert "X-Webhook-Signature" not in call_args[1]["headers"]
        assert "X-HP-Webhook-Secret" not in call_args[1]["headers"]

    async def test_send_webhook_noop_when_url_unset(self):
        with (
            patch("homepilot.config.get_settings") as mock_settings,
            patch("homepilot.webhooks.send.httpx.AsyncClient") as mock_client_cls,
        ):
            mock_settings.return_value = Settings(
                secret_key="test",
                events_webhook_url=None,
            )

            fm = _sample_frontmatter()
            await send_webhook("artifact_proposed", fm)

        mock_client_cls.assert_not_called()

    async def test_send_webhook_failure_does_not_raise(self):
        import httpx

        mock_post = AsyncMock(side_effect=httpx.ConnectError("connection refused"))
        with (
            patch("homepilot.config.get_settings") as mock_settings,
            patch("homepilot.webhooks.send.httpx.AsyncClient") as mock_client_cls,
        ):
            mock_settings.return_value = Settings(
                secret_key="test",
                events_webhook_url="https://hooks.example.com/artifacts",
            )
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = mock_post
            mock_client_cls.return_value = mock_client

            fm = _sample_frontmatter()
            await send_webhook("artifact_proposed", fm)

    async def test_send_webhook_posts_full_metadata(self):
        mock_post = AsyncMock(return_value=httpx.Response(200, text="OK"))
        with (
            patch("homepilot.config.get_settings") as mock_settings,
            patch("homepilot.webhooks.send.httpx.AsyncClient") as mock_client_cls,
        ):
            mock_settings.return_value = Settings(
                secret_key="test",
                events_webhook_url="https://hooks.example.com/artifacts",
            )
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = mock_post
            mock_client_cls.return_value = mock_client

            fm = _sample_frontmatter()
            await send_webhook("artifact_proposed", fm)

        call_args = mock_post.call_args
        payload = json.loads(call_args[1]["content"])
        assert payload["id"] == "2025-01-01-test-webhook-abc123"
        assert payload["kind"] == "ansible-playbook"
        assert payload["intent"] == "Configure VM networking"
        assert payload["status"] == "proposed"
        assert payload["mutating"] is True
        assert payload["target"] == {"kind": "vm", "vmid": 100, "node": "pve1"}
        assert "timestamp" in payload
        assert payload["source"] == {"session": "s1", "agent": "coder", "user": "admin"}

    async def test_send_webhook_uses_content_not_json(self):
        mock_post = AsyncMock(return_value=httpx.Response(200, text="OK"))
        with (
            patch("homepilot.config.get_settings") as mock_settings,
            patch("homepilot.webhooks.send.httpx.AsyncClient") as mock_client_cls,
        ):
            mock_settings.return_value = Settings(
                secret_key="test",
                events_webhook_url="https://hooks.example.com/artifacts",
            )
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = mock_post
            mock_client_cls.return_value = mock_client

            fm = _sample_frontmatter()
            await send_webhook("artifact_proposed", fm)

        call_args = mock_post.call_args
        assert "content" in call_args[1]
        assert isinstance(call_args[1]["content"], bytes)


class TestLifecycleHooksEmitEvents:
    async def test_approve_calls_emit_event(self):
        import homepilot.artifacts.lifecycle as lifecycle_mod

        mock_emit = AsyncMock()
        with patch.object(lifecycle_mod, "emit_event", mock_emit):
            from homepilot.artifacts.lifecycle import ArtifactLifecycle

            store = MagicMock()
            lc = ArtifactLifecycle(store=store)
            fm = {
                "id": "2025-01-01-wh-approve-abc123",
                "kind": "ansible-playbook",
                "intent": "test",
                "status": "approved",
                "mutating": True,
                "target": {"kind": "vm", "vmid": 100, "node": "pve1"},
                "idempotence": "via-precheck",
                "produced_by": {"session": "s1", "agent": "a1", "user": "u1"},
                "hash": "sha256:abc",
                "approved_by": {"user": "admin", "at": "2025-01-01T12:00:00Z"},
            }
            mock_transitions = MagicMock()
            mock_transitions.approve = AsyncMock()
            lc._transitions = mock_transitions
            store.read.return_value = (fm, "body")

            await lc.approve("2025-01-01-wh-approve-abc123", "admin")

        mock_emit.assert_called_once()
        assert mock_emit.call_args[0][0] == "artifact_approved"
        assert mock_emit.call_args[0][1]["id"] == "2025-01-01-wh-approve-abc123"

    async def test_reject_calls_emit_event(self):
        import homepilot.artifacts.lifecycle as lifecycle_mod

        mock_emit = AsyncMock()
        with patch.object(lifecycle_mod, "emit_event", mock_emit):
            from homepilot.artifacts.lifecycle import ArtifactLifecycle

            store = MagicMock()
            lc = ArtifactLifecycle(store=store)
            fm = {
                "id": "2025-01-01-wh-reject-abc123",
                "kind": "ansible-playbook",
                "intent": "test",
                "status": "rejected",
                "mutating": True,
                "target": {"kind": "vm", "vmid": 100, "node": "pve1"},
                "idempotence": "via-precheck",
                "produced_by": {"session": "s1", "agent": "a1", "user": "u1"},
                "hash": "sha256:abc",
                "rejected_by": {"user": "admin", "at": "2025-01-01T12:00:00Z"},
            }
            mock_transitions = MagicMock()
            mock_transitions.reject = AsyncMock()
            lc._transitions = mock_transitions
            store.read.return_value = (fm, "body")

            await lc.reject("2025-01-01-wh-reject-abc123", "admin")

        mock_emit.assert_called_once()
        assert mock_emit.call_args[0][0] == "artifact_rejected"

    async def test_mark_applied_calls_emit_event(self):
        import homepilot.artifacts.lifecycle as lifecycle_mod

        mock_emit = AsyncMock()
        with patch.object(lifecycle_mod, "emit_event", mock_emit):
            from homepilot.artifacts.lifecycle import ArtifactLifecycle

            store = MagicMock()
            lc = ArtifactLifecycle(store=store)
            fm = {
                "id": "2025-01-01-wh-apply-abc123",
                "kind": "ansible-playbook",
                "intent": "test",
                "status": "applied",
                "mutating": False,
                "target": {"kind": "vm", "vmid": 100, "node": "pve1"},
                "idempotence": "via-precheck",
                "produced_by": {"session": "s1", "agent": "a1", "user": "u1"},
                "hash": "sha256:abc",
            }
            mock_transitions = MagicMock()
            mock_transitions.mark_applied = AsyncMock()
            lc._transitions = mock_transitions
            store.read.return_value = (fm, "body")

            await lc.mark_applied("2025-01-01-wh-apply-abc123", "log")

        mock_emit.assert_called_once()
        assert mock_emit.call_args[0][0] == "artifact_applied"

    async def test_mark_failed_calls_emit_event(self):
        import homepilot.artifacts.lifecycle as lifecycle_mod

        mock_emit = AsyncMock()
        with patch.object(lifecycle_mod, "emit_event", mock_emit):
            from homepilot.artifacts.lifecycle import ArtifactLifecycle

            store = MagicMock()
            lc = ArtifactLifecycle(store=store)
            fm = {
                "id": "2025-01-01-wh-fail-abc123",
                "kind": "ansible-playbook",
                "intent": "test",
                "status": "failed",
                "mutating": False,
                "produced_by": {"session": "s1", "agent": "a1", "user": "u1"},
                "hash": "sha256:abc",
            }
            mock_transitions = MagicMock()
            mock_transitions.mark_failed = AsyncMock()
            lc._transitions = mock_transitions
            store.read.return_value = (fm, "body")

            await lc.mark_failed("2025-01-01-wh-fail-abc123", "error reason")

        mock_emit.assert_called_once()
        assert mock_emit.call_args[0][0] == "artifact_failed"

    async def test_supersede_calls_emit_event(self):
        import homepilot.artifacts.lifecycle as lifecycle_mod

        mock_emit = AsyncMock()
        with patch.object(lifecycle_mod, "emit_event", mock_emit):
            from homepilot.artifacts.lifecycle import ArtifactLifecycle

            store = MagicMock()
            lc = ArtifactLifecycle(store=store)
            fm = {
                "id": "2025-01-01-wh-sup-abc123",
                "kind": "ansible-playbook",
                "intent": "test",
                "status": "superseded",
                "mutating": True,
                "target": {"kind": "vm", "vmid": 100, "node": "pve1"},
                "idempotence": "via-precheck",
                "produced_by": {"session": "s1", "agent": "a1", "user": "u1"},
                "hash": "sha256:abc",
                "superseded_by": "2025-01-02-new-def456",
            }
            mock_transitions = MagicMock()
            mock_transitions.supersede = AsyncMock()
            lc._transitions = mock_transitions
            store.read.return_value = (fm, "body")

            await lc.supersede("2025-01-01-wh-sup-abc123", "2025-01-02-new-def456")

        mock_emit.assert_called_once()
        assert mock_emit.call_args[0][0] == "artifact_superseded"
        assert mock_emit.call_args[0][1]["superseded_by"] == "2025-01-02-new-def456"

    async def test_revoke_calls_emit_event(self):
        import homepilot.artifacts.lifecycle as lifecycle_mod

        mock_emit = AsyncMock()
        with patch.object(lifecycle_mod, "emit_event", mock_emit):
            from homepilot.artifacts.lifecycle import ArtifactLifecycle

            store = MagicMock()
            lc = ArtifactLifecycle(store=store)
            fm = {
                "id": "2025-01-01-wh-revoke-abc123",
                "kind": "ansible-playbook",
                "intent": "test",
                "status": "revoked",
                "mutating": True,
                "target": {"kind": "vm", "vmid": 100, "node": "pve1"},
                "idempotence": "via-precheck",
                "produced_by": {"session": "s1", "agent": "a1", "user": "u1"},
                "hash": "sha256:abc",
                "revoked_by": {"user": "admin", "at": "2025-01-01T12:00:00Z"},
            }
            mock_transitions = MagicMock()
            mock_transitions.revoke = AsyncMock()
            lc._transitions = mock_transitions
            store.read.return_value = (fm, "body")

            await lc.revoke("2025-01-01-wh-revoke-abc123", "admin")

        mock_emit.assert_called_once()
        assert mock_emit.call_args[0][0] == "artifact_revoked"

    async def test_emit_event_failure_does_not_crash_lifecycle(self):
        import homepilot.artifacts.lifecycle as lifecycle_mod

        mock_emit = AsyncMock(side_effect=httpx.ConnectError("webhook down"))
        with patch.object(lifecycle_mod, "emit_event", mock_emit):
            from homepilot.artifacts.lifecycle import ArtifactLifecycle

            store = MagicMock()
            lc = ArtifactLifecycle(store=store)
            fm = {
                "id": "2025-01-01-wh-approve-abc123",
                "kind": "ansible-playbook",
                "intent": "test",
                "status": "approved",
                "mutating": True,
                "approved_by": {"user": "admin"},
            }
            mock_transitions = MagicMock()
            mock_transitions.approve = AsyncMock()
            lc._transitions = mock_transitions
            store.read.return_value = (fm, "body")

            await lc.approve("2025-01-01-wh-approve-abc123", "admin")

    async def test_store_read_failure_does_not_crash_lifecycle(self):
        import homepilot.artifacts.lifecycle as lifecycle_mod

        mock_emit = AsyncMock()
        with patch.object(lifecycle_mod, "emit_event", mock_emit):
            from homepilot.artifacts.lifecycle import ArtifactLifecycle

            store = MagicMock()
            lc = ArtifactLifecycle(store=store)
            mock_transitions = MagicMock()
            mock_transitions.approve = AsyncMock()
            lc._transitions = mock_transitions
            store.read.side_effect = FileNotFoundError("gone")

            await lc.approve("2025-01-01-wh-approve-abc123", "admin")
