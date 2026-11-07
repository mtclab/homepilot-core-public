"""#701, backend half: whole-name targets, and a subscriber that falls behind is told.

1. The environment doc matched its target as a SUBSTRING: artifact history used
   ``target_json LIKE %web1%`` and the host list ``hostname LIKE %web1%``, so the
   page and the MCP ``get_environment_doc`` for ``web1`` listed artifacts and hosts
   of ``web10``. A target is matched as a whole name now, on the parsed target
   fields.

2. ``SSEBus.publish`` dropped an event for a subscriber whose queue was full and
   only logged it; that subscriber's page then showed a state that was no longer
   true with nothing to tell it so. A subscriber that misses an event is now
   marked, and its stream is ended, so the client reconnects and re-reads
   (the client-side resync is gated in web/src/lib/events.test.ts).

Teeth: restore the LIKE-only history query and the ``web10`` artifact appears
for ``web1``; restore the log-and-drop and ``missed()`` never turns true.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.db.repository import Repository
from homepilot.inventory.service import InventoryService


@pytest.fixture
async def repo(tmp_path: Path):
    db = Database(str(tmp_path / "h.db"))
    await db.connect()
    await run_migrations(db)
    yield Repository(db)
    await db.close()


async def _artifact(repo: Repository, aid: str, target: dict) -> None:
    await repo.upsert_artifact(
        id=aid,
        kind="ansible-playbook",
        intent=f"touch {aid}",
        status="applied",
        mutating=True,
        hash="h",
        target_json=json.dumps(target),
        idempotence="via-precheck",
        produced_by_json="{}",
        file_path=f"{aid}.md",
    )


async def test_artifact_history_matches_the_whole_name(repo):
    await repo.create_host(hostname="web1", host_type="vm", role="app", managed=True)
    await repo.create_host(hostname="web10", host_type="vm", role="app", managed=True)
    await _artifact(repo, "2026-09-26-web1-aaaaaa", {"kind": "host", "host": "web1"})
    await _artifact(repo, "2026-09-26-web10-bbbbbb", {"kind": "host", "host": "web10"})
    await _artifact(repo, "2026-09-26-node-cccccc", {"kind": "vm", "vmid": 101, "node": "web1"})
    await _artifact(repo, "2026-09-26-intent-dddddd", {"kind": "global", "host": "db1"})

    doc = await InventoryService(repo).get_environment_doc("web1")

    ids = {a["id"] for a in doc["artifact_history"]}
    assert ids == {"2026-09-26-web1-aaaaaa", "2026-09-26-node-cccccc"}, ids
    assert [h["hostname"] for h in doc["hosts"]] == ["web1"]
    assert doc["sources"]["artifact_history"] == {"read": True}


async def test_whole_name_match_ignores_case(repo):
    await _artifact(repo, "2026-09-26-web1-aaaaaa", {"kind": "host", "host": "Web1"})
    doc = await InventoryService(repo).get_environment_doc("web1")
    assert [a["id"] for a in doc["artifact_history"]] == ["2026-09-26-web1-aaaaaa"]


async def test_a_subscriber_that_falls_behind_is_marked():
    from homepilot.sse import SSEBus

    bus = SSEBus()
    q = bus.subscribe()
    assert q is not None
    for i in range(q.maxsize):
        await bus.publish("artifact_applied", {"id": str(i)})
    assert not bus.missed(q)

    await bus.publish("artifact_applied", {"id": "one too many"})

    assert bus.missed(q), "an event was dropped and nothing records that this subscriber missed it"
    bus.unsubscribe(q)
    assert not bus.missed(q)
