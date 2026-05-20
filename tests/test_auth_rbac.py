"""Tests for RBAC / multi-user auth foundation — role-based scopes and audit trail."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import pytest_asyncio
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from homepilot.auth.deps import require_scope, require_token
from homepilot.auth.scopes import (
    ROLE_ADMIN,
    ROLE_OPERATOR,
    ROLE_VIEWER,
    SCOPE_ADMIN,
    SCOPE_READ,
    SCOPE_WRITE,
    scopes_for_role,
)
from homepilot.auth.tokens import generate_api_token, normalize_scope, scope_allows
from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.db.repository import Repository


class TestRoleScopeMapping:
    def test_viewer_role_only_has_read_scope(self):
        assert scopes_for_role(ROLE_VIEWER) == [SCOPE_READ]

    def test_operator_role_has_read_write_scope(self):
        assert scopes_for_role(ROLE_OPERATOR) == [SCOPE_READ, SCOPE_WRITE]

    def test_admin_role_has_all_scopes(self):
        assert scopes_for_role(ROLE_ADMIN) == [SCOPE_READ, SCOPE_WRITE, SCOPE_ADMIN]

    def test_unknown_role_returns_empty(self):
        assert scopes_for_role("unknown") == []

    def test_none_role_returns_empty(self):
        assert scopes_for_role(None) == []


class TestNormalizeScopeWithRole:
    def test_viewer_role_overrides_scope(self):
        assert normalize_scope("full", role=ROLE_VIEWER) == [SCOPE_READ]

    def test_operator_role_overrides_read_only(self):
        assert normalize_scope("read_only", role=ROLE_OPERATOR) == [
            SCOPE_READ,
            SCOPE_WRITE,
        ]

    def test_admin_role_overrides_scope(self):
        result = normalize_scope("read_only", role=ROLE_ADMIN)
        assert result == [SCOPE_READ, SCOPE_WRITE, SCOPE_ADMIN]

    def test_no_role_falls_back_to_scope(self):
        assert normalize_scope("full") == ["*"]

    def test_no_role_no_scope_returns_empty(self):
        assert normalize_scope(None, role=None) == []

    def test_unknown_role_falls_back_to_scope(self):
        assert normalize_scope("read_only", role="unknown") == ["read"]


class TestScopeAllowsWithRole:
    def test_viewer_allows_read(self):
        assert scope_allows("read_only", "read", role=ROLE_VIEWER) is True

    def test_viewer_blocks_write(self):
        assert scope_allows("read_only", "write", role=ROLE_VIEWER) is False

    def test_operator_allows_write(self):
        assert scope_allows("full", "write", role=ROLE_OPERATOR) is True

    def test_operator_blocks_admin(self):
        assert scope_allows("full", "admin", role=ROLE_OPERATOR) is False

    def test_admin_allows_admin(self):
        assert scope_allows("full", "admin", role=ROLE_ADMIN) is True


@pytest_asyncio.fixture()
async def rbac_app(tmp_path):
    db = Database(str(tmp_path / "rbac_test.db"))
    await db.connect()
    await run_migrations(db)
    repo = Repository(db)

    app = FastAPI()
    app.state.repo = repo

    @app.get("/read", dependencies=[Depends(require_scope(SCOPE_READ))])
    async def read_endpoint(token=Depends(require_token)):  # noqa: B008
        return {"ok": True}

    @app.post("/write", dependencies=[Depends(require_scope(SCOPE_WRITE))])
    async def write_endpoint(token=Depends(require_token)):  # noqa: B008
        return {"ok": True}

    @app.post("/admin", dependencies=[Depends(require_scope(SCOPE_ADMIN))])
    async def admin_endpoint(token=Depends(require_token)):  # noqa: B008
        return {"ok": True}

    yield app, repo
    await db.close()


class TestViewerRoleIntegration:
    @pytest.mark.asyncio
    async def test_viewer_role_only_has_read_scope(self, rbac_app):
        app, repo = rbac_app
        user_id = await repo.create_user("viewer_user", "viewer@test.com")
        full_token, prefix, token_hash = generate_api_token()
        await repo.create_api_token(
            user_id=user_id,
            token_type="api",
            prefix=prefix,
            hash=token_hash,
            scope="read_only",
            role=ROLE_VIEWER,
        )
        await repo.db.conn.commit()

        client = TestClient(app, raise_server_exceptions=True)
        assert (
            client.get("/read", headers={"Authorization": f"Bearer {full_token}"}).status_code
            == 200
        )
        assert (
            client.post("/write", headers={"Authorization": f"Bearer {full_token}"}).status_code
            == 403
        )
        assert (
            client.post("/admin", headers={"Authorization": f"Bearer {full_token}"}).status_code
            == 403
        )


class TestOperatorRoleIntegration:
    @pytest.mark.asyncio
    async def test_operator_role_has_read_write_scope(self, rbac_app):
        app, repo = rbac_app
        user_id = await repo.create_user("op_user", "op@test.com")
        full_token, prefix, token_hash = generate_api_token()
        await repo.create_api_token(
            user_id=user_id,
            token_type="api",
            prefix=prefix,
            hash=token_hash,
            scope="full",
            role=ROLE_OPERATOR,
        )
        await repo.db.conn.commit()

        client = TestClient(app, raise_server_exceptions=True)
        assert (
            client.get("/read", headers={"Authorization": f"Bearer {full_token}"}).status_code
            == 200
        )
        assert (
            client.post("/write", headers={"Authorization": f"Bearer {full_token}"}).status_code
            == 200
        )
        assert (
            client.post("/admin", headers={"Authorization": f"Bearer {full_token}"}).status_code
            == 403
        )


class TestAdminRoleIntegration:
    @pytest.mark.asyncio
    async def test_admin_role_has_all_scopes(self, rbac_app):
        app, repo = rbac_app
        user_id = await repo.create_user("admin_user", "admin@test.com")
        full_token, prefix, token_hash = generate_api_token()
        await repo.create_api_token(
            user_id=user_id,
            token_type="api",
            prefix=prefix,
            hash=token_hash,
            scope="full",
            role=ROLE_ADMIN,
        )
        await repo.db.conn.commit()

        client = TestClient(app, raise_server_exceptions=True)
        assert (
            client.get("/read", headers={"Authorization": f"Bearer {full_token}"}).status_code
            == 200
        )
        assert (
            client.post("/write", headers={"Authorization": f"Bearer {full_token}"}).status_code
            == 200
        )
        assert (
            client.post("/admin", headers={"Authorization": f"Bearer {full_token}"}).status_code
            == 200
        )


class TestApproveAuditIncludesRole:
    @pytest.mark.asyncio
    async def test_approve_audit_includes_user_role(self):
        from homepilot.artifacts.transitions import ArtifactTransitionManager

        audit_calls: list[tuple[str, str, str | None, dict | None]] = []

        async def mock_audit(action, artifact_id, user=None, details=None):
            audit_calls.append((action, artifact_id, user, details))

        fm = {
            "id": "test-artifact-1",
            "kind": "config",
            "intent": "apply",
            "status": "proposed",
            "hash": "abc123",
        }
        body = "# config"

        from contextlib import contextmanager

        fs = MagicMock()
        fs.read.return_value = (fm, body)
        fs.serialize_frontmatter.return_value = "---\nid: test\n---\n"
        fs.write.return_value = None

        @contextmanager
        def _mock_file_lock(artifact_id):
            yield

        fs.file_lock = _mock_file_lock

        tm = ArtifactTransitionManager(
            file_store=fs,
            audit_fn=mock_audit,
            db=None,
        )

        with patch("homepilot.artifacts.transitions.compute_body_hash", return_value="abc123"):
            await tm.approve("test-artifact-1", "admin_user", reason="looks good", role=ROLE_ADMIN)

        assert len(audit_calls) == 1
        action, _artifact_id, user, details = audit_calls[0]
        assert action == "approve"
        assert user == "admin_user"
        assert details is not None
        assert details.get("approved_by_role") == ROLE_ADMIN


class TestAdminScopeRequiredForAutoApply:
    def test_admin_scope_required_for_auto_apply_config(self):
        from homepilot.auth.scopes import ROLE_ADMIN, ROLE_SCOPES, SCOPE_ADMIN

        assert SCOPE_ADMIN in ROLE_SCOPES[ROLE_ADMIN]
        assert SCOPE_ADMIN not in ROLE_SCOPES[ROLE_VIEWER]
        assert SCOPE_ADMIN not in ROLE_SCOPES[ROLE_OPERATOR]

        from homepilot.config import Settings

        settings = Settings()
        assert hasattr(settings, "auto_apply_enabled")
        assert settings.auto_apply_enabled is False
