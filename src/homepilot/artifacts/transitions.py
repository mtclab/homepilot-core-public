from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from .models import (
    ArtifactKind,
    ArtifactStatus,
    LifecycleError,
    compute_body_hash,
    extract_composite_steps,
    utcnow_iso,
)
from .validator import validate_transition

if TYPE_CHECKING:
    from .file_store import ArtifactFileStore

_logger = logging.getLogger(__name__)


class ArtifactTransitionManager:
    def __init__(
        self,
        file_store: ArtifactFileStore,
        audit_fn: Callable[
            [str, str, str | None, dict[str, Any] | None],
            Awaitable[None],
        ]
        | None = None,
        db: Any = None,
    ) -> None:
        self.file_store = file_store
        self._audit_fn = audit_fn
        self._db = db

    async def _audit(
        self,
        action: str,
        artifact_id: str,
        user: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        if self._audit_fn is not None:
            await self._audit_fn(action, artifact_id, user, details)

    async def _sync_to_db(self, artifact_id: str, fm: dict[str, Any]) -> None:
        if self._db is None:
            return
        try:
            produced_by = fm.get("produced_by")
            target = fm.get("target")
            supersedes = fm.get("supersedes")
            tags = fm.get("tags")
            approved_by = fm.get("approved_by")
            rejected_by = fm.get("rejected_by")
            revoked_by = fm.get("revoked_by")
            await self._db.upsert_artifact(
                id=fm["id"],
                kind=fm["kind"],
                intent=fm.get("intent", ""),
                status=fm["status"],
                mutating=fm.get("mutating", True),
                hash=fm.get("hash"),
                target_json=json.dumps(target) if target else None,
                idempotence=fm.get("idempotence"),
                produced_by_json=json.dumps(produced_by) if produced_by else None,
                file_path=self.file_store.relative_path(fm["id"]),
                supersedes_json=json.dumps(supersedes) if supersedes else None,
                tags_json=json.dumps(tags) if tags else None,
                rollback=fm.get("rollback", False),
                replay_safe=fm.get("replay_safe", True),
                requires_snapshot=fm.get("requires_snapshot", True),
                note_kind=fm.get("note_kind"),
                approved_by_json=json.dumps(approved_by) if approved_by else None,
                applied_at=fm.get("applied_at"),
                failed_at=fm.get("failed_at"),
                failure_reason=fm.get("failure_reason"),
                superseded_by=fm.get("superseded_by"),
                rejected_by_json=json.dumps(rejected_by) if rejected_by else None,
                revoked_by_json=json.dumps(revoked_by) if revoked_by else None,
            )
        except (sqlite3.OperationalError, sqlite3.IntegrityError) as exc:
            _logger.exception("DB sync failed for artifact %s: %s", artifact_id, exc)

    async def approve(
        self, id: str, user: str, reason: str | None = None, role: str | None = None
    ) -> None:
        with self.file_store.file_lock(id):
            fm, body = self.file_store.read(id)
            current_status = ArtifactStatus(fm["status"])
            validate_transition(current_status, ArtifactStatus.APPROVED)

            current_hash = compute_body_hash(body)
            if current_hash != fm.get("hash"):
                raise LifecycleError(f"Hash mismatch for {id}: body was modified after propose")

            # ARTIFACT_SPEC §7: "Cascade is atomic - if any sub-artifact fails to
            # approve (e.g. it was edited and the hash is now stale), the whole
            # cascade is rolled back and the composite stays `proposed`." So the
            # subs are CHECKED before the composite is written, and nothing is
            # written at all if one of them cannot go.
            cascade = self._composite_cascade(id, fm, body)

            now = utcnow_iso()
            fm["status"] = ArtifactStatus.APPROVED.value
            fm["approved_by"] = {"user": user, "at": now}
            if role:
                fm["approved_by"]["role"] = role
            if reason:
                fm["approved_by"]["reason"] = reason

            fm_yml = self.file_store.serialize_frontmatter(fm)
            self.file_store.write(id, fm_yml, body, "approve")
        details: dict[str, Any] | None = {"approved_by_role": role} if role else None
        if cascade:
            details = dict(details or {})
            details["cascaded_to"] = cascade
        await self._audit("approve", id, user, details)
        await self._sync_to_db(id, fm)
        for sub_id in cascade:
            await self._approve_cascaded(sub_id, user, reason, role, via=id)

    def _composite_cascade(
        self, composite_id: str, fm: dict[str, Any], body: str, visited: set[str] | None = None
    ) -> list[str]:
        """Every `proposed` sub-artifact this approval must carry with it.

        ARTIFACT_SPEC says approving a composite approves its subs, in §5.4
        ("approving a composite implicitly approves all referenced `proposed`
        sub-artifacts that aren't already `approved`") and again in §7. Nothing
        did it. The composite then failed at apply with "status is proposed,
        expected approved" - so the ONE mechanism D1 names for multi-target work
        could not be run without an operator approving every piece by hand.
        Confirmed live on dev 3.6.14.

        Raises LifecycleError if any sub cannot be approved, BEFORE anything is
        written, which is what makes the cascade atomic.
        """
        if fm.get("kind") != ArtifactKind.COMPOSITE.value:
            return []
        if visited is None:
            visited = set()
        visited.add(composite_id)

        try:
            steps = extract_composite_steps(body)
        except ValueError as exc:
            raise LifecycleError(
                f"composite {composite_id} has an unreadable spec, so approval cannot "
                f"cascade to its steps: {exc}"
            ) from None

        out: list[str] = []
        for step in steps:
            sub_id = str(step.get("artifact") or "")
            if not sub_id or sub_id in visited:
                continue
            visited.add(sub_id)
            try:
                sub_fm, sub_body = self.file_store.read(sub_id)
            except FileNotFoundError:
                raise LifecycleError(
                    f"composite step '{step.get('id', '?')}' references {sub_id}, which does "
                    "not exist; approving would approve a plan that cannot run"
                ) from None
            sub_status = ArtifactStatus(sub_fm.get("status", ""))
            if sub_status is ArtifactStatus.APPROVED:
                continue
            if sub_status is not ArtifactStatus.PROPOSED:
                raise LifecycleError(
                    f"sub-artifact {sub_id} is {sub_status.value}; a composite can only be "
                    "approved while every step it names is proposed or approved"
                )
            if compute_body_hash(sub_body) != sub_fm.get("hash"):
                raise LifecycleError(
                    f"sub-artifact {sub_id} was modified after propose (hash mismatch), so "
                    "the composite cannot be approved over it"
                )
            out.append(sub_id)
            # Composites of composites cascade too (§7). Cycles are refused at
            # propose; `visited` makes that structural rather than assumed.
            out.extend(self._composite_cascade(sub_id, sub_fm, sub_body, visited))
        return out

    async def _approve_cascaded(
        self,
        sub_id: str,
        user: str,
        reason: str | None,
        role: str | None,
        via: str,
    ) -> None:
        with self.file_store.file_lock(sub_id):
            fm, body = self.file_store.read(sub_id)
            if ArtifactStatus(fm["status"]) is not ArtifactStatus.PROPOSED:
                return
            fm["status"] = ArtifactStatus.APPROVED.value
            fm["approved_by"] = {"user": user, "at": utcnow_iso()}
            if role:
                fm["approved_by"]["role"] = role
            # The composite's reason is copied, per §7, with the composite named
            # so the audit trail says WHY this artifact was approved without
            # anyone opening it.
            fm["approved_by"]["reason"] = (
                f"{reason} (cascaded from composite {via})"
                if reason
                else f"cascaded from composite {via}"
            )
            fm_yml = self.file_store.serialize_frontmatter(fm)
            self.file_store.write(sub_id, fm_yml, body, "approve")
        # One audit row per sub-artifact, per §7.
        await self._audit("approve", sub_id, user, {"cascaded_from": via})
        await self._sync_to_db(sub_id, fm)
        if self._db is not None:
            try:
                await self._db.clear_approval_code(sub_id)
            except (sqlite3.OperationalError, sqlite3.IntegrityError):
                _logger.exception("Clearing approval code failed for %s", sub_id)

    async def reject(
        self, id: str, user: str, reason: str | None = None, role: str | None = None
    ) -> None:
        with self.file_store.file_lock(id):
            fm, body = self.file_store.read(id)
            current_status = ArtifactStatus(fm["status"])
            validate_transition(current_status, ArtifactStatus.REJECTED)

            now = utcnow_iso()
            fm["status"] = ArtifactStatus.REJECTED.value
            fm["rejected_by"] = {"user": user, "at": now}
            if role:
                fm["rejected_by"]["role"] = role
            if reason:
                fm["rejected_by"]["reason"] = reason

            fm_yml = self.file_store.serialize_frontmatter(fm)
            self.file_store.write(id, fm_yml, body, "reject")
        details = {"rejected_by_role": role} if role else None
        await self._audit("reject", id, user, details)
        await self._sync_to_db(id, fm)

    async def edit(self, id: str) -> None:
        with self.file_store.file_lock(id):
            fm, body = self.file_store.read(id)
            current_status = ArtifactStatus(fm["status"])

            terminal = {
                ArtifactStatus.APPLIED,
                ArtifactStatus.SUPERSEDED,
                ArtifactStatus.REVOKED,
                ArtifactStatus.REJECTED,
            }
            if current_status in terminal:
                raise LifecycleError(
                    f"Cannot edit artifact in terminal state: {current_status.value}"
                )

            new_hash = compute_body_hash(body)

            if new_hash == fm.get("hash"):
                return

            fm["hash"] = new_hash
            if current_status in (
                ArtifactStatus.APPROVED,
                ArtifactStatus.FAILED,
            ):
                fm["status"] = ArtifactStatus.PROPOSED.value
                fm.pop("approved_by", None)

            fm_yml = self.file_store.serialize_frontmatter(fm)
            self.file_store.write(id, fm_yml, body, "edit")

        await self._cascade_invalidate(id)
        await self._sync_to_db(id, fm)

    async def _cascade_invalidate(self, edited_id: str, visited: set[str] | None = None) -> None:
        if visited is None:
            visited = set()
        if edited_id in visited:
            return
        visited.add(edited_id)
        all_artifacts = self.file_store.list(
            status=ArtifactStatus.APPROVED.value,
            kind=ArtifactKind.COMPOSITE.value,
        )
        for candidate in all_artifacts:
            cid = candidate.get("id", "")
            if cid == edited_id:
                continue
            if cid in visited:
                continue
            _, cbody = self.file_store.read(cid)
            if edited_id in cbody:
                cfm = candidate.copy()
                cfm["status"] = ArtifactStatus.PROPOSED.value
                cfm.pop("approved_by", None)
                fm_yml = self.file_store.serialize_frontmatter(cfm)
                self.file_store.write(cid, fm_yml, cbody, "edit")
                await self._audit(
                    "invalidate",
                    cid,
                    details={"reason": f"sub-artifact {edited_id} edited"},
                )
                await self._sync_to_db(cid, cfm)
                await self._cascade_invalidate(cid, visited)

    async def mark_applied(self, id: str, execution_log: str) -> None:
        with self.file_store.file_lock(id):
            fm, body = self.file_store.read(id)
            current_status = ArtifactStatus(fm["status"])
            validate_transition(current_status, ArtifactStatus.APPLIED)

            body = body.rstrip("\n") + "\n\n## Execution log\n\n" + execution_log + "\n"

            fm["status"] = ArtifactStatus.APPLIED.value
            fm["applied_at"] = utcnow_iso()
            fm["hash"] = compute_body_hash(body)

            fm_yml = self.file_store.serialize_frontmatter(fm)
            self.file_store.write(id, fm_yml, body, "apply")
        await self._audit("apply", id)
        await self._sync_to_db(id, fm)

    async def mark_failed(self, id: str, reason: str, partial_log: str = "") -> None:
        with self.file_store.file_lock(id):
            fm, body = self.file_store.read(id)
            current_status = ArtifactStatus(fm["status"])
            validate_transition(current_status, ArtifactStatus.FAILED)

            if partial_log:
                body = body.rstrip("\n") + "\n\n## Execution log\n\n" + partial_log + "\n"

            fm["status"] = ArtifactStatus.FAILED.value
            fm["failed_at"] = utcnow_iso()
            fm["failure_reason"] = reason
            fm["hash"] = compute_body_hash(body)

            fm_yml = self.file_store.serialize_frontmatter(fm)
            self.file_store.write(id, fm_yml, body, "fail")
        await self._audit("fail", id)
        await self._sync_to_db(id, fm)

    async def supersede(self, old_id: str, new_id: str) -> None:
        with self.file_store.file_lock(old_id):
            fm, body = self.file_store.read(old_id)
            current_status = ArtifactStatus(fm["status"])
            validate_transition(current_status, ArtifactStatus.SUPERSEDED)

            fm["status"] = ArtifactStatus.SUPERSEDED.value
            fm["superseded_by"] = new_id

            fm_yml = self.file_store.serialize_frontmatter(fm)
            self.file_store.write(old_id, fm_yml, body, "supersede")
        await self._audit("supersede", old_id)
        await self._sync_to_db(old_id, fm)

    async def revoke(self, id: str, user: str, reason: str | None = None) -> None:
        with self.file_store.file_lock(id):
            fm, body = self.file_store.read(id)
            current_status = ArtifactStatus(fm["status"])
            validate_transition(current_status, ArtifactStatus.REVOKED)

            now = utcnow_iso()
            fm["status"] = ArtifactStatus.REVOKED.value
            fm["revoked_by"] = {"user": user, "at": now}
            if reason:
                fm["revoked_by"]["reason"] = reason

            fm_yml = self.file_store.serialize_frontmatter(fm)
            self.file_store.write(id, fm_yml, body, "revoke")
        await self._audit("revoke", id, user)
        await self._sync_to_db(id, fm)
