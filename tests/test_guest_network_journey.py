"""The guest network ships as an ARTIFACT: propose, approve with a code, apply (#553).

Owner mandate: "we do things through homepilot, even if you are the engine that
I drive - so we have the artifacts and KB and all up to date." The record of who
decided to rebuild the guest subnet has to live in the artifact store, not in a
POST nobody can find afterwards. So the gate here is the JOURNEY, not the call:

  propose (a bad body is refused HERE)
    -> the approval code exists and no MCP read carries it
    -> approve with the relayed code
    -> apply, and the FAKE CLUSTER actually has the zone, the vnet, the subnet
       and the fence afterwards, with the report in the execution log
    -> a second apply is a no-op, because the plan is empty.

Every component that owns a piece of the contract is real: the lifecycle, the
store, the validator, the transitions, the executor's dispatch, the database.
The ONLY fake is the PVE gateway - the boundary the estate's proxmox_mcp library
owns - and it is a recording fake, so "it worked" is checked by looking at what
the cluster ended up with rather than at a returned boolean.

Teeth (each proven by reverting and watching the NAMED test fail):
  - drop the `validate_guest_network_spec` call from validate_propose_spec ->
    `test_a_body_that_cannot_work_is_refused_at_propose` fails;
  - make the executor ignore the plan and report success ->
    `test_applying_actually_builds_the_network_on_the_cluster` fails;
  - make plan() non-empty when converged ->
    `test_a_second_apply_changes_nothing` fails (and the idempotence claim with it);
  - return the approval code from get_artifact -> `test_no_mcp_read_carries_the_code` fails.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from homepilot.artifacts.lifecycle import ArtifactLifecycle
from homepilot.artifacts.models import ArtifactKind, ArtifactStatus, LifecycleError
from homepilot.artifacts.store import ArtifactStore
from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.db.repository import Repository
from homepilot.executor.orchestrator import ArtifactExecutor
from homepilot.mcp.server import _handle_tool
from homepilot.reconciler.verify import DriftState, verify_artifact

from .test_guest_network import FakeCluster

pytestmark = pytest.mark.asyncio


BODY = """# Guest network

The subnet a friend's machine lives on.

