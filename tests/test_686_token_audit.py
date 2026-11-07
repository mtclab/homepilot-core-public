"""Minting, revoking and using credentials leaves a durable audit row (#686).

`POST /auth/tokens`, `DELETE /auth/tokens/{prefix}`, MCP `delete_auth_token`,
`POST /claim`, `POST /auth/login` and the CLI's direct mints never called
`log_audit`: the actions that create and destroy fleet-root credentials were
the ones missing from the trail an operator reads. And the one revoke log line
named `auth.get("prefix", "admin-secret")` while `require_token`'s dict had no
`prefix`, so every revoke by a token was attributed to "admin-secret".

Each row names WHO acted (the authenticated credential: label + token prefix,
or "admin-secret", or the CLI) and WHICH credential was affected (its prefix
and label - never the token itself).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI

from homepilot.auth import router as auth_router_module
from homepilot.auth.tokens import generate_api_token
from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.db.repository import Repository
from tests.test_first_run_claim import (  # noqa: F401 - fixtures
    boot,
    client_for,
    fresh_db,
    instance,
    pve,
)


async def _rows(db: Database, action: str) -> list[dict[str, Any]]:
    rows = await db.fetchall(
        "SELECT user_id, source, action, details_json FROM audit_log WHERE action = ?", (action,)
    )
    out = []
    for r in rows:
        d = dict(r)
        d["details"] = json.loads(d["details_json"]) if d["details_json"] else {}
        out.append(d)
    return out


@pytest.fixture
async def api(tmp_path: Path):
    auth_router_module._token_create_attempts.clear()
    db = Database(str(tmp_path / "hp.db"))
    await db.connect()
    await run_migrations(db)
    repo = Repository(db)
    user_id = await repo.create_user(display_name="owner", auth_source="api_token")
    admin, admin_prefix, admin_hash = generate_api_token()
    await repo.create_api_token(
        user_id=str(user_id),
        token_type="personal",
        prefix=admin_prefix,
        hash=admin_hash,
        scope="all",
        label="owner-console",
    )
    app = FastAPI()
    app.include_router(auth_router_module.router, prefix="/auth")
    app.state.repo = repo
    app.state.trusted_proxy_networks = []
    transport = httpx.ASGITransport(app=app, client=("192.168.1.20", 5000))
    async with httpx.AsyncClient(transport=transport, base_url="http://hp.test") as client:
        client.user_id = str(user_id)  # type: ignore[attr-defined]
        yield client, db, repo, admin, admin_prefix
    auth_router_module._token_create_attempts.clear()
    await db.close()


class TestRestTokenLifecycleIsAudited:
    async def test_mint_is_audited_with_minter_and_minted(self, api):
        client, db, _repo, admin, admin_prefix = api
        r = await client.post(
            "/auth/tokens",
            json={"label": "assistant", "scope": "read,write"},
            headers={"Authorization": f"Bearer {admin}"},
        )
        assert r.status_code == 201, r.text
        minted = r.json()["token"]
        rows = await _rows(db, "token_minted")
        assert len(rows) == 1, rows
        row = rows[0]
        assert admin_prefix in row["user_id"] and "owner-console" in row["user_id"]
        assert row["details"]["prefix"] == minted[:16]
        assert row["details"]["label"] == "assistant"
        assert row["details"]["scope"] == "read,write"
        assert minted not in json.dumps(row), "the token itself was written to the audit log"

    async def test_revoke_is_audited_and_names_the_revoking_token(self, api, caplog):
        client, db, repo, admin, admin_prefix = api
        _victim, victim_prefix, victim_hash = generate_api_token()
        await repo.create_api_token(
            user_id=client.user_id,
            token_type="personal",
            prefix=victim_prefix,
            hash=victim_hash,
            scope="read_only",
            label="old-laptop",
        )
        caplog.set_level("INFO", logger="homepilot.auth.router")
        r = await client.delete(
            f"/auth/tokens/{victim_prefix}", headers={"Authorization": f"Bearer {admin}"}
        )
        assert r.status_code == 204, r.text
        rows = await _rows(db, "token_revoked")
        assert len(rows) == 1, rows
        assert admin_prefix in rows[0]["user_id"], rows[0]
        assert rows[0]["details"]["prefix"] == victim_prefix
        assert rows[0]["details"]["label"] == "old-laptop"
        # The log line used to say by=admin-secret for EVERY revoke.
        revoke_lines = [m for m in caplog.messages if "Token revoked" in m]
        assert revoke_lines and all("by=admin-secret" not in m for m in revoke_lines)
        assert any(admin_prefix in m for m in revoke_lines)

    async def test_revoke_by_admin_secret_names_the_secret(self, api, monkeypatch):
        client, db, repo, _admin, _admin_prefix = api
        monkeypatch.setenv("HP_ADMIN_SECRET", "s3cret-for-test")
        from homepilot.config import get_settings

        get_settings.cache_clear()
        try:
            _victim, victim_prefix, victim_hash = generate_api_token()
            await repo.create_api_token(
                user_id=client.user_id,
                token_type="personal",
                prefix=victim_prefix,
                hash=victim_hash,
            )
            r = await client.delete(
                f"/auth/tokens/{victim_prefix}", headers={"X-HP-Admin-Secret": "s3cret-for-test"}
            )
            assert r.status_code == 204, r.text
        finally:
            get_settings.cache_clear()
        rows = await _rows(db, "token_revoked")
        assert [r["user_id"] for r in rows] == ["admin-secret"]

    async def test_login_is_audited(self, api):
        client, db, _repo, admin, admin_prefix = api
        r = await client.post("/auth/login", json={"token": admin})
        assert r.status_code == 200, r.text
        rows = await _rows(db, "login")
        assert len(rows) == 1, rows
        assert admin_prefix in rows[0]["user_id"]
        assert rows[0]["details"]["prefix"] == admin_prefix
        assert admin not in json.dumps(rows[0])

    async def test_a_failed_login_is_not_recorded_as_a_login(self, api):
        client, db, *_ = api
        r = await client.post("/auth/login", json={"token": "hp_nope0000000000000"})
        assert r.status_code == 401
        assert await _rows(db, "login") == []


class TestMcpRevokeIsAudited:
    async def test_delete_auth_token_writes_a_row_naming_the_mcp_caller(self, tmp_path: Path):
        from homepilot.mcp.server import _handle_tool

        db = Database(str(tmp_path / "hp.db"))
        await db.connect()
        await run_migrations(db)
        try:
            repo = Repository(db)
            uid = await repo.create_user(display_name="owner", auth_source="api_token")
            _t, prefix, token_hash = generate_api_token()
            await repo.create_api_token(
                user_id=str(uid), token_type="personal", prefix=prefix, hash=token_hash, label="ci"
            )
            ctx = {
                "repo": repo,
                "_mcp_token_scope": "admin",
                "_mcp_caller_id": "assistant (hp_aaaaaaaaaaaaa)",
            }
            out = await _handle_tool("delete_auth_token", {"prefix": prefix}, ctx)
            assert out["deleted"] is True
            rows = await _rows(db, "token_revoked")
            assert len(rows) == 1
            assert rows[0]["user_id"] == "assistant (hp_aaaaaaaaaaaaa)"
            assert rows[0]["source"] == "mcp"
            assert rows[0]["details"] == {"prefix": prefix, "label": "ci", "scope": None}
        finally:
            await db.close()


class TestClaimIsAudited:
    async def test_claim_records_the_claim_and_both_mints(self, instance: Any):  # noqa: F811
        await boot(instance)
        async with client_for(instance.app) as client:
            r = await client.post("/claim", json={"label": "olli-laptop"})
        assert r.status_code == 200, r.text
        token = r.json()["token"]
        claimed = await _rows(instance.db, "instance_claimed")
        assert len(claimed) == 1, claimed
        assert claimed[0]["details"]["prefix"] == token[:16]
        assert claimed[0]["details"]["client"]
        minted = await _rows(instance.db, "token_minted")
        labels = sorted(r["details"]["label"] for r in minted)
        assert "olli-laptop" in labels and len(minted) == 2, minted
        everything = json.dumps(claimed + minted)
        assert token not in everything


class TestCliBootstrapMintIsAudited:
    def test_hp_token_create_bootstrap_writes_a_row(self, tmp_path: Path):
        import sqlite3

        from typer.testing import CliRunner

        from homepilot.cli.main import app
        from homepilot.config import get_settings

        env = {
            "HP_DATA_DIR": str(tmp_path),
            "HP_PORT": "1",
            "HP_ADMIN_SECRET": "",
            "HP_ADMIN_TOKEN": "",
        }
        get_settings.cache_clear()
        try:
            with patch.dict("os.environ", env):
                result = CliRunner().invoke(app, ["token", "create", "--label", "first"])
        finally:
            get_settings.cache_clear()
        assert result.exit_code == 0, result.output
        conn = sqlite3.connect(str(tmp_path / "homepilot.db"))
        try:
            rows = conn.execute(
                "SELECT user_id, source, details_json FROM audit_log WHERE action='token_minted'"
            ).fetchall()
        finally:
            conn.close()
        assert len(rows) == 1, rows
        assert rows[0][0] == "cli" and rows[0][1] == "cli"
        assert json.loads(rows[0][2])["label"] == "first"
