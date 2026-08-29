"""A firewall lockout must be IMPOSSIBLE through HomePilot (#600).

Live incident (2026-08-27, dev pve1): enabling the PVE datacenter firewall to
make the guest fence enforce compiled a default INPUT-DROP with no management-
allow rule, severing SSH (22) and the API (8006) to the node until console
recovery. The lesson, verified live on that same node: enable WITH
``policy_in=ACCEPT`` and the host INPUT chain stays open - the per-VM guest
fences do the fencing, the node stays reachable, no lockout.

Four gates, each proven by planting the defect and watching the NAMED test fail:

* ``safe_datacenter_firewall_enable`` / ``ensure_datacenter_firewall_enabled``
  turn the switch on with ``enable=1`` AND ``policy_in=ACCEPT`` in ONE write, no-
  op when already safe, and NEVER produce ``policy_in=DROP``.
  Teeth: make the safe-enable write ``policy_in=DROP`` (or drop the idempotence
  check) -> ``test_enable_on_a_disabled_cluster_writes_accept_not_drop`` /
  ``test_an_already_safe_cluster_is_a_no_op`` fail.

* ``datacenter_firewall_lockout_safe`` REFUSES an enabled DROP (or an enabled
  firewall with no policy_in, which PVE defaults to DROP) and ACCEPTS an enabled
  ACCEPT. Teeth: make the guard return True unconditionally ->
  ``test_the_guard_refuses_an_enabled_drop`` fails.

* the guest-network plan with a fence includes the ensure step, applying it
  leaves the fake at ``enable=1/policy_in=ACCEPT``, a converged cluster plans
  nothing, and a no-fence network does NOT enable the firewall. Teeth: drop the
  ``isolate_cidrs`` guard in ``_datacenter_firewall_steps`` ->
  ``test_a_no_fence_network_does_not_enable_the_firewall`` fails; drop the
  idempotence guard -> ``test_a_converged_cluster_plans_no_firewall_step`` fails.

* the report/survey says ENFORCED vs CONFIGURED-but-not-enforced correctly for
  (fw off) and (fw on, safe). Teeth: make ``fence_enforced`` return True always
  -> ``test_the_report_says_configured_not_enforced_when_the_switch_is_off``
  fails.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from homepilot.adapters.pve_sdn import (
    PveSdnGateway,
    datacenter_firewall_lockout_safe,
    safe_datacenter_firewall_enable,
)
from homepilot.provision.guest_network import enforcement_note, plan, survey

from .test_guest_network import FakeCluster, converged_cluster, desired

pytestmark = pytest.mark.asyncio


# ── A fake MultiClient at the boundary proxmox_mcp owns ──────────────────────
# The gateway reads /cluster/firewall/options and writes it back; this models
# just that endpoint, recording every PUT so a test can prove exactly one safe
# write happened (or none).


class _FakeFirewallOptions:
    def __init__(self, store: dict[str, Any]) -> None:
        self._store = store

    async def get(self, **_kw: Any) -> dict[str, Any]:
        return dict(self._store["options"])

    async def put(self, **kw: Any) -> None:
        self._store["puts"].append(dict(kw))
        self._store["options"].update(kw)


class _FakeApi:
    def __init__(self, store: dict[str, Any]) -> None:
        self.cluster = SimpleNamespace(
            firewall=SimpleNamespace(options=_FakeFirewallOptions(store))
        )


class _FakeClient:
    """Enough of proxmox_mcp's MultiClient for the cluster-firewall endpoint."""

    def __init__(self, options: dict[str, Any] | None = None) -> None:
        self.store: dict[str, Any] = {"options": dict(options or {}), "puts": []}

    def get_client(self, elevated: bool = False) -> _FakeApi:
        return _FakeApi(self.store)

    async def safe_api_call(self, fn: Any, **kwargs: Any) -> Any:
        # The real surface consumes its own `elevated` flag; the wire fn never
        # sees it. Strip it here so the fake models the same contract.
        kwargs.pop("elevated", None)
        return await fn(**kwargs)


