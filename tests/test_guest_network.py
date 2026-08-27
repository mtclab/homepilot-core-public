"""The guest network: what it refuses, what it plans, and what it actually does (#553).

Four gates live here, and they are the reason the module is split the way it is:

* **The refusals.** A guest network that cannot work - a gateway outside its own
  subnet, a DHCP range that would hand out the router's address, a vnet name PVE
  cannot store - is refused at CONSTRUCTION, before anything is proposed,
  approved or applied. PVE accepts most of these happily and the operator finds
  out when a friend's machine has no route.

* **The plan.** Full create from an empty cluster; EMPTY when the cluster
  already matches (this is what makes an apply idempotent and a drift check
  meaningful, and both read this same function); and exactly the missing pieces
  when the cluster is half-built.

* **The execution.** Per-step truth against a fake cluster, including the case
  that matters most: a step that FAILS repeats the cluster's own words, the
  steps after it are reported as not attempted, and nothing pretends to have
  happened.

* **The fence.** One rule set, in one order, used both as vnet forward rules and
  as the per-VM rules the provision writes - because two hand-written copies are
  two fences that can disagree, and the one that disagrees quietly is the one
  that lets traffic through.

Teeth (each proven by planting the defect and watching the NAMED test fail):
  - drop the `gateway not in subnet` check -> the refusal case fails;
  - make `plan()` emit the create steps unconditionally ->
    `test_a_converged_cluster_plans_nothing` fails;
  - put the DROP rules before the ACCEPTs in `fence_rules` ->
    `test_the_accepts_come_before_the_drops` fails;
  - swallow the cluster's words in `execute` (report "failed" only) ->
    `test_a_failed_step_repeats_the_clusters_own_words` fails;
  - report the steps after a failure as done -> `test_steps_after_a_failure_are_not_attempted` fails.
"""

from __future__ import annotations

from typing import Any

import pytest

from homepilot.provision.guest_network import (
    DesiredGuestNetwork,
    GuestNetworkError,
    GuestNetworkSurvey,
    execute,
    fence_rules,
    plan,
    survey,
)

pytestmark = pytest.mark.asyncio


def desired(**overrides: Any) -> DesiredGuestNetwork:
    payload: dict[str, Any] = {
        "zone": "guest",
        "vnet": "innkeep",
        "subnet_cidr": "198.51.100.0/24",
        "gateway": "198.51.100.1",
        "snat": True,
        "dhcp": True,
        "dhcp_range": "198.51.100.100-198.51.100.199",
        "isolate_cidrs": ("192.0.2.0/24",),
    }
    payload.update(overrides)
    return DesiredGuestNetwork(**payload)


class FakeCluster:
    """Everything this slice asks of PVE, over the gateway's named operations.

    Faked at the GATEWAY boundary on purpose (owner mandate 2026-08-26): PVE's
    endpoints belong to the estate's proxmox_mcp library, so a test that faked
    HTTP paths would be asserting a duplication we deliberately do not have.
    """

    def __init__(
        self,
        zones: list[dict[str, Any]] | None = None,
        vnets: list[dict[str, Any]] | None = None,
        subnets: list[dict[str, Any]] | None = None,
        fw_options: dict[str, Any] | None = None,
        fw_rules: list[dict[str, Any]] | None = None,
        nftables: int = 0,
        fail_on: str | None = None,
        failure: str = "dnsmasq is not installed on node elizabeth",
    ) -> None:
        self.zones = zones or []
        self.vnets = vnets or []
        self.subnets = subnets or []
        self.fw_options = fw_options or {}
        self.fw_rules = fw_rules or []
        self.nftables = nftables
        self.fail_on = fail_on
        self.failure = failure
        self.calls: list[tuple[str, dict[str, Any]]] = []

    # reads
    async def list_nodes(self) -> list[dict[str, Any]]:
        return [{"node": "elizabeth", "status": "online"}]

    async def list_zones(self) -> list[dict[str, Any]]:
        return list(self.zones)

    async def list_vnets(self) -> list[dict[str, Any]]:
        return list(self.vnets)

    async def list_subnets(self, vnet: str) -> list[dict[str, Any]]:
        return list(self.subnets)

    async def vnet_firewall_options(self, vnet: str) -> dict[str, Any]:
        return dict(self.fw_options)

    async def vnet_firewall_rules(self, vnet: str) -> list[dict[str, Any]]:
        return list(self.fw_rules)

    async def node_firewall_options(self, node: str) -> dict[str, Any]:
        return {"enable": 1, "nftables": self.nftables}

    # mutations
    async def _record(self, op: str, params: dict[str, Any]) -> str:
        self.calls.append((op, params))
        if self.fail_on == op:
            raise RuntimeError(self.failure)
        return f"{op} ok"

    async def create_zone(self, **params: Any) -> str:
        return await self._record("create_zone", params)

    async def update_zone(self, **params: Any) -> str:
        return await self._record("update_zone", params)

    async def create_vnet(self, **params: Any) -> str:
        return await self._record("create_vnet", params)

    async def create_subnet(self, **params: Any) -> str:
        return await self._record("create_subnet", params)

    async def update_subnet(self, **params: Any) -> str:
        return await self._record("update_subnet", params)

    async def set_vnet_firewall_options(self, **params: Any) -> str:
        return await self._record("set_vnet_firewall_options", params)

    async def create_vnet_firewall_rule(self, **params: Any) -> str:
        return await self._record("create_vnet_firewall_rule", params)

    async def apply_sdn(self, **params: Any) -> str:
        return await self._record("apply_sdn", params)


