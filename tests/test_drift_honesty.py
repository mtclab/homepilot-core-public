"""Drift never reports "in spec" for something it did not check (#425).

`VerifyResult.drifted` was a BOOLEAN, so every unverifiable path and every
errored one returned `drifted=False` - and the UI rendered that as a green
"✓ ok". `shell-script: unverifiable`, `no_spec`, `no_host`, `no_executor`, an
unreachable precheck, a timeout, a raised exception: all green.

Ansible was the worst case. The verifier called `executor.ssh.exec(...)` on an
attribute that went with the jump server, so it raised AttributeError on every
run, the broad handler swallowed it, and EVERY applied ansible artifact reported
in-spec forever having checked nothing.

The dashboard's headline number was computed as `(total - drifted) / total`, so
the first screen an operator sees was inflated by exactly the artifacts nobody
had looked at.

These gates assert the distinction the boolean could not make: "I looked and it
matches" is not the same answer as "I could not look".
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from homepilot.artifacts.lifecycle import ArtifactLifecycle
from homepilot.artifacts.store import ArtifactStore
from homepilot.auth.deps import require_token
from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.db.repository import Repository
from homepilot.reconciler.verify import DriftState, VerifyResult, verify_artifact

pytestmark = pytest.mark.asyncio

ANSIBLE_BODY = """\
## Plan
Install nginx
## Spec
```yaml ansible-spec
- hosts: all
  tasks:
    - name: nginx
      package:
        name: nginx
```
"""

SHELL_BODY = """\
## Plan
Do the thing
## Idempotence preamble
Idempotent: guarded.
## Spec
```bash shell-spec
#!/bin/bash
echo apply
```
"""


def _spec(artifact_id: str, kind: str, body: str) -> dict:
    return {
        "id": artifact_id,
        "kind": kind,
        "intent": "Drift honesty",
        "body": body,
        "target": {"kind": "vm", "vmid": 100, "node": "pve1", "host": "web1"},
        "idempotence": "via-precheck",
        "produced_by": {"session": "s1", "agent": "a1", "user": "u1"},
    }


@pytest.fixture
async def env(tmp_path: Path):
    db = Database(str(tmp_path / "homepilot.db"))
    await db.connect()
    await run_migrations(db)
    repo = Repository(db)
    store = ArtifactStore(tmp_path / "artifacts")
    lifecycle = ArtifactLifecycle(store=store, repository=repo)
    yield repo, store, lifecycle
    await db.close()


async def _applied(lifecycle: ArtifactLifecycle, artifact_id: str, kind: str, body: str) -> None:
    await lifecycle.propose(_spec(artifact_id, kind, body))
    await lifecycle.approve(artifact_id, user="admin")
    await lifecycle.mark_applied(artifact_id, execution_log="applied")


class TestAnUncheckedArtifactIsNotInSpec:
    async def test_an_ansible_artifact_reports_unknown_not_green(self, env):
        """THE #425 headline: ansible drift is dead, and it reported health."""
        repo, store, lifecycle = env
        await _applied(lifecycle, "2026-08-21-ansible-drift", "ansible-playbook", ANSIBLE_BODY)

        result = await verify_artifact(
            "2026-08-21-ansible-drift", repo=repo, store=store, executor=MagicMock(pve_nodes=[])
        )

        assert result.state is DriftState.UNKNOWN, (
            "an ansible artifact that was never checked is reporting a real verdict"
        )
        assert result.details["reason"] == "ansible_unverifiable"

    async def test_a_shell_script_reports_unknown(self, env):
        repo, store, lifecycle = env
        await _applied(lifecycle, "2026-08-21-shell-drift", "shell-script", SHELL_BODY)

        result = await verify_artifact(
            "2026-08-21-shell-drift", repo=repo, store=store, executor=MagicMock(pve_nodes=[])
        )

        assert result.state is DriftState.UNKNOWN

    async def test_no_executor_reports_unknown(self, env):
        repo, store, lifecycle = env
        await _applied(lifecycle, "2026-08-21-no-exec", "ansible-playbook", ANSIBLE_BODY)

        result = await verify_artifact("2026-08-21-no-exec", repo=repo, store=store, executor=None)

        assert result.state is DriftState.UNKNOWN
        assert result.details["reason"] == "no_executor"

    async def test_the_default_state_is_unknown(self):
        """Fail-safe by construction: a path that forgets to say reads as "not
        established", never as a green tick."""
        assert VerifyResult(artifact_id="x").state is DriftState.UNKNOWN

    async def test_a_real_verdict_still_sets_drifted(self):
        """The two names for one fact cannot disagree."""
        assert VerifyResult(artifact_id="x", state=DriftState.DRIFTED).drifted is True
        assert VerifyResult(artifact_id="x", state=DriftState.IN_SPEC).drifted is False


class TestTheStateIsStored:
    async def test_an_unknown_check_is_not_stored_as_healthy(self, env):
        repo, _store, _lifecycle = env

        await repo.upsert_drift_check(
            artifact_id="2026-08-21-stored", drifted=False, state="unknown"
        )

        row = await repo.get_drift_check("2026-08-21-stored")
        assert row["state"] == "unknown"

    async def test_the_default_stored_state_is_unknown(self, env):
        """A caller that does not say must not be able to assert health."""
        repo, _store, _lifecycle = env

        await repo.upsert_drift_check(artifact_id="2026-08-21-silent", drifted=False)

        row = await repo.get_drift_check("2026-08-21-silent")
        assert row["state"] == "unknown"


