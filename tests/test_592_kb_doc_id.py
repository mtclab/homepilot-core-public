"""#592: get/update/delete_kb_doc accept the id record_fact returns.

The KB has two id spaces. `record_fact` writes a kb-note ARTIFACT and returns
its string slug id (indexed as source `artifact:<slug>`), but the sibling
read/update/delete handlers ran a bare `int(doc_id)` on the KB-INDEX integer row
id. Feeding back the id `record_fact` just returned therefore crashed with a raw
`invalid literal for int() with base 10: '...'` surfaced to the caller.

These gates drive the REAL stack (Repository + ArtifactStore + ArtifactLifecycle
+ KBService), not mocks, so the artifact -> doc_metadata -> resolver path is
genuinely exercised.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from homepilot.artifacts.lifecycle import ArtifactLifecycle
from homepilot.artifacts.store import ArtifactStore
from homepilot.kb.service import KBService
from homepilot.mcp.tools.kb_tools import (
    handle_delete_kb_doc,
    handle_get_kb_doc,
    handle_record_fact,
    handle_update_kb_doc,
)


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
    svc = KBService(repo=repo, store=artifact_store, lifecycle=lc)
    # Wire the back-reference so revoke() triggers a reindex, as app_state does.
    lc._kb_service = svc
    return svc


@pytest.fixture
async def ctx(repo, artifact_store, kb_service):
    return {
        "repo": repo,
        "store": artifact_store,
        "lifecycle": kb_service.lifecycle,
        "kb_service": kb_service,
        "_mcp_caller_id": "test-caller",
    }


async def _record_and_index(ctx: dict, *, target: str, content: str, kind: str = "note") -> str:
    """record_fact then make sure it is indexed, returning the artifact-slug id."""
    result = await handle_record_fact({"target": target, "kind": kind, "content": content}, ctx)
    # record_fact schedules a background reindex; force it deterministically so
    # the doc_metadata row exists before we read it back.
    await ctx["kb_service"].reindex(no_embeddings=True, reason="test")
    return result["id"]


class TestGetKbDocRoundTrips:
    async def test_record_fact_then_get_by_returned_id(self, ctx):
        """The exact broken journey: the id record_fact returns must read back."""
        fact_id = await _record_and_index(ctx, target="web01", content="web01 runs nginx 1.24")
        assert not fact_id.isdigit(), "record_fact must return the artifact slug, not an int"

        row = await handle_get_kb_doc({"doc_id": fact_id}, ctx)

        assert row["source"] == f"artifact:{fact_id}"
        assert row["content"] == "web01 runs nginx 1.24"

    async def test_get_by_artifact_prefixed_id(self, ctx):
        fact_id = await _record_and_index(ctx, target="db01", content="db01 is postgres 16")
        row = await handle_get_kb_doc({"doc_id": f"artifact:{fact_id}"}, ctx)
        assert row["content"] == "db01 is postgres 16"

    async def test_get_by_integer_row_id_still_works(self, ctx):
        fact_id = await _record_and_index(ctx, target="cache01", content="cache01 runs redis")
        by_slug = await handle_get_kb_doc({"doc_id": fact_id}, ctx)
        row_id = int(by_slug["id"])

        by_int = await handle_get_kb_doc({"doc_id": row_id}, ctx)
        assert by_int["id"] == row_id
        # A plain-digit string is the integer id too.
        by_str = await handle_get_kb_doc({"doc_id": str(row_id)}, ctx)
        assert by_str["id"] == row_id


class TestGarbageIdIsCleanError:
    async def test_unknown_slug_is_not_found_not_int_crash(self, ctx):
        with pytest.raises(ValueError) as exc:
            await handle_get_kb_doc({"doc_id": "2026-01-01-kb-nope-000000"}, ctx)
        msg = str(exc.value)
        assert "not found" in msg
        assert "invalid literal for int" not in msg

    async def test_unknown_integer_is_not_found(self, ctx):
        with pytest.raises(ValueError) as exc:
            await handle_get_kb_doc({"doc_id": 999999}, ctx)
        assert "not found" in str(exc.value)

    async def test_pure_garbage_is_not_found_not_int_crash(self, ctx):
        with pytest.raises(ValueError) as exc:
            await handle_get_kb_doc({"doc_id": "not-a-real-id"}, ctx)
        assert "invalid literal for int" not in str(exc.value)
        assert "not found" in str(exc.value)


class TestDeleteByArtifactId:
    async def test_delete_by_slug_sticks_across_reindex(self, ctx):
        """Deleting an artifact-backed row must revoke the artifact, else the next
        reindex resurrects it."""
        fact_id = await _record_and_index(ctx, target="tmp01", content="ephemeral fact")

        result = await handle_delete_kb_doc({"doc_id": fact_id}, ctx)
        assert result["deleted"] is True

        # Gone now...
        with pytest.raises(ValueError):
            await handle_get_kb_doc({"doc_id": fact_id}, ctx)

        # ...and STAYS gone after a reindex (the artifact was revoked, not just
        # the mirror row dropped).
        await ctx["kb_service"].reindex(no_embeddings=True, reason="test-after-delete")
        with pytest.raises(ValueError):
            await handle_get_kb_doc({"doc_id": fact_id}, ctx)

    async def test_delete_by_integer_row_id_still_works(self, ctx):
        fact_id = await _record_and_index(ctx, target="tmp02", content="another fact")
        row = await handle_get_kb_doc({"doc_id": fact_id}, ctx)
        row_id = int(row["id"])

        result = await handle_delete_kb_doc({"doc_id": row_id}, ctx)
        assert result["deleted"] is True

    async def test_delete_unknown_is_clean_error(self, ctx):
        with pytest.raises(ValueError) as exc:
            await handle_delete_kb_doc({"doc_id": "2026-01-01-kb-ghost-000000"}, ctx)
        assert "invalid literal for int" not in str(exc.value)
        assert "not found" in str(exc.value)


class TestUpdateByArtifactId:
    async def test_update_of_artifact_row_is_refused_with_actionable_error(self, ctx):
        """Artifact-backed rows are immutable: an in-place edit would be silently
        overwritten by the reindex, so the tool refuses and points at supersede."""
        fact_id = await _record_and_index(ctx, target="web02", content="web02 runs apache")

        with pytest.raises(ValueError) as exc:
            await handle_update_kb_doc({"doc_id": fact_id, "content": "changed"}, ctx)
        msg = str(exc.value)
        assert "invalid literal for int" not in msg
        assert "supersede" in msg.lower()
        assert fact_id in msg

        # The stored content is untouched by the refused edit.
        row = await handle_get_kb_doc({"doc_id": fact_id}, ctx)
        assert row["content"] == "web02 runs apache"

    async def test_update_of_integer_ingested_row_works(self, ctx):
        """A non-artifact row (the real record, not a mirror) edits in place."""
        row_id = await ctx["repo"].create_doc_metadata(
            source="ingest:deadbeef",
            title="handbook",
            content="original",
            kind="note",
            target="global",
        )
        assert row_id is not None

        result = await handle_update_kb_doc({"doc_id": row_id, "content": "revised"}, ctx)
        assert result["content"] == "revised"
        # And by its plain-digit string id too.
        result2 = await handle_update_kb_doc({"doc_id": str(row_id), "title": "handbook v2"}, ctx)
        assert result2["title"] == "handbook v2"

    async def test_update_unknown_is_clean_error(self, ctx):
        with pytest.raises(ValueError) as exc:
            await handle_update_kb_doc({"doc_id": "nope-nope", "content": "x"}, ctx)
        assert "invalid literal for int" not in str(exc.value)
        assert "not found" in str(exc.value)
