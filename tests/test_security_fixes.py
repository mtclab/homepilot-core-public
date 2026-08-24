import ipaddress
import logging
import os
import shutil
import tempfile
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from homepilot.adapters.ssh import GuestHostError, _validate_hostname
from homepilot.auth.deps import require_scope
from homepilot.config import Settings
from homepilot.db.repository import _sanitize_audit_field
from homepilot.db.utils import escape_like
from homepilot.main import _get_client_ip
from homepilot.vault.manager import VaultManager

age_available = shutil.which("age") is not None


class TestVaultPassphraseFile:
    async def test_reads_file_strips_newline(self, tmp_path):
        pf = tmp_path / "pass.txt"
        pf.write_text("testphrase\n")
        s = Settings(
            vault_passphrase_file=str(pf),
            vault_passphrase="",
        )
        assert s.vault_passphrase == "testphrase"

    async def test_logs_when_only_env_var_set(self, caplog):
        with caplog.at_level(logging.DEBUG, logger="homepilot.config"):
            Settings(
                vault_passphrase="testpassphrase",
                vault_passphrase_file="",
            )
        assert any("HP_VAULT_PASSPHRASE" in r.message for r in caplog.records)


@pytest.mark.skipif(not age_available, reason="age binary not installed")
class TestVaultTempFile:
    async def test_mkstemp_prefix(self, tmp_path):
        VaultManager(tmp_path, "test-passphrase")
        fd, path = tempfile.mkstemp(suffix=".age", prefix="hp_vault_")
        try:
            assert "hp_vault_" in os.path.basename(path)
        finally:
            os.close(fd)
            os.unlink(path)

    async def test_decrypt_uses_mkstemp_path_pattern(self, tmp_path):
        vm = VaultManager(tmp_path, "test-passphrase")
        identity_data = b"# age identity file\n# public key: age1test\nKEY\n"
        import os as _os

        salt = _os.urandom(16)
        protected = vm._protect_identity(identity_data, salt)
        protected_file = vm._identities_dir / "master.protected"
        protected_file.write_bytes(protected)
        _os.chmod(str(protected_file), 0o600)
        assert protected_file.exists()


class TestPathTraversal:
    async def test_rejects_traversal(self, real_store):
        with pytest.raises(ValueError, match="escapes store root"):
            real_store.resolve_path("2026-05-07/../../../../etc/passwd")

    async def test_normal_id_passes(self, real_store):
        path = real_store.resolve_path("2026-05-07-test-abc123")
        assert path.name == "2026-05-07-test-abc123.md"


class TestHostnameValidation:
    async def test_valid_simple(self):
        _validate_hostname("web1")

    async def test_valid_fqdn(self):
        _validate_hostname("my-host.example.com")

    async def test_rejects_newline(self):
        with pytest.raises(GuestHostError, match="Invalid hostname"):
            _validate_hostname("evil\nhost")

    async def test_rejects_semicolon(self):
        with pytest.raises(GuestHostError, match="Invalid hostname"):
            _validate_hostname("host;cmd")

    async def test_rejects_dotdot_slash(self):
        with pytest.raises(GuestHostError, match="Invalid hostname"):
            _validate_hostname("../../../evil")

    async def test_rejects_empty(self):
        with pytest.raises(GuestHostError, match="Invalid hostname"):
            _validate_hostname("")


class TestAuditSanitization:
    async def test_escapes_newline(self):
        assert _sanitize_audit_field("line1\nline2") == "line1\\nline2"

    async def test_escapes_carriage_return(self):
        assert _sanitize_audit_field("a\rb") == "a\\rb"

    async def test_escapes_tab(self):
        assert _sanitize_audit_field("a\tb") == "a\\tb"

    async def test_none_passthrough(self):
        assert _sanitize_audit_field(None) is None

    async def test_non_string_passthrough(self):
        assert _sanitize_audit_field(42) == 42


class TestLikeEscape:
    async def test_percent(self):
        assert escape_like("%") == "\\%"

    async def test_underscore(self):
        assert escape_like("_") == "\\_"

    async def test_backslash_percent(self):
        assert escape_like("\\%") == "\\\\\\%"

    async def test_normal_passthrough(self):
        assert escape_like("hostname") == "hostname"

    async def test_shared_function_consistency(self):
        assert escape_like("a%b_c") == "a\\%b\\_c"


