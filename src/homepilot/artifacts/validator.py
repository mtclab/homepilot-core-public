from __future__ import annotations

from collections.abc import Mapping
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
    parse_guest_network_spec,
    parse_host_provision_spec,
    utcnow_iso,
    validate_artifact_id,
)
from .store import ArtifactStore
from .validators import validate_artifact_expressions


def validate_composite_spec(body: str) -> None:
    import yaml

    try:
        steps = extract_composite_steps(body)
    except yaml.YAMLError as exc:
        # A spec block that does not parse is a refusal the author can act on,
        # not a 500 (#652: an edit could break it after propose had checked).
        mark = getattr(exc, "problem_mark", None)
        where = f" (line {mark.line + 1} of the block)" if mark is not None else ""
        raise LifecycleError(f"composite-spec is not valid YAML{where}") from None
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


def validate_guest_network_spec(body: str) -> None:
    """Refuse an unbuildable guest network at PROPOSE.

    The propose is the last moment before the description is committed to the
    artifact store and put in front of a human for approval, so a body that
    could never converge - a gateway outside its subnet, a vnet name PVE cannot
    store - is refused here rather than approved and then failed at apply.

    Fields the body leaves out are NOT filled in from the settings at this
    point: propose validation runs in processes that may have no database (the
    CLI), and a body that validates differently depending on where it was
    proposed is worse than one that must state itself. What is checked is that
    the spec is present, well-formed, and internally consistent as far as it
    goes.
    """
    try:
        parse_guest_network_spec(body)
    except ValueError as exc:
        message = str(exc)
        if "is missing" in message:
            # A body that names only some fields is legal: the executor fills
            # the rest from this instance's settings. Everything else - a
            # malformed value, an unknown key, a missing block - is refused.
            return
        raise LifecycleError(message) from exc


def validate_transition(current: ArtifactStatus, target: ArtifactStatus) -> None:
    allowed = VALID_TRANSITIONS.get(current, set())
    if target not in allowed:
        raise ConflictError(f"Invalid transition: {current.value} → {target.value}")


def _check_pve_firewall_writes(kind: ArtifactKind | str, body: str) -> None:
    """Refuse, at propose, an http-sequence step that writes PVE firewall options (#707).

    The http-sequence executor refuses the same step at apply (it cannot run the
    management-lockout guard ProxmoxClient runs on every PVE write); saying so
    here is the earlier, cheaper answer. A proxmox-api-sequence step is NOT
    refused: whether its write is safe depends on the cluster's rules at apply
    time, and the guard on the wire answers exactly that question then.
    """
    kind_value = kind.value if isinstance(kind, ArtifactKind) else str(kind)
    if kind_value != ArtifactKind.HTTP_SEQUENCE.value:
        return
    from homepilot.adapters.pve_firewall_guard import (
        PveFirewallLockoutError,
        addresses_firewall_options,
    )
    from homepilot.executor.http_sequence import _extract_steps

    for fence in ("http-spec", "http-rollback"):
        for step in _extract_steps(body, fence) or []:
            if not isinstance(step, dict):
                continue
            if str(step.get("method", "GET")).upper() not in ("POST", "PUT", "PATCH", "DELETE"):
                continue
            path = str(step.get("path", "") or "")
            try:
                hit = addresses_firewall_options(path)
            except PveFirewallLockoutError as exc:
                raise LifecycleError(f"step '{step.get('id', '?')}': {exc}") from None
            if hit:
                raise LifecycleError(
                    f"step '{step.get('id', '?')}' writes PVE firewall options ({path}) through "
                    "http-sequence, which cannot run the management-lockout guard. Use a "
                    "proxmox-api-sequence step: its writes pass the guard (#600, #707)."
                )


def _check_vault_references(kind: ArtifactKind | str, body: str) -> None:
    """Refuse a body that names a vault entry artifacts may not reference."""
    import yaml

    from homepilot.executor.secrets import (
        SecretResolutionError,
        check_referenceable,
        references,
    )

    names = [name for name, _field in references(body)]
    if kind == ArtifactKind.HTTP_SEQUENCE:
        # A step's (and its precheck's) `name` is the vault credential it calls
        # with - the same kind of reference, spelled differently.
        from homepilot.executor.http_sequence import (
            _ROLLBACK_FENCE,
            _SPEC_FENCE,
            _extract_steps,
        )

        for fence in (_SPEC_FENCE, _ROLLBACK_FENCE):
            try:
                steps = _extract_steps(body, fence) or []
            except yaml.YAMLError:
                # A malformed spec is refused by its own check; nothing to name here.
                steps = []
            for step in steps:
                if not isinstance(step, dict):
                    continue
                cred = step.get("name")
                if isinstance(cred, str) and cred:
                    names.append(cred)
                precheck = step.get("precheck")
                if isinstance(precheck, dict) and isinstance(precheck.get("name"), str):
                    names.append(precheck["name"])
    try:
        check_referenceable(names)
    except SecretResolutionError as exc:
        raise LifecycleError(str(exc)) from None


