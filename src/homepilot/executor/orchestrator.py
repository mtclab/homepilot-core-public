from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from homepilot.adapters.agent import AgentAdapter
from homepilot.adapters.proxmox import ProxmoxClient, ProxmoxError
from homepilot.artifacts.lifecycle import ArtifactLifecycle
from homepilot.artifacts.models import (
    ArtifactKind,
    ArtifactStatus,
    Idempotence,
    compute_body_hash,
)
from homepilot.artifacts.store import ArtifactStore
from homepilot.db.repository import Repository
from homepilot.vault.manager import VaultManager

from .ansible import execute as ansible_execute
from .composite import execute as composite_execute
from .guest_network import execute as guest_network_execute
from .host_provision import execute as host_provision_execute
from .http_sequence import execute as http_sequence_execute
from .kb_note import execute as kb_note_execute
from .proxmox_api import execute as proxmox_api_execute
from .rollback import kind_can_roll_back
from .shell_script import execute as shell_script_execute

logger = logging.getLogger(__name__)

# PVE refuses a `snapname` longer than this, so `hp-pre-<artifact id>` failed the
# apply of any artifact whose id ran past 33 characters - BEFORE step 1, on a
# guest that was fine. Hit twice on prod 3.6.9 and twice more on dev (#627).
PVE_SNAPNAME_MAX = 40
_SNAP_PREFIX = "hp-pre-"
# When the id has to be shortened, a hash of the WHOLE id rides along so two long
# ids that share a prefix cannot land on one snapshot.
_SNAP_HASH_LEN = 7


def snapshot_name_for(artifact_id: str) -> str:
    """The pre-apply snapshot name for this artifact, always ≤ 40 chars (#627).

    Short ids keep the exact `hp-pre-<id>` they have always had, so nothing that
    already exists on a cluster is renamed.
    """
    candidate = f"{_SNAP_PREFIX}{artifact_id}"
    if len(candidate) <= PVE_SNAPNAME_MAX:
        return candidate
    digest = hashlib.sha256(artifact_id.encode()).hexdigest()[:_SNAP_HASH_LEN]
    room = PVE_SNAPNAME_MAX - len(_SNAP_PREFIX) - 1 - _SNAP_HASH_LEN
    return f"{_SNAP_PREFIX}{artifact_id[:room]}-{digest}"


# The target kinds a Proxmox snapshot can actually be taken of. ARTIFACT_SPEC §3
# defaults `requires_snapshot` to true "for `mutating: true` against VM/LXC
# targets, `false` otherwise" - but an artifact that SETS it true against any
# other kind used to have that request silently dropped, with nothing said to the
# operator who read `requires_snapshot: true` on the review screen (#642 A7).
SNAPSHOTTABLE_TARGET_KINDS = ("vm", "lxc")


@dataclass
class ExecutionResult:
    success: bool
    execution_log: str
    snapshot_id: str | None = None
    failure_reason: str | None = None


@dataclass
class RevokeResult:
    """What a revoke actually did to the HOST, not just to the artifact.

    `rolled_back` is the whole point: "revoked" describes the artifact's status
    and says nothing about the machine. An operator undoing a change needs to
    know whether it was reversed or merely relabelled (#426).
    """

    rolled_back: bool
    reason: str
    execution_log: str = ""


