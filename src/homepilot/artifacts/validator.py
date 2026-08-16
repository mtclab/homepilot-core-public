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

    expr_errors = validate_artifact_expressions(fm, body)
    if expr_errors:
        raise LifecycleError("; ".join(expr_errors))

    return fm, event
