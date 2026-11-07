"""THE GATE for #713: the guest fence converges on its ORDER, and ENFORCED is earned.

Three defects, one theme - the fence was believed rather than read:

* Convergence asked only "is each wanted rule present somewhere". A vnet fence
  in the pre-#599 order (DROPs above the DNS/DHCP ACCEPTs) planned 0 steps and
  read converged; a DROP left over for an isolate CIDR that was removed was
  invisible.
* Per-VM fence rules were never read back after provision, so a guest fenced
  before #599 kept its reversed fence - no DNS, no DHCP - forever.
* "ENFORCED" was claimed from the datacenter switch alone. After the #600
  console recovery (`pve-firewall stop`) the switch still reads enable=1 while
  nothing enforces anything.

The cluster is modelled as CHAINS: an ordered rule list per vnet and per guest,
where a create PREPENDS (PVE's behaviour, #599) and a delete addresses a
position. Assertions read the chain back - the order PVE would compile - never
the list of calls made.

Teeth (each proven by reverting and watching the NAMED test fail):
  - compare rule SETS again in ``_fence_convergence_steps`` ->
    ``test_a_reversed_legacy_vnet_fence_is_not_converged``;
  - drop ``_vm_fence_steps`` from ``plan`` ->
    ``test_a_pre_599_guest_fence_is_corrected_in_place``;
  - make ``fence_enforced`` read the switch alone ->
    ``test_a_stopped_firewall_service_is_not_enforced``;
  - drop the ``_expected_rule`` check from the gateway delete ->
    ``test_the_gateway_never_deletes_a_rule_it_did_not_plan_to``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from homepilot.adapters.pve_sdn import PveSdnGateway
from homepilot.provision.guest_network import (
    enforcement_note,
    execute,
    fence_rules,
    is_fence_rule,
    plan,
    survey,
)

from .test_guest_network import FakeCluster, converged_cluster, desired

pytestmark = pytest.mark.asyncio

RUNNING = [{"service": "pve-firewall", "state": "running"}]

# An operator's own rules, which convergence must never move or remove.
FOREIGN_FORWARD = {
    "type": "forward",
    "action": "ACCEPT",
    "dest": "203.0.113.5",
    "enable": 1,
    "comment": "operator: the shared printer",
}
FOREIGN_VM_RULE = {
    "type": "in",
    "action": "ACCEPT",
    "proto": "tcp",
    "dport": "22",
    "enable": 1,
    "comment": "operator: ssh from the jump host",
}

_ROUTING_KEYS = ("pos", "vnet", "node", "vmid", "expect")


def _stored(rule: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in rule.items() if k not in _ROUTING_KEYS}


def _matches(rule: dict[str, Any], expect: dict[str, Any] | None) -> bool:
    return all(str(rule.get(k, "")) == str(v) for k, v in (expect or {}).items())


class ChainCluster(FakeCluster):
    """FakeCluster whose vnet and guest firewalls are ordered chains, as PVE holds them."""

    def __init__(self, **kwargs: Any) -> None:
        vm_chains = kwargs.pop("vm_chains", None)
        super().__init__(**kwargs)
        self.vm_chains: dict[tuple[str, int], list[dict[str, Any]]] = dict(vm_chains or {})

    @staticmethod
    def _with_pos(chain: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [dict(rule, pos=i) for i, rule in enumerate(chain)]

    # vnet chain: `fw_rules` IS the chain
    async def vnet_firewall_rules(self, vnet: str) -> list[dict[str, Any]]:
        return self._with_pos(self.fw_rules)

    async def create_vnet_firewall_rule(self, **params: Any) -> str:
        await self._record("create_vnet_firewall_rule", params)
        self.fw_rules.insert(0, _stored(params))  # PVE prepends, whatever pos says
        return "created"

    async def delete_vnet_firewall_rule(self, **params: Any) -> str:
        await self._record("delete_vnet_firewall_rule", params)
        pos = int(params["pos"])
        assert _matches(self.fw_rules[pos], params.get("expect")), "deleted the wrong rule"
        del self.fw_rules[pos]
        return "deleted"

    # guest chains
    async def list_guest_vms(self) -> list[dict[str, Any]]:
        return [
            {"node": node, "vmid": vmid, "type": "qemu", "template": 0}
            for node, vmid in self.vm_chains
        ]

    async def vm_firewall_rules(self, node: str, vmid: int) -> list[dict[str, Any]] | None:
        chain = self.vm_chains.get((node, vmid))
        return None if chain is None else self._with_pos(chain)

    async def create_vm_firewall_rule(self, node: str, vmid: int, **rule: Any) -> str:
        await self._record("create_vm_firewall_rule", {"node": node, "vmid": vmid, **rule})
        self.vm_chains[(node, vmid)].insert(0, _stored(rule))
        return "created"

    async def delete_vm_firewall_rule(
        self, node: str, vmid: int, pos: int, expect: dict[str, Any] | None = None
    ) -> str:
        await self._record("delete_vm_firewall_rule", {"node": node, "vmid": vmid, "pos": pos})
        chain = self.vm_chains[(node, vmid)]
        assert _matches(chain[pos], expect), "deleted the wrong rule"
        del chain[pos]
        return "deleted"


def _owned(chain: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [_stored(r) for r in chain if is_fence_rule(r)]


def _converged_chains(**kwargs: Any) -> ChainCluster:
    base = converged_cluster()
    return ChainCluster(
        zones=base.zones,
        vnets=base.vnets,
        subnets=base.subnets,
        fw_options=base.fw_options,
        fw_rules=list(kwargs.pop("fw_rules", base.fw_rules)),
        dc_fw_options=base.dc_fw_options,
        services=RUNNING,
        **kwargs,
    )


async def _apply(cluster: ChainCluster) -> None:
    result = await execute(cluster, plan(desired(), await survey(cluster, desired())).steps)
    assert result["success"] is True, result


class TestTheVnetFenceConvergesOnOrder:
    async def test_a_reversed_legacy_vnet_fence_is_not_converged(self) -> None:
        want = fence_rules(desired(), "forward")
        cluster = _converged_chains(fw_rules=[FOREIGN_FORWARD, *reversed(want)])

        the_plan = plan(desired(), await survey(cluster, desired()))
        assert the_plan.steps, "a fence with its DROPs above the DNS/DHCP ACCEPTs read converged"

        await _apply(cluster)
        assert _owned(cluster.fw_rules) == want, "the compiled fence is not in fence_rules order"
        assert FOREIGN_FORWARD in cluster.fw_rules, "an operator's rule was removed"
        assert plan(desired(), await survey(cluster, desired())).converged

    async def test_a_drop_for_a_removed_cidr_is_removed(self) -> None:
        old = desired(isolate_cidrs=("192.0.2.0/24", "198.18.0.0/15"))
        cluster = _converged_chains(fw_rules=fence_rules(old, "forward"))

        assert plan(desired(), await survey(cluster, desired())).steps
        await _apply(cluster)
        assert _owned(cluster.fw_rules) == fence_rules(desired(), "forward")
        assert not any(r.get("dest") == "198.18.0.0/15" for r in cluster.fw_rules)

    async def test_a_correct_fence_beside_foreign_rules_is_left_alone(self) -> None:
        want = fence_rules(desired(), "forward")
        cluster = _converged_chains(fw_rules=[*want[:2], FOREIGN_FORWARD, *want[2:]])
        assert plan(desired(), await survey(cluster, desired())).converged


class TestThePerVmFenceIsReadBack:
    async def test_a_pre_599_guest_fence_is_corrected_in_place(self) -> None:
        want = fence_rules(desired(), "out")
        legacy = [FOREIGN_VM_RULE, *reversed(want)]
        cluster = _converged_chains(vm_chains={("elizabeth", 105): list(legacy)})

        the_plan = plan(desired(), await survey(cluster, desired()))
        assert any(s.id.startswith("vm-105-fence") for s in the_plan.steps), (
            "a guest still fenced in the pre-#599 order was never corrected"
        )

        await _apply(cluster)
        chain = cluster.vm_chains[("elizabeth", 105)]
        assert _owned(chain) == want, "the guest's DNS/DHCP ACCEPTs still sit below its DROPs"
        assert FOREIGN_VM_RULE in chain, "an operator's rule on the guest was removed"
        assert plan(desired(), await survey(cluster, desired())).converged

    async def test_a_guest_without_the_fence_is_not_touched(self) -> None:
        cluster = _converged_chains(vm_chains={("elizabeth", 200): [dict(FOREIGN_VM_RULE)]})
        current = await survey(cluster, desired())
        assert current.vm_fences == {}
        assert plan(desired(), current).converged
        assert cluster.vm_chains[("elizabeth", 200)] == [FOREIGN_VM_RULE]


class TestEnforcedIsEarned:
    async def test_a_stopped_firewall_service_is_not_enforced(self) -> None:
        """The #600 recovery: switch still on, `pve-firewall stop` ran."""
        cluster = _converged_chains()
        cluster.services = [{"service": "pve-firewall", "state": "stopped"}]
        current = await survey(cluster, desired())

        assert current.datacenter_firewall_enabled is True
        assert current.fence_enforced is False
        assert current.enforcement == "configured"
        assert "NOT ENFORCED" in enforcement_note(current)

    async def test_an_unseen_service_is_unverified_not_enforced(self) -> None:
        cluster = _converged_chains()
        cluster.services = []
        current = await survey(cluster, desired())

        assert current.fence_enforced is False
        assert current.enforcement == "unverified"
        note = enforcement_note(current)
        assert "UNVERIFIED" in note
        assert "fence is ENFORCED" not in note

    async def test_a_running_service_is_enforced(self) -> None:
        current = await survey(_converged_chains(), desired())
        assert current.fence_enforced is True
        assert current.enforcement == "enforced"
        assert current.to_dict()["firewall_service_running"] is True