def converged_cluster() -> FakeCluster:
    """A cluster that already IS the desired network, down to the rules."""
    want = desired()
    return FakeCluster(
        zones=[{"zone": "guest", "type": "simple", "dhcp": "dnsmasq", "ipam": "pve"}],
        vnets=[{"vnet": "innkeep", "zone": "guest"}],
        subnets=[
            {
                "cidr": "198.51.100.0/24",
                "gateway": "198.51.100.1",
                "snat": 1,
                "dhcp-range": ["start-address=198.51.100.100,end-address=198.51.100.199"],
            }
        ],
        fw_options={"enable": 1, "policy_forward": "ACCEPT"},
        fw_rules=list(fence_rules(want, "forward")),
    )


class TestTheRefusals:
    async def test_a_gateway_outside_its_subnet_is_refused(self) -> None:
        with pytest.raises(GuestNetworkError) as exc:
            desired(gateway="203.0.113.1")
        assert "not inside subnet 198.51.100.0/24" in str(exc.value)
        assert "no route off its own wire" in str(exc.value)

    async def test_a_dhcp_range_outside_the_subnet_is_refused(self) -> None:
        with pytest.raises(GuestNetworkError) as exc:
            desired(dhcp_range="203.0.113.100-203.0.113.199")
        assert "not inside subnet" in str(exc.value)

    async def test_a_dhcp_range_that_would_hand_out_the_gateway_is_refused(self) -> None:
        with pytest.raises(GuestNetworkError) as exc:
            desired(dhcp_range="198.51.100.1-198.51.100.199")
        assert "contains the gateway" in str(exc.value)

    async def test_a_backwards_dhcp_range_is_refused(self) -> None:
        with pytest.raises(GuestNetworkError) as exc:
            desired(dhcp_range="198.51.100.199-198.51.100.100")
        assert "ends before it starts" in str(exc.value)

    async def test_a_vnet_name_pve_cannot_store_is_refused(self) -> None:
        with pytest.raises(GuestNetworkError) as exc:
            desired(vnet="guestnetwork")
        assert "1-8 characters" in str(exc.value)

    async def test_dhcp_with_no_range_is_refused(self) -> None:
        with pytest.raises(GuestNetworkError) as exc:
            desired(dhcp_range="")
        assert "nothing to hand out" in str(exc.value)

    async def test_dhcp_off_with_no_range_is_fine(self) -> None:
        want = desired(dhcp=False, dhcp_range="")
        assert want.dhcp is False
        assert want.dhcp_range_param == []

    async def test_a_bad_isolate_cidr_is_refused(self) -> None:
        with pytest.raises(GuestNetworkError):
            desired(isolate_cidrs=("203.0.113.1/24",))


