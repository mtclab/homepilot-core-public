"""The artifact archive push actually pushes (#442 follow-up).

THE DEFECT THIS FORBIDS: `artifacts_remote` was a setting whose self-check
promised "the next sync" while nothing anywhere synced. An operator who set it
had an off-box copy of NOTHING - discovered when the volume died.

The gates drive the REAL machinery: a real ArtifactStore with a real bare git
repository as its remote. The journey: write an artifact, run the reconciler,
and the BARE REPO contains the commit. Failure is recorded where the
self-check reads it, and the self-check turns broken - configured and working
are different claims now.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from homepilot.artifacts.store import ArtifactStore
from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.db.repository import Repository
from homepilot.reconciler.archive_push import (
    LAST_PUSH_ERROR,
    LAST_PUSH_OK,
    ArchivePushReconciler,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def repo(tmp_path: Path):
    db = Database(str(tmp_path / "archive.db"))
    await db.connect()
    await run_migrations(db)
    try:
        yield Repository(db)
    finally:
        await db.close()


@pytest.fixture
def bare_remote(tmp_path: Path) -> Path:
    remote = tmp_path / "archive.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    return remote


@pytest.fixture
def store(tmp_path: Path, bare_remote: Path) -> ArtifactStore:
    return ArtifactStore(tmp_path / "artifacts", remote=str(bare_remote))


def _remote_files(bare_remote: Path) -> list[str]:
    result = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", "HEAD"],
        cwd=bare_remote,
        capture_output=True,
        text=True,
    )
    return result.stdout.split()


class TestThePushIsReal:
    async def test_an_artifact_reaches_the_bare_remote(self, store, bare_remote, repo):
        store.write(
            "2026-08-archive-gate", "status: proposed", "real content", event="test artifact"
        )

        result = await ArchivePushReconciler(store, repo).run()

        assert result.success, result.details
        assert any("2026-08-archive-gate" in f for f in _remote_files(bare_remote)), (
            "the reconciler reported success but the remote holds nothing"
        )
        ok = await repo.get_setting(LAST_PUSH_OK)
        assert ok is not None and ok["value"] == "1"

    async def test_a_failed_push_is_recorded_not_swallowed(self, tmp_path, repo):
        # A remote that cannot exist: the push must fail, and the FAILURE must
        # land where the self-check reads it.
        store = ArtifactStore(
            tmp_path / "artifacts2", remote=str(tmp_path / "no-such-dir" / "gone.git")
        )
        store.write("2026-08-archive-gate", "status: proposed", "x", event="test")

        result = await ArchivePushReconciler(store, repo).run()

        assert not result.success
        ok = await repo.get_setting(LAST_PUSH_OK)
        err = await repo.get_setting(LAST_PUSH_ERROR)
        assert ok is not None and ok["value"] == "0"
        assert err is not None and err["value"], "the failure left no readable reason"


class TestTheSelfCheckStopsPromising:
    async def test_configured_plus_failed_push_reports_broken(self, repo):
        from types import SimpleNamespace

        from homepilot.selfcheck import _artifacts_remote_subsystem

        await repo.set_setting(LAST_PUSH_OK, "0")
        await repo.set_setting(LAST_PUSH_ERROR, "permission denied (publickey)")
        state = SimpleNamespace(repo=repo)
        settings = SimpleNamespace(artifacts_remote="git@example.com:me/archive.git")

        sub = _artifacts_remote_subsystem(state, settings)
        assert sub.configured
        assert sub.probe is not None, "the subsystem no longer verifies anything"
        assert await sub.probe() is False, (
            "a failed last push still reports as mirrored - the old lie"
        )
        assert "FAILED" in sub.broken

    async def test_first_run_is_unproven_not_ok(self, repo):
        """A push that has never run is not "the most recent push succeeded".

        This used to answer `True` - "(or the first one has not run yet)" - so
        an instance with no off-box copy at all read as green, indefinitely if
        the reconciler never ran. Unproven is its own state, and the report has
        one (#648 tranche 4).
        """
        from types import SimpleNamespace

        from homepilot.selfcheck import STATE_UNKNOWN, ProbeVerdict, _artifacts_remote_subsystem

        state = SimpleNamespace(repo=repo)
        settings = SimpleNamespace(artifacts_remote="git@example.com:me/archive.git")
        sub = _artifacts_remote_subsystem(state, settings)
        verdict = await sub.probe()
        assert isinstance(verdict, ProbeVerdict)
        assert verdict.state == STATE_UNKNOWN
        assert "no off-box copy" in verdict.consequence
        # And it does not alarm either: `unknown` is not `unreachable`.
        assert verdict.state != "unreachable"
