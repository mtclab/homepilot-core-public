from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from ..common import format_validation_error


class LifecycleError(Exception):
    pass


class ConflictError(LifecycleError):
    """Raised when a state transition is invalid (e.g., already in target state)."""

    pass


class ArtifactBusyError(ConflictError):
    """A run of this artifact is already in flight (#710).

    One artifact has at most ONE pending/running apply, replay or revoke - the
    database enforces it (a partial unique index on `tasks`), so it holds across
    processes and across every transport. Carries the run that holds the slot so
    every transport can NAME it: an operator told "busy" without a task id has
    nothing to wait on or cancel.
    """

    def __init__(self, artifact_id: str, task_id: str, action: str, status: str) -> None:
        self.artifact_id = artifact_id
        self.task_id = task_id
        self.action = action
        self.status = status
        super().__init__(
            f"{artifact_id} already has a {action} run in flight: task {task_id} is "
            f"{status}. Wait for it to finish, or cancel that task, before starting "
            "another run of this artifact."
        )

    def detail(self) -> dict[str, str]:
        """The structured 409 body: a human sentence plus the facts it names."""
        return {
            "message": str(self),
            "code": "artifact_busy",
            "artifact_id": self.artifact_id,
            "task_id": self.task_id,
            "action": self.action,
            "status": self.status,
        }


class ApprovalRefusedError(LifecycleError):
    """The actor has no authority to approve (#681): an assistant credential
    without a valid human-relayed approval code. Transports map it to 403 (REST)
    or a tool error (MCP); the message never contains the code."""


class ArtifactKind(StrEnum):
    ANSIBLE_PLAYBOOK = "ansible-playbook"
    PROXMOX_API_SEQUENCE = "proxmox-api-sequence"
    HTTP_SEQUENCE = "http-sequence"
    COMPOSITE = "composite"
    SHELL_SCRIPT = "shell-script"
    HOST_PROVISION = "host-provision"
    GUEST_NETWORK = "guest-network"
    GUEST_POWER = "guest-power"
    KB_NOTE = "kb-note"


class ArtifactStatus(StrEnum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"
    APPLIED = "applied"
    FAILED = "failed"
    SUPERSEDED = "superseded"
    REVOKED = "revoked"
    # Approved, never run, and then taken back by a human (#797). Not `revoked`:
    # revoke undoes an APPLIED change and may run its rollback, which for a
    # change that never ran would act on a host nobody touched. Not `rejected`:
    # the history should say it WAS approved and then withdrawn. Terminal.
    WITHDRAWN = "withdrawn"


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
    # A run's PVE task still running when the run stopped following it (#774):
    # node, upid, the step, the steps not run. Present means the outcome is
    # unknown; the next apply/replay settles it from PVE instead of re-running.
    unsettled_task: dict[str, Any] | None = None
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


class ServiceState(StrEnum):
    STARTED = "started"
    STOPPED = "stopped"
    RESTARTED = "restarted"
    ENABLED = "enabled"
    DISABLED = "disabled"


# Metachar-free identifier/path patterns. The agent re-validates on its side,
# but rejecting early gives a clear, host-round-trip-free error and keeps a
# metachar-y name (`nginx; rm -rf /`) from ever leaving the control plane.
_PACKAGE_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9+._-]*$")
_SERVICE_NAME_RE = re.compile(r"^[a-zA-Z0-9@][a-zA-Z0-9@._-]*$")
_CONFIG_PATH_RE = re.compile(r"^/[a-zA-Z0-9_./-]+$")
_FILE_MODE_RE = re.compile(r"^[0-7]{3,4}$")


class HostProvisionService(BaseModel):
    name: str
    state: ServiceState

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        if not _SERVICE_NAME_RE.match(v):
            raise ValueError(f"invalid service name: {v!r}")
        return v


class HostProvisionConfigFile(BaseModel):
    path: str
    content: str
    mode: str = "0644"

    @field_validator("path")
    @classmethod
    def _validate_path(cls, v: str) -> str:
        if ".." in v or not _CONFIG_PATH_RE.match(v):
            raise ValueError(f"invalid config_file path: {v!r}")
        return v

    @field_validator("mode")
    @classmethod
    def _validate_mode(cls, v: str) -> str:
        if not _FILE_MODE_RE.match(v):
            raise ValueError(f"invalid config_file mode: {v!r}")
        return v