class TestThePlan:
    async def test_an_empty_cluster_gets_the_whole_build_in_order(self) -> None:
        current = await survey(FakeCluster(), desired())
        result = plan(desired(), current)
        assert [s.id for s in result.steps] == [
            "create-zone",
            "create-vnet",
            "create-subnet",
            "vnet-firewall-options",
            "vnet-firewall-rule-0",
            "vnet-firewall-rule-1",
            "vnet-firewall-rule-2",
            "vnet-firewall-rule-3",
            "vnet-firewall-rule-4",
            "apply-sdn",
        ]
        by_id = {s.id: s for s in result.steps}
        assert by_id["create-zone"].op == "create_zone"
        assert by_id["create-zone"].params == {
            "zone": "guest",
            "type": "simple",
            "dhcp": "dnsmasq",
            "ipam": "pve",
        }
        assert by_id["create-subnet"].params == {
            "vnet": "innkeep",
            "subnet": "198.51.100.0/24",
            "type": "subnet",
            "gateway": "198.51.100.1",
            "snat": 1,
            "dhcp-range": ["start-address=198.51.100.100,end-address=198.51.100.199"],
        }
        # The apply is LAST and present: without it PVE holds the whole thing
        # pending and the guest network exists only in the cluster's intentions.
        assert result.steps[-1].op == "apply_sdn"

    async def test_a_converged_cluster_plans_nothing(self) -> None:
        current = await survey(converged_cluster(), desired())
        result = plan(desired(), current)
        assert result.steps == ()
        assert result.blockers == ()
        assert result.converged is True

    async def test_a_half_built_cluster_plans_only_the_rest(self) -> None:
        """Zone and vnet exist; the subnet and the firewall do not."""
        cluster = FakeCluster(
            zones=[{"zone": "guest", "type": "simple", "dhcp": "dnsmasq", "ipam": "pve"}],
            vnets=[{"vnet": "innkeep", "zone": "guest"}],
        )
        result = plan(desired(), await survey(cluster, desired()))
        ids = [s.id for s in result.steps]
        assert "create-zone" not in ids
        assert "create-vnet" not in ids
        assert ids[0] == "create-subnet"
        assert ids[-1] == "apply-sdn"

    async def test_a_subnet_whose_gateway_drifted_is_updated_not_recreated(self) -> None:
        cluster = converged_cluster()
        cluster.subnets = [
            {
                "cidr": "198.51.100.0/24",
                "gateway": "198.51.100.254",
                "snat": 1,
                "dhcp-range": ["start-address=198.51.100.100,end-address=198.51.100.199"],
            }
        ]
        result = plan(desired(), await survey(cluster, desired()))
        assert [s.id for s in result.steps] == ["update-subnet", "apply-sdn"]
        assert result.steps[0].op == "update_subnet"
        assert result.steps[0].params["gateway"] == "198.51.100.1"

    async def test_a_missing_firewall_rule_is_the_only_thing_planned(self) -> None:
        cluster = converged_cluster()
        cluster.fw_rules = cluster.fw_rules[:-1]  # the gateway DROP went missing
        result = plan(desired(), await survey(cluster, desired()))
        assert [s.id for s in result.steps] == ["vnet-firewall-rule-4", "apply-sdn"]
        assert result.steps[0].params["dest"] == "198.51.100.1/32"

    async def test_a_zone_somebody_else_built_is_a_blocker_not_a_step(self) -> None:
        cluster = FakeCluster(zones=[{"zone": "guest", "type": "vxlan"}])
        result = plan(desired(), await survey(cluster, desired()))
        assert result.steps == ()
        assert result.blockers and "not 'simple'" in result.blockers[0]
        assert result.converged is False

    async def test_a_vnet_in_another_zone_is_a_blocker(self) -> None:
        cluster = FakeCluster(
            zones=[{"zone": "guest", "type": "simple", "dhcp": "dnsmasq", "ipam": "pve"}],
            vnets=[{"vnet": "innkeep", "zone": "elsewhere"}],
        )
        result = plan(desired(), await survey(cluster, desired()))
        assert result.blockers and "Moving a vnet between zones" in result.blockers[0]

    async def test_no_isolate_list_means_no_firewall_steps(self) -> None:
        """An operator who named nothing to fence off gets no fence, and the
        plan says so by being shorter - not by writing empty rules."""
        want = desired(isolate_cidrs=())
        result = plan(want, await survey(FakeCluster(), want))
        assert not [s for s in result.steps if "firewall" in s.id]


class TestTheSurvey:
    async def test_the_firewall_stack_is_read_from_the_node(self) -> None:
        legacy = await survey(FakeCluster(nftables=0), desired())
        assert legacy.firewall_stack == "legacy"
        modern = await survey(FakeCluster(nftables=1), desired())
        assert modern.firewall_stack == "nftables"

    async def test_a_read_that_fails_is_recorded_not_swallowed(self) -> None:
        class Broken(FakeCluster):
            async def list_zones(self) -> list[dict[str, Any]]:
                raise RuntimeError("401 no ticket")

        current = await survey(Broken(), desired())
        assert current.zones == []
        assert any("401 no ticket" in e for e in current.errors)

    async def test_an_unread_firewall_stack_is_unknown_not_legacy(self) -> None:
        class Broken(FakeCluster):
            async def node_firewall_options(self, node: str) -> dict[str, Any]:
                raise RuntimeError("permission denied")

        current = await survey(Broken(), desired())
        assert current.firewall_stack == "unknown"


