"""Review #648 tranche 3: what the lifecycle CLAIMS versus what it does.

Companion to `test_precheck_truth.py`. These cover the half of the tranche that
is about the product's account of itself rather than about a mutating call:

* approving a composite must approve the steps it names (ARTIFACT_SPEC §5.4/§7);
* a composite's drift verdict must not be greener than its sub-artifacts';
* a `requires_snapshot: true` that cannot be honoured must be refused, not
  silently dropped;
* the interpolation ARTIFACT_SPEC D2 makes binding must be proposable;
* a validation refusal must reach the MCP caller as its own message.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from homepilot.artifacts.lifecycle import ArtifactLifecycle, LifecycleError
from homepilot.artifacts.models import ArtifactStatus
from homepilot.reconciler.verify import DriftState, VerifyResult, _verify_composite


def _spec(**overrides: Any) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "id": "2026-08-29-t3-sub",
        "kind": "proxmox-api-sequence",
        "intent": "a step",
        "body": (
            "# s\n\n```yaml proxmox-api-spec\nsteps:\n"
            "  - id: read\n    method: GET\n    path: /nodes/pve/status\n```\n"
        ),
        "target": {"kind": "cluster"},
        "idempotence": "via-precheck",
        "produced_by": {"session": "s", "agent": "a", "user": "u"},
    }
    spec.update(overrides)
    return spec


def _composite_spec(*sub_ids: str, **overrides: Any) -> dict[str, Any]:
    steps = "".join(f"  - id: s{n}\n    artifact: {sid}\n" for n, sid in enumerate(sub_ids))
    fields: dict[str, Any] = {
        "id": "2026-08-29-t3-comp",
        "kind": "composite",
        "intent": "a composite",
        "body": f"# c\n\n```yaml composite-spec\nsteps:\n{steps}```\n",
    }
    fields.update(overrides)
    return _spec(**fields)


# --------------------------------------------------------------------------- #
# Composite approval cascade
# --------------------------------------------------------------------------- #


class TestApprovingACompositeApprovesItsSteps:
    """ARTIFACT_SPEC §5.4: "approving a composite implicitly approves all
    referenced `proposed` sub-artifacts that aren't already `approved`", and §7
    again, with the cascade "recorded as separate audit log entries".

    Nothing did it. The apply then died with "status is proposed, expected
    approved", so `composite` - the ONE mechanism D1 names for multi-target
    work - could not be run without an operator approving every piece by hand.
    Reproduced live on dev 3.6.14 before this fix.
    """

    async def test_the_sub_artifact_is_approved_too(self, mock_store: MagicMock) -> None:
        lc = ArtifactLifecycle(store=mock_store)
        sub = await lc.propose(_spec())
        comp = await lc.propose(_composite_spec(sub))

        await lc.approve(comp, user="olli", reason="ship it")

        sub_fm, _ = mock_store.read(sub)
        assert sub_fm["status"] == ArtifactStatus.APPROVED.value, (
            "the composite was approved and its step was left proposed, which is "
            "exactly the state its apply refuses to run"
        )
        assert "cascaded from composite" in sub_fm["approved_by"]["reason"]
        assert sub_fm["approved_by"]["user"] == "olli"

    async def test_the_cascade_reaches_a_composite_of_composites(
        self, mock_store: MagicMock
    ) -> None:
        lc = ArtifactLifecycle(store=mock_store)
        leaf = await lc.propose(_spec(id="2026-08-29-t3-leaf"))
        inner = await lc.propose(_composite_spec(leaf, id="2026-08-29-t3-inner", intent="inner"))
        outer = await lc.propose(_composite_spec(inner, id="2026-08-29-t3-outer", intent="outer"))

        await lc.approve(outer, user="olli")

        for aid in (inner, leaf):
            fm, _ = mock_store.read(aid)
            assert fm["status"] == ArtifactStatus.APPROVED.value

    async def test_the_cascade_is_atomic(self, mock_store: MagicMock) -> None:
        """§7: "if any sub-artifact fails to approve … the whole cascade is
        rolled back and the composite stays `proposed`"."""
        lc = ArtifactLifecycle(store=mock_store)
        ok_sub = await lc.propose(_spec())
        comp = await lc.propose(_composite_spec(ok_sub, "2026-08-29-t3-missing"))

        with pytest.raises(LifecycleError, match="does not exist"):
            await lc.approve(comp, user="olli")

        comp_fm, _ = mock_store.read(comp)
        ok_fm, _ = mock_store.read(ok_sub)
        assert comp_fm["status"] == ArtifactStatus.PROPOSED.value
        assert ok_fm["status"] == ArtifactStatus.PROPOSED.value, (
            "a cascade that half-approved would leave a plan nobody reviewed as a whole"
        )

    async def test_an_already_approved_sub_is_left_alone(self, mock_store: MagicMock) -> None:
        lc = ArtifactLifecycle(store=mock_store)
        sub = await lc.propose(_spec())
        await lc.approve(sub, user="someone-else", reason="direct")
        comp = await lc.propose(_composite_spec(sub))

        await lc.approve(comp, user="olli")

        fm, _ = mock_store.read(sub)
        assert fm["approved_by"]["reason"] == "direct"


