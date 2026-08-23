""" "Every change is reversible" has to be true, or not claimed (#426).

Three independent failures, all with the same shape - the artifact reached
`revoked` and the host kept the change, reported as a clean success:

1. Rollback was gated on a proposer-set boolean. ARTIFACT_SPEC says `rollback` is
   *"true if a rollback section exists in the body"*, and nothing derived it - so
   a body with a perfectly good rollback section and no `rollback: true` line
   silently never rolled back.
2. A claim that could not be honoured was accepted at propose and only discovered
   on revoke, which is the worst possible moment: the operator has already
   decided to undo something.
3. The rollback's own outcome was thrown away. A handler returning
   `success: False` was discarded and an exception was logged and swallowed, so
   "reversed" and "relabelled" were indistinguishable.

These gates assert what an operator is buying: revoke either puts the host back
or SAYS it did not.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from homepilot.artifacts.lifecycle import ArtifactLifecycle
from homepilot.artifacts.models import ArtifactKind, LifecycleError
from homepilot.artifacts.store import ArtifactStore
from homepilot.executor.orchestrator import ArtifactExecutor
from homepilot.executor.rollback import derive_rollback, has_rollback_section

pytestmark = pytest.mark.asyncio

SHELL_WITH_ROLLBACK = """\
## Plan
Do the thing
## Idempotence preamble
Idempotent: guarded by a test.
## Spec
```bash shell-spec
#!/bin/bash
echo apply
```
## Rollback
```bash shell-rollback
#!/bin/bash
echo undo
```
"""

SHELL_WITHOUT_ROLLBACK = """\
## Plan
Do the thing
## Idempotence preamble
Idempotent: guarded by a test.
## Spec
```bash shell-spec
#!/bin/bash
echo apply
```
"""

# The exact shape the deleted CLI engine counted as a rollback: a heading, and
# nothing an executor could ever run.
SHELL_HEADING_ONLY = SHELL_WITHOUT_ROLLBACK + "## Rollback\nRun the undo by hand.\n"


def _spec(artifact_id: str, body: str, **overrides: object) -> dict:
    spec: dict = {
        "id": artifact_id,
        "kind": "shell-script",
        "intent": "Rollback truth",
        "body": body,
        "target": {"kind": "vm", "vmid": 100, "node": "pve1", "host": "web1"},
        "idempotence": "via-precheck",
        "produced_by": {"session": "s1", "agent": "a1", "user": "u1"},
    }
    spec.update(overrides)
    return spec


@pytest.fixture
def lifecycle(tmp_path: Path) -> ArtifactLifecycle:
    return ArtifactLifecycle(store=ArtifactStore(tmp_path / "artifacts"))


def _executor(lifecycle: ArtifactLifecycle) -> ArtifactExecutor:
    return ArtifactExecutor(
        store=lifecycle.store,
        lifecycle=lifecycle,
        repo=MagicMock(),
        proxmox=AsyncMock(),
        vault=AsyncMock(),
        agent=AsyncMock(),
        pve_nodes=["pve1"],
    )


class TestRollbackIsDerivedFromTheBody:
    async def test_a_body_with_a_rollback_section_is_reversible_without_being_told(self, lifecycle):
        """The #426 bug: this artifact would never have rolled back, because
        nobody wrote `rollback: true` in the frontmatter."""
        artifact_id = await lifecycle.propose(_spec("2026-08-21-derived-yes", SHELL_WITH_ROLLBACK))

        fm, _ = lifecycle.store.read(artifact_id)
        assert fm["rollback"] is True

    async def test_a_body_without_one_is_not(self, lifecycle):
        artifact_id = await lifecycle.propose(
            _spec("2026-08-21-derived-no", SHELL_WITHOUT_ROLLBACK)
        )

        fm, _ = lifecycle.store.read(artifact_id)
        assert fm["rollback"] is False

    async def test_a_heading_alone_is_not_a_rollback(self, lifecycle):
        """A `## Rollback` heading with prose under it is what the deleted CLI
        engine counted, and it is exactly why it reported rollbacks that could
        never run."""
        artifact_id = await lifecycle.propose(_spec("2026-08-21-heading-only", SHELL_HEADING_ONLY))

        fm, _ = lifecycle.store.read(artifact_id)
        assert fm["rollback"] is False

    async def test_the_claim_cannot_override_the_body(self, lifecycle):
        """`rollback` is a FACT about the body, not a switch. A proposer who
        writes `rollback: false` over a real rollback section does not get to
        make revoke leave the host changed."""
        artifact_id = await lifecycle.propose(
            _spec("2026-08-21-claim-false", SHELL_WITH_ROLLBACK, rollback=False)
        )

        fm, _ = lifecycle.store.read(artifact_id)
        assert fm["rollback"] is True


class TestAnUnhonourableClaimIsRefusedAtPropose:
    async def test_claiming_a_rollback_the_body_does_not_have(self, lifecycle):
        with pytest.raises(LifecycleError) as exc:
            await lifecycle.propose(
                _spec("2026-08-21-false-claim", SHELL_WITHOUT_ROLLBACK, rollback=True)
            )

        assert "no rollback section" in str(exc.value)

    async def test_a_kind_with_no_inverse_cannot_claim_one(self, lifecycle):
        """kb-note has nothing to undo - it is a note. A kind that cannot reverse
        itself must not be able to claim it, because the operator finds out on
        revoke otherwise.
        """
        from homepilot.artifacts.models import ArtifactKind
        from homepilot.executor.rollback import kind_can_roll_back

        assert kind_can_roll_back(ArtifactKind.KB_NOTE) is False

    async def test_host_provision_can_claim_one_now(self, lifecycle):
        """It used to be the example of a kind with no inverse. It captures the
        host's prior state at apply and puts back what it can (#426)."""
        body = (
            "## Plan\nProvision\n## Spec\n```yaml host-provision-spec\npackages:\n  - nginx\n```\n"
        )

        artifact_id = await lifecycle.propose(
            _spec(
                "2026-08-21-host-provision-rb",
                body,
                kind="host-provision",
                target={"kind": "vm", "vmid": 100, "node": "pve1", "host": "web1"},
            )
        )

        fm, _ = lifecycle.store.read(artifact_id)
        assert fm["rollback"] is True


