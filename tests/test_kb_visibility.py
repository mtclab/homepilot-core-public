"""The KB does not hide what you put into it (#433).

`vec_docs` is written only by the kb-note ARTIFACT executor. Ingested
documentation (`hp kb ingest`) and observed-state notes go straight to
`doc_metadata` with no embedding - and `search()` did an INNER JOIN on `vec_docs`
whenever an embedding was available. So those docs were returned only by the
keyword fallback that runs when the embedding service is DOWN: **the KB hid your
ingested documentation precisely when it was configured correctly.**

`reindex` did not fix it either; it only re-walked `source LIKE 'artifact:%'`.

Also gated here: propose wrote no audit row, so the one action that starts every
change was the one missing from the durable trail.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.db.repository import Repository
from homepilot.kb.service import KBService

pytestmark = pytest.mark.asyncio

# A stand-in embedding: what matters is that one EXISTS, not what it contains.
_EMBEDDING = [0.1] * 768


@pytest.fixture
async def kb(tmp_path: Path):
    db = Database(str(tmp_path / "homepilot.db"))
    await db.connect()
    await run_migrations(db)
    repo = Repository(db)
    service = KBService(repo=repo, store=MagicMock(), lifecycle=MagicMock())
    yield service, repo, db
    await db.close()


class TestSearchNeverHidesAnUnembeddedDoc:
    async def test_an_ingested_doc_is_found_while_embeddings_work(self, kb):
        """The headline. This doc has no embedding, so the vector query cannot
        return it - and before the fix, nothing else ran."""
        service, repo, _db = kb
        await repo.create_doc_metadata(
            source="ingest:abc",
            title="nginx runbook",
            content="reload nginx, never restart it",
            kind="note",
            target="web01",
        )

        with patch("homepilot.kb.service._get_embedding", AsyncMock(return_value=_EMBEDDING)):
            results = await service.search("nginx")

        assert [r["title"] for r in results] == ["nginx runbook"], (
            "an ingested doc was invisible to search because it had no embedding"
        )

    async def test_an_observed_state_note_is_found_too(self, kb):
        service, repo, _db = kb
        await repo.create_doc_metadata(
            source="introspect:host-1",
            title="As-found observation: db01",
            content="postgres 16, listening on 5432",
            kind="observed-state",
            target="db01",
        )

        with patch("homepilot.kb.service._get_embedding", AsyncMock(return_value=_EMBEDDING)):
            results = await service.search("postgres")

        assert results and results[0]["title"].endswith("db01")

    async def test_a_vector_hit_still_ranks_first(self, kb):
        """The merge must not demote real semantic matches: one is a meaning
        match and the other is a substring."""
        service, repo, _db = kb
        embedded_id = await repo.create_doc_metadata(
            source="artifact:2026-08-21-note",
            title="the embedded one",
            content="nginx tuning",
            kind="note",
        )
        await repo.create_doc_metadata(
            source="ingest:xyz",
            title="the keyword one",
            content="nginx tuning",
            kind="note",
        )
        await service._embed_doc(int(embedded_id), "nginx tuning")

        with patch("homepilot.kb.service._get_embedding", AsyncMock(return_value=_EMBEDDING)):
            results = await service.search("nginx")

        assert len(results) == 2
        assert results[0]["title"] == "the embedded one"

    async def test_the_limit_is_respected(self, kb):
        service, repo, _db = kb
        for n in range(6):
            await repo.create_doc_metadata(
                source=f"ingest:{n}", title=f"doc {n}", content="nginx", kind="note"
            )

        with patch("homepilot.kb.service._get_embedding", AsyncMock(return_value=_EMBEDDING)):
            results = await service.search("nginx", limit=3)

        assert len(results) == 3


class TestSemanticSearchActuallyRuns:
    """It never had. sqlite-vec requires the `k` constraint on the vec0 table's
    OWN query - a LIMIT on an outer join does not count - so every search raised
    "A LIMIT or 'k = ?' constraint is required on vec0 knn queries" and the
    handler quietly returned keyword results with a debug warning. HomePilot's
    semantic KB search has been keyword-only in silence."""

    async def test_the_vector_query_returns_rows_rather_than_raising(self, kb, caplog):
        service, repo, _db = kb
        doc_id = await repo.create_doc_metadata(
            source="artifact:2026-08-21-note",
            title="nginx tuning",
            content="worker_processes auto",
            kind="note",
        )
        with patch("homepilot.kb.service._get_embedding", AsyncMock(return_value=_EMBEDDING)):
            await service._embed_doc(int(doc_id), "nginx tuning")

            with caplog.at_level("WARNING"):
                results = await service.search("nginx")

        assert results, "search returned nothing at all"
        assert results[0]["score"] > 0, (
            "the top hit has no distance-derived score, so it came from the "
            "keyword fallback - the vector query is still failing"
        )
        assert not any("falling back to keyword" in r.message for r in caplog.records), (
            "the vector query raised and was silently swallowed"
        )


class TestEverythingGetsEmbedded:
    async def test_embed_missing_covers_docs_reindex_never_touched(self, kb):
        """`reindex` only re-walked artifact notes, so ingested docs stayed
        unembedded however often an operator ran it."""
        service, repo, db = kb
        await repo.create_doc_metadata(
            source="ingest:abc", title="a doc", content="content", kind="note"
        )

        with patch("homepilot.kb.service._get_embedding", AsyncMock(return_value=_EMBEDDING)):
            result = await service.embed_missing()

        assert result["embedded"] == 1
        row = await db.fetchone("SELECT COUNT(*) AS c FROM vec_docs")
        assert row["c"] == 1

    async def test_it_does_not_re_embed_what_is_already_indexed(self, kb):
        service, repo, _db = kb
        doc_id = await repo.create_doc_metadata(
            source="ingest:abc", title="a doc", content="content", kind="note"
        )
        with patch("homepilot.kb.service._get_embedding", AsyncMock(return_value=_EMBEDDING)):
            await service._embed_doc(int(doc_id), "content")

            result = await service.embed_missing()

        assert result["embedded"] == 0

    async def test_a_doc_that_cannot_be_embedded_is_still_searchable(self, kb):
        """Best-effort embedding, never a lost document."""
        service, repo, _db = kb
        await repo.create_doc_metadata(
            source="ingest:abc", title="unembeddable", content="nginx", kind="note"
        )

        with patch("homepilot.kb.service._get_embedding", AsyncMock(return_value=None)):
            results = await service.search("nginx")

        assert [r["title"] for r in results] == ["unembeddable"]


class TestProposeIsInTheTrail:
    async def test_proposing_writes_an_audit_row(self, tmp_path: Path):
        """Propose wrote an event and a git commit and no audit row - so the one
        action that starts every change was the one missing from the durable
        trail an operator reads."""
        from homepilot.artifacts.lifecycle import ArtifactLifecycle
        from homepilot.artifacts.store import ArtifactStore

        db = Database(str(tmp_path / "propose.db"))
        await db.connect()
        await run_migrations(db)
        repo = Repository(db)
        try:
            lifecycle = ArtifactLifecycle(
                store=ArtifactStore(tmp_path / "artifacts"), repository=repo
            )
            await lifecycle.propose(
                {
                    "id": "2026-08-21-audited-propose",
                    "kind": "kb-note",
                    "intent": "A note worth recording",
                    "body": "hello",
                    "produced_by": {"session": "s", "agent": "a", "user": "operator"},
                }
            )

            rows = await repo.query_audit_log(action="propose")

            assert rows, "proposing left no audit row"
            assert rows[0]["artifact_id"] == "2026-08-21-audited-propose"
            assert rows[0]["user_id"] == "operator", "the audit row lost who proposed it"
        finally:
            await db.close()
