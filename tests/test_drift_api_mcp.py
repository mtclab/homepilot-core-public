from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from homepilot.artifacts.router import router as artifacts_router
from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.db.repository import Repository
from homepilot.reconciler import DriftReconciler


@pytest.fixture
async def real_db(tmp_path: Path):
    db = Database(str(tmp_path / "test_api.db"))
    await db.connect()
    await run_migrations(db)
    yield db
    await db.close()


@pytest.fixture
async def repo(real_db):
    return Repository(real_db)


@pytest.fixture
def mock_store():
    store = MagicMock()
    return store


@pytest.fixture
def mock_executor():
    executor = MagicMock()
    executor.ssh = AsyncMock()
    executor.proxmox = AsyncMock()
    executor.vault = MagicMock()
    executor.pve_nodes = []
    return executor


def _make_artifact_fm(
    artifact_id: str = "2025-01-01-test-art",
    kind: str = "shell-script",
    status: str = "applied",
    host: str = "testhost",
    **extra: object,
) -> dict:
    fm = {
        "id": artifact_id,
        "kind": kind,
        "status": status,
        "intent": "test intent",
        "target": {"host": host, "kind": "vm", "node": "pve1", "vmid": 100},
        **extra,
    }
    return fm


@pytest.fixture
def api_client(repo, mock_store, mock_executor):
    app = FastAPI()
    app.include_router(artifacts_router, prefix="/artifacts")
    app.state.repo = repo
    app.state.artifact_store = mock_store

    reconciler = DriftReconciler(mock_store, repo, executor=mock_executor, inter_check_delay=0)
    app.state.drift_reconciler = reconciler

    client = TestClient(app, raise_server_exceptions=True)
    return client


