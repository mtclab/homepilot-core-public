"""Search across artifacts, hosts and the journal (#445 A4).

None existed. Each page had dropdown filters over a fixed vocabulary - status,
kind, role, source - and no way at all to ask "where is the thing I remember by
name". The gates below assert the property that makes a search worth building:
it finds a match that is NOT on the page you are looking at. Both the inventory
and the journal are paginated and the artifact list is capped, so a filter
applied in the browser searches the rows already fetched and silently misses the
rest - which is worse than no search, because it answers confidently.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from homepilot.auth.deps import require_token
from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.db.repository import Repository

pytestmark = pytest.mark.asyncio

PAGE = 50


@pytest.fixture
async def api(tmp_path: Path):
    from homepilot.artifacts.router import router as artifacts_router
    from homepilot.artifacts.store import ArtifactStore
    from homepilot.audit.router import router as audit_router
    from homepilot.inventory.router import router as inventory_router

    db = Database(str(tmp_path / "homepilot.db"))
    await db.connect()
    await run_migrations(db)
    repo = Repository(db)

    # A fleet big enough that the needle is NOT on the first page.
    for n in range(120):
        await repo.create_host(
            hostname=f"filler{n:03d}",
            host_type="vm",
            role="vm",
            source="discovered",
            ip_address=f"10.0.0.{n % 250}",
        )
    await repo.create_host(
        hostname="mail-relay-07",
        host_type="vm",
        role="service",
        source="discovered",
        ip_address="10.9.9.9",
        description="postfix outbound relay",
    )

    for n in range(120):
        await repo.log_audit(
            user_id="admin",
            source="ui",
            action="apply",
            artifact_id=f"2026-08-21-filler-{n:03d}",
            target_host=f"filler{n:03d}",
        )
    await repo.log_audit(
        user_id="admin",
        source="cli",
        action="apply",
        artifact_id="2026-08-21-rotate-mail-certs",
        target_host="mail-relay-07",
    )

    store = ArtifactStore(tmp_path / "artifacts")

    app = FastAPI()
    app.include_router(artifacts_router, prefix="/artifacts")
    app.include_router(inventory_router, prefix="/inventory")
    app.include_router(audit_router, prefix="/audit")
    app.state.repo = repo
    app.state.artifact_store = store
    app.dependency_overrides[require_token] = lambda: {"scope": "*", "role": "admin"}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, repo, store
    await db.close()


class TestSearchingHosts:
    async def test_it_finds_a_host_that_is_not_on_the_first_page(self, api):
        """The whole point: the list is paginated, so a browser-side filter can
        only ever search the page already fetched."""
        client, _repo, _store = api

        first_page = (await client.get("/inventory", params={"limit": PAGE})).json()
        assert "mail-relay-07" not in {h["hostname"] for h in first_page["items"]}, (
            "fixture precondition: the needle must be past the first page"
        )

        found = (await client.get("/inventory", params={"q": "mail-relay", "limit": PAGE})).json()

        assert [h["hostname"] for h in found["items"]] == ["mail-relay-07"]

    async def test_it_searches_more_than_the_name(self, api):
        client, _repo, _store = api

        found = (await client.get("/inventory", params={"q": "postfix"})).json()

        assert [h["hostname"] for h in found["items"]] == ["mail-relay-07"], (
            "a host is findable by what it is for, not only by what it is called"
        )

    async def test_search_and_filters_compose(self, api):
        client, _repo, _store = api

        both = (await client.get("/inventory", params={"q": "mail-relay", "role": "vm"})).json()

        assert both["items"] == [], (
            "the search ignored the role filter - the two must narrow together, "
            "not override each other"
        )


class TestSearchingTheJournal:
    async def test_it_finds_an_entry_beyond_the_first_page(self, api):
        client, _repo, _store = api

        found = (
            await client.get("/audit", params={"q": "rotate-mail-certs", "limit": PAGE})
        ).json()

        assert [e["artifact_id"] for e in found["items"]] == ["2026-08-21-rotate-mail-certs"]

    async def test_the_total_counts_the_search_not_the_whole_trail(self, api):
        """`items` and `total` come from two different queries. A filter added to
        one and forgotten in the other reports "50 of 4000" for a search that
        matched one thing, and the pager then offers pages that are empty."""
        client, _repo, _store = api

        found = (await client.get("/audit", params={"q": "rotate-mail-certs"})).json()

        assert found["total"] == len(found["items"]) == 1, (
            f"the pager says {found['total']} for a search that matched {len(found['items'])} rows"
        )

    async def test_it_searches_the_host_as_well_as_the_artifact(self, api):
        client, _repo, _store = api

        found = (await client.get("/audit", params={"q": "mail-relay-07"})).json()

        assert found["total"] == 1


class TestSearchingArtifacts:
    async def _write(self, store: Any, artifact_id: str, intent: str, host: str) -> None:
        frontmatter = yaml.safe_dump(
            {
                "id": artifact_id,
                "kind": "shell-command",
                "intent": intent,
                "status": "proposed",
                "mutating": True,
                "target": {"kind": "host", "host": host},
                "produced_by": {"agent": "test", "session": "s1", "user": "admin"},
            },
            sort_keys=False,
        )
        store.write(artifact_id, frontmatter, "echo hello\n", "propose")

    async def test_it_finds_an_artifact_by_intent(self, api):
        client, _repo, store = api
        await self._write(store, "2026-08-21-restart-nginx", "restart nginx on the edge", "web01")
        await self._write(store, "2026-08-21-prune-logs", "prune old logs", "db01")

        found = (await client.get("/artifacts", params={"q": "nginx"})).json()

        assert [a["id"] for a in found["items"]] == ["2026-08-21-restart-nginx"]

    async def test_it_finds_an_artifact_by_the_host_it_targets(self, api):
        client, _repo, store = api
        await self._write(store, "2026-08-21-restart-nginx", "restart nginx on the edge", "web01")
        await self._write(store, "2026-08-21-prune-logs", "prune old logs", "db01")

        found = (await client.get("/artifacts", params={"q": "db01"})).json()

        assert [a["id"] for a in found["items"]] == ["2026-08-21-prune-logs"], (
            "an artifact must be findable by the machine it acts on - that is how "
            "an operator remembers it"
        )

    async def test_the_search_is_case_insensitive(self, api):
        client, _repo, store = api
        await self._write(store, "2026-08-21-restart-nginx", "Restart NGINX on the edge", "web01")

        found = (await client.get("/artifacts", params={"q": "nginx"})).json()

        assert len(found["items"]) == 1

    async def test_search_composes_with_the_status_filter(self, api):
        client, _repo, store = api
        await self._write(store, "2026-08-21-restart-nginx", "restart nginx", "web01")

        both = (await client.get("/artifacts", params={"q": "nginx", "status": "applied"})).json()

        assert both["items"] == []