```yaml guest-network-spec
zone: guest
vnet: innkeep
subnet_cidr: 198.51.100.0/24
gateway: 198.51.100.1
snat: 1
dhcp: 1
dhcp_range: 198.51.100.100-198.51.100.199
isolate_cidrs:
  - 192.0.2.0/24
```
"""


def _spec(artifact_id: str = "2026-08-26-guest-network-a1b2c3", body: str = BODY) -> dict[str, Any]:
    return {
        "id": artifact_id,
        "kind": ArtifactKind.GUEST_NETWORK.value,
        "intent": "Build the guest subnet 198.51.100.0/24, fenced off the operator LAN",
        "body": body,
        "target": {"kind": "network", "network": "innkeep"},
        "idempotence": "via-precheck",
        "produced_by": {"session": "s1", "agent": "fable", "user": "olli"},
    }


@pytest.fixture
async def world(tmp_path: Path):
    db = Database(str(tmp_path / "hp.db"))
    await db.connect()
    await run_migrations(db)
    repo = Repository(db)
    store = ArtifactStore(tmp_path / "artifacts")
    lifecycle = ArtifactLifecycle(store, repository=repo)
    cluster = FakeCluster()
    executor = ArtifactExecutor(
        store=store,
        lifecycle=lifecycle,
        repo=repo,
        proxmox=None,
        vault=None,
    )
    # The one fake, at the boundary the proxmox_mcp library owns.
    executor.sdn_gateway = cluster
    yield lifecycle, store, repo, executor, cluster
    await db.close()


class TestTheProposeGate:
    async def test_a_body_that_cannot_work_is_refused_at_propose(self, world) -> None:
        """The last moment before it is committed and put in front of a human."""
        lifecycle, *_ = world
        bad = BODY.replace("gateway: 198.51.100.1", "gateway: 203.0.113.1")
        with pytest.raises(LifecycleError) as exc:
            await lifecycle.propose(_spec("2026-08-26-guest-network-bad001", bad))
        assert "not inside subnet" in str(exc.value)

    async def test_a_body_with_no_spec_block_is_refused(self, world) -> None:
        lifecycle, *_ = world
        with pytest.raises(LifecycleError) as exc:
            await lifecycle.propose(_spec("2026-08-26-guest-network-bad002", "# nothing here\n"))
        assert "guest-network-spec" in str(exc.value)

    async def test_an_unknown_field_is_refused_rather_than_ignored(self, world) -> None:
        lifecycle, *_ = world
        body = BODY.replace("snat: 1", "snat: 1\nfirewall_off: true")
        with pytest.raises(LifecycleError) as exc:
            await lifecycle.propose(_spec("2026-08-26-guest-network-bad003", body))
        assert "firewall_off" in str(exc.value)

    async def test_a_rollback_claim_is_refused_because_this_kind_cannot(self, world) -> None:
        """No deletes in this slice, so `rollback: true` is a promise that could
        not be kept - refused now rather than discovered at revoke."""
        lifecycle, *_ = world
        spec = _spec("2026-08-26-guest-network-bad004")
        spec["rollback"] = True
        with pytest.raises(LifecycleError) as exc:
            await lifecycle.propose(spec)
        assert "no way to reverse itself" in str(exc.value)

    async def test_a_good_body_is_proposed_and_waits_for_a_human(self, world) -> None:
        lifecycle, store, *_ = world
        aid = await lifecycle.propose(_spec())
        fm, _ = store.read(aid)
        assert fm["status"] == ArtifactStatus.PROPOSED.value
        assert fm["kind"] == "guest-network"
        assert fm["rollback"] is False


class TestTheJourney:
    async def test_an_assistant_can_propose_one_over_mcp(self, world) -> None:
        """Full parity, owner mandate: the assistant surveys with
        query_guest_network and PROPOSES the fix with propose_artifact. There is
        no mutating guest-network tool, so this is the only way it can act - and
        it must actually work, not fail on an unknown kind."""
        import json

        lifecycle, store, repo, _executor, _cluster = world
        ctx = {
            "repo": repo,
            "lifecycle": lifecycle,
            "store": store,
            "_mcp_caller_id": "mcp-test",
            "_mcp_token_scope": "full",
        }
        result = await _handle_tool("propose_artifact", {"spec": json.dumps(_spec())}, ctx)
        assert result["kind"] == "guest-network"
        assert result["status"] == ArtifactStatus.PROPOSED.value

    async def test_a_bad_body_is_refused_over_mcp_too(self, world) -> None:
        import json

        lifecycle, store, repo, _executor, _cluster = world
        bad = BODY.replace("subnet_cidr: 198.51.100.0/24", "subnet_cidr: not-a-subnet")
        with pytest.raises(LifecycleError):
            await _handle_tool(
                "propose_artifact",
                {"spec": json.dumps(_spec("2026-08-26-guest-network-mcpbad", bad))},
                {
                    "repo": repo,
                    "lifecycle": lifecycle,
                    "store": store,
                    "_mcp_caller_id": "mcp-test",
                    "_mcp_token_scope": "full",
                },
            )

    async def test_no_mcp_read_carries_the_code(self, world) -> None:
        lifecycle, store, repo, _executor, _cluster = world
        aid = await lifecycle.propose(_spec())
        row = await repo.get_approval_code_row(aid)
        assert row is not None
        code = str(row["code"])
        ctx = {
            "repo": repo,
            "lifecycle": lifecycle,
            "store": store,
            "_mcp_caller_id": "mcp-test",
            "_mcp_token_scope": "full",
        }
        got = await _handle_tool("get_artifact", {"artifact_id": aid}, ctx)
        assert code not in str(got)

    async def test_applying_actually_builds_the_network_on_the_cluster(self, world) -> None:
        """The goal, not the call: after the apply the cluster HAS the network."""
        lifecycle, store, repo, executor, cluster = world
        aid = await lifecycle.propose(_spec())

        # A human decided: the relayed code approves it over MCP.
        row = await repo.get_approval_code_row(aid)
        assert row is not None
        result = await _handle_tool(
            "approve_artifact",
            {"artifact_id": aid, "approval_code": str(row["code"])},
            {
                "repo": repo,
                "lifecycle": lifecycle,
                "store": store,
                "_mcp_caller_id": "mcp-test",
                "_mcp_token_scope": "full",
            },
        )
        assert result["status"] == "approved"

        outcome = await executor.apply(aid, approved_by="olli")
        assert outcome.success, outcome.failure_reason

        ops = [op for op, _ in cluster.calls]
        assert ops == [
            "create_zone",
            "create_vnet",
            "create_subnet",
            # apply BEFORE the firewall: PVE's vnet firewall API validates the
            # vnet against the APPLIED config, so options written while the
            # vnet is pending are refused with "invalid vnet" (first live apply).
            "apply_sdn",
            "set_vnet_firewall_options",
            "create_vnet_firewall_rule",
            "create_vnet_firewall_rule",
            "create_vnet_firewall_rule",
            "create_vnet_firewall_rule",
            "create_vnet_firewall_rule",
            # LAST in the SAME approved apply: enable the datacenter firewall
            # safely (policy_in=ACCEPT) so the fence actually enforces (#600).
            "ensure_datacenter_firewall_enabled",
        ]
        params = dict(cluster.calls)
        assert params["create_subnet"]["gateway"] == "198.51.100.1"
        assert params["create_subnet"]["snat"] == 1

        fm, _ = store.read(aid)
        assert fm["status"] == ArtifactStatus.APPLIED.value

        # The report is IN the execution log, in the artifact, where an operator
        # reads it - not only in a return value nobody kept.
        log = store.read_execution_log(aid) if hasattr(store, "read_execution_log") else ""
        haystack = log or store.resolve_path(aid).read_text()
        assert "create-zone" in haystack
        assert "LEGACY" in haystack, "the enforcement caveat must be recorded with the apply"

    async def test_a_second_apply_changes_nothing(self, world) -> None:
        """Idempotence, proven where it matters: the SAME desired state against a
        cluster that already matches touches the cluster zero times."""
        lifecycle, _store, _repo, executor, cluster = world
        aid = await lifecycle.propose(_spec())
        await lifecycle.approve(aid, "olli")
        await executor.apply(aid, approved_by="olli")
        cluster.calls.clear()

        # The cluster now HAS what was built. Re-run the same artifact.
        _promote_fake_cluster(cluster)
        second = await lifecycle.propose(_spec("2026-08-26-guest-network-again1"))
        await lifecycle.approve(second, "olli")
        outcome = await executor.apply(second, approved_by="olli")
        assert outcome.success
        assert cluster.calls == [], "a converged cluster must be touched zero times"
        assert "already matches" in outcome.execution_log

    async def test_a_cluster_refusal_fails_the_artifact_in_its_own_words(self, world) -> None:
        lifecycle, store, _repo, executor, cluster = world
        cluster.fail_on = "apply_sdn"
        aid = await lifecycle.propose(_spec())
        await lifecycle.approve(aid, "olli")
        outcome = await executor.apply(aid, approved_by="olli")
        assert outcome.success is False
        assert "dnsmasq is not installed" in (outcome.failure_reason or "")
        fm, _ = store.read(aid)
        assert fm["status"] == ArtifactStatus.FAILED.value


class TestDriftIsThePlan:
    async def test_a_converged_network_is_in_spec(self, world) -> None:
        lifecycle, store, repo, executor, cluster = world
        aid = await lifecycle.propose(_spec())
        await lifecycle.approve(aid, "olli")
        await executor.apply(aid, approved_by="olli")
        _promote_fake_cluster(cluster)

        result = await verify_artifact(aid, repo, store, executor)
        assert result.state is DriftState.IN_SPEC
        assert result.drifted is False

    async def test_a_network_somebody_changed_reads_as_drifted(self, world) -> None:
        lifecycle, store, repo, executor, cluster = world
        aid = await lifecycle.propose(_spec())
        await lifecycle.approve(aid, "olli")
        await executor.apply(aid, approved_by="olli")
        _promote_fake_cluster(cluster)
        # Somebody deleted the DROP towards the operator LAN.
        cluster.fw_rules = [r for r in cluster.fw_rules if r.get("dest") != "192.0.2.0/24"]

        result = await verify_artifact(aid, repo, store, executor)
        assert result.state is DriftState.DRIFTED
        assert "DROP" in result.verification_log

    async def test_an_unreachable_cluster_is_unknown_not_in_spec(self, world) -> None:
        lifecycle, store, repo, executor, _cluster = world
        aid = await lifecycle.propose(_spec())
        await lifecycle.approve(aid, "olli")
        await executor.apply(aid, approved_by="olli")

        class Dead(FakeCluster):
            async def list_zones(self) -> list[dict[str, Any]]:
                raise RuntimeError("connection refused")

            async def list_vnets(self) -> list[dict[str, Any]]:
                raise RuntimeError("connection refused")

        executor.sdn_gateway = Dead()
        result = await verify_artifact(aid, repo, store, executor)
        assert result.state is DriftState.UNKNOWN
        assert result.drifted is False


def _promote_fake_cluster(cluster: FakeCluster) -> None:
    """Make the recording fake REPORT what it was just told to build.

    The fake records calls rather than simulating PVE, so a converged-state test
    has to state the converged state. Kept here, once, so every test that needs
    "and now the cluster has it" says it the same way.
    """
    from tests.test_guest_network import converged_cluster

    done = converged_cluster()
    cluster.zones = done.zones
    cluster.vnets = done.vnets
    cluster.subnets = done.subnets
    cluster.fw_options = done.fw_options
    cluster.fw_rules = done.fw_rules
    # The master switch too: the apply enabled it safely, so a converged cluster
    # reports it on and safe and plans no ensure-datacenter-firewall step (#600).
    cluster.dc_fw_options = dict(done.dc_fw_options)
    cluster.calls.clear()
