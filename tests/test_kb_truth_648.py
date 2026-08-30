"""The knowledge base tells the truth about what it holds (#648 tranche 6).

Every assertion here was written against a behaviour reproduced live on the
shipped 3.6.17 image, either on dev (10.0.0.1) or in a scratch container
running the same image with a real embedding service attached.

The class the review was looking for is #642's: a claim produced from a read or
a write that did not happen. In the KB it appeared as

  * a document returned as the answer to a query about a DIFFERENT, deleted
    document, because the vector of the deleted one was stranded on a reusable
    row id;
  * `search_mode: "vector"` over an index with zero embeddings;
  * `status: "completed", errors: 0` from a rebuild that lost a document;
  * `{"id": ...}` and `status: "applied"` for a note that was not in the
    knowledge base at all;
  * a policy panel on the approval screen showing a rule about another machine
    and hiding the rule that governs this one.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from homepilot.artifacts.lifecycle import ArtifactLifecycle
from homepilot.artifacts.store import ArtifactStore
from homepilot.executor.kb_note import _store_embedding
from homepilot.kb.service import KBService

pytestmark = pytest.mark.asyncio


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
def store(tmp_path: Path) -> ArtifactStore:
    d = tmp_path / "artifacts"
    d.mkdir()
    return ArtifactStore(d)


@pytest.fixture
async def kb(repo, store):
    lifecycle = ArtifactLifecycle(store, repo)
    service = KBService(repo=repo, store=store, lifecycle=lifecycle)
    lifecycle._kb_service = service
    return service


def _vector(seed: float) -> list[float]:
    return [seed] * 768


# ── 1. A deleted document does not answer for a live one ─────────────────────


class TestADeletedDocumentTakesItsMeaningWithIt:
    """The headline. Reproduced on 3.6.17 with a real embedding service:

    a firewall policy came back as the TOP hit, at the same score as the genuine
    match, for a query about a reboot-window policy the operator had deleted -
    because it had inherited that policy's row id and, with it, its vector.
    The only trace was `WARNING ... UNIQUE constraint failed on vec_docs primary
    key`, and `get_kb_embedding_status` reported 5 embedded documents out of 5.
    """

    async def test_deleting_a_document_deletes_its_embedding(self, repo):
        doc_id = await repo.create_doc_metadata(
            source="test:doomed", title="Doomed", content="body", kind="note"
        )
        await _store_embedding(repo, doc_id, _vector(0.5))
        assert await repo.has_embedding(doc_id)

        await repo.delete_doc_metadata(doc_id)

        assert not await repo.has_embedding(doc_id), (
            "the vector outlived its document and the next document to take "
            "this row id would inherit it"
        )
        assert await repo.count_orphan_embeddings() == 0

    async def test_a_new_document_never_inherits_a_stale_vector(self, repo):
        """The end-to-end shape, without needing to force a rowid collision."""
        first = await repo.create_doc_metadata(
            source="test:first", title="First", content="about telescopes", kind="note"
        )
        await _store_embedding(repo, first, _vector(0.9))
        await repo.delete_doc_metadata(first)

        # Take the same row id back.
        await repo.db.execute(
            "INSERT INTO doc_metadata (id, source, kind, target, title, content, embedded_at) "
            "VALUES (?, 'test:second', 'note', NULL, 'Second', 'about firewalls', '2026-01-01')",
            (first,),
        )
        await repo.db.conn.commit()

        assert not await repo.has_embedding(first), (
            "the second document is indexed under the first one's meaning"
        )

    async def test_storing_an_embedding_replaces_rather_than_collides(self, repo):
        """A bare INSERT raised UNIQUE and the failure was swallowed one frame up."""
        doc_id = await repo.create_doc_metadata(
            source="test:replace", title="T", content="c", kind="note"
        )
        await _store_embedding(repo, doc_id, _vector(0.1))
        # Second write must succeed, not raise.
        await _store_embedding(repo, doc_id, _vector(0.2))
        assert await repo.has_embedding(doc_id)

    async def test_the_status_report_counts_orphans(self, repo, kb):
        await repo.db.execute(
            "INSERT INTO vec_docs (id, embedding) VALUES (?, ?)",
            (4242, b"".join(__import__("struct").pack("<f", 0.1) for _ in range(768))),
        )
        await repo.db.conn.commit()
        with patch(
            "homepilot.kb.service._call_embed_service", new_callable=AsyncMock, return_value=None
        ):
            status = await kb.embedding_status()
        assert status["orphan_embeddings"] == 1, (
            "more embeddings than documents read as a perfectly healthy index"
        )


# ── 2. search_mode is a fact about the next query ────────────────────────────


class TestSearchModeIsEstablishedNotAsserted:
    async def test_a_result_says_how_it_was_found(self, repo, kb):
        await repo.create_doc_metadata(
            source="test:mode", title="Redis", content="redis maxmemory", kind="note"
        )
        with patch(
            "homepilot.kb.service._get_embedding", new_callable=AsyncMock, return_value=None
        ):
            results = await kb.search("redis")
        assert results
        assert all(r["search_mode"] == "keyword" for r in results)

    async def test_the_endpoint_summary_can_say_vector(self):
        """`/kb/search` read a key the service never set, so it answered
        "unknown" for every search that found anything - verified on 3.6.17 with
        vector search demonstrably running."""
        from homepilot.kb.service import summarise_search_mode

        assert summarise_search_mode([])["search_mode"] == "no_matches"
        assert summarise_search_mode([{"search_mode": "keyword"}])["search_mode"] == "keyword"
        assert summarise_search_mode([{"search_mode": "vector"}])["search_mode"] == "vector"
        mixed = summarise_search_mode([{"search_mode": "vector"}, {"search_mode": "keyword"}])
        assert mixed["search_mode"] == "vector+keyword"
        assert mixed["vector_hits"] == 1
        assert mixed["keyword_hits"] == 1
        assert "unknown" not in {
            summarise_search_mode([{"search_mode": "keyword"}])["search_mode"],
        }


# ── 3. Keyword search finds a document by its words ──────────────────────────


class TestKeywordSearchIsASearchNotASubstringTest:
    """Keyword mode is what every install runs by default (ARCHITECTURE.md: "By
    default neither URL is set, so KB search is keyword-only").

    Live on dev 3.6.17, against a note reading *"Never restart nginx on
    dev-ct-web during business hours; drain the load balancer first"*:
    `restart nginx` returned it and `nginx business hours` returned NOTHING,
    because the whole query was one `LIKE '%...%'`. The UI then said "No entries
    match the current search / filter."
    """

    async def test_words_in_any_order_find_the_document(self, repo, kb):
        await repo.create_doc_metadata(
            source="test:policy",
            title="nginx policy",
            content=(
                "Never restart nginx on dev-ct-web during business hours; "
                "drain the load balancer first."
            ),
            kind="policy",
            target="dev-ct-web",
        )
        with patch(
            "homepilot.kb.service._get_embedding", new_callable=AsyncMock, return_value=None
        ):
            hits = await kb.search("nginx business hours")
        assert len(hits) == 1, "the document is right there and search said there was nothing"

    async def test_a_term_that_is_absent_still_excludes(self, repo, kb):
        await repo.create_doc_metadata(
            source="test:policy2",
            title="nginx policy",
            content="Never restart nginx on dev-ct-web during business hours.",
            kind="policy",
        )
        with patch(
            "homepilot.kb.service._get_embedding", new_callable=AsyncMock, return_value=None
        ):
            hits = await kb.search("nginx postgresql")
        assert hits == [], "every term has to be present, or the search means nothing"


# ── 4. A vector search that matched nothing says nothing ─────────────────────


class TestVectorSearchHasARelevanceFloor:
    """A k-nearest query always returns k rows. Verified on 3.6.17:
    `q=xylophone quantum banana` came back with every document in the KB at
    score -0.4142, and a policy lookup for a host mentioned nowhere returned
    every policy there was."""

    async def test_a_distant_neighbour_is_not_a_result(self, repo, kb):
        doc_id = await repo.create_doc_metadata(
            source="test:far", title="Printer", content="DYMO label stock", kind="note"
        )
        # Orthogonal unit vectors: distance sqrt(2), score 1 - 1.414 < 0.
        a = [0.0] * 768
        a[0] = 1.0
        b = [0.0] * 768
        b[1] = 1.0
        await _store_embedding(repo, doc_id, a)

        with patch("homepilot.kb.service._get_embedding", new_callable=AsyncMock, return_value=b):
            hits = await kb.search("nothing to do with printers")
        assert hits == [], "a nearest neighbour is not the same thing as a match"


# ── 5. A rebuild that lost a document does not report success ────────────────


class TestReindexDoesNotLoseDocumentsQuietly:
    async def test_an_unreadable_artifact_does_not_delete_its_document(self, repo, kb, store):
        """ "I could not read it" is not "the operator retired it".

        `store.list` walks the filesystem and silently skips any file it cannot
        open, so an unreadable artifact is missing from the listing in exactly
        the same way a deleted one is. Reproduced on the built image: `chmod 000`
        on one artifact file produced
        `{"removed": 1, "errors": 0, "status": "completed"}` with the note gone
        from the knowledge base and nothing anywhere saying so.
        """
        artifact_id = "2026-08-30-kb-note-db01"
        doc_id = await repo.create_doc_metadata(
            source=f"artifact:{artifact_id}",
            title="db01",
            content="db01 runs PostgreSQL 16",
            kind="note",
        )
        # A file that IS there and cannot be read: the exact state `chmod 000`
        # produces, and the one `store.list` renders as "not present".
        path = store.resolve_path(artifact_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("---\nid: x\nkind: kb-note\nstatus: applied\n---\nbody\n")
        path.chmod(0o000)
        try:
            result = await kb.reindex(no_embeddings=True)
        finally:
            path.chmod(0o644)

        assert await repo.get_doc_metadata(doc_id) is not None, (
            "a document was deleted because its artifact could not be read"
        )
        assert artifact_id in result["unverified"]
        assert result["status"] == "completed_with_errors"

    async def test_a_retired_artifact_does_lose_its_document(self, repo, kb, store):
        """The other half: a note that is genuinely gone goes away."""
        artifact_id = "2026-08-30-kb-note-retired"
        doc_id = await repo.create_doc_metadata(
            source=f"artifact:{artifact_id}",
            title="retired",
            content="no longer true",
            kind="note",
        )

        result = await kb.reindex(no_embeddings=True)

        assert await repo.get_doc_metadata(doc_id) is None
        assert result["removed"] == 1
        assert result["unverified"] == []

    async def test_a_failed_note_makes_the_status_say_so(self, repo, kb, store):
        store.list = MagicMock(return_value=[{"id": "2026-08-30-broken"}])
        store.read = MagicMock(side_effect=OSError("permission denied"))

        result = await kb.reindex(no_embeddings=True)

        assert result["errors"] == 1
        assert result["status"] == "completed_with_errors", (
            'a half-done rebuild called itself "completed", which is the line an '
            "operator reads and then does not go looking"
        )
        assert any("permission denied" in line for line in result["errors_detail"])

    async def test_row_ids_survive_a_reindex(self, repo, kb, store):
        """On dev 3.6.17 doc id 1 was the nginx policy, then - after two more
        facts were recorded - the dev-ct-db policy. `list_kb` hands that id out
        and `delete_kb_doc` takes it."""
        with patch(
            "homepilot.kb.service._get_embedding", new_callable=AsyncMock, return_value=None
        ):
            first = await kb.record_fact(target="a", kind="note", content="first note")
            second = await kb.record_fact(target="b", kind="note", content="second note")
            third = await kb.record_fact(target="c", kind="note", content="third note")

        # Retire the OLDEST note, so a delete-then-reinsert rebuild compacts the
        # remaining ids down onto the row ids it just freed. That is exactly what
        # happened on dev 3.6.17, where doc 1 stopped being the note it had been.
        # The background rebuild is suppressed so this measures ONE reindex.
        with patch.object(kb, "reindex_if_needed", new_callable=AsyncMock):
            await kb.lifecycle.revoke(first, user="test", reason="retired")

        before = {
            r["source"]: r["id"]
            for r in await repo.db.fetchall("SELECT id, source FROM doc_metadata")
        }
        with patch(
            "homepilot.kb.service._get_embedding", new_callable=AsyncMock, return_value=None
        ):
            await kb.reindex(no_embeddings=True)
        after = {
            r["source"]: r["id"]
            for r in await repo.db.fetchall("SELECT id, source FROM doc_metadata")
        }

        assert f"artifact:{second}" in after
        assert f"artifact:{third}" in after
        kept = {src: doc_id for src, doc_id in after.items() if src in before}
        assert kept == {src: before[src] for src in kept}, (
            "a surviving document changed its id, so every id an operator or an "
            f"agent had already been given now points somewhere else: "
            f"{before} -> {after}"
        )


# ── 6. Recording a fact does not destroy the index ───────────────────────────


class TestRecordingAFactDoesNotWipeTheVectorIndex:
    """Reproduced on the shipped 3.6.17 image against a healthy embedding
    service: one un-indexed KB write plus a restart took
    `indexed_with_embeddings` from 2 to 0, and nothing put it back."""

    async def test_the_background_rebuild_keeps_embeddings(self, repo, kb, store):
        """Drives `_run_reindex` - the path a restart and every kb-note
        lifecycle event take - not `reindex` directly, because the wipe was the
        background path passing `no_embeddings=True` into a rebuild that deleted
        every vector first."""
        with patch(
            "homepilot.kb.service._get_embedding",
            new_callable=AsyncMock,
            return_value=_vector(0.3),
        ):
            await kb.record_fact(target="keepme", kind="note", content="a durable fact")
            embedded_before = (await repo.db.fetchone("SELECT COUNT(*) AS c FROM vec_docs"))["c"]
            assert embedded_before == 1

            await kb._run_reindex("startup")

            embedded_after = (await repo.db.fetchone("SELECT COUNT(*) AS c FROM vec_docs"))["c"]
        assert embedded_after == 1, "a rebuild deleted the vectors and did not recompute them"

    async def test_force_embeddings_recomputes_everything(self, repo, kb):
        """The supported answer to "I changed the embedding model".

        Re-embedding is keyed on the text changing, so without this an unchanged
        document keeps a vector from a model that no longer exists - and nothing
        would say so.
        """
        calls = 0

        async def counting_embed(_text):
            nonlocal calls
            calls += 1
            return _vector(0.4)

        with patch("homepilot.kb.service._get_embedding", side_effect=counting_embed):
            await kb.record_fact(target="modelchange", kind="note", content="unchanging")
            after_write = calls
            await kb.reindex(reason="manual", force_embeddings=True)

        assert calls > after_write, "the model changed and every vector stayed"
        assert (await repo.db.fetchone("SELECT COUNT(*) AS c FROM vec_docs"))["c"] == 1

    async def test_an_unchanged_note_is_not_re_embedded(self, repo, kb):
        calls = 0

        async def counting_embed(_text):
            nonlocal calls
            calls += 1
            return _vector(0.4)

        with patch("homepilot.kb.service._get_embedding", side_effect=counting_embed):
            await kb.record_fact(target="stable", kind="note", content="unchanging")
            after_write = calls
            await kb.reindex(reason="manual")
        assert calls == after_write, (
            "a reindex over unchanged notes must not re-call the embedding service"
        )


# ── 7. A recorded note is in the knowledge base when the call returns ────────


class TestAnAppliedNoteIsSearchable:
    """`propose_artifact` answered `status: "applied"` for a note `get_kb_doc`
    could not find and `search_kb` did not return - verified on dev 3.6.17.
    `hp policy init` writes 19 notes down that path."""

    async def test_proposing_a_kb_note_indexes_it(self, repo, kb, store):
        artifact_id = "2026-08-30-kb-policy-snapshot-policy"
        with patch(
            "homepilot.kb.service._get_embedding", new_callable=AsyncMock, return_value=None
        ):
            await kb.lifecycle.propose(
                {
                    "id": artifact_id,
                    "kind": "kb-note",
                    "intent": "policy: snapshot_policy",
                    "body": "# Policy: snapshot_policy\n\nalways",
                    "note_kind": "policy",
                    "target": {"kind": "global"},
                    "produced_by": {"session": "policy-init", "agent": "cli", "user": "cli"},
                }
            )

        row = await repo.get_doc_by_source(f"artifact:{artifact_id}")
        assert row is not None, "applied, and not in the knowledge base"

        with patch(
            "homepilot.kb.service._get_embedding", new_callable=AsyncMock, return_value=None
        ):
            assert await kb.search("snapshot_policy")

    async def test_index_note_reports_a_failure_rather_than_claiming_success(self, kb, store):
        outcome = await kb.index_note("2026-08-30-does-not-exist")
        assert outcome["indexed"] is False
        assert outcome["reason"]


# ── 8. The policies beside the plan are the ones that bind this host ─────────


class TestThePolicyPanelIsTrue:
    """Reproduced on dev 3.6.17 while reviewing a change to `hp-test-server`:
    the panel showed a policy targeting `dev-ct-db` whose own text read "This
    rule is about dev-ct-db only", and did NOT show a global policy forbidding
    package installs without a snapshot, against a plan that installs packages.
    """

    async def _seed(self, repo):
        await repo.create_doc_metadata(
            source="artifact:p-host",
            title="host rule",
            content="Never restart nginx on hp-test-server during business hours.",
            kind="policy",
            target="hp-test-server",
        )
        await repo.create_doc_metadata(
            source="artifact:p-other",
            title="other host rule",
            content=(
                "dev-ct-db must never share a change window with hp-test-server. "
                "This rule is about dev-ct-db only."
            ),
            kind="policy",
            target="dev-ct-db",
        )
        await repo.create_doc_metadata(
            source="artifact:p-global",
            title="policy: snapshot_policy",
            content="No package installs on any managed machine without a snapshot first.",
            kind="policy",
            target=None,
        )
        await repo.create_doc_metadata(
            source="artifact:n-note",
            title="a note about hp-test-server",
            content="hp-test-server has 2 cores",
            kind="note",
            target="hp-test-server",
        )

    async def test_a_global_policy_is_shown(self, repo, kb):
        await self._seed(repo)
        policies = await kb.policies_for_target("hp-test-server")
        titles = {p["title"] for p in policies}
        assert "policy: snapshot_policy" in titles, (
            "every policy `hp policy init` writes is global; none could ever appear"
        )

    async def test_another_hosts_policy_is_not_shown(self, repo, kb):
        await self._seed(repo)
        policies = await kb.policies_for_target("hp-test-server")
        assert all(p["target"] != "dev-ct-db" for p in policies), (
            "a rule about a different machine, shown as a rule about this one"
        )

    async def test_only_policies_and_each_says_why(self, repo, kb):
        await self._seed(repo)
        policies = await kb.policies_for_target("hp-test-server")
        assert {p["applies_via"] for p in policies} <= {"target", "global"}
        assert policies[0]["applies_via"] == "target", "the host's own rules come first"
        assert all("a note about" not in p["title"] for p in policies)

    async def test_a_host_with_no_rules_of_its_own_gets_only_the_global_ones(self, repo, kb):
        await self._seed(repo)
        policies = await kb.policies_for_target("mail-relay-99")
        assert [p["applies_via"] for p in policies] == ["global"], (
            "with embeddings on, this screen showed the five nearest policies "
            "for any host whatsoever"
        )


# ── 9. Editing a document changes what it answers for ────────────────────────


class TestAnEditedDocumentStopsAnsweringForItsOldText:
    """Verified on 3.6.17: after rewriting a document, a query on its new
    wording ranked it LAST and a query on the text it no longer contains ranked
    it FIRST."""

    async def test_reembed_drops_the_old_vector_even_when_no_service_answers(self, repo, kb):
        doc_id = await repo.create_doc_metadata(
            source="test:edited", title="Telescope", content="azimuth ring", kind="note"
        )
        await _store_embedding(repo, doc_id, _vector(0.8))
        await repo.update_doc_metadata(doc_id, content="pantograph spares in bay 12")

        with patch(
            "homepilot.kb.service._get_embedding", new_callable=AsyncMock, return_value=None
        ):
            reembedded = await kb.reembed_doc(doc_id)

        assert reembedded is False
        assert not await repo.has_embedding(doc_id), (
            "a document indexed under text it no longer contains is a worse "
            "answer than one with no vector at all"
        )

    async def test_reembed_installs_the_new_vector_when_one_can_be_made(self, repo, kb):
        doc_id = await repo.create_doc_metadata(
            source="test:edited2", title="T", content="old", kind="note"
        )
        await _store_embedding(repo, doc_id, _vector(0.8))
        await repo.update_doc_metadata(doc_id, content="new")

        with patch(
            "homepilot.kb.service._get_embedding",
            new_callable=AsyncMock,
            return_value=_vector(0.2),
        ):
            assert await kb.reembed_doc(doc_id) is True
        assert await repo.has_embedding(doc_id)


# ── 10. The embedding client is not decided by a substring ───────────────────


class TestTheEmbeddingClientNegotiatesTheShape:
    async def test_an_ollama_answer_is_read_from_any_url(self):
        from homepilot.kb.service import _extract_embedding

        assert _extract_embedding({"embedding": [0.1, 0.2]}) == [0.1, 0.2]
        assert _extract_embedding({"data": [{"embedding": [0.3]}]}) == [0.3]
        assert _extract_embedding({"nothing": 1}) is None

    async def test_a_refused_first_dialect_is_retried_in_the_other(self):
        """`"/api/embeddings" in url` decided the request body. A bare
        `http://llm:11434` - the shape an operator actually writes - got the
        OpenAI body, and the failure was reported as the service being broken."""
        from homepilot.kb.service import _call_embed_service

        posts: list[dict] = []

        class Resp:
            def __init__(self, status: int, payload: dict):
                self.status_code = status
                self._payload = payload

            def raise_for_status(self):
                if self.status_code >= 400:
                    raise RuntimeError(f"HTTP {self.status_code}")

            def json(self):
                return self._payload

        async def post(url, json):
            posts.append(json)
            if "input" in json:
                return Resp(400, {})
            return Resp(200, {"embedding": [0.7] * 8})

        client = AsyncMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.post = post

        with patch("homepilot.kb.service.httpx.AsyncClient", return_value=client):
            got = await _call_embed_service("http://llm:11434", "m", "text")

        assert got == [0.7] * 8
        assert [sorted(p) for p in posts] == [["input", "model"], ["model", "prompt"]]


# ── 11. The fused environment document names what it could not read ──────────


class TestTheEnvironmentDocAdmitsAHole:
    async def test_a_failed_kb_read_is_not_an_empty_kb(self, repo):
        from homepilot.inventory.service import InventoryService

        kb_service = MagicMock()
        kb_service.search = AsyncMock(side_effect=RuntimeError("kb down"))
        service = InventoryService(repo=repo, proxmox=MagicMock(), kb_service=kb_service)

        doc = await service.get_environment_doc("web01")

        assert doc["kb_entries"] == []
        assert doc["sources"]["knowledge_base"]["read"] is False, (
            '"the KB said nothing" and "the KB could not be asked" printed identically'
        )
        assert "kb down" in doc["sources"]["knowledge_base"]["error"]
        assert doc["sources"]["inventory"]["read"] is True

    async def test_a_readable_kb_is_marked_read(self, repo):
        from homepilot.inventory.service import InventoryService

        kb_service = MagicMock()
        kb_service.search = AsyncMock(return_value=[])
        service = InventoryService(repo=repo, proxmox=MagicMock(), kb_service=kb_service)

        doc = await service.get_environment_doc("web01")
        assert doc["sources"]["knowledge_base"] == {"read": True}

    async def test_the_rendered_document_names_the_hole(self, repo):
        from homepilot.mcp.tools.inventory_tools import handle_get_environment_doc

        service = MagicMock()
        service.get_environment_doc = AsyncMock(
            return_value={
                "target": "web01",
                "hosts": [{"hostname": "web01", "proxmox_id": 1, "node": "pve"}],
                "services": [],
                "kb_entries": [],
                "artifact_history": [],
                "sources": {
                    "inventory": {"read": True},
                    "knowledge_base": {"read": False, "error": "kb down"},
                    "artifact_history": {"read": True},
                },
            }
        )
        out = await handle_get_environment_doc({"target": "web01"}, {"inventory_service": service})
        text = out[0].text
        assert "NOT READ" in text
        assert "knowledge_base: kb down" in text


# ── 12. Migration 32 clears the vectors already stranded ─────────────────────


class TestTheRepairMigrationRuns:
    async def test_orphans_present_before_the_migration_are_removed(self, tmp_path: Path):
        import struct

        from homepilot.db.connection import Database
        from homepilot.db.migrations import MIGRATIONS, run_migrations

        database = Database(str(tmp_path / "repair.db"))
        await database.connect()
        await run_migrations(database)
        vec = b"".join(struct.pack("<f", 0.1) for _ in range(768))
        await database.execute("INSERT INTO vec_docs (id, embedding) VALUES (?, ?)", (777, vec))
        await database.conn.commit()

        for statement in MIGRATIONS[32]:
            await database.execute(statement)
        await database.conn.commit()

        row = await database.fetchone("SELECT COUNT(*) AS c FROM vec_docs WHERE id = 777")
        assert row["c"] == 0
        await database.close()


# ── 13. The executor's own claim ─────────────────────────────────────────────


class TestTheExecutorNeverSaysIndexedWhenItIsNot:
    async def test_a_locked_database_is_a_failure(self, mock_repo):
        from homepilot.executor.kb_note import execute

        mock_repo.upsert_doc_metadata.side_effect = sqlite3.OperationalError("database is locked")
        result = await execute({"id": "n", "intent": "i"}, "body", mock_repo, no_embeddings=True)
        assert result["success"] is False
        assert "NOT indexed" in result["execution_log"]
