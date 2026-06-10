import sqlite3
from unittest.mock import AsyncMock

from homepilot.artifacts.lifecycle import ArtifactLifecycle


def _make_spec(**overrides):
    spec = {
        "id": "2025-01-01-test-artifact-abc123",
        "kind": "ansible-playbook",
        "intent": "Install nginx on web server",
        "body": "---\n- name: Install nginx\n  hosts: all\n  tasks: []",
        "target": {"kind": "vm", "vmid": 100, "node": "pve1"},
        "idempotence": "via-precheck",
        "produced_by": {"session": "s1", "agent": "a1", "user": "u1"},
    }
    spec.update(overrides)
    return spec


class TestDualStateApprove:
    async def test_approve_writes_to_file_and_db(self, mock_store):
        db = AsyncMock()
        db.upsert_artifact = AsyncMock()
        lc = ArtifactLifecycle(store=mock_store, repository=db)
        aid = await lc.propose(_make_spec())
        db.upsert_artifact.reset_mock()

        await lc.approve(aid, user="admin")

        fm, _ = mock_store.read(aid)
        assert fm["status"] == "approved"
        assert fm["approved_by"]["user"] == "admin"
        db.upsert_artifact.assert_called_once()
        call_kwargs = db.upsert_artifact.call_args
        assert call_kwargs.kwargs["status"] == "approved"
        assert call_kwargs.kwargs["id"] == aid

    async def test_reject_writes_to_file_and_db(self, mock_store):
        db = AsyncMock()
        db.upsert_artifact = AsyncMock()
        lc = ArtifactLifecycle(store=mock_store, repository=db)
        aid = await lc.propose(_make_spec())
        db.upsert_artifact.reset_mock()

        await lc.reject(aid, user="admin", reason="bad")

        fm, _ = mock_store.read(aid)
        assert fm["status"] == "rejected"
        db.upsert_artifact.assert_called_once()
        call_kwargs = db.upsert_artifact.call_args
        assert call_kwargs.kwargs["status"] == "rejected"

    async def test_mark_applied_writes_to_file_and_db(self, mock_store):
        db = AsyncMock()
        db.upsert_artifact = AsyncMock()
        lc = ArtifactLifecycle(store=mock_store, repository=db)
        aid = await lc.propose(_make_spec())
        await lc.approve(aid, user="admin")
        db.upsert_artifact.reset_mock()

        await lc.mark_applied(aid, execution_log="done")

        fm, _ = mock_store.read(aid)
        assert fm["status"] == "applied"
        db.upsert_artifact.assert_called_once()
        call_kwargs = db.upsert_artifact.call_args
        assert call_kwargs.kwargs["status"] == "applied"

    async def test_mark_failed_writes_to_file_and_db(self, mock_store):
        db = AsyncMock()
        db.upsert_artifact = AsyncMock()
        lc = ArtifactLifecycle(store=mock_store, repository=db)
        aid = await lc.propose(_make_spec())
        await lc.approve(aid, user="admin")
        db.upsert_artifact.reset_mock()

        await lc.mark_failed(aid, reason="timeout")

        fm, _ = mock_store.read(aid)
        assert fm["status"] == "failed"
        db.upsert_artifact.assert_called_once()
        call_kwargs = db.upsert_artifact.call_args
        assert call_kwargs.kwargs["status"] == "failed"

    async def test_supersede_writes_to_file_and_db(self, mock_store):
        db = AsyncMock()
        db.upsert_artifact = AsyncMock()
        lc = ArtifactLifecycle(store=mock_store, repository=db)
        aid = await lc.propose(_make_spec())
        await lc.approve(aid, user="admin")
        await lc.mark_applied(aid, execution_log="done")
        db.upsert_artifact.reset_mock()

        await lc.supersede(aid, "2025-01-02-new-artifact-def456")

        fm, _ = mock_store.read(aid)
        assert fm["status"] == "superseded"
        db.upsert_artifact.assert_called_once()
        call_kwargs = db.upsert_artifact.call_args
        assert call_kwargs.kwargs["status"] == "superseded"

    async def test_revoke_writes_to_file_and_db(self, mock_store):
        db = AsyncMock()
        db.upsert_artifact = AsyncMock()
        lc = ArtifactLifecycle(store=mock_store, repository=db)
        aid = await lc.propose(_make_spec())
        await lc.approve(aid, user="admin")
        db.upsert_artifact.reset_mock()

        await lc.revoke(aid, user="admin", reason="obsolete")

        fm, _ = mock_store.read(aid)
        assert fm["status"] == "revoked"
        db.upsert_artifact.assert_called_once()
        call_kwargs = db.upsert_artifact.call_args
        assert call_kwargs.kwargs["status"] == "revoked"

    async def test_propose_writes_to_db(self, mock_store):
        db = AsyncMock()
        db.upsert_artifact = AsyncMock()
        lc = ArtifactLifecycle(store=mock_store, repository=db)

        aid = await lc.propose(_make_spec())

        fm, _ = mock_store.read(aid)
        assert fm["status"] == "proposed"
        db.upsert_artifact.assert_called_once()
        call_kwargs = db.upsert_artifact.call_args
        assert call_kwargs.kwargs["status"] == "proposed"
        assert call_kwargs.kwargs["id"] == aid