class TestACompositeThatRanNothingIsNotASuccess:
    """#642 B9. A step naming no artifact was logged and skipped, and the
    composite came back `success: True` - so an artifact could reach `applied`
    having touched nothing at all. Propose refuses the shape, but `hp artifacts
    edit` and a hand edit of the file both re-validate nothing, so the executor
    is the last place it can be caught."""

    async def test_a_step_with_no_artifact_fails_the_composite(self) -> None:
        from homepilot.executor.composite import execute as composite_execute

        body = "```yaml composite-spec\nsteps:\n  - id: s1\n```\n"
        result = await composite_execute({"id": "c"}, body, MagicMock(), MagicMock())
        assert result["success"] is False
        assert "names no artifact" in result["execution_log"]


# --------------------------------------------------------------------------- #
# Composite drift
# --------------------------------------------------------------------------- #


class TestACompositeIsNoGreenerThanItsParts:
    """#642 B2. Both sibling verifiers guard this with `_evaluated`; the
    composite one tested `sub_result.drifted`, a boolean that is False for
    UNKNOWN as well as for IN_SPEC. So a composite of ansible artifacts - every
    one of which reports "not checked" - reported in spec. Confirmed live on dev
    3.6.14: sub state `unknown`, composite `in_spec`."""

    @staticmethod
    def _store_with(body: str) -> MagicMock:
        store = MagicMock()
        store.read = MagicMock(return_value=({"kind": "composite"}, body))
        return store

    @staticmethod
    def _body(*sub_ids: str) -> str:
        steps = "".join(f"  - id: s{n}\n    artifact: {s}\n" for n, s in enumerate(sub_ids))
        return f"```yaml composite-spec\nsteps:\n{steps}```\n"

    async def test_an_unknown_sub_makes_the_composite_unknown(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_verify(aid: str, *a: Any, **kw: Any) -> VerifyResult:
            return VerifyResult(artifact_id=aid, state=DriftState.UNKNOWN)

        monkeypatch.setattr("homepilot.reconciler.verify.verify_artifact", fake_verify)
        body = self._body("sub-a")
        result = await _verify_composite(
            "comp", {"kind": "composite"}, body, self._store_with(body), MagicMock(), MagicMock(), 0
        )
        assert result.state is DriftState.UNKNOWN
        assert result.drifted is False

    async def test_a_skipped_sub_makes_the_composite_unknown(self) -> None:
        store = MagicMock()
        store.read = MagicMock(side_effect=FileNotFoundError("gone"))
        body = self._body("sub-gone")
        result = await _verify_composite(
            "comp", {"kind": "composite"}, body, store, MagicMock(), MagicMock(), 0
        )
        assert result.state is DriftState.UNKNOWN

    async def test_all_subs_in_spec_is_still_in_spec(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def fake_verify(aid: str, *a: Any, **kw: Any) -> VerifyResult:
            return VerifyResult(artifact_id=aid, state=DriftState.IN_SPEC)

        monkeypatch.setattr("homepilot.reconciler.verify.verify_artifact", fake_verify)
        body = self._body("sub-a", "sub-b")
        result = await _verify_composite(
            "comp", {"kind": "composite"}, body, self._store_with(body), MagicMock(), MagicMock(), 0
        )
        assert result.state is DriftState.IN_SPEC


class TestADriftCheckThatFailedIsNotAVerdict:
    """A verifier that RAISED left the stored verdict standing, so an artifact
    kept whatever colour its last successful check gave it. Seen live: a
    `ProxmoxError` from a precheck path took the whole check down, the
    reconciler logged an "error" and wrote no row, and MCP said "Internal
    server error"."""

    async def test_a_raising_verifier_answers_unknown(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from homepilot.adapters.proxmox import ProxmoxError
        from homepilot.reconciler.verify import verify_artifact

        store = MagicMock()
        store.read = MagicMock(
            return_value=(
                {"kind": "proxmox-api-sequence", "status": "applied", "target": {"kind": "node"}},
                "```yaml proxmox-api-spec\nsteps: []\n```",
            )
        )

        async def boom(*a: Any, **kw: Any) -> VerifyResult:
            raise ProxmoxError("GET", "/x", 500, "boom")

        monkeypatch.setattr("homepilot.reconciler.verify._verify_proxmox_api", boom)
        result = await verify_artifact("a", MagicMock(), store, MagicMock())
        assert result.state is DriftState.UNKNOWN
        assert result.drifted is False
        assert "boom" in result.verification_log


# --------------------------------------------------------------------------- #
# requires_snapshot
# --------------------------------------------------------------------------- #


class TestRequiresSnapshotIsHonouredOrRefused:
    """#642 A7. `requires_snapshot: true` on anything but a vm/lxc target was
    dropped in silence: the review screen said the apply had a rollback point
    and it did not. The project already refuses an unhonourable `rollback: true`
    at propose (#426); this is the same rule for the same reason."""

    async def test_a_snapshot_claim_that_cannot_be_honoured_is_refused(
        self, mock_store: MagicMock
    ) -> None:
        lc = ArtifactLifecycle(store=mock_store)
        with pytest.raises(LifecycleError, match="requires_snapshot"):
            await lc.propose(_spec(requires_snapshot=True))

    async def test_it_is_accepted_on_a_guest(self, mock_store: MagicMock) -> None:
        lc = ArtifactLifecycle(store=mock_store)
        aid = await lc.propose(
            _spec(requires_snapshot=True, target={"kind": "vm", "vmid": 101, "node": "pve"})
        )
        fm, _ = mock_store.read(aid)
        assert fm["requires_snapshot"] is True

    async def test_a_required_snapshot_with_no_vmid_aborts_the_apply(self) -> None:
        """The docstring on the failure branch has forbidden a silent skip since
        #388; the guard above it did one anyway."""
        from homepilot.executor.orchestrator import ArtifactExecutor, ExecutorError

        ex = ArtifactExecutor(
            store=MagicMock(),
            lifecycle=MagicMock(),
            repo=MagicMock(),
            proxmox=MagicMock(),
            vault=MagicMock(),
        )
        with pytest.raises(ExecutorError, match=r"no vmid|no node"):
            await ex._maybe_snapshot({"id": "a", "requires_snapshot": True}, {"kind": "vm"})


# --------------------------------------------------------------------------- #
# The interpolation the spec makes binding
# --------------------------------------------------------------------------- #


class TestTheSpecsOwnInterpolationCanBeProposed:
    """ARTIFACT_SPEC D2: "all path / value interpolation in spec bodies uses
    Jinja2 syntax: `{{ target.node }}`, `{{ target.vmid }}`, `{{ target.host }}`".

    The propose-time validator rendered the body with `target` faked as the empty
    STRING, so every one of those raised "'str object' has no attribute 'node'"
    and the propose was refused - for §13's own worked Example 1 among others.
    Reproduced live on dev 3.6.14, where it reached the agent as "Internal server
    error" on top (#635).
    """

    @pytest.mark.parametrize(
        "fragment",
        [
            "{{ target.node }}",
            "{{ target.vmid }}",
            "{{ target.host }}",
            "{{ artifact.id }}",
            "{{ now }}",
        ],
    )
    async def test_d2_names_are_accepted(self, mock_store: MagicMock, fragment: str) -> None:
        lc = ArtifactLifecycle(store=mock_store)
        body = (
            "# x\n\n```yaml proxmox-api-spec\nsteps:\n  - id: s\n    method: GET\n"
            f"    path: /nodes/{fragment}\n```\n"
        )
        aid = await lc.propose(
            _spec(body=body, target={"kind": "lxc", "vmid": 142, "node": "pve1", "host": "jf"})
        )
        assert aid

    async def test_a_typo_is_still_refused(self, mock_store: MagicMock) -> None:
        lc = ArtifactLifecycle(store=mock_store)
        body = (
            "# x\n\n```yaml proxmox-api-spec\nsteps:\n  - id: s\n    method: GET\n"
            "    path: /nodes/{{ target.nodee }}\n```\n"
        )
        with pytest.raises(LifecycleError, match="Jinja2"):
            await lc.propose(_spec(body=body, target={"kind": "lxc", "vmid": 142, "node": "pve1"}))

    async def test_a_cluster_target_may_not_name_a_node(self, mock_store: MagicMock) -> None:
        """§11.7: for cluster artifacts "paths must NOT contain
        `{{ target.node }}` (the executor picks the node)". Now enforced by the
        same render, because a cluster target genuinely has no `node`."""
        lc = ArtifactLifecycle(store=mock_store)
        body = (
            "# x\n\n```yaml proxmox-api-spec\nsteps:\n  - id: s\n    method: GET\n"
            "    path: /nodes/{{ target.node }}/status\n```\n"
        )
        with pytest.raises(LifecycleError, match="Jinja2"):
            await lc.propose(_spec(body=body, target={"kind": "cluster"}))

    async def test_a_broken_skip_if_in_the_body_is_refused(self, mock_store: MagicMock) -> None:
        """Nothing ever validated these: the propose-time check looked at a
        FRONTMATTER `skip_if`, a key ARTIFACT_SPEC does not define."""
        lc = ArtifactLifecycle(store=mock_store)
        body = (
            "# x\n\n```yaml proxmox-api-spec\nsteps:\n  - id: s\n    method: POST\n"
            "    path: /go\n    precheck:\n      method: GET\n      path: /probe\n"
            '      skip_if: "len(response.json) > 0"\n```\n'
        )
        with pytest.raises(LifecycleError, match="skip_if"):
            await lc.propose(_spec(body=body))


# --------------------------------------------------------------------------- #
# A refusal must reach the caller
# --------------------------------------------------------------------------- #


class TestMcpSaysWhatItRefused:
    """#635, still live at 3.6.14. `propose_artifact` answered a validation
    failure with a bare "Internal server error"; the message naming the fix was
    in a log the MCP caller cannot read."""

    async def test_a_lifecycle_error_becomes_a_named_refusal(self) -> None:
        import json

        from homepilot.mcp.tools.artifact_tools import handle_propose_artifact

        lifecycle = MagicMock()
        lifecycle.propose = AsyncMock(side_effect=LifecycleError("intent must be 1-200 chars"))
        ctx = {"lifecycle": lifecycle, "store": MagicMock()}

        with pytest.raises(ValueError, match="intent must be 1-200 chars"):
            await handle_propose_artifact({"spec": json.dumps(_spec())}, ctx)


# --------------------------------------------------------------------------- #
# host-provision: unreadable is not absent
# --------------------------------------------------------------------------- #


class TestUnreadableIsNotAbsent:
    """#642 A6. `capture_pre_state`'s own docstring says a failed read "is
    recorded as 'unknown' rather than as 'absent'"; the code wrote
    `existed: False`. The apply then overwrote the file as a FIRST write and the
    revoke reported "created by this artifact" - so the prior bytes went away
    with nothing recording that they had ever been there. The agent's read fails
    for permission, for a file over the hub's frame budget, and for a denied
    path, none of which mean the file is missing."""

    @staticmethod
    def _spec_with_config() -> Any:
        from homepilot.artifacts.models import parse_host_provision_spec

        return parse_host_provision_spec(
            "```yaml host-provision-spec\nconfig_files:\n"
            "  - path: /etc/nginx/nginx.conf\n    content: |\n      new\n    mode: '0644'\n```\n"
        )

    async def test_a_permission_error_is_recorded_as_unknown(self) -> None:
        from homepilot.executor.host_provision import capture_pre_state

        agent = MagicMock()
        agent.read_file = AsyncMock(
            side_effect=Exception("permission denied: /etc/nginx/nginx.conf")
        )
        agent.exec_readonly = AsyncMock(return_value=(0, "", ""))

        captured = await capture_pre_state(agent, "web1", self._spec_with_config())
        entry = captured[0]
        assert entry["existed"] is None, "an unreadable file was filed as absent"
        assert "permission denied" in entry["read_error"]

    async def test_a_genuinely_missing_file_is_still_absent(self) -> None:
        from homepilot.executor.host_provision import capture_pre_state

        agent = MagicMock()
        agent.read_file = AsyncMock(side_effect=Exception("file not found: /etc/nginx/nginx.conf"))
        agent.exec_readonly = AsyncMock(return_value=(0, "", ""))

        captured = await capture_pre_state(agent, "web1", self._spec_with_config())
        assert captured[0]["existed"] is False

    async def test_the_revoke_names_what_it_never_read(self) -> None:
        from homepilot.executor.host_provision import execute

        agent = MagicMock()
        agent.write_config = AsyncMock(return_value={"changed": True})
        result = await execute(
            {"id": "a"},
            "",
            {"kind": "vm", "host": "web1", "vmid": 1, "node": "pve"},
            agent,
            rollback=True,
            pre_state=[
                {
                    "kind": "config",
                    "name": "/etc/nginx/nginx.conf",
                    "existed": None,
                    "prior_content": None,
                    "read_error": "permission denied",
                }
            ],
        )
        assert result["success"] is False
        assert "never established" in result["failure_reason"]
        assert "created by this artifact" not in result["failure_reason"]
        agent.write_config.assert_not_called()

    async def test_drift_answers_unknown_when_the_host_did_not_answer(self) -> None:
        from homepilot.executor.host_provision import check_drift

        agent = MagicMock()
        agent.read_file = AsyncMock(side_effect=Exception("permission denied"))
        agent.exec_readonly = AsyncMock(return_value=(0, "", ""))

        result = await check_drift(agent, "web1", self._spec_with_config())
        assert result["drifted"] is False, "an unread file was reported as drift"
        assert result["unknown_items"] == ["config:/etc/nginx/nginx.conf"]

    async def test_the_verifier_reports_unknown_not_in_spec(self) -> None:
        """The tri-state has to reach the STORED verdict, not stop at
        `check_drift`: `_verify_host_provision` had no UNKNOWN branch at all."""
        from homepilot.reconciler.verify import _verify_host_provision

        agent = MagicMock()
        agent.read_file = AsyncMock(side_effect=Exception("permission denied"))
        agent.exec_readonly = AsyncMock(return_value=(0, "", ""))
        executor = MagicMock()
        executor.host_adapter = agent
        executor.pve_nodes = []

        body = (
            "```yaml host-provision-spec\nconfig_files:\n"
            "  - path: /etc/nginx/nginx.conf\n    content: |\n      new\n    mode: '0644'\n```\n"
        )
        result = await _verify_host_provision(
            "a", {"target": {"kind": "vm", "host": "web1"}}, body, executor
        )
        assert result.state is DriftState.UNKNOWN

    async def test_a_package_query_that_failed_is_not_absent(self) -> None:
        from homepilot.artifacts.models import parse_host_provision_spec
        from homepilot.executor.host_provision import check_drift

        spec = parse_host_provision_spec("```yaml host-provision-spec\npackages:\n  - nginx\n```\n")
        agent = MagicMock()
        # dpkg exits 2 for its own errors; the old test was `rc == 0`, so this
        # read as "absent" and the plan promised to install.
        agent.exec_readonly = AsyncMock(return_value=(2, "", "dpkg: error: dpkg frontend lock"))

        result = await check_drift(agent, "web1", spec)
        assert result["drifted"] is False
        assert result["unknown_items"] == ["package:nginx"]


# --------------------------------------------------------------------------- #
# A3: the storage content list is REPLACED, so it has to be read first
# --------------------------------------------------------------------------- #


class TestTheStorageContentListIsNeverGuessed:
    """#642 A3. `set_storage_content` REPLACES the list - PVE takes no delta - so
    a read that did not establish the existing types would send `content=import`
    ALONE and un-declare `images`, `iso` and `backup` on a storage other people's
    guests are sitting on, while recording
    `storage_import_content_added: true` as if it had added one type."""

    async def test_an_unestablished_read_refuses_to_write(self) -> None:
        from homepilot.provision.template import GuestTemplateService

        proxmox = MagicMock()
        proxmox.get_storage = AsyncMock(return_value={})
        proxmox.set_storage_content = AsyncMock()

        svc = GuestTemplateService.__new__(GuestTemplateService)
        svc.proxmox = proxmox  # type: ignore[attr-defined]

        with pytest.raises(RuntimeError, match="did not report its content types"):
            await svc._ensure_import_content("shared-nvme")
        proxmox.set_storage_content.assert_not_called()

    async def test_a_real_read_keeps_every_existing_type(self) -> None:
        from homepilot.provision.template import GuestTemplateService

        proxmox = MagicMock()
        proxmox.get_storage = AsyncMock(return_value={"content": "images,iso,backup"})
        proxmox.set_storage_content = AsyncMock()

        svc = GuestTemplateService.__new__(GuestTemplateService)
        svc.proxmox = proxmox  # type: ignore[attr-defined]

        assert await svc._ensure_import_content("shared-nvme") is True
        sent = proxmox.set_storage_content.call_args[0][1]
        for kept in ("images", "iso", "backup"):
            assert kept in sent
        assert "import" in sent
