from __future__ import annotations

from pathlib import Path

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

    from homepilot.auth.deps import require_token
    from homepilot.db.connection import Database
    from homepilot.db.migrations import run_migrations
    from homepilot.db.repository import Repository
    from homepilot.kb.router import router as kb_router

    db = Database(str(tmp_path / "ingest_test.db"))
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
        "display_name": "Admin",
        "scope": "admin",
    }
    test_app.include_router(kb_router, prefix="/kb")
    test_app.state.repo = repo
    test_app.state.kb_service = kb_svc

    client = TestClient(test_app, raise_server_exceptions=True)
    yield client, repo, kb_svc
    await db.close()


@pytest.fixture
async def read_client(tmp_path: Path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from homepilot.auth.deps import require_token
    from homepilot.db.connection import Database
    from homepilot.db.migrations import run_migrations
    from homepilot.db.repository import Repository
    from homepilot.kb.router import router as kb_router

    db = Database(str(tmp_path / "ingest_read_test.db"))
    await db.connect()
    await run_migrations(db)
    repo = Repository(db)

    artifact_dir = tmp_path / "artifacts_read"
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
    }
    test_app.include_router(kb_router, prefix="/kb")
    test_app.state.repo = repo
    test_app.state.kb_service = kb_svc

    client = TestClient(test_app, raise_server_exceptions=True)
    yield client, repo, kb_svc
    await db.close()


def _make_doc_dir(base_dir: Path, files: dict[str, str]) -> str:
    d = base_dir / "docs"
    d.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        p = d / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return str(d)