class TestTheSafeEnablePrimitive:
    async def test_enable_on_a_disabled_cluster_writes_accept_not_drop(self) -> None:
        client = _FakeClient({})  # a fresh cluster: firewall off
        gw = PveSdnGateway(client)
        said = await gw.ensure_datacenter_firewall_enabled()
        assert client.store["puts"] == [{"enable": 1, "policy_in": "ACCEPT"}]
        assert client.store["options"]["enable"] == 1
        assert client.store["options"]["policy_in"] == "ACCEPT"
        assert "policy_in=ACCEPT" in said

    async def test_an_already_safe_cluster_is_a_no_op(self) -> None:
        client = _FakeClient({"enable": 1, "policy_in": "ACCEPT"})
        gw = PveSdnGateway(client)
        said = await gw.ensure_datacenter_firewall_enabled()
        assert client.store["puts"] == [], "an already-safe cluster must not be written"
        assert "nothing to do" in said

    async def test_it_never_writes_a_drop_input_policy(self) -> None:
        """About what HomePilot WRITES, not what it finds.

        An operator's existing DROP is left exactly where it is (see
        test_an_enabled_firewall_is_never_written_to_at_all) - what must never
        happen is HomePilot putting a DROP input policy onto the wire itself.
        """
        for start in ({}, {"enable": 1, "policy_in": "DROP"}, {"enable": 0}):
            client = _FakeClient(dict(start))
            gw = PveSdnGateway(client)
            await gw.ensure_datacenter_firewall_enabled()
            for put in client.store["puts"]:
                assert str(put.get("policy_in", "")).upper() != "DROP"

    async def test_an_enabled_firewall_is_never_written_to_at_all(self) -> None:
        """THE OWNER'S RULE: "We do not touch rules already there and we will
        not break them."

        WHAT THIS FORBIDS: rewriting the input policy of a firewall that is
        already on. The old behaviour "repaired" an enabled DROP to ACCEPT,
        which on a real cluster meant reaching into a hand-built firewall - 34
        operator rules, ipsets, the lot - and loosening its input policy, to
        avert a lockout that was not happening. An enabled firewall belongs to
        the operator; the only thing HomePilot may do is turn a DISABLED one on.
        """
        for start in (
            {"enable": 1},
            {"enable": 1, "policy_in": "DROP"},
            {"enable": 1, "policy_in": "REJECT"},
            {"enable": 1, "policy_in": "ACCEPT"},
        ):
            client = _FakeClient(dict(start))
            gw = PveSdnGateway(client)
            said = await gw.ensure_datacenter_firewall_enabled()

            assert client.store["puts"] == [], (
                f"HomePilot wrote to an already-enabled firewall: {start} -> {client.store['puts']}"
            )
            assert client.store["options"] == start, "the operator's options were modified"
            assert "nothing to do" in said

    async def test_the_pure_helper_agrees(self) -> None:
        assert safe_datacenter_firewall_enable({}) == {"enable": 1, "policy_in": "ACCEPT"}
        # ALREADY ON = not ours. Whatever the policy says, there is no write.
        assert safe_datacenter_firewall_enable({"enable": 1, "policy_in": "ACCEPT"}) is None
        assert safe_datacenter_firewall_enable({"enable": 1, "policy_in": "DROP"}) is None
        assert safe_datacenter_firewall_enable({"enable": 1}) is None
        # Only a DISABLED firewall is ever turned on, and only at ACCEPT.
        assert safe_datacenter_firewall_enable({"enable": 0, "policy_in": "DROP"}) == {
            "enable": 1,
            "policy_in": "ACCEPT",
        }


class TestTheLockoutGuard:
    async def test_the_guard_refuses_an_enabled_drop(self) -> None:
        assert datacenter_firewall_lockout_safe({"enable": 1, "policy_in": "DROP"}) is False

    async def test_the_guard_refuses_an_enabled_firewall_with_no_policy(self) -> None:
        # PVE defaults an enabled firewall with no explicit input policy to DROP,
        # so absent IS the lockout case, not a safe blank.
        assert datacenter_firewall_lockout_safe({"enable": 1}) is False

    async def test_the_guard_accepts_an_enabled_accept(self) -> None:
        assert datacenter_firewall_lockout_safe({"enable": 1, "policy_in": "ACCEPT"}) is True

    async def test_a_disabled_firewall_cannot_lock_anyone_out(self) -> None:
        assert datacenter_firewall_lockout_safe({"enable": 0, "policy_in": "DROP"}) is True
        assert datacenter_firewall_lockout_safe({}) is True


