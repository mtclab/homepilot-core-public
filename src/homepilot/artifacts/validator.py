from __future__ import annotations

from typing import Any

from .models import (
    VALID_TRANSITIONS,
    ArtifactKind,
    ArtifactStatus,
    ConflictError,
    Idempotence,
    LifecycleError,
    Target,
    compute_body_hash,
    extract_composite_steps,
    parse_host_provision_spec,
    utcnow_iso,
    validate_artifact_id,
)
from .store import ArtifactStore
from .validators import validate_artifact_expressions


def validate_composite_spec(body: str) -> None:
    try:
        steps = extract_composite_steps(body)
    except ValueError as exc:
        raise LifecycleError(str(exc)) from exc
    if not steps:
        raise LifecycleError("composite spec must have at least one step")
    seen_ids: set[str] = set()
    for step in steps:
        step_id = step.get("id")
        if not step_id:
            raise LifecycleError("each composite step must have an 'id'")
        if step_id in seen_ids:
            raise LifecycleError(f"duplicate step id: {step_id}")
        seen_ids.add(step_id)
        if not step.get("artifact"):
            raise LifecycleError(f"step '{step_id}' must have an 'artifact' reference")


def validate_host_provision_spec(body: str) -> None:
    try:
        parse_host_provision_spec(body)
    except ValueError as exc:
        raise LifecycleError(str(exc)) from exc


def validate_transition(current: ArtifactStatus, target: ArtifactStatus) -> None:
    allowed = VALID_TRANSITIONS.get(current, set())
    if target not in allowed:
        raise ConflictError(f"Invalid transition: {current.value} → {target.value}")


def validate_propose_spec(spec: dict[str, Any], store: ArtifactStore) -> tuple[dict[str, Any], str]:
    id_str = str(spec.get("id", ""))
    if not validate_artifact_id(id_str):
        raise LifecycleError(f"Invalid artifact ID format: {id_str}")
    if store.exists(id_str):
        raise LifecycleError(f"Artifact ID already exists: {id_str}")

    kind_str = spec.get("kind", "")
    try:
        kind = ArtifactKind(kind_str)
    except ValueError as e:
        raise LifecycleError(f"Invalid kind: {kind_str}") from e

    body = spec.get("body", "")
    if not body:
        raise LifecycleError("Body must not be empty")

    target_data = spec.get("target")
    if target_data and kind != ArtifactKind.KB_NOTE:
        try:
            Target(**target_data)
        except Exception as e:
            raise LifecycleError(f"Invalid target: {e}") from e

    if kind != ArtifactKind.KB_NOTE and target_data is None:
        raise LifecycleError(f"Mutating kind {kind.value} requires a target")

    idempotence_str = spec.get("idempotence")
    if kind != ArtifactKind.KB_NOTE:
        if not idempotence_str:
            raise LifecycleError(f"Mutating kind {kind.value} requires idempotence")
        try:
            Idempotence(idempotence_str)
        except ValueError as e:
            raise LifecycleError(f"Invalid idempotence: {idempotence_str}") from e

    intent = spec.get("intent", "")
    if not intent or len(intent) > 200:
        raise LifecycleError("intent must be 1-200 chars")

    mutating = kind != ArtifactKind.KB_NOTE
    produced_by = spec.get("produced_by", {})
    has_required = (
        produced_by.get("session") and produced_by.get("agent") and produced_by.get("user")
    )
    if not has_required:
        raise LifecycleError("produced_by requires session, agent, user")

    if kind == ArtifactKind.COMPOSITE:
        validate_composite_spec(body)

    if kind == ArtifactKind.HOST_PROVISION:
        validate_host_provision_spec(body)

    body_hash = compute_body_hash(body)

    fm: dict[str, Any] = {
        "id": id_str,
        "kind": kind_str,
        "intent": intent,
        "mutating": mutating,
        "produced_by": produced_by,
        "hash": body_hash,
    }

    if target_data:
        fm["target"] = target_data
        if kind != ArtifactKind.KB_NOTE:
            fm["idempotence"] = idempotence_str

    if kind == ArtifactKind.KB_NOTE:
        fm["status"] = ArtifactStatus.APPLIED.value
        fm["applied_at"] = utcnow_iso()
        note_kind = spec.get("note_kind")
        if note_kind:
            fm["note_kind"] = note_kind
        event = "apply"
    else:
        fm["status"] = ArtifactStatus.PROPOSED.value
        event = "propose"

    supersedes = spec.get("supersedes")
    if supersedes:
        fm["supersedes"] = supersedes

    for opt_key in ("tags", "rollback", "replay_safe", "requires_snapshot", "skip_if"):
        if opt_key in spec:
            fm[opt_key] = spec[opt_key]

    # `rollback` is DERIVED, per ARTIFACT_SPEC: "true if a rollback section exists
    # in the body". Nothing derived it, and the executor gates rollback on this
    # field - so a body carrying a perfectly good rollback section and no
    # `rollback: true` line silently never rolled back, and revoke relabelled the
    # artifact while the host kept the change (#426).
    #
    # A claim that cannot be honoured is refused HERE rather than discovered on
    # revoke, which is the worst possible moment: the operator has already decided
    # to undo something.
    if kind != ArtifactKind.KB_NOTE:
        # Imported here, not at module scope: `executor` imports the artifact
        # models, so a top-level import would close a package cycle.
        from homepilot.executor.rollback import derive_rollback, kind_can_roll_back

        claimed = spec.get("rollback")
        derived = derive_rollback(kind, body)
        if claimed and not derived:
            if not kind_can_roll_back(kind):
                raise LifecycleError(
                    f"rollback: true is not possible for kind '{kind.value}' - it has no "
                    "way to reverse itself, so revoking would relabel the artifact and "
                    "leave the host changed"
                )
            raise LifecycleError(
                f"rollback: true but the body carries no rollback section for kind "
                f"'{kind.value}' - add one, or drop the claim"
            )
        fm["rollback"] = derived

    # A credential written out in full, in a body about to be committed to a git
    # repository designed to be pushed (#505). Refused at propose because that is
    # the last moment before it is in history, and history is a one-way door.
    if kind in (ArtifactKind.HOST_PROVISION, ArtifactKind.SHELL_SCRIPT):
        from homepilot.executor.secrets import literal_secrets

        leaked = literal_secrets(body)
        if leaked:
            raise LifecycleError(
                "this body appears to contain a literal credential ("
                + ", ".join(leaked)
                + "). Store it with `hp vault set` and reference it as "
                "{{ vault.<name>.<field> }} - the artifact store is a git "
                "repository, so a committed secret cannot be taken back."
            )

    expr_errors = validate_artifact_expressions(fm, body)
    if expr_errors:
        raise LifecycleError("; ".join(expr_errors))

    return fm, event
