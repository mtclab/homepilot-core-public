"""#622: the KB's background reindex is drained before its database closes.

`KBService.reindex_if_needed` starts the rebuild as a fire-and-forget task that
writes to the database throughout. Nothing awaited it: not the lifespan's
shutdown (which drains every other background writer before closing the
database, #496), and not the test fixtures that wire a KBService into a
lifecycle. A rebuild still running when its loop closed killed aiosqlite's
worker thread ("Event loop is closed"), which surfaced as a flaky failure in a
NEIGHBOURING test - test_592 in the full suite - and passed alone.

The gates: `drain()` really waits for the in-flight rebuild and its write
lands; the lifespan's drain list names the KB service; and an exception that
kills a thread fails the run instead of warning (pyproject filterwarnings).

Teeth: remove `KBService.drain` and the first test fails; drop the kb entry
from main.py's drainable list, or the filterwarnings line, and the others do.
"""

from __future__ import annotations

import asyncio
import tomllib
from pathlib import Path

import pytest

from homepilot.artifacts.lifecycle import ArtifactLifecycle
from homepilot.artifacts.store import ArtifactStore
from homepilot.kb.service import KBService

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
async def db(tmp_path: Path):
    from homepilot.db.connection import Database
    from homepilot.db.migrations import run_migrations

    database = Database(str(tmp_path / "drain.db"))
    await database.connect()
    await run_migrations(database)
    yield database
    await database.close()


async def test_drain_waits_for_the_running_rebuild_and_its_write_lands(db, tmp_path: Path):
    from homepilot.db.repository import Repository

    repo = Repository(db)
    store_dir = tmp_path / "artifacts"
    store_dir.mkdir()
    store = ArtifactStore(store_dir)
    svc = KBService(repo=repo, store=store, lifecycle=ArtifactLifecycle(store, repo))

    started = asyncio.Event()

    async def slow_rebuild(**_kwargs):
        started.set()
        await asyncio.sleep(0.2)
        await repo.create_doc_metadata(
            source="test:drained", title="drained", content="landed", kind="note", target=None
        )
        return {"indexed": 1}

    svc.reindex = slow_rebuild  # type: ignore[method-assign]
    svc._reindex_task = asyncio.create_task(svc._run_reindex("test"))
    await started.wait()

    await svc.drain()

    assert svc._reindex_task.done() and not svc._reindex_task.cancelled()
    row = await db.fetchone("SELECT content FROM doc_metadata WHERE source = 'test:drained'")
    assert row is not None and row["content"] == "landed", "the rebuild's write did not land"


async def test_drain_with_nothing_running_is_a_no_op(db, tmp_path: Path):
    from homepilot.db.repository import Repository

    repo = Repository(db)
    store_dir = tmp_path / "artifacts"
    store_dir.mkdir()
    store = ArtifactStore(store_dir)
    svc = KBService(repo=repo, store=store, lifecycle=ArtifactLifecycle(store, repo))
    await svc.drain()


def test_the_lifespan_drains_the_kb_service_before_the_database_closes() -> None:
    src = (REPO / "src" / "homepilot" / "main.py").read_text()
    drain_list = src.index("drainable = (")
    entry = src.find('("kb service", getattr(app.state, "kb_service", None))', drain_list)
    close = src.index("state.database.close()", drain_list)
    assert entry != -1, "the KB service is not in the lifespan's drain list"
    assert entry < close, "the KB service must be drained BEFORE the database closes"


def test_a_thread_killed_by_an_exception_fails_the_run() -> None:
    config = tomllib.loads((REPO / "pyproject.toml").read_text())
    filters = config["tool"]["pytest"]["ini_options"].get("filterwarnings", [])
    assert "error::pytest.PytestUnhandledThreadExceptionWarning" in filters
