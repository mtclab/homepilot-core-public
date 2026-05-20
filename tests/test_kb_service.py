from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from homepilot.artifacts.lifecycle import ArtifactLifecycle
from homepilot.artifacts.store import ArtifactStore
from homepilot.kb.service import KBService

# ── fixtures ──────────────────────────────────────────────────────────────────


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


async def _insert_doc(
    repo, title: str, content: str, kind: str = "note", target: str | None = None
) -> int:
    return await repo.create_doc_metadata(
        source=f"test:{title}",
        title=title,
        content=content,
        kind=kind,
        target=target,
    )


# ── KBService.search ──────────────────────────────────────────────────────────


class TestKBServiceSearch:
    async def test_keyword_fallback_when_embedding_fails(self, kb_service, repo):
        await _insert_doc(repo, "Redis config", "redis maxmemory 2gb", kind="note", target="redis")
        await _insert_doc(repo, "Unrelated", "something else entirely", kind="note")

        with patch(
            "homepilot.kb.service._get_embedding", new_callable=AsyncMock, return_value=None
        ):
            results = await kb_service.search("redis")

        assert any(r["title"] == "Redis config" for r in results)

    async def test_keyword_search_filters_by_kind(self, kb_service, repo):
        await _insert_doc(repo, "Policy doc", "use debian templates", kind="policy")
        await _insert_doc(repo, "Note doc", "use debian templates", kind="note")

        with patch(
            "homepilot.kb.service._get_embedding", new_callable=AsyncMock, return_value=None
        ):
            results = await kb_service.search("debian", kind="policy")

        assert all(r["kind"] == "policy" for r in results)
        assert any(r["title"] == "Policy doc" for r in results)

    async def test_keyword_search_limit(self, kb_service, repo):
        for i in range(5):
            await _insert_doc(repo, f"Doc {i}", "shared keyword match content", kind="note")

        with patch(
            "homepilot.kb.service._get_embedding", new_callable=AsyncMock, return_value=None
        ):
            results = await kb_service.search("keyword", limit=3)

        assert len(results) <= 3

    async def test_empty_results_when_no_match(self, kb_service, repo):
        await _insert_doc(repo, "Irrelevant", "nothing here", kind="note")

        with patch(
            "homepilot.kb.service._get_embedding", new_callable=AsyncMock, return_value=None
        ):
            results = await kb_service.search("xyznonexistent")

        assert results == []

    async def test_vector_search_falls_back_on_sql_error(self, kb_service, repo):
        await _insert_doc(repo, "Fallback test", "jellyfin media server setup", kind="note")

        fake_embedding = [0.1] * 768
        with patch(
            "homepilot.kb.service._get_embedding",
            new_callable=AsyncMock,
            return_value=fake_embedding,
        ):
            results = await kb_service.search("jellyfin")

        assert isinstance(results, list)


# ── KBService.record_fact ─────────────────────────────────────────────────────


