from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from homepilot.auth.deps import require_token
from homepilot.kb.router import router as kb_router


@pytest.fixture
async def real_db(tmp_path: Path):
    from homepilot.db.connection import Database
    from homepilot.db.migrations import run_migrations

    db = Database(str(tmp_path / "test.db"))
    await db.connect()
    await run_migrations(db)
    yield db
    await db.close()


async def _seed_docs(db, count: int, kind: str = "note") -> None:
    for i in range(count):
        await db.execute(
            "INSERT INTO doc_metadata (source, kind, target, title, content, embedded_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                f"test://{kind}/{i}",  # source is UNIQUE
                kind,
                "host-a",
                f"doc {i:03d}",
                f"body {i}",
                f"2026-01-01T00:{i:02d}:00Z",
            ),
        )
    await db.conn.commit()


@pytest.fixture
async def client(real_db):
    from homepilot.db.repository import Repository

    app = FastAPI()
    app.include_router(kb_router, prefix="/kb")
    app.state.repo = Repository(real_db)
    # require_scope() builds a fresh closure per call, so it can never be the
    # override key; the shared dependency underneath it is require_token.
    app.dependency_overrides[require_token] = lambda: {"scope": "read"}
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


class TestKBListingIsReachable:
    """#434: `total` was len(items) over a hardcoded LIMIT 100, so the count
    saturated and every document past the first hundred was unreachable."""

    async def test_every_document_can_be_paged_to(self, client, real_db):
        await _seed_docs(real_db, 120)

        first = (await client.get("/kb", params={"limit": 50})).json()
        assert first["total"] == 120, "total must count matching rows, not the page"
        assert len(first["items"]) == 50

        # The goal is reaching document 120, not that the endpoint returned 200.
        seen: list[str] = []
        offset = 0
        while offset < first["total"]:
            page = (await client.get("/kb", params={"limit": 50, "offset": offset})).json()
            seen.extend(item["title"] for item in page["items"])
            offset += 50
        assert len(seen) == 120
        assert len(set(seen)) == 120
        assert "doc 000" in seen and "doc 119" in seen

    async def test_total_counts_filtered_rows_not_the_page(self, client, real_db):
        await _seed_docs(real_db, 10, kind="note")
        await _seed_docs(real_db, 4, kind="observed-state")

        body = (await client.get("/kb", params={"kind": "observed-state", "limit": 2})).json()
        assert body["total"] == 4
        assert len(body["items"]) == 2

    async def test_paging_echoes_the_window(self, client, real_db):
        await _seed_docs(real_db, 3)

        body = (await client.get("/kb", params={"limit": 2, "offset": 1})).json()
        assert body["limit"] == 2
        assert body["offset"] == 1
        assert len(body["items"]) == 2

    async def test_limit_is_bounded(self, client, real_db):
        await _seed_docs(real_db, 1)

        assert (await client.get("/kb", params={"limit": 5000})).status_code == 422
        assert (await client.get("/kb", params={"offset": -1})).status_code == 422
