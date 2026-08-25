"""Human-relay approval over MCP (#385 follow-up): the per-artifact approval code.

The assistant proposes but must never approve its own change. A random per-
artifact code, generated at propose and returned by NO MCP read, is the bridge: a
human reads it from an operator surface and relays it, so a valid code is proof a
human decided. These gates prove, with teeth:

* the code NEVER leaks through an MCP read (get_artifact / query_artifacts / the
  artifact file bytes) - the critical property;
* wrong code refused (artifact stays proposed), right code approves, the code is
  then cleared (replay refused);
* five wrong codes lock the artifact until an operator reset; the lock is
  per-artifact;
* approve with no code is refused (the self-approve guard);
* the code IS surfaced on the webhook payload and the management API GET, and is
  NOT on the MCP get_artifact.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from homepilot.artifacts.approval_code import LOCK_THRESHOLD
from homepilot.artifacts.lifecycle import ArtifactLifecycle
from homepilot.artifacts.store import ArtifactStore
from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.db.repository import Repository
from homepilot.mcp.server import _handle_tool


@pytest.fixture(autouse=True)
def _clear_approve_ratelimit():
    """The per-caller call-frequency limiter is module state (10/min). Clear it
    around each test so one test's calls cannot starve another's lock attempts."""
    from homepilot.mcp.tools import artifact_tools

    artifact_tools._approve_ratelimit.clear()
    yield
    artifact_tools._approve_ratelimit.clear()


def _spec(aid: str = "2025-03-01-install-nginx-abc123") -> dict[str, Any]:
    return {
        "id": aid,
        "kind": "ansible-playbook",
        "intent": "Install nginx on web server",
        "body": "---\n- name: Install nginx\n  hosts: all\n  tasks: []",
        "target": {"kind": "vm", "vmid": 100, "node": "pve1"},
        "idempotence": "via-precheck",
        "produced_by": {"session": "s1", "agent": "a1", "user": "u1"},
    }


@pytest.fixture
async def repo(tmp_path: Path):
    database = Database(str(tmp_path / "hp.db"))
    await database.connect()
    await run_migrations(database)
    yield Repository(database)
    await database.close()


@pytest.fixture
def store(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore(tmp_path / "artifacts")


@pytest.fixture
def lifecycle(store: ArtifactStore, repo: Repository) -> ArtifactLifecycle:
    return ArtifactLifecycle(store, repository=repo)


def _ctx(lifecycle: ArtifactLifecycle, store: ArtifactStore, repo: Repository) -> dict[str, Any]:
    return {
        "repo": repo,
        "lifecycle": lifecycle,
        "store": store,
        "_mcp_caller_id": "mcp-test",
        "_mcp_token_scope": "full",
    }


async def _stored_code(repo: Repository, aid: str) -> str:
    row = await repo.get_approval_code_row(aid)
    assert row is not None, "expected an approval code row for a proposed artifact"
    return str(row["code"])


def _contains(obj: Any, needle: str) -> bool:
    """True if the string `needle` appears anywhere in a nested JSON-ish value."""
    if isinstance(obj, str):
        return needle in obj
    if isinstance(obj, dict):
        return any(_contains(k, needle) or _contains(v, needle) for k, v in obj.items())
    if isinstance(obj, (list, tuple)):
        return any(_contains(v, needle) for v in obj)
    return _contains(str(obj), needle) if obj is not None else False


# ── The leak gate (critical) ─────────────────────────────────────────────────


class TestLeakGate:
    async def test_code_absent_from_every_mcp_read(self, lifecycle, store, repo):
        aid = await lifecycle.propose(_spec())
        code = await _stored_code(repo, aid)
        ctx = _ctx(lifecycle, store, repo)

        got = await _handle_tool("get_artifact", {"artifact_id": aid}, ctx)
        assert not _contains(got, code), "approval code leaked through get_artifact"

        q = await _handle_tool("query_artifacts", {"filter": None}, ctx)
        assert not _contains(q, code), "approval code leaked through query_artifacts"

        status = await _handle_tool("get_artifact_status", {"artifact_id": aid}, ctx)
        assert not _contains(status, code), "approval code leaked through get_artifact_status"

    async def test_code_absent_from_the_artifact_file_bytes(self, lifecycle, store, repo):
        aid = await lifecycle.propose(_spec())
        code = await _stored_code(repo, aid)
        raw = store.resolve_path(aid).read_bytes()
        assert code.encode() not in raw, "approval code is written into the artifact file"

    async def test_the_leak_check_has_teeth(self, lifecycle, store, repo):
        """If the code WERE planted in a response, the gate must catch it. A green
        leak gate that stays green with the code present is worthless."""
        aid = await lifecycle.propose(_spec())
        code = await _stored_code(repo, aid)
        # Simulate the regression: get_artifact returning the code in frontmatter.
        planted = {"frontmatter": {"id": aid, "approval_code": code}, "body": ""}
        assert _contains(planted, code), "the leak detector cannot see a planted code"

    async def test_file_byte_check_has_teeth(self, lifecycle, store, repo):
        aid = await lifecycle.propose(_spec())
        code = await _stored_code(repo, aid)
        path = store.resolve_path(aid)
        path.write_text(path.read_text() + f"\ncode: {code}\n", encoding="utf-8")
        assert code.encode() in path.read_bytes(), "byte-level leak check has no teeth"


# ── Approve flow ─────────────────────────────────────────────────────────────


class TestApproveFlow:
    async def test_wrong_code_refused_artifact_stays_proposed(self, lifecycle, store, repo):
        aid = await lifecycle.propose(_spec())
        ctx = _ctx(lifecycle, store, repo)
        with pytest.raises(ValueError, match="Incorrect approval code"):
            await _handle_tool(
                "approve_artifact", {"artifact_id": aid, "approval_code": "WRONGCODE0"}, ctx
            )
        fm, _ = store.read(aid)
        assert fm["status"] == "proposed", "a wrong code must not transition the artifact"

    async def test_right_code_approves(self, lifecycle, store, repo):
        aid = await lifecycle.propose(_spec())
        code = await _stored_code(repo, aid)
        ctx = _ctx(lifecycle, store, repo)
        result = await _handle_tool(
            "approve_artifact", {"artifact_id": aid, "approval_code": code}, ctx
        )
        assert result["status"] == "approved"
        fm, _ = store.read(aid)
        assert fm["status"] == "approved"
        # Actor recorded as an operator-via-code, NOT the assistant/caller.
        assert fm["approved_by"]["user"] == "operator-code via MCP"

    async def test_grouped_and_lowercased_code_still_verifies(self, lifecycle, store, repo):
        aid = await lifecycle.propose(_spec())
        code = await _stored_code(repo, aid)
        relayed = f"{code[:5]}-{code[5:]}".lower()  # how a human might retype it
        ctx = _ctx(lifecycle, store, repo)
        result = await _handle_tool(
            "approve_artifact", {"artifact_id": aid, "approval_code": relayed}, ctx
        )
        assert result["status"] == "approved"

    async def test_code_cleared_after_approval_replay_refused(self, lifecycle, store, repo):
        aid = await lifecycle.propose(_spec())
        code = await _stored_code(repo, aid)
        ctx = _ctx(lifecycle, store, repo)
        await _handle_tool("approve_artifact", {"artifact_id": aid, "approval_code": code}, ctx)
        assert await repo.get_approval_code_row(aid) is None, "spent code must be cleared"
        # Replaying the same code cannot re-approve.
        with pytest.raises(ValueError, match="not awaiting a coded approval"):
            await _handle_tool("approve_artifact", {"artifact_id": aid, "approval_code": code}, ctx)

    async def test_reject_clears_code(self, lifecycle, store, repo):
        aid = await lifecycle.propose(_spec())
        assert await repo.get_approval_code_row(aid) is not None
        await lifecycle.reject(aid, user="admin")
        assert await repo.get_approval_code_row(aid) is None


# ── Self-approve guard ───────────────────────────────────────────────────────


class TestSelfApproveGuard:
    async def test_missing_code_refused_no_transition(self, lifecycle, store, repo):
        aid = await lifecycle.propose(_spec())
        ctx = _ctx(lifecycle, store, repo)
        with pytest.raises(ValueError, match="requires an approval_code"):
            await _handle_tool("approve_artifact", {"artifact_id": aid}, ctx)
        fm, _ = store.read(aid)
        assert fm["status"] == "proposed"

    async def test_blank_code_refused(self, lifecycle, store, repo):
        aid = await lifecycle.propose(_spec())
        ctx = _ctx(lifecycle, store, repo)
        with pytest.raises(ValueError, match="requires an approval_code"):
            await _handle_tool(
                "approve_artifact", {"artifact_id": aid, "approval_code": "   "}, ctx
            )


# ── Brute-force lock ─────────────────────────────────────────────────────────


class TestBruteForceLock:
    async def test_lock_after_threshold_then_reset(self, lifecycle, store, repo):
        aid = await lifecycle.propose(_spec())
        code = await _stored_code(repo, aid)
        ctx = _ctx(lifecycle, store, repo)

        # LOCK_THRESHOLD wrong codes. The last one reports the lock.
        for _ in range(LOCK_THRESHOLD):
            with pytest.raises(ValueError, match=r"[Ii]ncorrect approval code|locked"):
                await _handle_tool(
                    "approve_artifact", {"artifact_id": aid, "approval_code": "BADCODEXYZ"}, ctx
                )
        row = await repo.get_approval_code_row(aid)
        assert int(row["locked"]) == 1, "artifact must be locked at the threshold"

        # Even the CORRECT code is now refused while locked.
        with pytest.raises(ValueError, match="locked"):
            await _handle_tool("approve_artifact", {"artifact_id": aid, "approval_code": code}, ctx)
        fm, _ = store.read(aid)
        assert fm["status"] == "proposed"

        # Operator reset, then the correct code works.
        assert await repo.reset_approval_lock(aid) is True
        result = await _handle_tool(
            "approve_artifact", {"artifact_id": aid, "approval_code": code}, ctx
        )
        assert result["status"] == "approved"

    async def test_lock_is_per_artifact(self, lifecycle, store, repo):
        aid1 = await lifecycle.propose(_spec("2025-03-02-lock-one-aaa111"))
        aid2 = await lifecycle.propose(_spec("2025-03-02-lock-two-bbb222"))
        code2 = await _stored_code(repo, aid2)
        ctx = _ctx(lifecycle, store, repo)

        for _ in range(LOCK_THRESHOLD):
            with pytest.raises(ValueError):
                await _handle_tool(
                    "approve_artifact", {"artifact_id": aid1, "approval_code": "BADCODEXYZ"}, ctx
                )
        assert int((await repo.get_approval_code_row(aid1))["locked"]) == 1
        # aid2 is untouched: its correct code still approves.
        row2 = await repo.get_approval_code_row(aid2)
        assert int(row2["locked"]) == 0
        result = await _handle_tool(
            "approve_artifact", {"artifact_id": aid2, "approval_code": code2}, ctx
        )
        assert result["status"] == "approved"


# ── Surfacing (operator surfaces get the code; MCP does not) ──────────────────


class TestSurfacing:
    async def test_webhook_payload_carries_the_code(self, store, repo, monkeypatch):
        captured: list[tuple[str, dict[str, Any]]] = []

        async def _fake_emit(event_type: str, payload: dict[str, Any], repo: Any = None) -> None:
            captured.append((event_type, payload))

        monkeypatch.setattr("homepilot.artifacts.lifecycle.emit_event", _fake_emit)
        lifecycle = ArtifactLifecycle(store, repository=repo)
        aid = await lifecycle.propose(_spec())
        code = await _stored_code(repo, aid)

        proposed = [p for e, p in captured if e == "artifact_proposed"]
        assert proposed, "artifact_proposed event was not emitted"
        assert proposed[0]["approval_code"] == code, "webhook payload omits the approval code"

    async def test_api_get_surfaces_code_but_mcp_get_does_not(self, lifecycle, store, repo):
        from homepilot.artifacts.approval_code import normalize_code

        aid = await lifecycle.propose(_spec())
        code = await _stored_code(repo, aid)

        # The MCP get_artifact must not carry it.
        ctx = _ctx(lifecycle, store, repo)
        mcp = await _handle_tool("get_artifact", {"artifact_id": aid}, ctx)
        assert not _contains(mcp, code)

        # The management API GET route DOES surface it (this is the web review
        # screen's source). Drive the route function directly with a fake request.
        from homepilot.artifacts.router import get_artifact as api_get_artifact

        class _FakeTaskRepo:
            async def get_active_task(self, _id: str) -> None:
                return None

        class _State:
            def __init__(self) -> None:
                self.artifact_store = store
                self.task_repo = _FakeTaskRepo()
                self.repo = repo

        class _App:
            def __init__(self) -> None:
                self.state = _State()

        class _Req:
            def __init__(self) -> None:
                self.app = _App()

        detail = await api_get_artifact(_Req(), aid)  # type: ignore[arg-type]
        assert detail["approval_code"] is not None
        # Displayed grouped; normalising recovers the stored code.
        assert normalize_code(detail["approval_code"]) == code
        assert detail["approval_locked"] is False


# ── Migration ────────────────────────────────────────────────────────────────


async def _tables(db: Database) -> set[str]:
    rows = await db.fetchall("SELECT name FROM sqlite_master WHERE type='table'")
    return {r["name"] for r in rows}


class TestMigration:
    async def test_fresh_db_has_the_table(self, tmp_path: Path):
        db = Database(str(tmp_path / "fresh.db"))
        await db.connect()
        try:
            await run_migrations(db)
            assert "artifact_approval_codes" in await _tables(db)
        finally:
            await db.close()

    async def test_upgrade_over_existing_db_adds_the_table_and_keeps_data(
        self, tmp_path: Path, monkeypatch
    ):
        from homepilot.db import migrations as mig

        # Bring a DB up to the version BEFORE this feature's migration.
        target = max(mig.MIGRATIONS.keys())
        feature_version = target  # 27 is the last key; the table lives there
        saved = mig.MIGRATIONS[feature_version]
        monkeypatch.delitem(mig.MIGRATIONS, feature_version)

        db = Database(str(tmp_path / "upgrade.db"))
        await db.connect()
        try:
            await run_migrations(db)
            assert "artifact_approval_codes" not in await _tables(db)
            # A row of pre-existing data to prove the upgrade is non-destructive.
            await db.execute(
                "INSERT INTO settings (key, value, updated_at) VALUES ('probe', 'keep', 't')"
            )
            await db.conn.commit()

            # Restore the feature migration and upgrade in place.
            monkeypatch.setitem(mig.MIGRATIONS, feature_version, saved)
            await run_migrations(db)
            assert "artifact_approval_codes" in await _tables(db)
            probe = await db.fetchone("SELECT value FROM settings WHERE key='probe'")
            assert probe is not None and probe["value"] == "keep"
        finally:
            await db.close()

    async def test_existing_proposed_artifact_gets_code_lazily(self, store, repo):
        """Backfill: an artifact proposed before the feature (no code row) gets one
        the first time an operator surface reads it."""
        from homepilot.artifacts.approval_code import ensure_approval_code

        # Propose WITHOUT the code (simulate a pre-feature proposal) by clearing it.
        lifecycle = ArtifactLifecycle(store, repository=repo)
        aid = await lifecycle.propose(_spec())
        await repo.clear_approval_code(aid)
        assert await repo.get_approval_code_row(aid) is None

        code = await ensure_approval_code(repo, aid)
        assert code and await repo.get_approval_code_row(aid) is not None
        # Idempotent: a second read returns the SAME code.
        assert await ensure_approval_code(repo, aid) == code