class TestRevokeSaysWhetherTheHostWasPutBack:
    async def _applied(self, lifecycle, artifact_id: str, body: str) -> ArtifactExecutor:
        await lifecycle.propose(_spec(artifact_id, body))
        await lifecycle.approve(artifact_id, user="admin")
        executor = _executor(lifecycle)
        executor._dispatch = AsyncMock(
            return_value=__import__(
                "homepilot.executor.orchestrator", fromlist=["ExecutionResult"]
            ).ExecutionResult(success=True, execution_log="applied")
        )
        await executor.apply(artifact_id, approved_by="admin")
        return executor

    async def test_a_revoke_with_no_rollback_says_the_host_keeps_the_change(self, lifecycle):
        """The headline: `revoked` describes the ARTIFACT. It says nothing about
        the machine, and it used to be all an operator got."""
        executor = await self._applied(lifecycle, "2026-08-21-no-rollback", SHELL_WITHOUT_ROLLBACK)

        outcome = await executor.revoke("2026-08-21-no-rollback", user="admin")

        assert outcome.rolled_back is False
        assert "keeps the change" in outcome.reason

    async def test_a_rollback_that_ran_reports_reversed(self, lifecycle, monkeypatch):
        executor = await self._applied(lifecycle, "2026-08-21-real-rollback", SHELL_WITH_ROLLBACK)
        ran: dict[str, bool] = {}

        async def _fake_shell(
            fm, body, target, adapter, pve_nodes=None, rollback=False, vault=None
        ):
            ran["rollback"] = rollback
            return {"success": True, "execution_log": "undo ran"}

        monkeypatch.setattr("homepilot.executor.orchestrator.shell_script_execute", _fake_shell)

        outcome = await executor.revoke("2026-08-21-real-rollback", user="admin")

        assert ran.get("rollback") is True
        assert outcome.rolled_back is True

    async def test_a_rollback_that_failed_is_not_reported_as_reversed(self, lifecycle, monkeypatch):
        """The revoke still SUCCEEDS - a failed rollback must not strand the
        artifact - but it must not claim the host was put back."""
        executor = await self._applied(lifecycle, "2026-08-21-rb-fails", SHELL_WITH_ROLLBACK)

        async def _boom(fm, body, target, adapter, pve_nodes=None, rollback=False, vault=None):
            raise OSError("host unreachable")

        monkeypatch.setattr("homepilot.executor.orchestrator.shell_script_execute", _boom)

        outcome = await executor.revoke("2026-08-21-rb-fails", user="admin")

        assert outcome.rolled_back is False
        assert "host unreachable" in outcome.reason
        fm, _ = lifecycle.store.read("2026-08-21-rb-fails")
        assert fm["status"] == "revoked", "a failed rollback stranded the artifact"

    async def test_a_rollback_that_no_ops_is_not_reported_as_reversed(self, lifecycle, monkeypatch):
        """A handler returning `success: False` was discarded outright."""
        executor = await self._applied(lifecycle, "2026-08-21-rb-noop", SHELL_WITH_ROLLBACK)

        async def _noop(fm, body, target, adapter, pve_nodes=None, rollback=False, vault=None):
            return {
                "success": False,
                "execution_log": "",
                "failure_reason": "missing spec",
            }

        monkeypatch.setattr("homepilot.executor.orchestrator.shell_script_execute", _noop)

        outcome = await executor.revoke("2026-08-21-rb-noop", user="admin")

        assert outcome.rolled_back is False
        assert "missing spec" in outcome.reason