class TestTheFence:
    async def test_the_accepts_come_before_the_drops(self) -> None:
        rules = fence_rules(desired(), "out")
        actions = [r["action"] for r in rules]
        assert actions == ["ACCEPT", "ACCEPT", "ACCEPT", "DROP", "DROP"]
        # And the ACCEPTs are exactly DHCP + DNS to the gateway, nothing wider.
        assert rules[0]["proto"] == "udp" and rules[0]["dport"] == "67:68"
        assert {rules[1]["proto"], rules[2]["proto"]} == {"udp", "tcp"}
        assert all(r["dport"] == "53" for r in rules[1:3])
        assert all(r["dest"] == "198.51.100.1" for r in rules[:3])

    async def test_every_isolated_network_gets_its_own_drop(self) -> None:
        want = desired(isolate_cidrs=("192.0.2.0/24", "192.168.1.0/24"))
        drops = [r["dest"] for r in fence_rules(want, "out") if r["action"] == "DROP"]
        assert drops == ["192.0.2.0/24", "192.168.1.0/24", "198.51.100.1/32"]

    async def test_the_gateway_itself_is_dropped_last(self) -> None:
        """The guest talks to the gateway for DHCP and DNS and nothing else."""
        rules = fence_rules(desired(), "out")
        assert rules[-1] == {
            "type": "out",
            "action": "DROP",
            "dest": "198.51.100.1/32",
            "enable": 1,
            "comment": "the gateway is a router for guests, not a host they talk to",
        }

    async def test_dhcp_off_writes_no_dhcp_accept(self) -> None:
        want = desired(dhcp=False, dhcp_range="")
        rules = fence_rules(want, "out")
        assert not [r for r in rules if r.get("dport") == "67:68"]


class TestTheExecution:
    async def test_every_step_is_run_and_reported(self) -> None:
        cluster = FakeCluster()
        steps = plan(desired(), await survey(cluster, desired())).steps
        result = await execute(cluster, steps)
        assert result["success"] is True
        assert [op for op, _ in cluster.calls] == [
            "create_zone",
            "create_vnet",
            "create_subnet",
            "set_vnet_firewall_options",
            "create_vnet_firewall_rule",
            "create_vnet_firewall_rule",
            "create_vnet_firewall_rule",
            "create_vnet_firewall_rule",
            "create_vnet_firewall_rule",
            "apply_sdn",
        ]
        assert all(s["status"] == "done" for s in result["steps"])

    async def test_the_vnet_firewall_is_written_with_exact_params(self) -> None:
        cluster = FakeCluster()
        await execute(cluster, plan(desired(), await survey(cluster, desired())).steps)
        by_op = dict(cluster.calls)
        assert by_op["set_vnet_firewall_options"] == {
            "vnet": "innkeep",
            "enable": 1,
            "policy_forward": "ACCEPT",
        }
        rules = [params for op, params in cluster.calls if op == "create_vnet_firewall_rule"]
        assert [r["pos"] for r in rules] == [0, 1, 2, 3, 4]
        assert rules[0]["type"] == "forward"
        assert rules[-1]["action"] == "DROP" and rules[-1]["dest"] == "198.51.100.1/32"

    async def test_a_failed_step_repeats_the_clusters_own_words(self) -> None:
        cluster = FakeCluster(fail_on="apply_sdn")
        steps = plan(desired(), await survey(cluster, desired())).steps
        result = await execute(cluster, steps)
        assert result["success"] is False
        assert "dnsmasq is not installed on node elizabeth" in result["failure_reason"]
        assert "dnsmasq is not installed on node elizabeth" in result["execution_log"]

    async def test_steps_after_a_failure_are_not_attempted(self) -> None:
        cluster = FakeCluster(fail_on="create_vnet")
        steps = plan(desired(), await survey(cluster, desired())).steps
        result = await execute(cluster, steps)
        statuses = {s["id"]: s["status"] for s in result["steps"]}
        assert statuses["create-zone"] == "done"
        assert statuses["create-vnet"] == "failed"
        # Every remaining step is accounted for, and none of them ran: a log
        # that simply ends looks like a log that finished.
        assert statuses["apply-sdn"] == "not_attempted"
        assert len(statuses) == len(steps)
        assert "apply_sdn" not in [op for op, _ in cluster.calls]

    async def test_an_empty_plan_touches_nothing_and_says_so(self) -> None:
        cluster = converged_cluster()
        result = await execute(cluster, plan(desired(), await survey(cluster, desired())).steps)
        assert result["success"] is True
        assert cluster.calls == []
        assert "already matches" in result["execution_log"]


class TestPlanIsPureAndTestableWithoutACluster:
    async def test_plan_needs_no_io_at_all(self) -> None:
        """The whole point of the split: idempotence is decided here, in a
        function a test can call with two plain values."""
        empty = GuestNetworkSurvey()
        assert len(plan(desired(), empty).steps) == 10