class TestKBIngestService:
    async def test_ingest_from_directory(self, kb_service, repo, tmp_path):
        doc_dir = _make_doc_dir(
            tmp_path,
            {
                "proxmox.md": "# Proxmox\nProxmox VE runs on pve1 with ZFS.",
                "network.txt": "VLAN 10 is management network.",
            },
        )
        result = await kb_service.ingest(
            sources=[{"path": doc_dir, "kind": "note", "target": "pve1"}],
        )
        assert result["created"] == 2
        assert result["skipped"] == 0
        assert result["errors"] == 0
        assert result["errors_detail"] == []

        rows = await repo.db.fetchall("SELECT * FROM doc_metadata WHERE source LIKE 'ingest:%'")
        assert len(rows) == 2

    async def test_ingest_dedup_on_reingest(self, kb_service, repo, tmp_path):
        doc_dir = _make_doc_dir(
            tmp_path,
            {
                "redis.md": "Redis maxmemory 2gb policy allkeys-lru.",
            },
        )
        result1 = await kb_service.ingest(
            sources=[{"path": doc_dir, "kind": "note", "target": "redis"}],
        )
        assert result1["created"] == 1

        result2 = await kb_service.ingest(
            sources=[{"path": doc_dir, "kind": "note", "target": "redis"}],
        )
        assert result2["skipped"] == 1
        assert result2["created"] == 0

        rows = await repo.db.fetchall("SELECT * FROM doc_metadata WHERE source LIKE 'ingest:%'")
        assert len(rows) == 1

    async def test_ingest_skips_empty_files(self, kb_service, repo, tmp_path):
        doc_dir = _make_doc_dir(
            tmp_path,
            {
                "empty.md": "",
                "content.md": "Has content.",
            },
        )
        result = await kb_service.ingest(
            sources=[{"path": doc_dir, "kind": "note"}],
        )
        assert result["created"] == 1
        assert result["errors"] == 0

    async def test_ingest_skips_non_text_files(self, kb_service, repo, tmp_path):
        doc_dir = _make_doc_dir(
            tmp_path,
            {
                "readme.md": "Good content.",
            },
        )
        (Path(doc_dir) / "image.png").write_bytes(b"\x89PNG")
        result = await kb_service.ingest(
            sources=[{"path": doc_dir, "kind": "note"}],
        )
        assert result["created"] == 1

    async def test_ingest_missing_directory_error(self, kb_service, repo):
        result = await kb_service.ingest(
            sources=[{"path": "/nonexistent/path/12345", "kind": "note"}],
        )
        assert result["errors"] == 1
        assert result["created"] == 0

    async def test_ingest_file_not_dir_error(self, kb_service, repo, tmp_path):
        f = tmp_path / "single.txt"
        f.write_text("not a dir")
        result = await kb_service.ingest(
            sources=[{"path": str(f), "kind": "note"}],
        )
        assert result["errors"] == 1

    async def test_ingest_missing_path_field(self, kb_service, repo):
        result = await kb_service.ingest(sources=[{"kind": "note"}])
        assert result["errors"] == 1

    async def test_ingest_source_metadata(self, kb_service, repo, tmp_path):
        doc_dir = _make_doc_dir(
            tmp_path,
            {
                "policy.md": "Use LXC for stateless services.",
            },
        )
        await kb_service.ingest(
            sources=[{"path": doc_dir, "kind": "policy", "target": "infra"}],
        )
        rows = await repo.db.fetchall("SELECT * FROM doc_metadata WHERE source LIKE 'ingest:%'")
        assert len(rows) == 1
        assert rows[0]["kind"] == "policy"
        assert rows[0]["target"] == "infra"

    async def test_ingest_nested_directories(self, kb_service, repo, tmp_path):
        doc_dir = _make_doc_dir(
            tmp_path,
            {
                "infra/dns.md": "DNS is 10.0.0.1",
                "infra/network.md": "VLAN 10 is mgmt",
                "services/redis.md": "Redis on port 6379",
            },
        )
        result = await kb_service.ingest(
            sources=[{"path": doc_dir, "kind": "note", "target": "infra"}],
        )
        assert result["created"] == 3

    async def test_ingest_multiple_sources(self, kb_service, repo, tmp_path):
        dir_a = _make_doc_dir(tmp_path / "a", {"doc_a.md": "Content A"})
        dir_b = _make_doc_dir(tmp_path / "b", {"doc_b.txt": "Content B"})
        result = await kb_service.ingest(
            sources=[
                {"path": dir_a, "kind": "note", "target": "svc-a"},
                {"path": dir_b, "kind": "fact", "target": "svc-b"},
            ],
        )
        assert result["created"] == 2

    async def test_ingest_content_hash_dedup_across_sources(self, kb_service, repo, tmp_path):
        content = "Identical content across sources."
        dir_a = _make_doc_dir(tmp_path / "a", {"same.md": content})
        dir_b = _make_doc_dir(tmp_path / "b", {"same.md": content})
        result = await kb_service.ingest(
            sources=[
                {"path": dir_a, "kind": "note"},
                {"path": dir_b, "kind": "note"},
            ],
        )
        assert result["created"] == 1
        assert result["skipped"] == 1