class TestGetDriftEndpointCachedRead:
    def test_get_drift_empty(self, api_client, repo):
        resp = api_client.get("/artifacts/drift")
        assert resp.status_code == 200
        data = resp.json()
        assert data["items"] == []
        assert data["total"] == 0

    def test_get_drift_returns_cached(self, api_client, repo):
        import asyncio

        asyncio.get_event_loop().run_until_complete(
            repo.upsert_drift_check("art-1", drifted=False, details_json='{"reason":"no_executor"}')
        )
        resp = api_client.get("/artifacts/drift")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["artifact_id"] == "art-1"
        assert data["items"][0]["drifted"] == 0

    def test_get_drift_filter_drifted_true(self, api_client, repo):
        import asyncio

        asyncio.get_event_loop().run_until_complete(repo.upsert_drift_check("art-a", drifted=False))
        asyncio.get_event_loop().run_until_complete(
            repo.upsert_drift_check("art-b", drifted=True, details_json='{"reason":"drifted"}')
        )
        resp = api_client.get("/artifacts/drift", params={"drifted": "true"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["artifact_id"] == "art-b"

    def test_get_drift_filter_drifted_false(self, api_client, repo):
        import asyncio

        asyncio.get_event_loop().run_until_complete(repo.upsert_drift_check("art-a", drifted=False))
        asyncio.get_event_loop().run_until_complete(repo.upsert_drift_check("art-b", drifted=True))
        resp = api_client.get("/artifacts/drift", params={"drifted": "false"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["artifact_id"] == "art-a"

    def test_get_drift_by_artifact_id(self, api_client, repo):
        import asyncio

        asyncio.get_event_loop().run_until_complete(repo.upsert_drift_check("art-x", drifted=False))
        resp = api_client.get("/artifacts/drift", params={"artifact_id": "art-x"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["artifact_id"] == "art-x"

    def test_get_drift_by_artifact_id_not_found(self, api_client, repo):
        resp = api_client.get("/artifacts/drift", params={"artifact_id": "ghost"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["items"] == []
        assert data["total"] == 0

    def test_get_drift_no_reconciler_501(self, repo, mock_store, mock_executor):
        app = FastAPI()
        app.include_router(artifacts_router, prefix="/artifacts")
        app.state.repo = repo
        app.state.artifact_store = mock_store
        app.state.drift_reconciler = None

        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get("/artifacts/drift", params={"refresh": "true"})
        assert resp.status_code == 501


class TestGetDriftEndpointRefresh:
    def test_refresh_single_artifact(self, api_client, mock_store, mock_executor, repo):
        fm = _make_artifact_fm(artifact_id="art-refresh-1", kind="shell-script", status="applied")

        mock_store.read.return_value = (fm, "script body")
        mock_store.list.return_value = [fm]

        resp = api_client.get(
            "/artifacts/drift",
            params={"refresh": "true", "artifact_id": "art-refresh-1"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["artifact_id"] == "art-refresh-1"

    def test_refresh_not_found_404(self, api_client, mock_store, mock_executor, repo):
        mock_store.read.side_effect = FileNotFoundError("not found")

        resp = api_client.get(
            "/artifacts/drift",
            params={"refresh": "true", "artifact_id": "nonexistent"},
        )
        assert resp.status_code == 404

    def test_refresh_full_cycle(self, api_client, mock_store, mock_executor, repo):
        fm = _make_artifact_fm(artifact_id="art-cycle", kind="shell-script", status="applied")

        mock_store.list.return_value = [fm]

        def side_effect(aid):
            return (fm, "script body")

        mock_store.read = MagicMock(side_effect=side_effect)

        resp = api_client.get("/artifacts/drift", params={"refresh": "true"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] >= 1


class TestMCPCheckArtifactDrift:
    def _make_ctx(self, repo, mock_store, drift_reconciler):
        return {
            "repo": repo,
            "lifecycle": MagicMock(),
            "proxmox": AsyncMock(),
            "ssh_adapter": AsyncMock(),
            "vault": MagicMock(),
            "store": mock_store,
            "kb_service": AsyncMock(),
            "drift_reconciler": drift_reconciler,
        }

    @pytest.mark.asyncio
    async def test_check_artifact_drift_success(self, repo, mock_store, mock_executor):
        fm = _make_artifact_fm(artifact_id="mcp-art-1", kind="shell-script", status="applied")
        mock_store.read.return_value = (fm, "script body")

        drift_reconciler = DriftReconciler(mock_store, repo, executor=mock_executor)
        ctx = self._make_ctx(repo, mock_store, drift_reconciler)

        from homepilot.mcp.server import _handle_tool

        result = await _handle_tool(
            "check_artifact_drift",
            {"artifact_id": "mcp-art-1"},
            ctx,
        )
        assert result["artifact_id"] == "mcp-art-1"
        assert result["drifted"] is False
        assert "verification_log" in result
        assert isinstance(result["details"], dict)

    @pytest.mark.asyncio
    async def test_check_artifact_drift_not_found(self, repo, mock_store):
        mock_store.read.side_effect = FileNotFoundError("gone")
        drift_reconciler = DriftReconciler(mock_store, repo, executor=None)
        ctx = self._make_ctx(repo, mock_store, drift_reconciler)

        from homepilot.mcp.server import _handle_tool

        with pytest.raises(ValueError, match="Artifact not found"):
            await _handle_tool(
                "check_artifact_drift",
                {"artifact_id": "missing"},
                ctx,
            )

    @pytest.mark.asyncio
    async def test_check_artifact_drift_not_configured(self, repo, mock_store):
        ctx = self._make_ctx(repo, mock_store, None)

        from homepilot.mcp.server import _handle_tool

        with pytest.raises(RuntimeError, match="DriftReconciler not configured"):
            await _handle_tool(
                "check_artifact_drift",
                {"artifact_id": "any"},
                ctx,
            )

    @pytest.mark.asyncio
    async def test_check_artifact_drift_verification_error(self, repo, mock_store):
        fm = _make_artifact_fm(artifact_id="mcp-err", kind="proxmox-api-sequence", status="applied")
        body = (
            "```yaml proxmox-api-spec\n"
            "steps:\n"
            "  - id: s1\n"
            "    method: POST\n"
            "    path: /test\n"
            "    precheck:\n"
            "      method: GET\n"
            "      path: /test\n"
            '      skip_if: "response.ok == true"\n'
            "```\n"
        )
        mock_store.read.return_value = (fm, body)

        executor = MagicMock()
        executor.proxmox = AsyncMock()
        executor.proxmox.call = AsyncMock(side_effect=httpx.ConnectError("conn refused"))

        drift_reconciler = DriftReconciler(mock_store, repo, executor=executor)
        ctx = self._make_ctx(repo, mock_store, drift_reconciler)

        from homepilot.mcp.server import _handle_tool

        result = await _handle_tool(
            "check_artifact_drift",
            {"artifact_id": "mcp-err"},
            ctx,
        )
        assert result["artifact_id"] == "mcp-err"
        assert result["drifted"] is False

    @pytest.mark.asyncio
    async def test_check_artifact_drift_generic_error_no_leak(self, repo, mock_store):
        mock_store.read.side_effect = RuntimeError("internal path /secret/key泄露")
        drift_reconciler = DriftReconciler(mock_store, repo, executor=None)
        ctx = self._make_ctx(repo, mock_store, drift_reconciler)

        from homepilot.mcp.server import _handle_tool

        with pytest.raises(RuntimeError, match=r"^Drift check failed$") as exc_info:
            await _handle_tool(
                "check_artifact_drift",
                {"artifact_id": "leak-test"},
                ctx,
            )
        assert "secret" not in str(exc_info.value)
        assert "/secret/key" not in str(exc_info.value)
