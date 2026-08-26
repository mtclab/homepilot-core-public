"""The artifacts DB mirror holds EVERY artifact, not just the first (#545).

`lifecycle.propose` used to write every mirror row with `file_path=""`, and the
column is NOT NULL UNIQUE - so the second-and-later propose failed the insert
(caught and logged, invisible in the API because reads go to the file store).
The mirror silently held at most one row, which is a lie waiting for the first
consumer that trusts it. These gates pin the fix: the mirror records the
store-relative path, one row per artifact, and a pre-fix row heals on the next
status sync.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from homepilot.artifacts.lifecycle import ArtifactLifecycle
from homepilot.artifacts.store import ArtifactStore
from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.db.repository import Repository


def _spec(artifact_id: str, intent: str) -> dict:
    return {
        "id": artifact_id,
        "kind": "http-sequence",
        "intent": intent,
        "body": "```yaml http-sequence\nsteps:\n  - method: GET\n    path: /health\n```\n",
        "target": {"kind": "service", "service": "demo", "vmid": 100, "node": "pve1"},
        "idempotence": "via-precheck",
        "produced_by": {"session": "s1", "agent": "a1", "user": "operator"},
    }


@pytest.fixture
async def mirror(tmp_path: Path):
    db = Database(str(tmp_path / "homepilot.db"))
    await db.connect()
    await run_migrations(db)
    repo = Repository(db)
    store = ArtifactStore(tmp_path / "artifacts")
    lifecycle = ArtifactLifecycle(store=store, repository=repo)
    try:
        yield repo, store, lifecycle, db
    finally:
        await db.close()


class TestEveryArtifactGetsAMirrorRow:
    async def test_two_proposes_yield_two_rows_with_their_paths(self, mirror) -> None:
        repo, store, lifecycle, _ = mirror
        a = "2026-08-26-mirror-first-a1b2c3"
        b = "2026-08-26-mirror-second-d4e5f6"
        await lifecycle.propose(_spec(a, "first"))
        await lifecycle.propose(_spec(b, "second"))

        rows = {r["id"]: r for r in [await repo.get_artifact(a), await repo.get_artifact(b)] if r}
        assert set(rows) == {a, b}, (
            f"the mirror holds {sorted(rows)} - a missing row means the propose "
            "insert failed again (#545)"
        )
        for artifact_id, row in rows.items():
            assert row["file_path"] == store.relative_path(artifact_id)
            # The path the mirror records must be the file the store wrote.
            assert (store.root / row["file_path"]).is_file()

    async def test_relative_path_is_store_relative_and_unique_per_id(self, mirror) -> None:
        _, store, _, _ = mirror
        a = store.relative_path("2026-08-26-mirror-first-a1b2c3")
        b = store.relative_path("2026-08-26-mirror-second-d4e5f6")
        assert a != b
        assert not Path(a).is_absolute()
        assert a == "2026/08/2026-08-26-mirror-first-a1b2c3.md"

    async def test_a_stale_empty_path_row_heals_on_the_next_upsert(self, mirror) -> None:
        repo, store, lifecycle, db = mirror
        a = "2026-08-26-mirror-heals-a9b8c7"
        await lifecycle.propose(_spec(a, "heals"))
        # Regress the row to the pre-fix shape a live DB may still carry.
        await db.execute("UPDATE artifacts SET file_path = '' WHERE id = ?", (a,))
        await db.conn.commit()

        fm, _body = store.read(a)
        # Any status sync re-upserts; shaped like transitions._sync_to_db, which
        # is the call that runs on every approve/apply/reject.
        import json

        await repo.upsert_artifact(
            id=a,
            kind=fm["kind"],
            intent=fm.get("intent", ""),
            status=fm["status"],
            hash=fm.get("hash"),
            produced_by_json=json.dumps(fm.get("produced_by")),
            file_path=store.relative_path(a),
        )
        row = await repo.get_artifact(a)
        assert row is not None and row["file_path"] == store.relative_path(a), (
            "the conflict-update must repair file_path, or pre-fix rows stay empty forever"
        )