# ── The gateway's delete names the rule it means ─────────────────────────────


class _Rule:
    def __init__(self, store: dict[str, Any], row: dict[str, Any]) -> None:
        self._store, self._row = store, row

    async def get(self, **_kw: Any) -> dict[str, Any]:
        return dict(self._row)

    async def delete(self, **kw: Any) -> None:
        self._store["deletes"].append(kw)


class _Library:
    """proxmox_mcp's MultiClient, as far as a vnet rule delete goes."""

    def __init__(self, at_pos: dict[str, Any]) -> None:
        self.store: dict[str, Any] = {"deletes": []}
        self._at_pos = at_pos

    def get_client(self, elevated: bool = False, endpoint: str | None = None) -> Any:
        rule = _Rule(self.store, self._at_pos)

        class _Rules:
            def __call__(self, pos: int) -> _Rule:
                return rule

        rules = _Rules()
        vnet = SimpleNamespace(firewall=SimpleNamespace(rules=rules))

        class _Vnets:
            def __call__(self, name: str) -> Any:
                return vnet

        return SimpleNamespace(cluster=SimpleNamespace(sdn=SimpleNamespace(vnets=_Vnets())))

    async def safe_api_call(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        kwargs.pop("elevated", None)
        return await fn(*args, **kwargs)


class TestTheGatewayDelete:
    async def test_the_gateway_never_deletes_a_rule_it_did_not_plan_to(self) -> None:
        planned = fence_rules(desired(), "forward")[0]
        expect = {k: planned[k] for k in ("type", "action", "proto", "dport", "dest", "comment")}
        lib = _Library(at_pos=dict(FOREIGN_FORWARD, pos=7))
        gw = PveSdnGateway(lib)

        with pytest.raises(RuntimeError) as exc:
            await gw.delete_vnet_firewall_rule(vnet="innkeep", pos=7, expect=expect)
        assert "nothing was deleted" in str(exc.value)
        assert lib.store["deletes"] == [], "an operator's rule was deleted"

    async def test_the_planned_rule_is_deleted(self) -> None:
        planned = fence_rules(desired(), "forward")[0]
        expect = {k: planned[k] for k in ("type", "action", "proto", "dport", "dest", "comment")}
        lib = _Library(at_pos=dict(planned, pos=7, digest="abc"))
        gw = PveSdnGateway(lib)

        await gw.delete_vnet_firewall_rule(vnet="innkeep", pos=7, expect=expect)
        assert lib.store["deletes"] == [{"digest": "abc"}]