def validate_artifact_content(spec: Mapping[str, Any], body: str) -> dict[str, Any]:
    """Every check on WHAT an artifact says - the one pipeline propose and edit share.

    ``spec`` is the artifact's declared fields (a propose spec, or the
    frontmatter an edit submits) and ``body`` its body. Raises
    ``LifecycleError`` naming the first thing that is wrong. Returns the fields
    the platform DERIVES rather than accepts: ``kind`` (an ``ArtifactKind``),
    ``mutating`` and, for a non-note, ``rollback``.

    It exists as one function because an edit used to run none of it (#652): a
    composite could lose its `artifact:` reference, a body could gain a literal
    credential or a `{{ name }}` nothing defines, a `skip_if` could become one
    the evaluator refuses - all after propose had checked, and all approvable.
    What propose refuses, an edit now refuses, by construction.

    Identity (id format, uniqueness) is NOT here: an edit cannot change the id.
    """
    kind_str = spec.get("kind", "")
    try:
        kind = ArtifactKind(kind_str)
    except ValueError as e:
        raise LifecycleError(f"Invalid kind: {kind_str}") from e

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

    produced_by = spec.get("produced_by") or {}
    has_required = isinstance(produced_by, dict) and (
        produced_by.get("session") and produced_by.get("agent") and produced_by.get("user")
    )
    if not has_required:
        raise LifecycleError("produced_by requires session, agent, user")

    if kind == ArtifactKind.COMPOSITE:
        validate_composite_spec(body)

    if kind == ArtifactKind.HOST_PROVISION:
        validate_host_provision_spec(body)

    if kind == ArtifactKind.GUEST_NETWORK:
        validate_guest_network_spec(body)

    derived: dict[str, Any] = {"kind": kind, "mutating": kind != ArtifactKind.KB_NOTE}

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
        derived_rollback = derive_rollback(kind, body)
        if claimed and not derived_rollback:
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
        derived["rollback"] = derived_rollback

    # `requires_snapshot` is a REQUEST the executor can only honour against a VM
    # or an LXC - there is no such thing as a snapshot of a `cluster` or a
    # `service`. Setting it true anywhere else used to be dropped in silence, so
    # the review screen said `requires_snapshot: true` over an apply that had no
    # rollback point at all (#642 A7). Refuse the claim at propose, the same way
    # an unhonourable `rollback: true` is refused above.
    if kind != ArtifactKind.KB_NOTE and spec.get("requires_snapshot") is True:
        from homepilot.executor.orchestrator import SNAPSHOTTABLE_TARGET_KINDS

        t_kind = (target_data or {}).get("kind", "")
        if t_kind not in SNAPSHOTTABLE_TARGET_KINDS:
            raise LifecycleError(
                f"requires_snapshot: true cannot be honoured for target.kind '{t_kind}' - "
                "only vm and lxc targets can be snapshotted, so this artifact would apply "
                "with no rollback point. Drop the claim, or target the guest."
            )

    # Vault references the platform forbids (#684): its own credentials always,
    # and anything outside HP_ARTIFACT_VAULT_ALLOWLIST when that is set. Checked
    # on the RAW body with the executor's own regex - before the Jinja pass,
    # which a `{% raw %}` block walks straight past while the executor still
    # resolves what is inside it. Enforced again at execute time.
    _check_vault_references(kind, body)
    # An http-sequence step that writes PVE firewall options would go round the
    # lockout guard ProxmoxClient runs on the wire (#707).
    _check_pve_firewall_writes(kind, body)

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

    # Rendered against the artifact's own declared fields (target, id, intent,
    # kind, skip_if) - the names the executor's context is built from.
    expr_errors = validate_artifact_expressions(
        {k: v for k, v in spec.items() if k != "body"}, body
    )
    if expr_errors:
        raise LifecycleError("; ".join(expr_errors))

    return derived


def validate_propose_spec(spec: dict[str, Any], store: ArtifactStore) -> tuple[dict[str, Any], str]:
    id_str = str(spec.get("id", ""))
    if not validate_artifact_id(id_str):
        raise LifecycleError(f"Invalid artifact ID format: {id_str}")
    if store.exists(id_str):
        raise LifecycleError(f"Artifact ID already exists: {id_str}")

    body = spec.get("body", "")
    derived = validate_artifact_content(spec, body)
    kind = derived["kind"]
    kind_str = spec.get("kind", "")
    target_data = spec.get("target")
    idempotence_str = spec.get("idempotence")
    intent = spec.get("intent", "")
    mutating = derived["mutating"]
    produced_by = spec.get("produced_by", {})
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

    if "rollback" in derived:
        fm["rollback"] = derived["rollback"]

    return fm, event