class TestTheDashboardDoesNotCountUncheckedAsHealthy:
    @pytest.fixture
    async def api(self, tmp_path: Path):
        from homepilot.dashboard.router import router as dashboard_router

        db = Database(str(tmp_path / "dash.db"))
        await db.connect()
        await run_migrations(db)
        repo = Repository(db)
        app = FastAPI()
        app.include_router(dashboard_router)
        app.state.repo = repo
        app.dependency_overrides[require_token] = lambda: {"scope": "*", "role": "admin"}
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, repo
        await db.close()

    async def test_unchecked_artifacts_do_not_inflate_the_headline(self, api):
        """`(total - drifted) / total` counted every unverifiable artifact as
        healthy, on the first screen an operator sees."""
        client, repo = api
        # One checked and fine, one checked and drifting, eight never checked.
        # The honest number is 50% (of what was checked). The old formula,
        # (total - drifted) / total, reports 90% - it counts the eight unknowns
        # as healthy, which is the entire defect.
        await repo.upsert_drift_check("a", drifted=False, state="in_spec")
        await repo.upsert_drift_check("b", drifted=True, state="drifted")
        for n in range(8):
            await repo.upsert_drift_check(f"u{n}", drifted=False, state="unknown")

        body = (await client.get("/dashboard/summary")).json()

        assert body["drift"]["in_spec_pct"] == 50, (
            "the headline counts artifacts nobody checked as in spec"
        )
        assert body["drift"]["unknown"] == 8, (
            "the eight unchecked artifacts vanished instead of being reported"
        )
        assert body["drift"]["checked"] == 2

    async def test_a_drifted_artifact_still_lowers_it(self, api):
        client, repo = api
        await repo.upsert_drift_check("a", drifted=False, state="in_spec")
        await repo.upsert_drift_check("b", drifted=True, state="drifted")

        body = (await client.get("/dashboard/summary")).json()

        assert body["drift"]["in_spec_pct"] == 50
        assert body["drift"]["drifted"] == 1


class TestTheMigrationDoesNotInventHealth:
    async def test_pre_existing_rows_become_unknown_not_in_spec(self, tmp_path: Path):
        """What a pre-migration `drifted = 0` row meant is unknowable. Guessing
        "fine" would carry the whole defect across the upgrade."""
        db = Database(str(tmp_path / "old.db"))
        await db.connect()
        await run_migrations(db)
        # Simulate a row written before the state column existed.
        await db.execute(
            "INSERT INTO drift_checks (artifact_id, drifted, checked_at, state) "
            "VALUES ('legacy', 0, '2026-01-01T00:00:00Z', NULL)"
        )
        await db.conn.commit()
        await db.execute("UPDATE drift_checks SET state = 'unknown' WHERE state IS NULL")
        await db.conn.commit()

        row = await db.fetchone("SELECT state FROM drift_checks WHERE artifact_id = 'legacy'")
        assert row["state"] == "unknown"
        await db.close()


class TestNothingEvaluatedIsNotInSpec:
    async def test_a_sequence_where_every_step_was_skipped_is_unknown(self, env):
        """Steps are skipped when they carry no precheck, when the precheck is
        not a GET, and when the call to it FAILED - so an unreachable target
        produced a clean green tick with nothing checked at all."""
        from unittest.mock import MagicMock as _MagicMock

        repo, store, lifecycle = env
        body = (
            "## Plan\nCreate\n## Spec\n```yaml proxmox-api-spec\n"
            "steps:\n  - id: s1\n    method: POST\n    path: /nodes/pve1/qemu\n```\n"
        )
        await _applied(lifecycle, "2026-08-21-all-skipped", "proxmox-api-sequence", body)
        executor = _MagicMock(pve_nodes=[])
        executor.proxmox.call = AsyncMock(side_effect=TimeoutError())

        result = await verify_artifact(
            "2026-08-21-all-skipped", repo=repo, store=store, executor=executor
        )

        assert result.state is DriftState.UNKNOWN, (
            "a sequence whose every step was skipped is reporting as in spec"
        )
        assert result.details["reason"] == "nothing_evaluated"


class TestTheDeadAnsibleVerifierIsGone:
    async def test_nothing_calls_executor_dot_ssh(self) -> None:
        """`ArtifactExecutor` has no `.ssh` - it went with the jump server. Every
        call raised AttributeError into a handler that turned it into a green
        tick."""
        verify_src = (
            Path(__file__).resolve().parents[1] / "src" / "homepilot" / "reconciler" / "verify.py"
        ).read_text(encoding="utf-8")

        code_lines = [line for line in verify_src.splitlines() if not line.lstrip().startswith("#")]
        assert "executor.ssh" not in "\n".join(code_lines), (
            "the drift verifier reaches for executor.ssh again - an attribute that "
            "does not exist, whose AttributeError reads as 'in spec'"
        )
