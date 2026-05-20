from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from homepilot.artifacts.lifecycle import ArtifactLifecycle
from homepilot.artifacts.store import ArtifactStore
from homepilot.kb.service import KBService, _get_embedding


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


class TestGetEmbeddingFallback:
    async def test_primary_succeeds_no_fallback(self):
        fake_embedding = [0.1] * 1024

        with patch(
            "homepilot.kb.service._call_embed_service",
            new_callable=AsyncMock,
            return_value=fake_embedding,
        ):
            result = await _get_embedding("test query")

        assert result == fake_embedding

    async def test_fallback_on_primary_failure(self):
        fake_embedding = [0.2] * 768

        call_count = 0

        async def mock_call_embed(url, model, text):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return None
            return fake_embedding

        with patch(
            "homepilot.kb.service._call_embed_service",
            side_effect=mock_call_embed,
        ):
            result = await _get_embedding("test query")

        assert result == fake_embedding
        assert call_count == 2

    async def test_both_fail_returns_none(self):
        with patch(
            "homepilot.kb.service._call_embed_service",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await _get_embedding("test query")

        assert result is None

    async def test_no_fallback_url_configured_primary_fails(self):
        with (
            patch("homepilot.kb.service.get_settings") as mock_settings,
            patch(
                "homepilot.kb.service._call_embed_service",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            settings = mock_settings.return_value
            settings.embedding_service_url = "http://llm-embed:8081/v1/embeddings"
            settings.embedding_model = "bge-m3"
            settings.embedding_fallback_url = ""
            settings.embedding_fallback_model = ""

            result = await _get_embedding("test query")

        assert result is None

    async def test_primary_uses_configured_model(self):
        fake_embedding = [0.1] * 1024
        captured_calls = []

        async def capture_call(url, model, text):
            captured_calls.append({"url": url, "model": model, "text": text})
            return fake_embedding

        with patch(
            "homepilot.kb.service._call_embed_service",
            side_effect=capture_call,
        ):
            await _get_embedding("hello world")

        assert len(captured_calls) >= 1
        assert captured_calls[0]["model"] == "bge-m3"

    async def test_fallback_uses_fallback_url(self):
        fake_embedding = [0.2] * 768
        captured_calls = []

        async def capture_call(url, model, text):
            captured_calls.append({"url": url, "model": model, "text": text})
            if len(captured_calls) == 1:
                return None
            return fake_embedding

        with patch(
            "homepilot.kb.service._call_embed_service",
            side_effect=capture_call,
        ):
            result = await _get_embedding("test query")

        assert result == fake_embedding
        assert len(captured_calls) == 2


class TestCallEmbedServiceProtocol:
    async def test_openai_format_parse(self):
        from homepilot.kb.service import _call_embed_service

        fake_embedding = [0.1] * 1024
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "object": "list",
            "data": [{"object": "embedding", "embedding": fake_embedding}],
        }

        with patch("homepilot.kb.service.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post.return_value = mock_resp
            mock_client_cls.return_value = mock_client

            result = await _call_embed_service(
                "http://llm-embed:8081/v1/embeddings", "bge-m3", "test query"
            )
        assert result == fake_embedding
        call_kwargs = mock_client.post.call_args
        json_payload = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
        assert json_payload["input"] == "test query"
        assert "prompt" not in json_payload

    async def test_ollama_format_parse(self):
        from homepilot.kb.service import _call_embed_service

        fake_embedding = [0.2] * 768
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"embedding": fake_embedding}

        with patch("homepilot.kb.service.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post.return_value = mock_resp
            mock_client_cls.return_value = mock_client

            result = await _call_embed_service(
                "http://localhost:11434/api/embeddings", "nomic-embed-text", "test"
            )
        assert result == fake_embedding
        call_kwargs = mock_client.post.call_args
        json_payload = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
        assert json_payload["prompt"] == "test"
        assert "input" not in json_payload

    async def test_connection_failure_returns_none(self):
        from homepilot.kb.service import _call_embed_service

        with patch("homepilot.kb.service.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(side_effect=Exception("connection refused"))
            mock_client_cls.return_value = mock_client

            result = await _call_embed_service(
                "http://llm-embed:8081/v1/embeddings", "bge-m3", "test"
            )
        assert result is None

    async def test_openai_empty_data_returns_none(self):
        from homepilot.kb.service import _call_embed_service

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"object": "list", "data": []}

        with patch("homepilot.kb.service.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post.return_value = mock_resp
            mock_client_cls.return_value = mock_client

            result = await _call_embed_service(
                "http://llm-embed:8081/v1/embeddings", "bge-m3", "test"
            )
        assert result is None

    async def test_ollama_no_embedding_key_returns_none(self):
        from homepilot.kb.service import _call_embed_service

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"model": "nomic-embed-text"}

        with patch("homepilot.kb.service.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post.return_value = mock_resp
            mock_client_cls.return_value = mock_client

            result = await _call_embed_service(
                "http://localhost:11434/api/embeddings", "nomic-embed-text", "test"
            )
        assert result is None


class TestKBSearchWithEmbeddingFallback:
    async def test_search_falls_back_to_keyword_when_all_embeddings_fail(self, kb_service, repo):
        await repo.create_doc_metadata(
            source="test:embedding-fallback",
            title="Redis config",
            content="redis maxmemory 2gb",
            kind="note",
            target="redis",
        )

        with patch(
            "homepilot.kb.service._get_embedding",
            new_callable=AsyncMock,
            return_value=None,
        ):
            results = await kb_service.search("redis")

        assert any("redis" in r["content"].lower() for r in results)

    async def test_search_uses_embedding_when_available(self, kb_service, repo):
        await repo.create_doc_metadata(
            source="test:embed-available",
            title="Embedding test",
            content="vector search test content",
            kind="note",
        )

        fake_embedding = [0.1] * 768
        with patch(
            "homepilot.kb.service._get_embedding",
            new_callable=AsyncMock,
            return_value=fake_embedding,
        ):
            results = await kb_service.search("test query")

        assert isinstance(results, list)

    async def test_no_embeddings_mode_still_works(self, kb_service, repo):
        await repo.create_doc_metadata(
            source="test:no-embed",
            title="No embed test",
            content="content without embedding",
            kind="note",
        )

        with patch(
            "homepilot.kb.service._get_embedding",
            new_callable=AsyncMock,
            return_value=None,
        ):
            results = await kb_service.search("content")

        assert any("content" in r["content"].lower() for r in results)
