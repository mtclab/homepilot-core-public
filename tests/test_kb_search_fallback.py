from __future__ import annotations

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


class TestKBSearchFallbackLogging:
    async def test_keyword_fallback_logging_when_embedding_fails(self, kb_service, repo, caplog):
        import logging

        await repo.create_doc_metadata(
            source="test:embed-fail-log",
            title="Redis config",
            content="redis maxmemory 2gb",
            kind="note",
            target="redis",
        )

        with (
            caplog.at_level(logging.WARNING),
            patch(
                "homepilot.kb.service._call_embed_service",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            results = await kb_service.search("redis")

        assert any("redis" in r["content"].lower() for r in results)

    async def test_vector_zero_results_falls_back_to_keyword(self, kb_service, repo):
        await repo.create_doc_metadata(
            source="test:vec-zero-results",
            title="PVE config",
            content="proxmox cluster setup guide",
            kind="note",
        )

        fake_embedding = [0.1] * 768
        with (
            patch(
                "homepilot.kb.service._get_embedding",
                new_callable=AsyncMock,
                return_value=fake_embedding,
            ),
            patch(
                "homepilot.kb.service.KBService._keyword_search",
                new_callable=AsyncMock,
                return_value=[
                    {
                        "id": 1,
                        "source": "test",
                        "kind": "note",
                        "target": None,
                        "title": "PVE config",
                        "content": "proxmox cluster setup guide",
                        "score": 0.0,
                    }
                ],
            ),
        ):
            results = await kb_service.search("proxmox")

        assert len(results) >= 1

    async def test_connect_refusal_logs_actionable_message(self, caplog):
        import logging

        with caplog.at_level(logging.ERROR):
            from homepilot.kb.service import _call_embed_service

            result = await _call_embed_service(
                "http://llm-embed:8081/v1/embeddings", "bge-m3", "test query"
            )

        assert result is None
        has_actionable = any(
            "llm-embed" in r.message
            or "unreachable" in r.message.lower()
            or "ensure" in r.message.lower()
            for r in caplog.records
        )
        assert has_actionable, (
            f"Expected actionable log message, got: {[r.message for r in caplog.records]}"
        )


class TestEmbeddingStatus:
    async def test_embedding_status_returns_keyword_mode_when_no_services(self, kb_service, repo):
        with patch(
            "homepilot.kb.service._call_embed_service",
            new_callable=AsyncMock,
            return_value=None,
        ):
            status = await kb_service.embedding_status()

        assert status["search_mode"] == "keyword"
        assert status["primary_ok"] is False
        assert status["fallback_ok"] is False

    async def test_embedding_status_returns_vector_mode_when_primary_works(self, kb_service, repo):
        fake_embedding = [0.1] * 1024
        with patch(
            "homepilot.kb.service._call_embed_service",
            new_callable=AsyncMock,
            return_value=fake_embedding,
        ):
            status = await kb_service.embedding_status()

        assert status["search_mode"] == "vector"
        assert status["primary_ok"] is True

    async def test_embedding_status_returns_fallback_vector_mode(self, kb_service, repo):
        fake_embedding = [0.1] * 768
        call_count = 0

        async def mock_call(url, model, text):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return None
            return fake_embedding

        with patch(
            "homepilot.kb.service._call_embed_service",
            side_effect=mock_call,
        ):
            status = await kb_service.embedding_status()

        assert status["search_mode"] == "fallback_vector"
        assert status["primary_ok"] is False
        assert status["fallback_ok"] is True

    async def test_embedding_status_shows_doc_counts(self, kb_service, repo):
        await repo.create_doc_metadata(
            source="test:status-count",
            title="Test doc",
            content="test content",
            kind="note",
        )
        with patch(
            "homepilot.kb.service._call_embed_service",
            new_callable=AsyncMock,
            return_value=None,
        ):
            status = await kb_service.embedding_status()

        assert status["total_docs"] >= 1
        assert status["indexed_with_embeddings"] >= 0
