from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any

import httpx

from ..auth.principal import Principal
from ..events import emit_event
from ..runtime.blocking import run_blocking
from .file_store import ArtifactFileStore
from .models import ApprovalRefusedError, ArtifactStatus
from .models import ConflictError as ConflictError
from .models import LifecycleError as LifecycleError
from .store import ArtifactStore
from .transitions import ArtifactTransitionManager
from .validator import validate_propose_spec, validate_transition

logger = logging.getLogger(__name__)

_NetError = (httpx.HTTPError, httpx.TimeoutException, ConnectionError, OSError)
_ServiceError = (httpx.HTTPError, httpx.TimeoutException, ConnectionError, OSError, RuntimeError)


class ArtifactLifecycle:
    def __init__(self, store: ArtifactStore, repository: Any = None) -> None:
        self.store = store
        self.repo = repository
        self._proxmox: Any = None
        self._ssh: Any = None
        self._vault_mgr: Any = None
        self._pve_nodes_list: list[str] | None = None
        self._executor_ref: Any = None
        self._kb_service: Any = None

        self._file_store = ArtifactFileStore(store)
        self._transitions: ArtifactTransitionManager | None = None

    def _ensure_transitions(self) -> ArtifactTransitionManager:
        if self._transitions is None:
            self._transitions = ArtifactTransitionManager(
                file_store=self._file_store,
                audit_fn=self._log_audit,
                db=self.repo,
            )
        return self._transitions

    async def _log_audit(
        self,
        action: str,
        artifact_id: str,
        user: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        if self.repo is not None:
            await self.repo.log_audit(
                user_id=user or "system",
                source="lifecycle",
                action=action,
                artifact_id=artifact_id,
                details_json=json.dumps(details) if details else None,
            )

    def _proxmox_client(self) -> Any:
        if self._proxmox is None:
            raise RuntimeError("Proxmox client not configured on ArtifactLifecycle")
        return self._proxmox

    def _ssh_adapter(self) -> Any:
        if self._ssh is None:
            raise RuntimeError("SSH adapter not configured on ArtifactLifecycle")
        return self._ssh

    def _vault(self) -> Any:
        if self._vault_mgr is None:
            raise RuntimeError("Vault not configured on ArtifactLifecycle")
        return self._vault_mgr

    def _pve_nodes(self) -> list[str] | None:
        return self._pve_nodes_list

    def _executor(self) -> Any:
        if self._executor_ref is None:
            raise RuntimeError("Executor not configured on ArtifactLifecycle")
        return self._executor_ref

    def _validate_transition(self, current: ArtifactStatus, target: ArtifactStatus) -> None:
        validate_transition(current, target)

    async def propose(self, spec: dict[str, Any]) -> str:
        def _validate_and_write() -> dict[str, Any]:
            # One serialised step, as it was on the loop: the "id is free" check
            # inside the validation and the write that takes the id cannot be
            # split by another propose of the same id.
            fm, event = validate_propose_spec(spec, self.store)
            body: str = spec.get("body", "")
            fm_yml: str = self._file_store.serialize_frontmatter(fm)
            self._file_store.write(spec["id"], fm_yml, body, event)
            return fm

        fm = await self._file_store.run_serialized(_validate_and_write)
        id_str: str = spec["id"]
        # Approval code: generated at PROPOSE and surfaced to operators only. Set
        # below once the artifacts row exists; stays None when there is no repo
        # (e.g. the CLI lifecycle) - the webhook simply omits it there.
        approval_code: str | None = None
        if self.repo is not None:
            try:
                import json as _json

                produced_by = fm.get("produced_by")
                target = fm.get("target")
                supersedes = fm.get("supersedes")
                tags = fm.get("tags")
                await self.repo.upsert_artifact(
                    id=fm["id"],
                    kind=fm["kind"],
                    intent=fm.get("intent", ""),
                    status=fm["status"],
                    mutating=fm.get("mutating", True),
                    hash=fm.get("hash"),
                    target_json=_json.dumps(target) if target else None,
                    idempotence=fm.get("idempotence"),
                    produced_by_json=_json.dumps(produced_by) if produced_by else None,
                    file_path=self._file_store.relative_path(id_str),
                    supersedes_json=_json.dumps(supersedes) if supersedes else None,
                    tags_json=_json.dumps(tags) if tags else None,
                    rollback=fm.get("rollback", False),
                    replay_safe=fm.get("replay_safe", True),
                    requires_snapshot=fm.get("requires_snapshot", True),
                    note_kind=fm.get("note_kind"),
                    applied_at=fm.get("applied_at"),
                )
            except (sqlite3.OperationalError, sqlite3.IntegrityError) as exc:
                logger.exception("DB sync failed for artifact %s: %s", id_str, exc)

            # Mint the approval code AFTER the artifacts row exists. Never logged
            # (it is a human-relay secret); it reaches operators only via the
            # outbound webhook POST below, the review screen for an operator
            # credential, and `hp artifacts show`. No MCP read, no SSE event and
            # no non-operator REST read returns it (#681).
            try:
                from .approval_code import ensure_approval_code

                approval_code = await ensure_approval_code(self.repo, id_str)
            except (sqlite3.OperationalError, sqlite3.IntegrityError) as exc:
                logger.exception("Approval-code mint failed for %s: %s", id_str, exc)

        # Propose wrote an event and a git commit and NO audit row (#433), so the
        # one action that starts every change was the one action missing from the
        # durable trail an operator reads. Every other transition logs one.
        try:
            await self._log_audit(
                "propose",
                id_str,
                (fm.get("produced_by") or {}).get("user"),
                {"kind": fm.get("kind"), "intent": fm.get("intent", "")[:200]},
            )
        except (sqlite3.OperationalError, sqlite3.IntegrityError) as exc:
            logger.exception("Audit log failed for propose %s: %s", id_str, exc)

        try:
            await emit_event(
                "artifact_proposed",
                {
                    "id": fm["id"],
                    "kind": fm["kind"],
                    "intent": fm.get("intent", ""),
                    "status": fm["status"],
                    "mutating": fm.get("mutating", True),
                    "target": fm.get("target"),
                    "idempotence": fm.get("idempotence"),
                    "source": fm.get("produced_by", {}),
                },
                repo=self.repo,
                # The code rides ONLY the outbound webhook POST, to an endpoint an
                # admin configured, so a notification reaches a human without them
                # opening the portal. Never the SSE stream (read scope: every
                # read token sees it) and never the stored delivery row (#681).
                outbound_only={"approval_code": approval_code} if approval_code else None,
            )
        except _NetError:
            logger.warning(
                "Webhook emission failed for artifact_proposed %s",
                id_str,
                exc_info=True,
            )

        # A kb-note is `applied` the moment it is proposed and the executor
        # never runs, so nothing put it in the knowledge base. `propose_artifact`
        # answered `status: "applied"` for a note that `get_kb_doc` could not
        # find and `search_kb` did not return - the exact defect #388 fixed for
        # the `record_fact` tool and left standing on every other propose path,
        # including the one `hp policy init` uses (#648 tranche 6).
        if fm.get("kind") == "kb-note":
            kb_service = getattr(self, "_kb_service", None)
            if kb_service is not None:
                try:
                    outcome = await kb_service.index_note(id_str)
                    if not outcome.get("indexed"):
                        logger.warning(
                            "kb-note %s is applied but NOT searchable: %s",
                            id_str,
                            outcome.get("reason"),
                        )
                except Exception:  # indexing must never fail the propose itself
                    logger.warning("Indexing kb-note %s failed", id_str, exc_info=True)

        return id_str

    async def _approval_authority(
        self, id: str, principal: Principal, approval_code: str | None
    ) -> str:
        """Decide whether `principal` may approve `id`, and on what authority (#681).

        This is THE approval gate, for every transport - REST, MCP, CLI. It used
        to live only in the MCP handler, so the same write token could approve
        over REST with no code at all.

        * An operator credential approves on its own authority ("operator").
        * Anyone else needs the per-artifact code a human relayed
          ("relayed-code"): wrong codes count toward the brute-force lock, a
          locked or absent code refuses. With no live code (already decided, or
          a failed artifact being re-approved) an assistant has no way in.

        Raises ApprovalRefusedError. The message never contains the code.
        """
        if principal.is_operator:
            return "operator"
        if approval_code is None or not str(approval_code).strip():
            raise ApprovalRefusedError(
                "Approving needs an approval code a human relays to you, or an "
                "operator credential. Ask the operator to read the code from the "
                "review screen or `hp artifacts show`."
            )
        if self.repo is None:
            raise ApprovalRefusedError("approval-code store unavailable")

        from .approval_code import LOCK_THRESHOLD, verify_code

        row = await self.repo.get_approval_code_row(id)
        if row is None:
            # Either not awaiting approval or already decided. Do not distinguish,
            # and do not leak whether the id even exists as PROPOSED.
            raise ApprovalRefusedError(
                f"Artifact '{id}' is not awaiting a coded approval "
                "(no approval code is active for it)."
            )
        if int(row["locked"]):
            raise ApprovalRefusedError(
                f"Approval for '{id}' is locked after too many wrong codes. "
                "An operator must reset it from the web UI or "
                "`hp artifacts reset-approval` before it can be approved with a code."
            )
        if not verify_code(str(approval_code), str(row["code"])):
            state = await self.repo.record_failed_approval(id, LOCK_THRESHOLD)
            if state["locked"]:
                raise ApprovalRefusedError(
                    f"Incorrect approval code. Approval for '{id}' is now "
                    "locked; an operator must reset it before any further attempt."
                )
            raise ApprovalRefusedError("Incorrect approval code.")
        return "relayed-code"

    async def approve(
        self,
        id: str,
        principal: Principal,
        reason: str | None = None,
        *,
        approval_code: str | None = None,
    ) -> None:
        # A bare name is not authority: every caller must say WHO is approving as
        # an authenticated Principal, so a transport that forgets the check
        # cannot approve by passing a string (#681).
        if not isinstance(principal, Principal):
            raise TypeError(
                "ArtifactLifecycle.approve needs an authenticated Principal, "
                f"not {type(principal).__name__}"
            )
        authority = await self._approval_authority(id, principal, approval_code)
        await self._ensure_transitions().approve(id, principal.name, reason, authority=authority)
        # The artifact has left PROPOSED, so its approval code is spent: clear it
        # so a replay of the same code cannot re-approve anything (#385 gate 2).
        if self.repo is not None:
            try:
                await self.repo.clear_approval_code(id)
            except (sqlite3.OperationalError, sqlite3.IntegrityError):
                logger.exception("Clearing approval code failed for %s", id)
        try:
            fm, _ = await run_blocking("io", self.store.read, id)
            await emit_event(
                "artifact_approved",
                {
                    "id": id,
                    "status": "approved",
                    "approved_by": fm.get("approved_by"),
                    "kind": fm.get("kind", ""),
                    "intent": fm.get("intent", ""),
                },
                repo=self.repo,
            )
        except _NetError:
            logger.warning(
                "Webhook emission failed for artifact_approved %s",
                id,
                exc_info=True,
            )

    async def reject(self, id: str, user: str, reason: str | None = None) -> None:
        await self._ensure_transitions().reject(id, user, reason)
        # Left PROPOSED (rejected): the approval code is dead, remove it.
        if self.repo is not None:
            try:
                await self.repo.clear_approval_code(id)
            except (sqlite3.OperationalError, sqlite3.IntegrityError):
                logger.exception("Clearing approval code failed for %s", id)
        try:
            fm, _ = await run_blocking("io", self.store.read, id)
            await emit_event(
                "artifact_rejected",
                {
                    "id": id,
                    "status": "rejected",
                    "rejected_by": fm.get("rejected_by"),
                    "kind": fm.get("kind", ""),
                    "intent": fm.get("intent", ""),
                },
                repo=self.repo,
            )
        except _NetError:
            logger.warning(
                "Webhook emission failed for artifact_rejected %s",
                id,
                exc_info=True,
            )

    async def edit(self, id: str) -> None:
        await self._ensure_transitions().edit(id)

    async def edit_content(
        self, id: str, new_fm: dict[str, Any], new_body: str, user: str | None = None
    ) -> dict[str, Any]:
        result = await self._ensure_transitions().edit_content(id, new_fm, new_body, user)
        if result.get("changed") and self.repo is not None:
            # The edited artifact is back in (or still in) review with content
            # nobody has approved. A code a human read off the OLD content must
            # not approve the new one (#652), so it is replaced; an approved
            # artifact sent back to proposed gets one for the same reason.
            try:
                from .approval_code import generate_code

                await self.repo.rotate_approval_code(id, generate_code())
            except (sqlite3.OperationalError, sqlite3.IntegrityError):
                logger.exception("Rotating the approval code failed for %s", id)
        return result

    async def mark_applied(self, id: str, execution_log: str) -> None:
        await self._ensure_transitions().mark_applied(id, execution_log)
        try:
            fm, _ = await run_blocking("io", self.store.read, id)
            if fm.get("kind") == "kb-note":
                try:
                    kb_service = getattr(self, "_kb_service", None)
                    if kb_service is not None:
                        await kb_service.reindex_if_needed()
                except _ServiceError:
                    logger.warning("KB reindex after apply failed for %s", id, exc_info=True)
            await emit_event(
                "artifact_applied",
                {
                    "id": id,
                    "status": "applied",
                    "kind": fm.get("kind", ""),
                    "intent": fm.get("intent", ""),
                    "target": fm.get("target"),
                },
                repo=self.repo,
            )
        except _NetError:
            logger.warning("Webhook emission failed for artifact_applied %s", id, exc_info=True)

    async def mark_failed(self, id: str, reason: str, partial_log: str = "") -> None:
        await self._ensure_transitions().mark_failed(id, reason, partial_log)
        try:
            fm, _ = await run_blocking("io", self.store.read, id)
            await emit_event(
                "artifact_failed",
                {
                    "id": id,
                    "status": "failed",
                    "kind": fm.get("kind", ""),
                    "intent": fm.get("intent", ""),
                },
                repo=self.repo,
            )
        except _NetError:
            logger.warning("Webhook emission failed for artifact_failed %s", id, exc_info=True)

    async def mark_unsettled(self, id: str, entry: dict[str, Any]) -> None:
        await self._ensure_transitions().mark_unsettled(id, entry)

    async def clear_unsettled(self, id: str) -> None:
        await self._ensure_transitions().clear_unsettled(id)

    async def supersede(self, old_id: str, new_id: str) -> None:
        await self._ensure_transitions().supersede(old_id, new_id)
        try:
            fm, _ = await run_blocking("io", self.store.read, old_id)
            if fm.get("kind") == "kb-note":
                try:
                    kb_service = getattr(self, "_kb_service", None)
                    if kb_service is not None:
                        await kb_service.reindex_if_needed(reason="lifecycle")
                except _ServiceError:
                    logger.warning(
                        "KB reindex after supersede failed for %s",
                        old_id,
                        exc_info=True,
                    )
            await emit_event(
                "artifact_superseded",
                {
                    "id": old_id,
                    "superseded_by": new_id,
                    "kind": fm.get("kind", ""),
                    "intent": fm.get("intent", ""),
                },
                repo=self.repo,
            )
        except _NetError:
            logger.warning(
                "Webhook emission failed for artifact_superseded %s",
                old_id,
                exc_info=True,
            )

    async def revoke(self, id: str, user: str, reason: str | None = None) -> None:
        await self._ensure_transitions().revoke(id, user, reason)
        try:
            fm, _ = await run_blocking("io", self.store.read, id)
            if fm.get("kind") == "kb-note":
                try:
                    kb_service = getattr(self, "_kb_service", None)
                    if kb_service is not None:
                        await kb_service.reindex_if_needed(reason="lifecycle")
                except _ServiceError:
                    logger.warning("KB reindex after revoke failed for %s", id, exc_info=True)
            await emit_event(
                "artifact_revoked",
                {
                    "id": id,
                    "status": "revoked",
                    "revoked_by": fm.get("revoked_by"),
                    "kind": fm.get("kind", ""),
                    "intent": fm.get("intent", ""),
                },
                repo=self.repo,
            )
        except _NetError:
            logger.warning("Webhook emission failed for artifact_revoked %s", id, exc_info=True)
