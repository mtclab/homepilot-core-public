"""The KB vector index follows the embedding model's dimension (#777).

What happened: the embedding model setting was a 4096-dimension model. The
sqlite-vec table had been created `embedding float[768]` and was never looked at
again, so every document failed to store ("Dimension mismatch ... Expected 768
dimensions but received 4096") while `reindex_kb` said `status: completed`,
`errors: 0`. The selfcheck and the embedding status only tested reachability,
so the model was "ok" throughout.

These run against a real migrated database with sqlite-vec loaded and a fake
embedder of another size, and assert what an operator is told.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from homepilot.artifacts.lifecycle import ArtifactLifecycle
from homepilot.artifacts.store import ArtifactStore
from homepilot.executor.kb_note import _store_embedding
from homepilot.kb.service import KBService
from homepilot.kb.vector_index import index_dimension
from homepilot.selfcheck import STATE_UNREACHABLE, ProbeVerdict, _embeddings_subsystem

pytestmark = pytest.mark.asyncio

EMBED_URL = "http://embed.example.com/api/embeddings"


@pytest.fixture
async def db(tmp_path: Path):
    from homepilot.db.connection import Database
    from homepilot.db.migrations import run_migrations

    database = Database(str(tmp_path / "kb.db"))
    await database.connect()
    await run_migrations(database)
    yield database
    await database.close()


@pytest.fixture
async def repo(db):
    from homepilot.db.repository import Repository

    return Repository(db)


@pytest.fixture
async def kb(repo, tmp_path: Path) -> KBService:
    d = tmp_path / "artifacts"
    d.mkdir()
    store = ArtifactStore(d)
    lifecycle = ArtifactLifecycle(store, repo)
    service = KBService(repo=repo, store=store, lifecycle=lifecycle)
    lifecycle._kb_service = service
    return service


def _vector(dimensions: int, seed: float = 0.1) -> list[float]:
    return [seed] * dimensions


async def _doc(repo: Any, name: str) -> int:
    doc_id = await repo.create_doc_metadata(
        source=f"ingest:{name}", title=name, content=f"{name} content", kind="note"
    )
    return int(doc_id)


async def _vectors(repo: Any) -> int:
    row = await repo.db.fetchone("SELECT COUNT(*) AS c FROM vec_docs")
    return int(row["c"])


async def test_an_empty_index_takes_the_models_dimension(repo, kb) -> None:
    """A fresh install pointed at a 1024-dim model: nothing to lose, so it fits."""
    doc_id = await _doc(repo, "fresh")
    assert await index_dimension(repo.db) == 768

    with patch("homepilot.kb.service._get_embedding", AsyncMock(return_value=_vector(1024))):
        stored = await kb._embed_doc(doc_id, "fresh content")

    assert stored, "a 1024-dim vector could not be stored into an empty index"
    assert await index_dimension(repo.db) == 1024
    assert await _vectors(repo) == 1


async def test_a_populated_index_refuses_another_size_and_names_both(repo) -> None:
    """One insert never rebuilds an index that holds vectors: that is a reindex's call."""
    kept = await _doc(repo, "old-model")
    await _store_embedding(repo, kept, _vector(768))
    newer = await _doc(repo, "new-model")

    with pytest.raises(ValueError, match=r"1024 dimensions.*holds 768"):
        await _store_embedding(repo, newer, _vector(1024))

    assert await index_dimension(repo.db) == 768
    assert await _vectors(repo) == 1, "a refused insert must not cost other documents"


async def test_reindex_rebuilds_the_index_for_a_new_model_and_says_so(repo, kb) -> None:
    """The model changed to 1024 dims. The reindex fits the index and re-embeds all."""
    old = await _doc(repo, "embedded-by-the-old-model")
    await _store_embedding(repo, old, _vector(768))
    await _doc(repo, "never-embedded")

    with patch("homepilot.kb.service._get_embedding", AsyncMock(return_value=_vector(1024))):
        result = await kb.reindex(reason="manual")

    assert result["vector_index_rebuilt"] == {"from": 768, "to": 1024, "vectors_dropped": 1}
    assert await index_dimension(repo.db) == 1024
    assert await _vectors(repo) == 2, "every document is embedded by the new model"
    assert result["embedding_failures"] == 0
    assert result["status"] == "completed", result


async def test_a_reindex_whose_inserts_fail_does_not_say_completed(repo, kb) -> None:
    await _doc(repo, "cannot-store")

    with (
        patch("homepilot.kb.service._get_embedding", AsyncMock(return_value=_vector(768))),
        patch(
            "homepilot.executor.kb_note._store_embedding",
            AsyncMock(side_effect=sqlite3.OperationalError("disk I/O error")),
        ),
    ):
        result = await kb.reindex(reason="manual")

    assert result["status"] == "completed_with_errors", (
        "every embedding failed to store and the reindex called itself completed"
    )
    assert result["embedding_store_failures"] == 1
    assert "disk I/O error" in (result["embedding_first_failure"] or "")


async def test_a_vector_search_names_the_mismatch(repo, kb, caplog) -> None:
    doc_id = await _doc(repo, "nginx")
    await _store_embedding(repo, doc_id, _vector(768))

    with (
        patch("homepilot.kb.service._get_embedding", AsyncMock(return_value=_vector(1024))),
        caplog.at_level("WARNING"),
    ):
        results = await kb.search("nginx")

    assert results, "the keyword pass must still answer"
    assert any("KB reindex" in r.getMessage() for r in caplog.records), (
        "the fallback did not say why the vector search could not run"
    )


async def _effective(key: str, _settings: Any) -> str:
    return {"embedding_service_url": EMBED_URL, "embedding_model": "big-model"}.get(key, "")


async def test_the_embedding_status_reports_a_model_that_does_not_fit(repo, kb) -> None:
    with (
        patch("homepilot.kb.service.effective", side_effect=_effective),
        patch(
            "homepilot.kb.service._call_embed_service",
            AsyncMock(return_value=_vector(4096)),
        ),
    ):
        status = await kb.embedding_status()

    assert status["primary_ok"] is True
    assert status["model_dimension"] == 4096
    assert status["index_dimension"] == 768
    assert status["dimension_ok"] is False
    assert status["search_mode"] == "keyword"


async def test_the_selfcheck_is_not_ok_for_a_model_that_does_not_fit(repo) -> None:
    settings = SimpleNamespace(
        embedding_service_url=EMBED_URL,
        embedding_fallback_url="",
        embedding_model="big-model",
        embedding_fallback_model="",
    )
    subsystem = _embeddings_subsystem(settings, SimpleNamespace(repo=repo))
    assert subsystem.probe is not None

    with patch("homepilot.kb.service._call_embed_service", AsyncMock(return_value=_vector(4096))):
        verdict = await subsystem.probe()

    assert isinstance(verdict, ProbeVerdict), "a 4096-dim model was reported ok over a 768 index"
    assert verdict.state == STATE_UNREACHABLE
    assert "4096" in verdict.consequence
    assert "768" in verdict.consequence