class TestTrustedProxies:
    def _make_request(self, peer_ip, xff=None):
        request = MagicMock()
        request.client = MagicMock()
        request.client.host = peer_ip
        if xff is not None:
            request.headers = {"x-forwarded-for": xff}
        else:
            request.headers = {}
        return request

    async def test_no_trusted_proxies_returns_peer(self):
        req = self._make_request("10.0.0.1")
        assert _get_client_ip(req, []) == "10.0.0.1"

    async def test_trusted_proxy_matching_returns_xff(self):
        req = self._make_request("10.0.0.1", xff="1.2.3.4, 10.0.0.1")
        nets = [ipaddress.ip_network("10.0.0.0/8")]
        assert _get_client_ip(req, nets) == "1.2.3.4"

    async def test_trusted_proxy_not_matching_returns_peer(self):
        req = self._make_request("192.168.1.1", xff="1.2.3.4")
        nets = [ipaddress.ip_network("10.0.0.0/8")]
        assert _get_client_ip(req, nets) == "192.168.1.1"

    async def test_invalid_xff_falls_back_to_peer(self):
        req = self._make_request("10.0.0.1", xff="not-an-ip, 10.0.0.1")
        nets = [ipaddress.ip_network("10.0.0.0/8")]
        assert _get_client_ip(req, nets) == "10.0.0.1"


