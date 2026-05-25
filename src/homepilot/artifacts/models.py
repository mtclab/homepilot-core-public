from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, model_validator


class LifecycleError(Exception):
    pass


class ConflictError(LifecycleError):
    """Raised when a state transition is invalid (e.g., already in target state)."""
    pass


class ArtifactKind(StrEnum):
    ANSIBLE_PLAYBOOK = "ansible-playbook"
    PROXMOX_API_SEQUENCE = "proxmox-api-sequence"
    HTTP_SEQUENCE = "http-sequence"
    COMPOSITE = "composite"
    SHELL_SCRIPT = "shell-script"
    KB_NOTE = "kb-note"


class ArtifactStatus(StrEnum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"
    APPLIED = "applied"
    FAILED = "failed"
    SUPERSEDED = "superseded"
    REVOKED = "revoked"


class Idempotence(StrEnum):
    VIA_PRECHECK = "via-precheck"
    DECLARED_NATURAL = "declared-natural"
    REPLAY_ONLY = "replay-only"


class TargetKind(StrEnum):
    VM = "vm"
    LXC = "lxc"
    NODE = "node"
    CLUSTER = "cluster"
    SERVICE = "service"
    NETWORK = "network"
    GLOBAL = "global"


class Target(BaseModel):
    kind: TargetKind
    host: str | None = None
    vmid: int | None = None
    node: str | None = None
    service: str | None = None
    network: str | None = None

    @model_validator(mode="after")
    def validate_sub_fields(self) -> Target:
        k = self.kind
        if k in (TargetKind.VM, TargetKind.LXC):
            if self.vmid is None:
                raise ValueError(f"target.kind={k.value}: vmid is required")
            if self.node is None:
                raise ValueError(f"target.kind={k.value}: node is required")
        if k == TargetKind.NODE and self.node is None:
            raise ValueError("target.kind=node: node is required")
        if k == TargetKind.CLUSTER and self.node is not None:
            raise ValueError("target.kind=cluster: node MUST NOT be set")
        if k == TargetKind.SERVICE and self.service is None:
            raise ValueError("target.kind=service: service is required")
        if k == TargetKind.NETWORK and self.network is None:
            raise ValueError("target.kind=network: network is required")
        return self


class ProducedBy(BaseModel):
    session: str
    agent: str
    user: str
    at: str


class ApprovedBy(BaseModel):
    user: str
    at: str
    reason: str | None = None


class RejectedBy(BaseModel):
    user: str
    at: str
    reason: str | None = None


class RevokedBy(BaseModel):
    user: str
    at: str
    reason: str | None = None


class ArtifactFrontmatter(BaseModel):
    id: str
    kind: ArtifactKind
    intent: str = Field(max_length=200)
    status: ArtifactStatus
    mutating: bool
    produced_by: ProducedBy
    hash: str

    target: Target | None = None
    idempotence: Idempotence | None = None

    approved_by: ApprovedBy | None = None
    applied_at: str | None = None
    failed_at: str | None = None
    failure_reason: str | None = None
    supersedes: list[str] | None = None
    superseded_by: str | None = None
    rejected_by: RejectedBy | None = None
    revoked_by: RevokedBy | None = None

    tags: list[str] | None = None
    rollback: bool | None = None
    replay_safe: bool | None = None
    requires_snapshot: bool | None = None
    note_kind: str | None = None

    @model_validator(mode="after")
    def validate_mutating_fields(self) -> ArtifactFrontmatter:
        if self.mutating:
            if self.target is None:
                raise ValueError("mutating artifacts require a target")
            if self.idempotence is None:
                raise ValueError("mutating artifacts require idempotence")
        if not self.mutating and self.kind == ArtifactKind.KB_NOTE and self.idempotence is not None:
            raise ValueError("kb-note must not have idempotence")
        return self


_ID_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}-[a-z0-9-]{1,60}(-[a-f0-9]{6})?$")


def validate_artifact_id(id_str: str) -> bool:
    return bool(_ID_PATTERN.match(id_str))


def compute_body_hash(body: str) -> str:
    normalized_lines = [line.rstrip() for line in body.split("\n")]
    while normalized_lines and normalized_lines[-1] == "":
        normalized_lines.pop()
    normalized_lines.append("")
    normalized = "\n".join(normalized_lines)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def utcnow_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def extract_composite_steps(body: str) -> list[dict[str, Any]]:
    import yaml as _yaml

    pattern = re.compile(r"```yaml\s+composite-spec\s*\n(.*?)```", re.DOTALL)
    m = pattern.search(body)
    if not m:
        return []
    content = m.group(1).strip()
    parsed = _yaml.safe_load(content)
    if not isinstance(parsed, dict) or "steps" not in parsed:
        return []
    steps_raw: list[dict[str, Any]] = parsed["steps"]
    return steps_raw


VALID_TRANSITIONS: dict[ArtifactStatus, set[ArtifactStatus]] = {
    ArtifactStatus.PROPOSED: {
        ArtifactStatus.APPROVED,
        ArtifactStatus.REJECTED,
        ArtifactStatus.PROPOSED,
    },
    ArtifactStatus.APPROVED: {
        ArtifactStatus.APPLIED,
        ArtifactStatus.FAILED,
        ArtifactStatus.REVOKED,
    },
    ArtifactStatus.FAILED: {ArtifactStatus.APPROVED, ArtifactStatus.REVOKED},
    ArtifactStatus.APPLIED: {ArtifactStatus.SUPERSEDED, ArtifactStatus.REVOKED},
    ArtifactStatus.REJECTED: set(),
    ArtifactStatus.SUPERSEDED: set(),
    ArtifactStatus.REVOKED: set(),
}