class TestKBServiceRecordFact:
    async def test_record_fact_creates_applied_artifact(self, kb_service, artifact_store):
        artifact_id = await kb_service.record_fact(
            target="media-lxc",
            kind="note",
            content="Jellyfin runs on port 8096 with HW transcode enabled.",
        )

        assert artifact_id is not None
        fm, body = artifact_store.read(artifact_id)
        assert fm["kind"] == "kb-note"
        assert fm["status"] == "applied"
        assert "8096" in body

    async def test_record_fact_stores_kind(self, kb_service, artifact_store):
        artifact_id = await kb_service.record_fact(
            target="global",
            kind="policy",
            content="Always use LXC for stateless services.",
        )

        fm, _ = artifact_store.read(artifact_id)
        assert fm.get("note_kind") == "policy"

    async def test_record_fact_with_supersedes(self, kb_service, artifact_store):
        first_id = await kb_service.record_fact(
            target="pve1",
            kind="decision",
            content="Use vmbr0 for all VMs.",
        )
        second_id = await kb_service.record_fact(
            target="pve1",
            kind="decision",
            content="Use vmbr1 for media VLAN VMs.",
            supersedes=[first_id],
        )

        fm2, _ = artifact_store.read(second_id)
        assert first_id in (fm2.get("supersedes") or [])

    async def test_record_fact_empty_target(self, kb_service, artifact_store):
        artifact_id = await kb_service.record_fact(
            target="",
            kind="note",
            content="Global infrastructure note.",
        )

        assert artifact_id is not None
        fm, _ = artifact_store.read(artifact_id)
        assert fm["kind"] == "kb-note"

    async def test_record_fact_dict_target_kind_global(self, kb_service, artifact_store):
        artifact_id = await kb_service.record_fact(
            target={"kind": "global"},
            kind="note",
            content="Note with dict target kind=global.",
        )

        assert artifact_id is not None
        fm, _ = artifact_store.read(artifact_id)
        assert fm["kind"] == "kb-note"
        assert "-global" in artifact_id
        assert fm.get("target") == {"kind": "global"}

    async def test_record_fact_dict_target_service(self, kb_service, artifact_store):
        artifact_id = await kb_service.record_fact(
            target={"kind": "service", "service": "jellyfin"},
            kind="note",
            content="Jellyfin note with dict target.",
        )

        assert artifact_id is not None
        fm, _ = artifact_store.read(artifact_id)
        assert fm["kind"] == "kb-note"
        assert "-jellyfin" in artifact_id
        assert fm.get("target") == {"kind": "service", "service": "jellyfin"}

    async def test_record_fact_none_target(self, kb_service, artifact_store):
        artifact_id = await kb_service.record_fact(
            target=None,
            kind="note",
            content="Global note with None target.",
        )

        assert artifact_id is not None
        fm, _ = artifact_store.read(artifact_id)
        assert fm["kind"] == "kb-note"
        assert "-global" in artifact_id
        assert fm.get("target") is None

    async def test_record_fact_empty_string_target_same_as_none(self, kb_service, artifact_store):
        artifact_id = await kb_service.record_fact(
            target="",
            kind="note",
            content="Empty string target note.",
        )

        assert artifact_id is not None
        _fm, _ = artifact_store.read(artifact_id)
        assert "-global" in artifact_id


# ── KB router ─────────────────────────────────────────────────────────────────


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
                "display_name": "Test",
                "scope": "write",
            }
    test_app.include_router(kb_router, prefix="/kb")
    test_app.state.repo = repo
    test_app.state.kb_service = kb_svc

    client = TestClient(test_app, raise_server_exceptions=True)
    yield client, repo
    await db.close()