class TestRateLimiterExcludesUiRoutes:
    """Rate limiter must NOT apply to /ui/* (static assets) or /health."""

    def _make_request(self, path: str, client_ip: str = "1.2.3.4") -> MagicMock:
        req = MagicMock()
        req.url.path = path
        req.client.host = client_ip
        req.headers = {}
        return req

    async def test_ui_path_bypasses_rate_limiter(self):
        from homepilot.main import _RATE_WINDOW, rate_limit_middleware

        _RATE_WINDOW.clear()
        calls: list[str] = []

        async def call_next(req: MagicMock) -> MagicMock:
            calls.append(req.url.path)
            from fastapi.responses import JSONResponse

            return JSONResponse({"ok": True})

        req = self._make_request("/ui/inventory")
        import time as _time

        _RATE_WINDOW["1.2.3.4"] = [_time.time()] * 200
        resp = await rate_limit_middleware(req, call_next)
        # Should NOT be 429 — UI routes bypass the limiter
        assert resp.status_code == 200
        assert calls == ["/ui/inventory"]
        _RATE_WINDOW.clear()

    async def test_health_path_bypasses_rate_limiter(self):
        from homepilot.main import _RATE_WINDOW, rate_limit_middleware

        _RATE_WINDOW.clear()
        calls: list[str] = []

        async def call_next(req: MagicMock) -> MagicMock:
            calls.append(req.url.path)
            from fastapi.responses import JSONResponse

            return JSONResponse({"ok": True})

        req = self._make_request("/health")
        import time as _time

        _RATE_WINDOW["1.2.3.4"] = [_time.time()] * 200
        resp = await rate_limit_middleware(req, call_next)
        assert resp.status_code == 200
        _RATE_WINDOW.clear()

    async def test_api_path_still_rate_limited(self):
        from homepilot.main import _RATE_WINDOW, rate_limit_middleware

        _RATE_WINDOW.clear()
        calls: list[str] = []

        async def call_next(req: MagicMock) -> MagicMock:
            calls.append(req.url.path)
            from fastapi.responses import JSONResponse

            return JSONResponse({"ok": True})

        req = self._make_request("/inventory")
        import time as _time

        _RATE_WINDOW["1.2.3.4"] = [_time.time()] * 200
        resp = await rate_limit_middleware(req, call_next)
        # API route SHOULD get 429
        assert resp.status_code == 429
        assert calls == []
        _RATE_WINDOW.clear()

    async def test_authenticated_bearer_still_rate_limited(self):
        from homepilot.main import _RATE_WINDOW, rate_limit_middleware

        _RATE_WINDOW.clear()

        req = self._make_request("/inventory")
        req.headers = {"authorization": "Bearer hp_sometoken123"}
        req.cookies = {}
        req.app = MagicMock()
        repo = MagicMock()
        repo.get_token_by_prefix = AsyncMock(return_value=None)
        req.app.state.repo = repo
        import time as _time

        from homepilot.main import _RATE_WINDOW_AUTH as _AW

        # Two-lane limiter (#518): junk credentials fall back to the anonymous
        # lane once verification fires. Seed both lanes so the fallback is what
        # gets tested.
        _AW["1.2.3.4"] = [_time.time()] * 200
        _RATE_WINDOW["1.2.3.4"] = [_time.time()] * 200
        resp = await rate_limit_middleware(req, MagicMock(return_value=MagicMock(status_code=200)))
        assert resp.status_code == 429
        _RATE_WINDOW.clear()

    async def test_valid_bearer_has_higher_rate_limit(self):
        from homepilot.main import (
            _AUTH_RATE_LIMIT,
            _RATE_LIMIT,
            _RATE_WINDOW,
            rate_limit_middleware,
        )

        _RATE_WINDOW.clear()
        calls: list[str] = []

        async def call_next(req: MagicMock) -> MagicMock:
            calls.append(req.url.path)
            from fastapi.responses import JSONResponse

            return JSONResponse({"ok": True})

        req = self._make_request("/inventory")
        req.headers = {"authorization": "Bearer hp_validtoken"}
        req.cookies = {}
        req.app = MagicMock()
        from homepilot.auth.tokens import hash_token

        token_hash = hash_token("hp_validtoken")
        repo = MagicMock()
        repo.get_token_by_prefix = AsyncMock(
            return_value={"id": 1, "hash": token_hash, "user_id": 1, "expires_at": None}
        )
        req.app.state.repo = repo
        import time as _time

        # Two-lane limiter (#518): credentialed traffic is counted in its own
        # window. Up to the auth limit passes ...
        from homepilot.main import _RATE_WINDOW_AUTH

        _RATE_WINDOW_AUTH["1.2.3.4"] = [_time.time()] * _RATE_LIMIT
        resp = await rate_limit_middleware(req, call_next)
        assert resp.status_code == 200
        assert calls == ["/inventory"]

        # ... but exceeding the authenticated limit still gets 429
        _RATE_WINDOW_AUTH["1.2.3.4"] = [_time.time()] * _AUTH_RATE_LIMIT
        resp = await rate_limit_middleware(req, MagicMock(return_value=MagicMock(status_code=200)))
        assert resp.status_code == 429
        _RATE_WINDOW.clear()
        _RATE_WINDOW_AUTH.clear()

    async def test_csrf_token_header_does_not_bypass_rate_limiter(self):
        from homepilot.main import _RATE_WINDOW, rate_limit_middleware

        _RATE_WINDOW.clear()

        req = self._make_request("/inventory")
        req.headers = {"x-csrf-token": "some-csrf-value"}
        req.cookies = {}
        req.app = MagicMock()
        repo = MagicMock()
        repo.get_token_by_prefix = AsyncMock(return_value=None)
        req.app.state.repo = repo
        import time as _time

        _RATE_WINDOW["1.2.3.4"] = [_time.time()] * 200
        resp = await rate_limit_middleware(req, MagicMock(return_value=MagicMock(status_code=200)))
        assert resp.status_code == 429
        _RATE_WINDOW.clear()

    async def test_cookie_auth_with_valid_token_has_higher_rate_limit(self):
        from homepilot.main import (
            _AUTH_RATE_LIMIT,
            _RATE_LIMIT,
            _RATE_WINDOW,
            rate_limit_middleware,
        )

        _RATE_WINDOW.clear()
        calls: list[str] = []

        async def call_next(req: MagicMock) -> MagicMock:
            calls.append(req.url.path)
            from fastapi.responses import JSONResponse

            return JSONResponse({"ok": True})

        req = self._make_request("/inventory")
        req.headers = {"x-csrf-token": "csrf123"}
        req.cookies = {"hp_token": "hp_cookievalid", "hp_csrf": "csrf123"}
        req.app = MagicMock()
        from homepilot.auth.tokens import hash_token

        token_hash = hash_token("hp_cookievalid")
        repo = MagicMock()
        repo.get_token_by_prefix = AsyncMock(
            return_value={"id": 1, "hash": token_hash, "user_id": 1, "expires_at": None}
        )
        req.app.state.repo = repo
        import time as _time

        from homepilot.main import _RATE_WINDOW_AUTH

        # Within auth limit → allowed (two-lane limiter: the credentialed lane)
        req.method = "GET"
        _RATE_WINDOW_AUTH["1.2.3.4"] = [_time.time()] * _RATE_LIMIT
        resp = await rate_limit_middleware(req, call_next)
        assert resp.status_code == 200

        # Exceeding auth limit → 429
        calls.clear()
        _RATE_WINDOW_AUTH["1.2.3.4"] = [_time.time()] * _AUTH_RATE_LIMIT
        resp = await rate_limit_middleware(req, MagicMock(return_value=MagicMock(status_code=200)))
        assert resp.status_code == 429
        _RATE_WINDOW.clear()
        _RATE_WINDOW_AUTH.clear()

    async def test_cookie_auth_without_csrf_still_rate_limited(self):
        from homepilot.main import _RATE_WINDOW, rate_limit_middleware

        _RATE_WINDOW.clear()

        req = self._make_request("/inventory")
        req.headers = {}
        req.cookies = {"hp_token": "hp_cookievalid"}
        req.app = MagicMock()
        from homepilot.auth.tokens import hash_token

        token_hash = hash_token("hp_cookievalid")
        repo = MagicMock()
        repo.get_token_by_prefix = AsyncMock(
            return_value={"id": 1, "hash": token_hash, "user_id": 1, "expires_at": None}
        )
        req.app.state.repo = repo
        import time as _time

        from homepilot.main import _RATE_WINDOW_AUTH as _AW

        # Two-lane limiter (#518): junk credentials fall back to the anonymous
        # lane once verification fires. Seed both lanes so the fallback is what
        # gets tested.
        _AW["1.2.3.4"] = [_time.time()] * 200
        _RATE_WINDOW["1.2.3.4"] = [_time.time()] * 200
        resp = await rate_limit_middleware(req, MagicMock(return_value=MagicMock(status_code=200)))
        assert resp.status_code == 429
        _RATE_WINDOW.clear()

    async def test_invalid_bearer_still_rate_limited(self):
        from homepilot.main import _RATE_WINDOW, rate_limit_middleware

        _RATE_WINDOW.clear()

        req = self._make_request("/inventory")
        req.headers = {"authorization": "Bearer hp_fake123"}
        req.cookies = {}
        req.app = MagicMock()
        repo = MagicMock()
        repo.get_token_by_prefix = AsyncMock(return_value=None)
        req.app.state.repo = repo
        import time as _time

        from homepilot.main import _RATE_WINDOW_AUTH as _AW

        # Two-lane limiter (#518): junk credentials fall back to the anonymous
        # lane once verification fires. Seed both lanes so the fallback is what
        # gets tested.
        _AW["1.2.3.4"] = [_time.time()] * 200
        _RATE_WINDOW["1.2.3.4"] = [_time.time()] * 200
        resp = await rate_limit_middleware(req, MagicMock(return_value=MagicMock(status_code=200)))
        assert resp.status_code == 429
        _RATE_WINDOW.clear()

    async def test_auth_login_always_rate_limited(self):
        from homepilot.main import _RATE_WINDOW, rate_limit_middleware

        _RATE_WINDOW.clear()
        calls: list[str] = []

        async def call_next(req: MagicMock) -> MagicMock:
            calls.append(req.url.path)
            from fastapi.responses import JSONResponse

            return JSONResponse({"ok": True})

        req = self._make_request("/auth/login")
        req.headers = {}
        req.cookies = {}
        req.app = MagicMock()
        from homepilot.auth.tokens import hash_token

        token_hash = hash_token("hp_sometoken123")
        repo = MagicMock()
        repo.get_token_by_prefix = AsyncMock(
            return_value={"id": 1, "hash": token_hash, "user_id": 1, "expires_at": None}
        )
        req.app.state.repo = repo
        import time as _time

        _RATE_WINDOW["1.2.3.4"] = [_time.time()] * 200
        resp = await rate_limit_middleware(req, call_next)
        assert resp.status_code == 429
        _RATE_WINDOW.clear()


class TestTokenScope:
    async def test_read_only_rejected_for_write(self):
        checker = require_scope("write")
        token = {"scope": "read_only"}
        with pytest.raises(HTTPException) as exc_info:
            await checker(token)
        assert exc_info.value.status_code == 403

    async def test_wildcard_passes_all(self):
        checker = require_scope("write")
        token = {"scope": "*"}
        result = await checker(token)
        assert result is token

    async def test_matching_scope_passes(self):
        checker = require_scope("read")
        token = {"scope": "read,write"}
        result = await checker(token)
        assert result is token

    async def test_none_scope_denied(self):
        checker = require_scope("write")
        token = {"scope": None}
        with pytest.raises(HTTPException) as exc_info:
            await checker(token)
        assert exc_info.value.status_code == 403