class TestKBIngestEndpoint:
    async def test_ingest_endpoint(self, test_client, tmp_path):
        client, _repo, _kb_svc = test_client
        doc_dir = _make_doc_dir(
            tmp_path,
            {
                "test.md": "Test document for ingest endpoint.",
            },
        )
        resp = client.post(
            "/kb/ingest",
            json={
                "sources": [{"path": doc_dir, "kind": "note", "target": "pve1"}],
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["created"] == 1
        assert data["skipped"] == 0

    async def test_ingest_endpoint_dedup(self, test_client, tmp_path):
        client, _repo, _kb_svc = test_client
        doc_dir = _make_doc_dir(
            tmp_path,
            {
                "unique.md": "Unique content for dedup test.",
            },
        )
        resp1 = client.post(
            "/kb/ingest",
            json={"sources": [{"path": doc_dir, "kind": "note"}]},
        )
        assert resp1.json()["created"] == 1

        resp2 = client.post(
            "/kb/ingest",
            json={"sources": [{"path": doc_dir, "kind": "note"}]},
        )
        assert resp2.json()["skipped"] == 1
        assert resp2.json()["created"] == 0

    async def test_ingest_requires_admin_scope(self, tmp_path: Path):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from homepilot.auth.deps import require_token
        from homepilot.db.connection import Database
        from homepilot.db.migrations import run_migrations
        from homepilot.db.repository import Repository
        from homepilot.kb.router import router as kb_router

        db = Database(str(tmp_path / "no_admin_test.db"))
        await db.connect()
        await run_migrations(db)
        repo = Repository(db)

        artifact_dir = tmp_path / "artifacts_no_admin"
        artifact_dir.mkdir()
        store = ArtifactStore(artifact_dir)
        lc = ArtifactLifecycle(store, repo)
        kb_svc = KBService(repo=repo, store=store, lifecycle=lc)

        test_app = FastAPI()
        test_app.dependency_overrides[require_token] = lambda: {
            "user_id": "test-user",
            "token_id": "test-token",
            "display_name": "Writer",
            "scope": "write",
        }
        test_app.include_router(kb_router, prefix="/kb")
        test_app.state.repo = repo
        test_app.state.kb_service = kb_svc

        client = TestClient(test_app, raise_server_exceptions=True)
        resp = client.post(
            "/kb/ingest",
            json={"sources": [{"path": "/tmp", "kind": "note"}]},
        )
        assert resp.status_code == 403
        await db.close()

    async def test_ingest_read_scope_denied(self, read_client):
        client, _repo, _kb_svc = read_client
        resp = client.post(
            "/kb/ingest",
            json={"sources": [{"path": "/tmp", "kind": "note"}]},
        )
        assert resp.status_code == 403

    async def test_ingest_empty_sources(self, test_client):
        client, _repo, _kb_svc = test_client
        resp = client.post(
            "/kb/ingest",
            json={"sources": []},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["created"] == 0
        assert data["skipped"] == 0
        assert data["errors"] == 0
        assert data["errors_detail"] == []


class TestKBIngestSecurity:
    async def test_ingest_path_traversal_symlink_skipped(self, kb_service, repo, tmp_path):
        doc_dir = _make_doc_dir(
            tmp_path,
            {
                "safe.md": "Safe content.",
            },
        )
        outside_dir = tmp_path / "outside"
        outside_dir.mkdir()
        (outside_dir / "secret.txt").write_text("secret data outside root", encoding="utf-8")
        link = Path(doc_dir) / "linked.txt"
        link.symlink_to(outside_dir / "secret.txt")
        result = await kb_service.ingest(
            sources=[{"path": doc_dir, "kind": "note"}],
        )
        assert result["created"] == 1
        assert result["errors"] == 0
        rows = await repo.db.fetchall(
            "SELECT content FROM doc_metadata WHERE source LIKE 'ingest:%'"
        )
        for row in rows:
            assert "secret" not in row["content"]

    async def test_ingest_non_utf8_file_skipped(self, kb_service, repo, tmp_path):
        doc_dir = _make_doc_dir(
            tmp_path,
            {
                "good.md": "Valid UTF-8 content.",
            },
        )
        bad_file = Path(doc_dir) / "bad.md"
        bad_file.write_bytes(b"\x80\x81\x82 invalid utf8 \xff\xfe")
        result = await kb_service.ingest(
            sources=[{"path": doc_dir, "kind": "note"}],
        )
        assert result["created"] == 1
        assert result["errors"] == 0

    async def test_ingest_errors_detail_missing_path(self, kb_service, repo):
        result = await kb_service.ingest(sources=[{"kind": "note"}])
        assert result["errors"] == 1
        assert len(result["errors_detail"]) == 1
        assert "missing path" in result["errors_detail"][0]

    async def test_ingest_errors_detail_not_found(self, kb_service, repo):
        result = await kb_service.ingest(
            sources=[{"path": "/nonexistent/path/12345", "kind": "note"}],
        )
        assert result["errors"] == 1
        assert len(result["errors_detail"]) == 1
        assert "directory not found" in result["errors_detail"][0]