class TestThePlanEnablesTheFenceSafely:
    async def test_a_fence_plan_includes_the_ensure_firewall_step(self) -> None:
        current = await survey(FakeCluster(), desired())  # firewall off
        the_plan = plan(desired(), current)
        step = next((s for s in the_plan.steps if s.id == "ensure-datacenter-firewall"), None)
        assert step is not None, "a fenced network must ensure the master switch is on"
        assert step.op == "ensure_datacenter_firewall_enabled"
        assert step.params == {}
        # It is the LAST step: the fence rules exist before the switch flips.
        assert the_plan.steps[-1].id == "ensure-datacenter-firewall"

    async def test_applying_the_plan_leaves_the_switch_on_and_safe(self) -> None:
        cluster = FakeCluster()  # firewall off
        from homepilot.provision.guest_network import execute

        result = await execute(cluster, plan(desired(), await survey(cluster, desired())).steps)
        assert result["success"] is True
        assert cluster.dc_fw_options == {"enable": 1, "policy_in": "ACCEPT"}

    async def test_a_converged_cluster_plans_no_firewall_step(self) -> None:
        cluster = converged_cluster()  # switch already on and safe
        the_plan = plan(desired(), await survey(cluster, desired()))
        assert the_plan.steps == ()
        assert not any(s.id == "ensure-datacenter-firewall" for s in the_plan.steps)

    async def test_a_no_fence_network_does_not_enable_the_firewall(self) -> None:
        want = desired(isolate_cidrs=())
        the_plan = plan(want, await survey(FakeCluster(), want))
        assert not any(s.id == "ensure-datacenter-firewall" for s in the_plan.steps)


class TestTheReportSurfacesTheTruth:
    async def test_the_report_says_configured_not_enforced_when_the_switch_is_off(self) -> None:
        current = await survey(FakeCluster(dc_fw_options={}), desired())
        assert current.datacenter_firewall_enabled is False
        assert current.fence_enforced is False
        note = enforcement_note(current)
        assert "NOT ENFORCED" in note
        assert "OFF" in note

    async def test_the_report_says_enforced_when_the_switch_is_on_and_safe(self) -> None:
        current = await survey(
            FakeCluster(dc_fw_options={"enable": 1, "policy_in": "ACCEPT"}), desired()
        )
        assert current.datacenter_firewall_enabled is True
        assert current.datacenter_firewall_policy_in == "ACCEPT"
        assert current.fence_enforced is True
        note = enforcement_note(current)
        assert "ENFORCED" in note and "NOT ENFORCED" not in note

    async def test_the_report_never_calls_the_operators_policy_a_lockout_risk(self) -> None:
        """It reported a "management-lockout risk" from ONE field.

        WHAT THIS FORBIDS: turning the cluster input policy into a verdict. The
        claim was made without reading the cluster rules, the node rules or the
        ipsets that decide reachability, and it was wrong on a live cluster -
        the operator was talking to the node while being told they were about
        to lose it. HomePilot writes no deny of any kind, so it has no finding
        to report here: it states the policy and says whose it is.
        """
        for policy in ("DROP", "REJECT", None):
            options = {"enable": 1}
            if policy:
                options["policy_in"] = policy
            current = await survey(FakeCluster(dc_fw_options=options), desired())
            note = enforcement_note(current)

            assert "lockout" not in note.lower(), f"policy_in={policy} still reported as a lockout"
            assert "risk" not in note.lower()
            # The fence IS enforced, which is the thing HomePilot actually knows.
            assert current.fence_enforced is True
            assert "ENFORCED" in note and "NOT ENFORCED" not in note
            assert current.datacenter_firewall_policy_in == (policy or "unset")

    async def test_the_survey_dict_carries_the_enforcement_fields(self) -> None:
        current = await survey(
            FakeCluster(dc_fw_options={"enable": 1, "policy_in": "ACCEPT"}), desired()
        )
        d = current.to_dict()
        assert d["datacenter_firewall_enabled"] is True
        assert d["fence_enforced"] is True
        assert d["datacenter_firewall_options"] == {"enable": 1, "policy_in": "ACCEPT"}