class TestKBRouter:
    async def test_list_kb_empty(self, test_client):
        client, _ = test_client
        resp = client.get("/kb")
        assert resp.status_code == 200
        data = resp.json()
        assert data["items"] == []
        assert data["total"] == 0

    async def test_list_kb_with_entries(self, test_client):
        client, repo = test_client
        await _insert_doc(repo, "List test", "some content", kind="note")
        resp = client.get("/kb")
        assert resp.status_code == 200
        assert resp.json()["total"] == 1

    async def test_list_kb_filter_by_kind(self, test_client):
        client, repo = test_client
        await _insert_doc(repo, "Note item", "note content", kind="note")
        await _insert_doc(repo, "Policy item", "policy content", kind="policy")
        resp = client.get("/kb?kind=policy")
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert all(i["kind"] == "policy" for i in items)

    async def test_search_kb_returns_results(self, test_client):
        client, repo = test_client
        await _insert_doc(repo, "Proxmox setup", "proxmox backup server config", kind="note")
        with patch(
            "homepilot.kb.service._get_embedding", new_callable=AsyncMock, return_value=None
        ):
            resp = client.get("/kb/search?q=proxmox")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] >= 1
        assert any(
            "proxmox" in r["title"].lower() or "proxmox" in r["content"].lower()
            for r in data["results"]
        )

    async def test_search_kb_requires_q(self, test_client):
        client, _ = test_client
        resp = client.get("/kb/search")
        assert resp.status_code == 422

    async def test_get_kb_entry_not_found(self, test_client):
        client, _ = test_client
        resp = client.get("/kb/99999")
        assert resp.status_code == 404

    async def test_get_kb_entry_found(self, test_client):
        client, repo = test_client
        doc_id = await _insert_doc(repo, "Get test", "fetch this content", kind="note")
        resp = client.get(f"/kb/{doc_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["title"] == "Get test"

    async def test_create_note_with_dict_target(self, test_client):
        client, _ = test_client
        resp = client.post(
            "/kb/notes",
            json={
                "target": {"kind": "service", "service": "jellyfin"},
                "kind": "note",
                "content": "Jellyfin runs on port 8096.",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] is not None
        assert "-jellyfin" in data["id"]

    async def test_create_note_with_none_target(self, test_client):
        client, _ = test_client
        resp = client.post(
            "/kb/notes",
            json={
                "target": None,
                "kind": "note",
                "content": "Global note via API.",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] is not None
        assert "-global" in data["id"]

    async def test_create_note_without_target(self, test_client):
        client, _ = test_client
        resp = client.post(
            "/kb/notes",
            json={
                "kind": "note",
                "content": "No target field at all.",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] is not None

    async def test_record_fact_endpoint_with_dict_target(self, test_client):
        client, _ = test_client
        resp = client.post(
            "/kb",
            json={
                "target": {"kind": "global"},
                "content": "Dict target on /kb endpoint.",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] is not None
        assert "-global" in data["id"]

    async def test_create_note_validates_kind(self, test_client):
        client, _ = test_client
        resp = client.post(
            "/kb/notes",
            json={
                "kind": "invalid",
                "content": "Bad kind.",
            },
        )
        assert resp.status_code == 400


# ── #170: KB read auth enforcement ────────────────────────────────────────────


@pytest.fixture
async def auth_test_client(tmp_path: Path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from homepilot.db.connection import Database
    from homepilot.db.migrations import run_migrations
    from homepilot.db.repository import Repository
    from homepilot.kb.router import router as kb_router

    db = Database(str(tmp_path / "auth_test.db"))
    await db.connect()
    await run_migrations(db)
    repo = Repository(db)

    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()
    store = ArtifactStore(artifact_dir)
    lc = ArtifactLifecycle(store, repo)
    kb_svc = KBService(repo=repo, store=store, lifecycle=lc)

    test_app = FastAPI()
    test_app.include_router(kb_router, prefix="/kb")
    test_app.state.repo = repo
    test_app.state.kb_service = kb_svc

    client = TestClient(test_app, raise_server_exceptions=True)
    yield client, repo
    await db.close()


class TestKBReadAuth:
    async def test_list_kb_requires_auth(self, auth_test_client):
        client, _ = auth_test_client
        resp = client.get("/kb")
        assert resp.status_code in (401, 403)

    async def test_search_kb_requires_auth(self, auth_test_client):
        client, _ = auth_test_client
        resp = client.get("/kb/search?q=test")
        assert resp.status_code in (401, 403)

    async def test_get_kb_entry_requires_auth(self, auth_test_client):
        client, _ = auth_test_client
        resp = client.get("/kb/1")
        assert resp.status_code in (401, 403)

    async def test_list_kb_allows_read_scope(self, tmp_path: Path):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from homepilot.auth.deps import require_token
        from homepilot.db.connection import Database
        from homepilot.db.migrations import run_migrations
        from homepilot.db.repository import Repository
        from homepilot.kb.router import router as kb_router

        db = Database(str(tmp_path / "scope_test.db"))
        await db.connect()
        await run_migrations(db)
        repo = Repository(db)
        artifact_dir = tmp_path / "artifacts"
        artifact_dir.mkdir()
        store = ArtifactStore(artifact_dir)
        lc = ArtifactLifecycle(store, repo)
        kb_svc = KBService(repo=repo, store=store, lifecycle=lc)

        test_app = FastAPI()
        test_app.dependency_overrides[require_token] = lambda: {
            "user_id": "test-user",
            "token_id": "test-token",
            "display_name": "Reader",
            "scope": "read",
            "role": None,
        }
        test_app.include_router(kb_router, prefix="/kb")
        test_app.state.repo = repo
        test_app.state.kb_service = kb_svc

        client = TestClient(test_app, raise_server_exceptions=True)
        resp = client.get("/kb")
        assert resp.status_code == 200
        await db.close()

    async def test_list_kb_denies_insufficient_scope(self, tmp_path: Path):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from homepilot.auth.deps import require_token
        from homepilot.db.connection import Database
        from homepilot.db.migrations import run_migrations
        from homepilot.db.repository import Repository
        from homepilot.kb.router import router as kb_router

        db = Database(str(tmp_path / "deny_test.db"))
        await db.connect()
        await run_migrations(db)
        repo = Repository(db)
        artifact_dir = tmp_path / "artifacts"
        artifact_dir.mkdir()
        store = ArtifactStore(artifact_dir)
        lc = ArtifactLifecycle(store, repo)
        kb_svc = KBService(repo=repo, store=store, lifecycle=lc)

        test_app = FastAPI()
        test_app.dependency_overrides[require_token] = lambda: {
            "user_id": "test-user",
            "token_id": "test-token",
            "display_name": "Limited",
            "scope": "write",
            "role": None,
        }
        test_app.include_router(kb_router, prefix="/kb")
        test_app.state.repo = repo
        test_app.state.kb_service = kb_svc

        client = TestClient(test_app, raise_server_exceptions=True)
        resp = client.get("/kb/search?q=test")
        assert resp.status_code == 403
        await db.close()


# ── #172: KB reindex_if_needed ────────────────────────────────────────────────


class TestKBReindexIfNeeded:
    async def test_reindex_if_needed_noop_when_up_to_date(self, kb_service, repo):
        with patch(
            "homepilot.kb.service._get_embedding",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await kb_service.reindex_if_needed()
        assert result is None

    async def test_reindex_if_needed_triggers_when_stale(self, kb_service, repo):
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
                target="stale-test",
                kind="note",
                content="This note should trigger reindex.",
            )

        await kb_service.reindex_if_needed()
        await asyncio.gather(*asyncio.all_tasks() - {asyncio.current_task()})

        rows = await repo.db.fetchall("SELECT * FROM doc_metadata WHERE source LIKE 'artifact:%'")
        assert len(rows) >= 1

    async def test_propose_does_not_trigger_reindex_for_kb_note(
        self, kb_service, repo, artifact_store
    ):
        kb_service.lifecycle._kb_service = kb_service

        reindex_spy = AsyncMock(return_value=None)
        with patch.object(kb_service, "reindex_if_needed", reindex_spy):
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
                    target="propose-no-reindex-test",
                    kind="note",
                    content="Propose should not trigger reindex.",
                )
            reindex_spy.assert_not_awaited()

    async def test_mark_applied_triggers_reindex_for_kb_note(
        self, kb_service, repo, artifact_store
    ):
        from unittest.mock import AsyncMock, patch

        kb_service.lifecycle._kb_service = kb_service

        mock_reindex = AsyncMock(return_value=None)
        with (
            patch.object(
                kb_service.lifecycle._ensure_transitions(),
                "mark_applied",
                new_callable=AsyncMock,
            ),
            patch.object(kb_service, "reindex_if_needed", mock_reindex),
            patch.object(
                kb_service.lifecycle.store,
                "read",
                return_value=({"kind": "kb-note", "id": "test-note"}, "body"),
            ),
        ):
            await kb_service.lifecycle.mark_applied("test-note", "test log")
            mock_reindex.assert_awaited()

    async def test_mark_applied_no_reindex_for_non_kb_note(self, kb_service, repo, artifact_store):
        kb_service.lifecycle._kb_service = kb_service

        from homepilot.artifacts.models import utcnow_iso

        spec = {
            "id": "2025-01-01-shell-noreindex",
            "kind": "shell-script",
            "intent": "test shell script no reindex",
            "target": {"kind": "service", "service": "testsvc"},
            "mutating": True,
            "idempotence": "via-precheck",
            "produced_by": {"session": "test", "agent": "test", "user": "test", "at": utcnow_iso()},
            "body": "echo hello",
        }
        artifact_id = await kb_service.lifecycle.propose(spec)
        await kb_service.lifecycle.approve(artifact_id, user="test-user")

        mock_reindex = AsyncMock(return_value=None)
        with patch.object(kb_service, "reindex_if_needed", mock_reindex):
            await kb_service.lifecycle.mark_applied(artifact_id, "test log")
            mock_reindex.assert_not_awaited()

    async def test_reindex_if_needed_triggers_when_indexed_exceeds_applied(self, kb_service, repo):
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
            aid1 = await kb_service.record_fact(
                target="supersede-indexed-a",
                kind="note",
                content="First note to be superseded.",
            )
            await kb_service.record_fact(
                target="supersede-indexed-b",
                kind="note",
                content="Second note stays applied.",
            )

        with patch(
            "homepilot.executor.kb_note._compute_embedding",
            new_callable=AsyncMock,
            return_value=None,
        ):
            await kb_service.reindex(no_embeddings=True)

        assert await kb_service.reindex_if_needed() is None

        fm, body = kb_service.store.read(aid1)
        fm["status"] = "superseded"
        kb_service.store.write(
            aid1, kb_service.lifecycle._file_store.serialize_frontmatter(fm), body, "supersede"
        )

        applied_notes = kb_service.store.list(kind="kb-note", status="applied")
        assert len(applied_notes) == 1

        row = await repo.db.fetchone(
            "SELECT COUNT(*) AS c FROM doc_metadata WHERE source LIKE 'artifact:%'"
        )
        assert row["c"] == 2

        await kb_service.reindex_if_needed()
        await asyncio.gather(*asyncio.all_tasks() - {asyncio.current_task()})

        row2 = await repo.db.fetchone(
            "SELECT COUNT(*) AS c FROM doc_metadata WHERE source LIKE 'artifact:%'"
        )
        assert row2["c"] == 1

    async def test_mark_applied_survives_reindex_failure(self, kb_service, repo, artifact_store):
        kb_service.lifecycle._kb_service = kb_service

        with (
            patch.object(
                kb_service.lifecycle._ensure_transitions(),
                "mark_applied",
                new_callable=AsyncMock,
            ),
            patch.object(
                kb_service,
                "reindex_if_needed",
                side_effect=OSError("reindex failed"),
            ),
            patch.object(
                kb_service.lifecycle.store,
                "read",
                return_value=({"kind": "kb-note", "id": "test-note-reindex-fail"}, "body"),
            ),
        ):
            await kb_service.lifecycle.mark_applied(
                "test-note-reindex-fail", "applied despite failure"
            )

    async def test_reindex_if_needed_creates_task(self, kb_service, repo):
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
                target="async-reindex-test",
                kind="note",
                content="Trigger async reindex.",
            )

        with patch("homepilot.kb.service.asyncio.create_task") as mock_create_task:
            await kb_service.reindex_if_needed(reason="startup")
            mock_create_task.assert_called_once()

    async def test_reindex_if_needed_no_task_when_matched(self, kb_service):
        with patch.object(kb_service, "_run_reindex", new_callable=AsyncMock) as mock_run:
            await kb_service.reindex_if_needed(reason="lifecycle")
            mock_run.assert_not_awaited()

    async def test_supersede_triggers_reindex_for_kb_note(self, kb_service, repo, artifact_store):
        kb_service.lifecycle._kb_service = kb_service

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

    async def test_supersede_no_reindex_for_non_kb_note(self, kb_service, repo, artifact_store):
        kb_service.lifecycle._kb_service = kb_service

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
                return_value=({"kind": "shell-script", "id": "test-script"}, "body"),
            ),
            patch.object(kb_service, "reindex_if_needed", mock_reindex),
        ):
            await kb_service.lifecycle.supersede("test-script", "new-script")
            mock_reindex.assert_not_awaited()

    async def test_revoke_no_reindex_for_non_kb_note(self, kb_service, repo, artifact_store):
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
                return_value=({"kind": "shell-script", "id": "test-script"}, "body"),
            ),
            patch.object(kb_service, "reindex_if_needed", mock_reindex),
        ):
            await kb_service.lifecycle.revoke("test-script", user="admin", reason="obsolete")
            mock_reindex.assert_not_awaited()
