from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from homepilot.artifacts.lifecycle import ArtifactLifecycle
from homepilot.artifacts.store import ArtifactStore
from homepilot.kb.service import KBService


@pytest.fixture
async def real_db(tmp_path: Path):
    from homepilot.db.connection import Database
    from homepilot.db.migrations import run_migrations

    db = Database(str(tmp_path / "test.db"))
    await db.connect()
    await run_migrations(db)
    yield db
    await db.close()


@pytest.fixture
async def repo(real_db):
    from homepilot.db.repository import Repository

    return Repository(real_db)


@pytest.fixture
def artifact_store(tmp_path: Path) -> ArtifactStore:
    d = tmp_path / "artifacts"
    d.mkdir()
    return ArtifactStore(d)


@pytest.fixture
async def kb_service(repo, artifact_store):
    lc = ArtifactLifecycle(artifact_store, repo)
    return KBService(repo=repo, store=artifact_store, lifecycle=lc)


@pytest.fixture
async def test_client(tmp_path: Path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from homepilot.db.connection import Database
    from homepilot.db.migrations import run_migrations
    from homepilot.db.repository import Repository
    from homepilot.kb.router import router as kb_router

    db = Database(str(tmp_path / "router_test.db"))
    await db.connect()
    await run_migrations(db)
    repo = Repository(db)

    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()
    store = ArtifactStore(artifact_dir)
    lc = ArtifactLifecycle(store, repo)
    kb_svc = KBService(repo=repo, store=store, lifecycle=lc)

    test_app = FastAPI()
    for route in kb_router.routes:
        for dep in getattr(route, "dependencies", []):
            test_app.dependency_overrides[dep.dependency] = lambda: {
                "user_id": "test-user",
                "token_id": "test-token",
                "display_name": "Admin",
                "scope": "admin",
            }
    test_app.include_router(kb_router, prefix="/kb")
    test_app.state.repo = repo
    test_app.state.kb_service = kb_svc

    client = TestClient(test_app, raise_server_exceptions=True)
    yield client, repo, kb_svc
    await db.close()


class TestKBReindexService:
    async def test_reindex_empty_db(self, kb_service):
        result = await kb_service.reindex(no_embeddings=True)
        assert result["deleted"] == 0
        assert result["reindexed"] == 0
        assert result["errors"] == 0

    async def test_reindex_deletes_artifact_rows(self, kb_service, repo):
        await repo.create_doc_metadata(
            source="artifact:test-note",
            title="Test",
            content="test content",
            kind="note",
        )
        await repo.create_doc_metadata(
            source="manual:something",
            title="Keep",
            content="should stay",
            kind="note",
        )
        result = await kb_service.reindex(no_embeddings=True)
        assert result["deleted"] == 1
        remaining = await repo.db.fetchall("SELECT * FROM doc_metadata")
        assert len(remaining) == 1
        assert remaining[0]["source"] == "manual:something"

    async def test_reindex_after_note_creation(self, kb_service, repo):
        with (
            patch(
                "homepilot.kb.service._get_embedding",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "homepilot.executor.kb_note._compute_embedding",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            await kb_service.record_fact(
                target="test-host",
                kind="note",
                content="Jellyfin runs on port 8096 with HW transcode.",
            )

        with patch(
            "homepilot.executor.kb_note._compute_embedding",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await kb_service.reindex(no_embeddings=True)

        assert result["reindexed"] >= 1

        with patch(
            "homepilot.kb.service._get_embedding",
            new_callable=AsyncMock,
            return_value=None,
        ):
            search_results = await kb_service.search("jellyfin")
        assert any("jellyfin" in r["content"].lower() for r in search_results)


class TestKBReindexEndpoint:
    async def test_reindex_endpoint(self, test_client):
        client, _repo, _kb_svc = test_client
        with (
            patch(
                "homepilot.kb.service._get_embedding",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "homepilot.executor.kb_note._compute_embedding",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            resp = client.post("/kb/reindex?no_embeddings=true")
        assert resp.status_code == 200
        data = resp.json()
        assert "deleted" in data
        assert "reindexed" in data
        assert "errors" in data

    async def test_reindex_endpoint_empty_db(self, test_client):
        client, _repo, _kb_svc = test_client
        resp = client.post("/kb/reindex?no_embeddings=true")
        assert resp.status_code == 200
        data = resp.json()
        assert data["deleted"] == 0
        assert data["reindexed"] == 0

    async def test_reindex_after_note_creation(self, test_client):
        client, _repo, kb_svc = test_client
        with (
            patch(
                "homepilot.kb.service._get_embedding",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "homepilot.executor.kb_note._compute_embedding",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            await kb_svc.record_fact(
                target="redis-svc",
                kind="note",
                content="Redis maxmemory 2gb policy allkeys-lru",
            )

        with patch(
            "homepilot.executor.kb_note._compute_embedding",
            new_callable=AsyncMock,
            return_value=None,
        ):
            resp = client.post("/kb/reindex?no_embeddings=true")
        assert resp.status_code == 200
        data = resp.json()
        assert data["reindexed"] >= 1


class TestReindexIfNeeded:
    async def test_reindex_if_needed_noop_when_index_matches(self, kb_service, repo):
        with patch(
            "homepilot.kb.service._get_embedding",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await kb_service.reindex_if_needed()
        assert result is None

    async def test_reindex_if_needed_triggers_when_note_missing(self, kb_service, repo):
        with (
            patch(
                "homepilot.kb.service._get_embedding",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "homepilot.executor.kb_note._compute_embedding",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            await kb_service.record_fact(
                target="reindex-need-test",
                kind="note",
                content="Verify reindex_if_needed works.",
            )

        with patch(
            "homepilot.executor.kb_note._compute_embedding",
            new_callable=AsyncMock,
            return_value=None,
        ):
            await kb_service.reindex_if_needed(reason="lifecycle")

        await asyncio.gather(*asyncio.all_tasks() - {asyncio.current_task()})

        rows = await repo.db.fetchall("SELECT * FROM doc_metadata WHERE source LIKE 'artifact:%'")
        assert len(rows) >= 1

    async def test_reindex_if_needed_accepts_reason_param(self, kb_service):
        with patch.object(kb_service, "_run_reindex", new_callable=AsyncMock) as mock_run:
            await kb_service.reindex_if_needed(reason="startup")
            mock_run.assert_not_awaited()

    async def test_reindex_reentrancy_returns_early(self, kb_service):
        result = await kb_service.reindex(no_embeddings=True, reason="manual")
        assert result["deleted"] == 0

        kb_service._reindexing = True
        try:
            result2 = await kb_service.reindex(no_embeddings=True, reason="manual")
            assert result2["status"] == "already_running"
            assert result2["reindexed"] == 0
        finally:
            kb_service._reindexing = False

    async def test_supersede_triggers_reindex_for_kb_note(self, kb_service, repo, artifact_store):
        from unittest.mock import AsyncMock, patch

        kb_service.lifecycle._kb_service = kb_service

        with (
            patch(
                "homepilot.kb.service._get_embedding",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "homepilot.executor.kb_note._compute_embedding",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            await kb_service.record_fact(
                target="supersede-reindex-test",
                kind="note",
                content="Note to be superseded.",
            )

        mock_reindex = AsyncMock(return_value=None)
        with (
            patch.object(
                kb_service.lifecycle._ensure_transitions(),
                "supersede",
                new_callable=AsyncMock,
            ),
            patch.object(
                kb_service.lifecycle.store,
                "read",
                return_value=({"kind": "kb-note", "id": "test-note"}, "body"),
            ),
            patch.object(kb_service, "reindex_if_needed", mock_reindex),
        ):
            await kb_service.lifecycle.supersede("test-note", "new-note")
            mock_reindex.assert_awaited()

    async def test_revoke_triggers_reindex_for_kb_note(self, kb_service, repo, artifact_store):
        from unittest.mock import AsyncMock, patch

        kb_service.lifecycle._kb_service = kb_service

        mock_reindex = AsyncMock(return_value=None)
        with (
            patch.object(
                kb_service.lifecycle._ensure_transitions(),
                "revoke",
                new_callable=AsyncMock,
            ),
            patch.object(
                kb_service.lifecycle.store,
                "read",
                return_value=({"kind": "kb-note", "id": "test-note"}, "body"),
            ),
            patch.object(kb_service, "reindex_if_needed", mock_reindex),
        ):
            await kb_service.lifecycle.revoke("test-note", user="admin", reason="obsolete")
            mock_reindex.assert_awaited()

    async def test_reindex_reason_logged(self, kb_service, caplog):
        import logging

        with caplog.at_level(logging.INFO):
            await kb_service.reindex(no_embeddings=True, reason="manual")
        assert "KB reindex starting: reason=manual" in caplog.text