class TestHostProvisionActuallyReverses:
    """The #426 headline for this kind: revoking used to relabel the artifact
    while packages stayed installed and configs stayed written - and report
    success. It has no rollback SECTION to write; the inverse comes from a
    capture taken at apply time."""

    HOST_PROVISION_BODY = (
        "## Plan\nProvision\n## Spec\n"
        "```yaml host-provision-spec\n"
        "config_files:\n"
        "  - path: /etc/app.conf\n"
        "    content: |\n"
        "      new = 1\n"
        "    mode: '0644'\n"
        "```\n"
    )

    async def _executor_with_agent(self, lifecycle, repo, agent):
        return ArtifactExecutor(
            store=lifecycle.store,
            lifecycle=lifecycle,
            repo=repo,
            proxmox=AsyncMock(),
            vault=AsyncMock(),
            agent=agent,
            pve_nodes=["pve1"],
        )

    async def test_the_prior_config_is_written_back(self, lifecycle, tmp_path):
        from homepilot.db.connection import Database
        from homepilot.db.migrations import run_migrations
        from homepilot.db.repository import Repository

        db = Database(str(tmp_path / "rb.db"))
        await db.connect()
        await run_migrations(db)
        repo = Repository(db)
        try:
            agent = AsyncMock()
            agent.install_package = AsyncMock(return_value={"changed": True})
            agent.manage_service = AsyncMock(return_value={"changed": True})
            agent.write_config = AsyncMock(return_value={"changed": True})
            agent.exec_readonly = AsyncMock(return_value=(0, "0600", ""))
            agent.read_file = AsyncMock(return_value="old = 1\n")

            artifact_id = "2026-08-21-hp-reverse"
            await lifecycle.propose(
                _spec(
                    artifact_id,
                    self.HOST_PROVISION_BODY,
                    kind="host-provision",
                    target={"kind": "vm", "vmid": 100, "node": "pve1", "host": "web1"},
                )
            )
            await lifecycle.approve(artifact_id, user="admin")
            executor = await self._executor_with_agent(lifecycle, repo, agent)

            await executor.apply(artifact_id, approved_by="admin")
            # The capture is what makes the inverse possible at all.
            assert await repo.get_host_state_capture(artifact_id), (
                "apply recorded nothing about the host it was about to change"
            )

            outcome = await executor.revoke(artifact_id, user="admin")

            assert outcome.rolled_back is True, outcome.reason
            restored = [
                call.args
                for call in agent.write_config.await_args_list
                if call.args[2] == "old = 1\n"
            ]
            assert restored, "the prior config bytes were never written back"
        finally:
            await db.close()


class TestTheFenceListMatchesTheExecutors:
    """The derivation reads fence names copied from the executors (to keep propose
    off the execution stack). If an executor renames its fence, derivation goes
    quietly wrong - `rollback` becomes false for artifacts that have one."""

    async def test_every_declared_fence_is_the_one_its_executor_looks_for(self) -> None:
        from homepilot.executor import ansible, http_sequence, proxmox_api, shell_script

        expected = {
            ArtifactKind.SHELL_SCRIPT: shell_script._ROLLBACK_FENCE,
            ArtifactKind.PROXMOX_API_SEQUENCE: proxmox_api._ROLLBACK_FENCE,
            ArtifactKind.HTTP_SEQUENCE: http_sequence._ROLLBACK_FENCE,
        }
        for kind, fence in expected.items():
            body = f"## Rollback\n```yaml {fence}\nsteps: []\n```\n"
            assert has_rollback_section(kind, body), (
                f"{kind.value} declares a fence its executor does not use"
            )

        # ansible names its rollback tag inline rather than in a constant, so
        # the check goes the other way: the body this module calls a rollback
        # must be one ansible's own extractor can read.
        ansible_body = "## Rollback\n```yaml ansible-rollback\n- hosts: all\n```\n"
        assert derive_rollback(ArtifactKind.ANSIBLE_PLAYBOOK, ansible_body)
        assert ansible._extract_spec(ansible_body, "ansible-rollback") is not None, (
            "ansible's extractor cannot read the block this module counts as its rollback"
        )