class TestDualStateDBFailureIsolation:
    async def test_approve_succeeds_even_if_db_fails(self, mock_store):
        db = AsyncMock()
        db.upsert_artifact = AsyncMock(side_effect=sqlite3.OperationalError("DB down"))
        lc = ArtifactLifecycle(store=mock_store, repository=db)
        aid = await lc.propose(_make_spec())
        db.upsert_artifact.reset_mock()
        db.upsert_artifact.side_effect = sqlite3.OperationalError("DB down")

        await lc.approve(aid, user="admin")

        fm, _ = mock_store.read(aid)
        assert fm["status"] == "approved"
        db.upsert_artifact.assert_called_once()

    async def test_reject_succeeds_even_if_db_fails(self, mock_store):
        db = AsyncMock()
        lc = ArtifactLifecycle(store=mock_store, repository=db)
        aid = await lc.propose(_make_spec())
        db.upsert_artifact.side_effect = sqlite3.OperationalError("DB down")

        await lc.reject(aid, user="admin")

        fm, _ = mock_store.read(aid)
        assert fm["status"] == "rejected"

    async def test_mark_applied_succeeds_even_if_db_fails(self, mock_store):
        db = AsyncMock()
        lc = ArtifactLifecycle(store=mock_store, repository=db)
        aid = await lc.propose(_make_spec())
        await lc.approve(aid, user="admin")
        db.upsert_artifact.side_effect = sqlite3.OperationalError("DB down")

        await lc.mark_applied(aid, execution_log="done")

        fm, _ = mock_store.read(aid)
        assert fm["status"] == "applied"

    async def test_revoke_succeeds_even_if_db_fails(self, mock_store):
        db = AsyncMock()
        lc = ArtifactLifecycle(store=mock_store, repository=db)
        aid = await lc.propose(_make_spec())
        await lc.approve(aid, user="admin")
        db.upsert_artifact.side_effect = sqlite3.OperationalError("DB down")

        await lc.revoke(aid, user="admin")

        fm, _ = mock_store.read(aid)
        assert fm["status"] == "revoked"

    async def test_propose_succeeds_even_if_db_fails(self, mock_store):
        db = AsyncMock()
        db.upsert_artifact = AsyncMock(side_effect=sqlite3.OperationalError("DB down"))
        lc = ArtifactLifecycle(store=mock_store, repository=db)

        aid = await lc.propose(_make_spec())

        fm, _ = mock_store.read(aid)
        assert fm["status"] == "proposed"


class TestDualStateNoDB:
    async def test_approve_works_without_db(self, mock_store):
        lc = ArtifactLifecycle(store=mock_store, repository=None)
        aid = await lc.propose(_make_spec())
        await lc.approve(aid, user="admin")
        fm, _ = mock_store.read(aid)
        assert fm["status"] == "approved"

    async def test_reject_works_without_db(self, mock_store):
        lc = ArtifactLifecycle(store=mock_store, repository=None)
        aid = await lc.propose(_make_spec())
        await lc.reject(aid, user="admin")
        fm, _ = mock_store.read(aid)
        assert fm["status"] == "rejected"
