from __future__ import annotations

import functools
import json
import logging
import sqlite3
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from .models import (
    ArtifactKind,
    ArtifactStatus,
    ConflictError,
    LifecycleError,
    compute_body_hash,
    extract_composite_steps,
    utcnow_iso,
)
from .validator import validate_artifact_content, validate_transition

if TYPE_CHECKING:
    from .file_store import ArtifactFileStore

_logger = logging.getLogger(__name__)

# Frontmatter the lifecycle writes. An operator's edit may change what the
# artifact DOES; it may not rewrite where it stands (#693).
LIFECYCLE_OWNED_FIELDS = frozenset(
    {
        "id",
        "status",
        "hash",
        "created_at",
        "approved_by",
        "applied_at",
        "failed_at",
        "failure_reason",
        "rejected_by",
        "revoked_by",
        "superseded_by",
        "withdrawn_by",
        "partial_apply",
    }
)


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

    async def _forget_drift_verdict(self, artifact_id: str) -> None:
        """An artifact that leaves the drift universe takes its verdict with it.

        The drift loop re-checks `applied` only, so a revoked artifact's row was
        frozen at its last check and went on being counted: dev's Overview said
        "2 artifacts disagree" for one real drift and one revoked artifact.
        Best-effort, like the DB sync above - the status change is the record;
        the dashboard also joins on active status, so a row left behind by a
        failed delete is still not counted.
        """
        if self._db is None or not hasattr(self._db, "delete_drift_check"):
            return
        try:
            await self._db.delete_drift_check(artifact_id)
        except sqlite3.OperationalError as exc:
            _logger.warning("Could not drop the drift verdict of %s: %s", artifact_id, exc)

    async def approve(
        self,
        id: str,
        user: str,
        reason: str | None = None,
        role: str | None = None,
        authority: str | None = None,
    ) -> None:
        def _locked() -> tuple[dict[str, Any], list[str]]:
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
                # WHERE the human decision came from (#681): "operator" (an operator
                # credential approved) or "relayed-code" (a human relayed the code).
                if authority:
                    fm["approved_by"]["authority"] = authority
                if reason:
                    fm["approved_by"]["reason"] = reason

                fm_yml = self.file_store.serialize_frontmatter(fm)
                self.file_store.write(id, fm_yml, body, "approve")
                return fm, cascade

        fm, cascade = await self.file_store.run_serialized(_locked)
        details: dict[str, Any] | None = {"approved_by_role": role} if role else None
        if authority:
            details = dict(details or {})
            details["authority"] = authority
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
        def _locked() -> dict[str, Any] | None:
            with self.file_store.file_lock(sub_id):
                fm, body = self.file_store.read(sub_id)
                if ArtifactStatus(fm["status"]) is not ArtifactStatus.PROPOSED:
                    return None
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
                return fm

        fm_or_none = await self.file_store.run_serialized(_locked)
        if fm_or_none is None:
            return
        fm = fm_or_none
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
        def _locked() -> dict[str, Any]:
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
                return fm

        fm = await self.file_store.run_serialized(_locked)
        details = {"rejected_by_role": role} if role else None
        await self._audit("reject", id, user, details)
        await self._sync_to_db(id, fm)

    async def edit(self, id: str) -> None:
        def _locked() -> dict[str, Any] | None:
            with self.file_store.file_lock(id):
                fm, body = self.file_store.read(id)
                current_status = ArtifactStatus(fm["status"])

                terminal = {
                    ArtifactStatus.APPLIED,
                    ArtifactStatus.SUPERSEDED,
                    ArtifactStatus.REVOKED,
                    ArtifactStatus.REJECTED,
                    ArtifactStatus.WITHDRAWN,
                }
                if current_status in terminal:
                    raise LifecycleError(
                        f"Cannot edit artifact in terminal state: {current_status.value}"
                    )

                new_hash = compute_body_hash(body)

                if new_hash == fm.get("hash"):
                    return None

                # The same checks propose runs (#652). Refused, the edit is NOT
                # recorded: the stored hash stays stale, so approve refuses the
                # file until it is put right.
                derived = validate_artifact_content(fm, body)
                if "rollback" in derived:
                    fm["rollback"] = derived["rollback"]

                fm["hash"] = new_hash
                if current_status in (
                    ArtifactStatus.APPROVED,
                    ArtifactStatus.FAILED,
                ):
                    fm["status"] = ArtifactStatus.PROPOSED.value
                    fm.pop("approved_by", None)

                fm_yml = self.file_store.serialize_frontmatter(fm)
                self.file_store.write(id, fm_yml, body, "edit")
                return fm

        fm_or_none = await self.file_store.run_serialized(_locked)
        if fm_or_none is None:
            return
        fm = fm_or_none

        await self._cascade_invalidate(id)
        await self._sync_to_db(id, fm)

    async def edit_content(
        self,
        id: str,
        new_fm: dict[str, Any],
        new_body: str,
        user: str | None = None,
    ) -> dict[str, Any]:
        """Replace an artifact's frontmatter and body with an operator's edit (#693).

        `edit()` only records an edit someone already wrote into the store file,
        and only notices a BODY change - so a frontmatter-only edit of an
        approved artifact (a new target, say) kept its approval. This is the
        whole edit, under the file lock:

        * a terminal artifact is refused (``ConflictError``) and nothing is
          written;
        * the fields the lifecycle owns (``LIFECYCLE_OWNED_FIELDS``) may not be
          changed by an edit - status, hash and the approval/rejection records
          are the lifecycle's to write, not the editor's;
        * ANY change to an approved or failed artifact sends it back to
          ``proposed`` and drops the approval: what was approved is no longer
          what is on disk;
        * the edited artifact must pass the SAME validation propose runs
          (``validate_artifact_content``, #652) - otherwise nothing is written;
        * the hash is recomputed, dependants are invalidated, the DB row is
          synced and the edit is audited.

        Returns ``{"changed": bool, "status": str}``.
        """
        terminal = {
            ArtifactStatus.APPLIED,
            ArtifactStatus.SUPERSEDED,
            ArtifactStatus.REVOKED,
            ArtifactStatus.REJECTED,
            ArtifactStatus.WITHDRAWN,
        }

        def _locked() -> tuple[ArtifactStatus, dict[str, Any] | None]:
            with self.file_store.file_lock(id):
                fm, body = self.file_store.read(id)
                current_status = ArtifactStatus(fm["status"])
                if current_status in terminal:
                    raise ConflictError(
                        f"Cannot edit artifact in terminal state: {current_status.value}"
                    )
                changed_owned = sorted(
                    key for key in LIFECYCLE_OWNED_FIELDS if new_fm.get(key) != fm.get(key)
                )
                if changed_owned:
                    raise LifecycleError(
                        "An edit cannot change "
                        + ", ".join(changed_owned)
                        + ": the lifecycle writes those. Leave them as they are."
                    )
                if new_fm == fm and new_body == body:
                    return current_status, None

                # Everything propose checks, checked again on what the edit would
                # store (#652): the edit used to run no validation at all, so every
                # propose-time refusal could be walked around by proposing something
                # valid and editing it afterwards. Refused here, nothing is written.
                derived = validate_artifact_content(new_fm, new_body)

                updated = dict(new_fm)
                updated["hash"] = compute_body_hash(new_body)
                updated["mutating"] = derived["mutating"]
                if "rollback" in derived:
                    updated["rollback"] = derived["rollback"]
                if current_status in (ArtifactStatus.APPROVED, ArtifactStatus.FAILED):
                    updated["status"] = ArtifactStatus.PROPOSED.value
                    updated.pop("approved_by", None)
                self.file_store.write(
                    id, self.file_store.serialize_frontmatter(updated), new_body, "edit"
                )
                return current_status, updated

        current_status, maybe_updated = await self.file_store.run_serialized(_locked)
        if maybe_updated is None:
            return {"changed": False, "status": current_status.value}
        updated = maybe_updated

        await self._cascade_invalidate(id)
        await self._sync_to_db(id, updated)
        await self._audit(
            "edit",
            id,
            user,
            {"from_status": current_status.value, "to_status": updated["status"]},
        )
        return {"changed": True, "status": updated["status"]}

    async def _cascade_invalidate(self, edited_id: str, visited: set[str] | None = None) -> None:
        if visited is None:
            visited = set()
        if edited_id in visited:
            return
        visited.add(edited_id)
        all_artifacts = await self.file_store.alist(
            status=ArtifactStatus.APPROVED.value,
            kind=ArtifactKind.COMPOSITE.value,
        )
        for candidate in all_artifacts:
            cid = candidate.get("id", "")
            if cid == edited_id:
                continue
            if cid in visited:
                continue
            cfm = await self.file_store.run_serialized(
                functools.partial(self._invalidate_if_referenced, cid, edited_id)
            )
            if cfm is not None:
                await self._audit(
                    "invalidate",
                    cid,
                    details={"reason": f"sub-artifact {edited_id} edited"},
                )
                await self._sync_to_db(cid, cfm)
                await self._cascade_invalidate(cid, visited)

    def _invalidate_if_referenced(self, cid: str, edited_id: str) -> dict[str, Any] | None:
        """Send approved composite `cid` back to proposed if its body names
        `edited_id`. Runs serialised with every other artifact write, under the
        file lock, on the CURRENT file (the listing it came from may be stale)."""
        with self.file_store.file_lock(cid):
            cfm, cbody = self.file_store.read(cid)
            if edited_id not in cbody or cfm.get("status") != ArtifactStatus.APPROVED.value:
                return None
            cfm["status"] = ArtifactStatus.PROPOSED.value
            cfm.pop("approved_by", None)
            fm_yml = self.file_store.serialize_frontmatter(cfm)
            self.file_store.write(cid, fm_yml, cbody, "edit")
            return cfm

    async def mark_applied(self, id: str, execution_log: str) -> None:
        def _locked() -> dict[str, Any]:
            with self.file_store.file_lock(id):
                fm, body = self.file_store.read(id)
                current_status = ArtifactStatus(fm["status"])
                validate_transition(current_status, ArtifactStatus.APPLIED)

                body = body.rstrip("\n") + "\n\n## Execution log\n\n" + execution_log + "\n"

                fm["status"] = ArtifactStatus.APPLIED.value
                # A verdict ends an unknown outcome (#774).
                fm.pop("unsettled_task", None)
                # A run that got all the way through supersedes an earlier
                # partial one (#796): the change is whole now.
                fm.pop("partial_apply", None)
                fm["applied_at"] = utcnow_iso()
                fm["hash"] = compute_body_hash(body)

                fm_yml = self.file_store.serialize_frontmatter(fm)
                self.file_store.write(id, fm_yml, body, "apply")
                return fm

        fm = await self.file_store.run_serialized(_locked)
        await self._audit("apply", id)
        await self._sync_to_db(id, fm)

    async def mark_failed(
        self,
        id: str,
        reason: str,
        partial_log: str = "",
        partial_apply: dict[str, Any] | None = None,
    ) -> None:
        """Record a failed run.

        `partial_apply` (#796) is what the run DID change before it stopped -
        the steps that completed, and, where a failed write was re-read, the
        keys PVE took anyway. "Failed" alone told the owner nothing changed when
        a disk had in fact been removed; this is the record that says otherwise.
        None means the run changed nothing it knows of.
        """

        def _locked() -> dict[str, Any]:
            with self.file_store.file_lock(id):
                fm, body = self.file_store.read(id)
                current_status = ArtifactStatus(fm["status"])
                validate_transition(current_status, ArtifactStatus.FAILED)

                if partial_log:
                    body = body.rstrip("\n") + "\n\n## Execution log\n\n" + partial_log + "\n"

                fm["status"] = ArtifactStatus.FAILED.value
                fm.pop("unsettled_task", None)
                fm["failed_at"] = utcnow_iso()
                fm["failure_reason"] = reason
                if partial_apply:
                    fm["partial_apply"] = partial_apply
                else:
                    fm.pop("partial_apply", None)
                fm["hash"] = compute_body_hash(body)

                fm_yml = self.file_store.serialize_frontmatter(fm)
                self.file_store.write(id, fm_yml, body, "fail")
                return fm

        fm = await self.file_store.run_serialized(_locked)
        await self._audit(
            "fail", id, details={"partial_apply": partial_apply} if partial_apply else None
        )
        await self._sync_to_db(id, fm)

    async def mark_unsettled(self, id: str, entry: dict[str, Any]) -> None:
        """Record that a run's outcome is UNKNOWN: its PVE task was still running
        when the run stopped following it (#774). No status change - the
        artifact is neither applied nor failed until PVE says how the task
        ended - and the body is untouched, so the approval hash still holds."""

        def _locked() -> None:
            with self.file_store.file_lock(id):
                fm, body = self.file_store.read(id)
                fm["unsettled_task"] = entry
                fm_yml = self.file_store.serialize_frontmatter(fm)
                self.file_store.write(id, fm_yml, body, "unsettled")

        await self.file_store.run_serialized(_locked)
        await self._audit("unsettled", id, details={"upid": entry.get("upid")})

    async def clear_unsettled(self, id: str) -> None:
        """Drop the unknown-outcome record without a status change - for a run
        whose verdict does not move the artifact (a replay of an applied one)."""

        def _locked() -> None:
            with self.file_store.file_lock(id):
                fm, body = self.file_store.read(id)
                if fm.pop("unsettled_task", None) is None:
                    return
                fm_yml = self.file_store.serialize_frontmatter(fm)
                self.file_store.write(id, fm_yml, body, "settled")

        await self.file_store.run_serialized(_locked)

    async def supersede(self, old_id: str, new_id: str) -> None:
        def _locked() -> dict[str, Any]:
            with self.file_store.file_lock(old_id):
                fm, body = self.file_store.read(old_id)
                current_status = ArtifactStatus(fm["status"])
                validate_transition(current_status, ArtifactStatus.SUPERSEDED)

                fm["status"] = ArtifactStatus.SUPERSEDED.value
                fm["superseded_by"] = new_id

                fm_yml = self.file_store.serialize_frontmatter(fm)
                self.file_store.write(old_id, fm_yml, body, "supersede")
                return fm

        fm = await self.file_store.run_serialized(_locked)
        await self._audit("supersede", old_id)
        await self._sync_to_db(old_id, fm)
        await self._forget_drift_verdict(old_id)

    async def revoke(self, id: str, user: str, reason: str | None = None) -> None:
        def _locked() -> dict[str, Any]:
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
                return fm

        fm = await self.file_store.run_serialized(_locked)
        await self._audit("revoke", id, user)
        await self._sync_to_db(id, fm)
        await self._forget_drift_verdict(id)

    async def withdraw(self, id: str, user: str, reason: str | None = None) -> dict[str, Any]:
        """Take back an approval that was never run (#797): approved -> withdrawn.

        Nothing runs - no executor, no rollback - because nothing was applied.
        Refused for an artifact whose last run left a PVE task unsettled (#774):
        that change may be happening right now, and "withdrawn" would say it is
        not. The caller holds the artifact's run slot (TaskRunner.withdraw), so
        no apply can start underneath this.
        """

        def _locked() -> dict[str, Any]:
            with self.file_store.file_lock(id):
                fm, body = self.file_store.read(id)
                current_status = ArtifactStatus(fm["status"])
                validate_transition(current_status, ArtifactStatus.WITHDRAWN)
                unsettled = fm.get("unsettled_task")
                if unsettled:
                    raise ConflictError(
                        f"cannot withdraw {id}: its last run left PVE task "
                        f"{unsettled.get('upid')} unsettled, so the change may be "
                        "happening now. Open the change to settle it first."
                    )
                fm["status"] = ArtifactStatus.WITHDRAWN.value
                fm["withdrawn_by"] = {"user": user, "at": utcnow_iso()}
                if reason:
                    fm["withdrawn_by"]["reason"] = reason
                fm_yml = self.file_store.serialize_frontmatter(fm)
                self.file_store.write(id, fm_yml, body, "withdraw")
                return fm

        fm = await self.file_store.run_serialized(_locked)
        await self._audit("withdraw", id, user, {"reason": reason} if reason else None)
        await self._sync_to_db(id, fm)
        return fm