class HostProvisionSpec(BaseModel):
    packages: list[str] = Field(default_factory=list)
    services: list[HostProvisionService] = Field(default_factory=list)
    config_files: list[HostProvisionConfigFile] = Field(default_factory=list)

    @field_validator("packages")
    @classmethod
    def _validate_packages(cls, v: list[str]) -> list[str]:
        for name in v:
            if not isinstance(name, str) or not _PACKAGE_NAME_RE.match(name):
                raise ValueError(f"invalid package name: {name!r}")
        return v

    @model_validator(mode="after")
    def _validate_nonempty(self) -> HostProvisionSpec:
        if not self.packages and not self.services and not self.config_files:
            raise ValueError(
                "host-provision spec must declare at least one of "
                "packages / services / config_files"
            )
        return self


_HOST_PROVISION_FENCE = "host-provision-spec"


def parse_host_provision_spec(body: str) -> HostProvisionSpec:
    """Parse + validate the ```yaml host-provision-spec``` block from an artifact
    body into a :class:`HostProvisionSpec`.

    Raises ``ValueError`` with a clear message when the fenced block is missing,
    is not a mapping, carries an unknown service state, is missing a required
    field, or contains a metachar-y package/service name or config path."""
    import yaml as _yaml
    from pydantic import ValidationError

    pattern = re.compile(rf"```yaml\s+{_HOST_PROVISION_FENCE}\s*\n(.*?)```", re.DOTALL)
    m = pattern.search(body)
    if not m:
        raise ValueError(f"missing ```yaml {_HOST_PROVISION_FENCE}``` block")
    try:
        parsed = _yaml.safe_load(m.group(1).strip())
    except _yaml.YAMLError as exc:
        raise ValueError(f"host-provision spec is not valid YAML: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("host-provision spec must be a YAML mapping")
    try:
        return HostProvisionSpec.model_validate(parsed)
    except ValidationError as exc:
        # A spec can carry config-file content; name the field, not the value.
        raise ValueError(f"invalid host-provision spec: {format_validation_error(exc)}") from None


_GUEST_NETWORK_FENCE = "guest-network-spec"


def parse_guest_network_spec(body: str, defaults: Any = None) -> Any:
    """Parse the ```yaml guest-network-spec``` block into a DesiredGuestNetwork.

    ``defaults`` is a ``DesiredGuestNetwork`` (this instance's settings) whose
    fields fill in whatever the body leaves out - so a propose can say "the
    guest network, as this instance describes it" without restating eight
    values, and a propose that states them wins over the settings. The RECORD of
    what was applied is the body either way, because the merged desired state is
    written into the execution log.

    Raises ``ValueError`` with a readable message when the block is missing, is
    not a mapping, carries an unknown key, or describes a network that cannot
    work (a gateway outside its subnet, a DHCP range that would hand out the
    router's own address).
    """
    import yaml as _yaml

    from homepilot.provision.guest_network import DesiredGuestNetwork, GuestNetworkError

    pattern = re.compile(rf"```yaml\s+{_GUEST_NETWORK_FENCE}\s*\n(.*?)```", re.DOTALL)
    m = pattern.search(body)
    if not m:
        raise ValueError(f"missing ```yaml {_GUEST_NETWORK_FENCE}``` block")
    try:
        parsed = _yaml.safe_load(m.group(1).strip())
    except _yaml.YAMLError as exc:
        raise ValueError(f"guest-network spec is not valid YAML: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("guest-network spec must be a YAML mapping")

    allowed = {
        "zone",
        "vnet",
        "subnet_cidr",
        "gateway",
        "snat",
        "dhcp",
        "dhcp_range",
        "dhcp_dns_server",
        "isolate_cidrs",
    }
    unknown = sorted(set(parsed) - allowed)
    if unknown:
        raise ValueError(
            f"unknown guest-network field(s): {', '.join(unknown)}. "
            f"Known fields: {', '.join(sorted(allowed))}"
        )

    merged: dict[str, Any] = {}
    if defaults is not None:
        merged.update(defaults.to_dict())
        merged["isolate_cidrs"] = tuple(merged.get("isolate_cidrs") or ())
    merged.update({k: v for k, v in parsed.items() if v is not None})
    if "isolate_cidrs" in merged and not isinstance(merged["isolate_cidrs"], list | tuple):
        from homepilot.provision.guest_network import split_cidrs

        merged["isolate_cidrs"] = tuple(split_cidrs(merged["isolate_cidrs"]))
    else:
        merged["isolate_cidrs"] = tuple(merged.get("isolate_cidrs") or ())

    missing = [f for f in ("zone", "vnet", "subnet_cidr", "gateway") if not merged.get(f)]
    if missing:
        raise ValueError(
            "guest-network spec is missing "
            + ", ".join(missing)
            + " and this instance has no setting to fill it in"
        )
    try:
        return DesiredGuestNetwork(**merged)
    except GuestNetworkError as exc:
        raise ValueError(f"invalid guest-network spec: {exc}") from exc


_GUEST_POWER_FENCE = "guest-power-spec"


class GuestPowerSpec(BaseModel):
    """One power action on the artifact's target guest (lane B3, #781)."""

    model_config = {"extra": "forbid"}

    action: str
    # Only for shutdown/reboot: how long the guest gets to shut down before the
    # result says "timed out". Never forces anything.
    timeout_s: int | None = None

    @field_validator("action")
    @classmethod
    def _validate_action(cls, v: str) -> str:
        from homepilot.inventory.power import POWER_ACTIONS

        if v not in POWER_ACTIONS:
            raise ValueError(f"action must be one of: {', '.join(POWER_ACTIONS)}")
        return v

    @field_validator("timeout_s")
    @classmethod
    def _validate_timeout(cls, v: int | None) -> int | None:
        from homepilot.inventory.power import MAX_SHUTDOWN_TIMEOUT_S, MIN_SHUTDOWN_TIMEOUT_S

        if v is not None and not MIN_SHUTDOWN_TIMEOUT_S <= v <= MAX_SHUTDOWN_TIMEOUT_S:
            raise ValueError(
                f"timeout_s must be from {MIN_SHUTDOWN_TIMEOUT_S} to {MAX_SHUTDOWN_TIMEOUT_S}"
            )
        return v


def parse_guest_power_spec(body: str) -> GuestPowerSpec:
    """Parse the ```yaml guest-power-spec``` block; ValueError names what is wrong."""
    import yaml as _yaml
    from pydantic import ValidationError

    pattern = re.compile(rf"```yaml\s+{_GUEST_POWER_FENCE}\s*\n(.*?)```", re.DOTALL)
    m = pattern.search(body)
    if not m:
        raise ValueError(f"missing ```yaml {_GUEST_POWER_FENCE}``` block")
    try:
        parsed = _yaml.safe_load(m.group(1).strip())
    except _yaml.YAMLError as exc:
        raise ValueError(f"guest-power spec is not valid YAML: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("guest-power spec must be a YAML mapping")
    try:
        return GuestPowerSpec.model_validate(parsed)
    except ValidationError as exc:
        raise ValueError(f"invalid guest-power spec: {format_validation_error(exc)}") from None


def render_guest_power_body(action: str, hostname: str, timeout_s: int | None = None) -> str:
    """The body of a guest-power artifact: what a reviewer reads, and the spec."""
    from homepilot.inventory.power import DEFAULT_SHUTDOWN_TIMEOUT_S

    words = {
        "start": "Start the guest. Refused at apply if a running guest holds one of its "
        "addresses or has one of its passthrough disks attached.",
        "shutdown": "Shut the guest down gracefully (ACPI / guest agent). If it has not "
        "stopped when the timeout runs out it is left running and the apply fails as "
        "timed out - nothing is forced.",
        "reboot": "Proxmox reboot (shutdown, then start): pending config such as a "
        "cloud-init ipconfig is applied. Refused at apply if a pending address change "
        "would take a running guest's address.",
        "stop": "Stop the guest FORCED, like pulling the power: unsaved state in the "
        "guest is lost.",
    }[action]
    lines = [f"## Power action: {action} {hostname}", "", words, ""]
    if action in ("shutdown", "reboot"):
        lines += [
            f"Shutdown timeout: {timeout_s or DEFAULT_SHUTDOWN_TIMEOUT_S}s.",
            "",
        ]
    spec = f"action: {action}\n" + (f"timeout_s: {timeout_s}\n" if timeout_s else "")
    lines += ["## Spec", "", f"```yaml {_GUEST_POWER_FENCE}", spec + "```", ""]
    return "\n".join(lines)


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
        ArtifactStatus.WITHDRAWN,
    },
    ArtifactStatus.FAILED: {ArtifactStatus.APPROVED, ArtifactStatus.REVOKED},
    ArtifactStatus.APPLIED: {ArtifactStatus.SUPERSEDED, ArtifactStatus.REVOKED},
    ArtifactStatus.REJECTED: set(),
    ArtifactStatus.SUPERSEDED: set(),
    ArtifactStatus.REVOKED: set(),
    ArtifactStatus.WITHDRAWN: set(),
}
