from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from homepilot.adapters.agent import AgentAdapter
from homepilot.adapters.proxmox import ProxmoxClient, ProxmoxError
from homepilot.adapters.ssh import SSHAdapter
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
from .http_sequence import execute as http_sequence_execute
from .kb_note import execute as kb_note_execute
from .proxmox_api import execute as proxmox_api_execute
from .shell_script import execute as shell_script_execute

logger = logging.getLogger(__name__)


@dataclass
class ExecutionResult:
    success: bool
    execution_log: str
    snapshot_id: str | None = None
    failure_reason: str | None = None


class ArtifactExecutor:
    def __init__(
        self,
        store: ArtifactStore,
        lifecycle: ArtifactLifecycle,
        repo: Repository,
        proxmox: ProxmoxClient,
        ssh: SSHAdapter,
        vault: VaultManager,
        pve_nodes: list[str] | None = None,
        agent: AgentAdapter | None = None,
    ):
        self.store = store
        self.lifecycle = lifecycle
        self.repo = repo
        self.proxmox = proxmox
        self.ssh = ssh
        self.vault = vault
        self.pve_nodes = pve_nodes or []
        self.agent = agent

    @property
    def host_adapter(self) -> AgentAdapter | SSHAdapter:
        """Return agent adapter if available, otherwise SSH.

        Agent adapter tries the agent hub first and falls back to SSH.
        """
        if self.agent is not None:
            return self.agent
        return self.ssh

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

        not_replay_safe = fm.get("replay_safe") is False
        already_applied = ArtifactStatus(fm["status"]) == ArtifactStatus.APPLIED
        if not_replay_safe and already_applied:
            raise ExecutorError(
                f"Artifact {artifact_id} is not replay-safe and cannot be re-applied"
            )

        snapshot_id: str | None = None
        mutating = fm.get("mutating", True)
        target_data = fm.get("target", {})
        target_kind = target_data.get("kind", "")

        if mutating and target_kind in ("vm", "lxc"):
            snapshot_id = await self._maybe_snapshot(fm, target_data)

        try:
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
            await self.lifecycle._log_audit("apply", artifact_id, approved_by)
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

        if mutating and target_kind in ("vm", "lxc"):
            snapshot_id = await self._maybe_snapshot(fm, target_data)

        try:
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

    async def revoke(self, artifact_id: str, user: str, reason: str | None = None) -> None:
        fm, body = self.store.read(artifact_id)
        kind = ArtifactKind(fm["kind"])

        if fm.get("rollback") and kind != ArtifactKind.KB_NOTE:
            await self._run_rollback(kind, fm, body)

        await self.lifecycle.revoke(artifact_id, user, reason)
        await self.lifecycle._log_audit("revoke", artifact_id, user, {"reason": reason})

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
            result = await shell_script_execute(fm, body, target, self.host_adapter, self.pve_nodes)
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
        if fm.get("requires_snapshot") is False:
            return None
        vmid = target.get("vmid")
        node = target.get("node")
        if not vmid or not node:
            return None
        snap_name = f"hp-pre-{fm['id']}"
        try:
            result = await self.proxmox.snapshot(node, vmid, snap_name)
            snap_result: Any = result.get("data", snap_name)
            return str(snap_result) if snap_result is not None else snap_name
        except (ProxmoxError, httpx.RequestError, httpx.TimeoutException) as exc:
            logger.warning("Snapshot failed for vmid=%s node=%s: %s", vmid, node, exc)
            return None

    async def _run_rollback(self, kind: ArtifactKind, fm: dict[str, Any], body: str) -> None:
        target = fm.get("target", {})
        try:
            if kind == ArtifactKind.ANSIBLE_PLAYBOOK:
                await ansible_execute(
                    fm, body, target, self.host_adapter, self.repo, self.pve_nodes, rollback=True
                )
            elif kind == ArtifactKind.PROXMOX_API_SEQUENCE:
                await proxmox_api_execute(fm, body, target, self.proxmox, self.vault, rollback=True)
            elif kind == ArtifactKind.HTTP_SEQUENCE:
                await http_sequence_execute(fm, body, target, self.vault, rollback=True)
            elif kind == ArtifactKind.SHELL_SCRIPT:
                await shell_script_execute(
                    fm, body, target, self.host_adapter, self.pve_nodes, rollback=True
                )
            elif kind == ArtifactKind.COMPOSITE:
                await composite_execute(fm, body, self.lifecycle, self, rollback=True)
        except (ProxmoxError, httpx.TimeoutException, OSError) as exc:
            logger.error("Rollback failed for %s (best-effort): %s", fm.get("id"), exc)


class ExecutorError(Exception):
    pass


class TamperError(ExecutorError):
    pass