class ArtifactExecutor:
    def __init__(
        self,
        store: ArtifactStore,
        lifecycle: ArtifactLifecycle,
        repo: Repository,
        proxmox: ProxmoxClient,
        vault: VaultManager,
        pve_nodes: list[str] | None = None,
        agent: AgentAdapter | None = None,
        settings_source: Any = None,
    ):
        self.store = store
        self.lifecycle = lifecycle
        self.repo = repo
        self.proxmox = proxmox
        self.vault = vault
        self.pve_nodes = pve_nodes or []
        self.agent = agent
        # Where operator settings are resolved from at APPLY time (#553). None
        # falls back to the process-wide resolver, which is what a CLI apply and
        # the unit tests get - and which honestly answers "nothing configured"
        # when there is no database.
        self.settings_source = settings_source
        # The PVE SDN/firewall gateway (the estate's proxmox_mcp library). Built
        # lazily from the Proxmox client's own credentials so there is ONE
        # configuration surface, and settable so a test can drive an apply
        # against a fake cluster at that boundary.
        self.sdn_gateway: Any = None

    @property
    def host_adapter(self) -> AgentAdapter:
        """The agent hub is the only host-management transport (the SSH/jump
        server was removed). Host operations require a connected agent."""
        if self.agent is None:
            raise RuntimeError("no host adapter — the agent hub is required for host operations")
        return self.agent

    async def apply(self, artifact_id: str, approved_by: str) -> ExecutionResult:
        fm, body = self.store.read(artifact_id)
        kind = ArtifactKind(fm["kind"])
        status = ArtifactStatus(fm["status"])

        if kind == ArtifactKind.KB_NOTE:
            return ExecutionResult(success=True, execution_log="kb-note auto-applied on propose")

        if status != ArtifactStatus.APPROVED:
            raise ExecutorError(
                f"Artifact {artifact_id} status is {status.value}, expected approved"
            )

        current_hash = compute_body_hash(body)
        if current_hash != fm.get("hash"):
            raise TamperError(f"Hash mismatch for {artifact_id}: body tampered after approval")

        # (The `replay_safe` re-apply guard that used to sit here could never
        # fire: the status check above has already refused anything but
        # `approved`, so its `status == applied` test was dead. The real guard is
        # in `replay()`, which is the path an already-applied artifact takes.)

        snapshot_id: str | None = None
        mutating = fm.get("mutating", True)
        target_data = fm.get("target", {})
        target_kind = target_data.get("kind", "")

        try:
            if mutating and target_kind in SNAPSHOTTABLE_TARGET_KINDS:
                snapshot_id = await self._maybe_snapshot(fm, target_data)
            result = await self._dispatch(kind, fm, body, target_data)
        except Exception as exc:
            logger.exception("Executor failed for %s", artifact_id)
            fail_reason = str(exc)
            partial_log = f"Executor error: {fail_reason}"
            await self.lifecycle.mark_failed(artifact_id, fail_reason, partial_log)
            await self.lifecycle._log_audit(
                "apply_failed", artifact_id, approved_by, {"reason": fail_reason}
            )
            return ExecutionResult(
                success=False,
                execution_log=partial_log,
                snapshot_id=snapshot_id,
                failure_reason=fail_reason,
            )

        if result.success:
            await self.lifecycle.mark_applied(artifact_id, result.execution_log)
            # The single actor-bearing record of this apply. The snapshot id rides
            # here because it is what an operator needs to undo the change.
            await self.lifecycle._log_audit(
                "apply",
                artifact_id,
                approved_by,
                {"snapshot_id": snapshot_id} if snapshot_id else None,
            )
        else:
            await self.lifecycle.mark_failed(
                artifact_id, result.failure_reason or "unknown", result.execution_log
            )
            await self.lifecycle._log_audit(
                "apply_failed", artifact_id, approved_by, {"reason": result.failure_reason}
            )

        result.snapshot_id = snapshot_id
        return result

    async def replay(self, artifact_id: str) -> ExecutionResult:
        fm, body = self.store.read(artifact_id)
        kind = ArtifactKind(fm["kind"])
        status = ArtifactStatus(fm["status"])

        if kind == ArtifactKind.KB_NOTE:
            return ExecutionResult(success=True, execution_log="kb-note: no-op replay")

        if fm.get("replay_safe") is False and status == ArtifactStatus.APPLIED:
            raise ExecutorError(
                f"Artifact {artifact_id} is not replay-safe. Revoke and create a new artifact."
            )

        idempotence_str = fm.get("idempotence", "")
        if idempotence_str == Idempotence.REPLAY_ONLY.value and status == ArtifactStatus.APPLIED:
            raise ExecutorError(
                f"Artifact {artifact_id} is replay-only. Use revoke + new artifact instead."
            )

        if status not in (ArtifactStatus.APPROVED, ArtifactStatus.APPLIED):
            raise ExecutorError(f"Artifact {artifact_id} status is {status.value}, cannot replay")

        current_hash = compute_body_hash(body)
        if current_hash != fm.get("hash"):
            raise TamperError(f"Hash mismatch for {artifact_id}: body tampered after approval")

        target_data = fm.get("target", {})
        target_kind = target_data.get("kind", "")
        snapshot_id: str | None = None
        mutating = fm.get("mutating", True)

        try:
            if mutating and target_kind in SNAPSHOTTABLE_TARGET_KINDS:
                snapshot_id = await self._maybe_snapshot(fm, target_data)
            result = await self._dispatch(kind, fm, body, target_data)
        except Exception as exc:
            logger.exception("Replay failed for %s", artifact_id)
            fail_reason = str(exc)
            partial_log = f"Replay error: {fail_reason}"
            await self.lifecycle.mark_failed(artifact_id, fail_reason, partial_log)
            await self.lifecycle._log_audit(
                "replay_failed", artifact_id, "replay", {"reason": fail_reason}
            )
            return ExecutionResult(
                success=False,
                execution_log=partial_log,
                snapshot_id=snapshot_id,
                failure_reason=fail_reason,
            )

        if result.success:
            if status == ArtifactStatus.APPROVED:
                await self.lifecycle.mark_applied(artifact_id, result.execution_log)
            await self.lifecycle._log_audit("replay", artifact_id, "replay")
        else:
            if status == ArtifactStatus.APPROVED:
                await self.lifecycle.mark_failed(
                    artifact_id, result.failure_reason or "unknown", result.execution_log
                )
            await self.lifecycle._log_audit(
                "replay_failed", artifact_id, "replay", {"reason": result.failure_reason}
            )

        result.snapshot_id = snapshot_id
        return result

    async def revoke(self, artifact_id: str, user: str, reason: str | None = None) -> RevokeResult:
        """Revoke an artifact, and say whether the host was actually REVERSED.

        Revoke used to return nothing, so a rollback that was skipped, that was a
        no-op, or that failed outright was indistinguishable from one that
        worked: the artifact went to `revoked` and everyone moved on (#426). The
        outcome now rides back to the caller and into the audit row, because
        "reversed" and "relabelled" are different facts about a host.
        """
        fm, body = self.store.read(artifact_id)
        kind = ArtifactKind(fm["kind"])

        outcome = RevokeResult(rolled_back=False, reason="no rollback for this artifact")
        if kind != ArtifactKind.KB_NOTE:
            if fm.get("rollback"):
                outcome = await self._run_rollback(kind, fm, body)
            elif kind_can_roll_back(kind):
                outcome = RevokeResult(
                    rolled_back=False,
                    reason="the artifact carries no rollback section; the host keeps the change",
                )
            else:
                outcome = RevokeResult(
                    rolled_back=False,
                    reason=f"kind '{kind.value}' cannot reverse itself; the host keeps the change",
                )

        await self.lifecycle.revoke(artifact_id, user, reason)
        await self.lifecycle._log_audit(
            "revoke",
            artifact_id,
            user,
            {
                "reason": reason,
                # The word an operator reads in the journal.
                "outcome": "reversed" if outcome.rolled_back else "relabelled",
                "rollback_detail": outcome.reason,
            },
        )
        return outcome

    async def _dispatch(
        self, kind: ArtifactKind, fm: dict[str, Any], body: str, target: dict[str, Any]
    ) -> ExecutionResult:
        result: dict[str, Any]
        if kind == ArtifactKind.ANSIBLE_PLAYBOOK:
            result = await ansible_execute(
                fm, body, target, self.host_adapter, self.repo, self.pve_nodes
            )
        elif kind == ArtifactKind.PROXMOX_API_SEQUENCE:
            result = await proxmox_api_execute(fm, body, target, self.proxmox, self.vault)
        elif kind == ArtifactKind.HTTP_SEQUENCE:
            result = await http_sequence_execute(fm, body, target, self.vault)
        elif kind == ArtifactKind.COMPOSITE:
            result = await composite_execute(fm, body, self.lifecycle, self)
        elif kind == ArtifactKind.SHELL_SCRIPT:
            result = await shell_script_execute(
                fm, body, target, self.host_adapter, self.pve_nodes, vault=self.vault
            )
        elif kind == ArtifactKind.HOST_PROVISION:
            result = await host_provision_execute(
                fm, body, target, self.host_adapter, self.pve_nodes, vault=self.vault
            )
            # The executor captured what the host looked like before it acted;
            # store it, because after this the prior bytes are gone and a revoke
            # would have nothing to invert to (#426). Persisted even on FAILURE:
            # a partial apply is exactly when putting the host back matters most.
            await self._store_pre_state(str(fm.get("id", "")), result.pop("pre_state", None))
        elif kind == ArtifactKind.GUEST_NETWORK:
            # The settings source is the app state when there is one: the desired
            # guest network a body leaves unstated comes from THIS instance's
            # settings, resolved at apply time like every other C2 consumer.
            result = await guest_network_execute(
                fm, body, target, self.proxmox, self.settings_source, self.sdn_gateway
            )
        elif kind == ArtifactKind.KB_NOTE:
            result = await kb_note_execute(fm, body, self.repo)
        else:
            raise ExecutorError(f"Unknown artifact kind: {kind.value}")
        return ExecutionResult(
            success=bool(result.get("success", False)),
            execution_log=result.get("execution_log", ""),
            snapshot_id=result.get("snapshot_id"),
            failure_reason=result.get("failure_reason"),
        )

    async def _maybe_snapshot(self, fm: dict[str, Any], target: dict[str, Any]) -> str | None:
        requires_snapshot = fm.get("requires_snapshot") is not False
        if not requires_snapshot:
            return None
        vmid = target.get("vmid")
        node = target.get("node")
        if vmid is None or not node:
            # A REQUIRED snapshot that cannot be addressed is not a snapshot that
            # was not needed. Returning None here silently dropped the safety net
            # the artifact asked for and let the apply proceed (#642 A7); the
            # docstring on the failure branch below has forbidden exactly that
            # since #388.
            raise ExecutorError(
                f"Pre-apply snapshot required for {fm.get('id')} but the target names "
                f"no {'vmid' if vmid is None else 'node'} to snapshot"
            )
        snap_name = snapshot_name_for(str(fm["id"]))
        # The target already knows what the guest IS ("vm" => qemu, "lxc" =>
        # lxc). Handing it over stops the snapshot call guessing a collection
        # from the VMID and posting a QEMU guest to /lxc/ (#617); a target that
        # says neither leaves the adapter to ask the cluster.
        guest_type = target.get("kind")
        try:
            result = await self.proxmox.snapshot(node, vmid, snap_name, guest_type=guest_type)
            # PVE answers the snapshot with a UPID and returns at once: the
            # guest is LOCKED (snapshot) while the task runs. Starting the
            # steps on that answer raced the safety net itself - the first
            # step came straight back with "VM is locked (snapshot)" and the
            # apply failed on a guest that was fine (seen live on dev 3.6.10).
            # Worse than a failed step: proceeding would mutate a guest whose
            # rollback point is not finished being taken.
            upid = ProxmoxClient.upid_of(result)
            if upid:
                await self.proxmox.wait_for_task(node, upid)
            # The snapshot's NAME is what an operator rolls back to; the UPID
            # is the task that took it and is worthless once it has finished.
            return snap_name
        except (ProxmoxError, httpx.RequestError, httpx.TimeoutException) as exc:
            # A requested pre-apply snapshot is the rollback safety net. If it
            # fails we must NOT silently proceed to mutate the guest with no way
            # back — abort the apply instead (#388).
            logger.error("Required snapshot failed for vmid=%s node=%s: %s", vmid, node, exc)
            raise ExecutorError(
                f"Pre-apply snapshot required for {fm.get('id')} but failed: {exc}"
            ) from exc

    async def _store_pre_state(self, artifact_id: str, captured: Any) -> None:
        if not artifact_id or captured is None:
            return
        try:
            await self.repo.save_host_state_capture(artifact_id, json.dumps(captured))
        except Exception as exc:
            # Best-effort: failing to record the capture must not fail an apply
            # that already succeeded, but it DOES mean the revoke will honestly
            # report that it has nothing to roll back to.
            logger.warning("could not store pre-apply state for %s: %s", artifact_id, exc)

    async def _run_rollback(
        self, kind: ArtifactKind, fm: dict[str, Any], body: str
    ) -> RevokeResult:
        """Run the artifact's rollback and REPORT what happened.

        The result used to be discarded on every path - a handler returning
        `success: False` (e.g. a shell-script with no rollback fence) was thrown
        away, and an exception was logged and swallowed. Both produced a `revoked`
        artifact over an unchanged host, reported as a clean success.
        """
        target = fm.get("target", {})
        result: dict[str, Any] | None = None
        try:
            if kind == ArtifactKind.ANSIBLE_PLAYBOOK:
                result = await ansible_execute(
                    fm, body, target, self.host_adapter, self.repo, self.pve_nodes, rollback=True
                )
            elif kind == ArtifactKind.PROXMOX_API_SEQUENCE:
                result = await proxmox_api_execute(
                    fm, body, target, self.proxmox, self.vault, rollback=True
                )
            elif kind == ArtifactKind.HTTP_SEQUENCE:
                result = await http_sequence_execute(fm, body, target, self.vault, rollback=True)
            elif kind == ArtifactKind.SHELL_SCRIPT:
                result = await shell_script_execute(
                    fm,
                    body,
                    target,
                    self.host_adapter,
                    self.pve_nodes,
                    rollback=True,
                    vault=self.vault,
                )
            elif kind == ArtifactKind.COMPOSITE:
                result = await composite_execute(fm, body, self.lifecycle, self, rollback=True)
            elif kind == ArtifactKind.HOST_PROVISION:
                result = await host_provision_execute(
                    fm,
                    body,
                    target,
                    self.host_adapter,
                    self.pve_nodes,
                    rollback=True,
                    pre_state=await self.repo.get_host_state_capture(str(fm.get("id", ""))),
                    vault=self.vault,
                )
            else:
                return RevokeResult(
                    rolled_back=False,
                    reason=f"kind '{kind.value}' has no rollback path",
                )
        except Exception as exc:
            # Still best-effort - a failed rollback must not block the revoke
            # transition - but no longer SILENT.
            logger.error("Rollback failed for %s (best-effort): %s", fm.get("id"), exc)
            return RevokeResult(rolled_back=False, reason=f"rollback failed: {exc}")

        if result is not None and not result.get("success", False):
            detail = (
                result.get("failure_reason") or result.get("execution_log") or "no reason given"
            )
            return RevokeResult(rolled_back=False, reason=f"rollback did not run: {detail}")
        log = (result or {}).get("execution_log") or ""
        return RevokeResult(rolled_back=True, reason="rollback executed", execution_log=str(log))


class ExecutorError(Exception):
    pass


class TamperError(ExecutorError):
    pass
