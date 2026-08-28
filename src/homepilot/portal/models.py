from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from ..provision.models import (
    ProvisionRequest,
    validate_guest_name,
    validate_ssh_authorized_key,
    validate_tailscale_auth_key,
)

# Stand-in identity used only to prove a set of caps is provisionable at mint
# time. The real name/ciuser arrive from the redeemer and are validated then.
_PROBE_NAME = "invite-probe"


class InviteCaps(BaseModel):
    """The machine an invite is good for. Chosen by the OPERATOR at mint time.

    Every field here is validated by constructing a real ProvisionRequest, so an
    invite can never be minted with caps that provisioning would later reject —
    the friend must not discover an operator typo halfway through a redemption.
    """

    template_vmid: int = Field(gt=0)
    node: str = Field(min_length=1)
    pool: str | None = None
    # Frozen at mint like node and template_vmid (#618): the storage an invite
    # promises is part of the machine it is good for, so a default changed
    # after the invite left the operator's hands must not re-target it. None
    # means the clone inherits the template's storage.
    storage: str | None = None
    cores: int | None = None
    memory_mb: int | None = None
    disk_gb: int | None = None
    disk: str = "scsi0"
    ipconfig0: str = "ip=dhcp"

    @model_validator(mode="after")
    def _provisionable(self) -> InviteCaps:
        ProvisionRequest(
            name=_PROBE_NAME,
            node=self.node,
            template_vmid=self.template_vmid,
            cores=self.cores,
            memory_mb=self.memory_mb,
            disk_gb=self.disk_gb,
            disk=self.disk,
            ipconfig0=self.ipconfig0,
            pool=self.pool,
            storage=self.storage,
        )
        return self


class RedemptionIdentity(BaseModel):
    """The ONLY things a redeemer gets to choose.

    Caps are deliberately absent: they come from the invite row, so a hostile
    post carrying cores/template/node fields changes nothing.
    """

    ciuser: str = "friend"
    ssh_authorized_key: str
    hostname: str | None = None
    tailscale_auth_key: str | None = None

    @field_validator("ciuser")
    @classmethod
    def _check_ciuser(cls, v: str) -> str:
        return validate_guest_name(v.strip(), "username")

    @field_validator("hostname")
    @classmethod
    def _check_hostname(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        return validate_guest_name(v.strip(), "hostname")

    @field_validator("ssh_authorized_key")
    @classmethod
    def _check_key(cls, v: str) -> str:
        return validate_ssh_authorized_key(v)

    @field_validator("tailscale_auth_key")
    @classmethod
    def _check_tailscale(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        return validate_tailscale_auth_key(v)


def _optional_column(row: Any, column: str) -> Any:
    """A column that may not be there yet, as None rather than an exception.

    Rows come back as sqlite3.Row, where `in` tests the VALUES, not the column
    names - so the membership test has to go through `keys()` explicitly (and
    through a list, because `x in row.keys()` is the very idiom the linter
    rewrites into the wrong thing for a Row).
    """
    return row[column] if column in list(row.keys()) else None


def caps_from_row(row: dict[str, Any]) -> InviteCaps:
    return InviteCaps(
        template_vmid=int(row["template_vmid"]),
        node=str(row["node"]),
        pool=row["pool"],
        # Absent on rows minted before #618 added the column, and on any row
        # written by a build that predates it - `.get`-shaped access rather
        # than row["storage"] so an old invite still redeems, inheriting the
        # template's storage exactly as it did when it was minted.
        storage=_optional_column(row, "storage"),
        cores=row["cores"],
        memory_mb=row["memory_mb"],
        disk_gb=row["disk_gb"],
        disk=str(row["disk"]),
        ipconfig0=str(row["ipconfig0"]),
    )


def build_provision_request(
    caps: InviteCaps,
    identity: RedemptionIdentity,
    name: str,
    owner: str,
) -> ProvisionRequest:
    """Caps from the invite, identity from the redeemer, and nothing else.

    Written as explicit keywords rather than a dict merge so no future field can
    reach ProvisionRequest from the redeemer's side without a code change.
    """
    return ProvisionRequest(
        name=name,
        node=caps.node,
        template_vmid=caps.template_vmid,
        cores=caps.cores,
        memory_mb=caps.memory_mb,
        disk_gb=caps.disk_gb,
        disk=caps.disk,
        ipconfig0=caps.ipconfig0,
        pool=caps.pool,
        storage=caps.storage,
        ciuser=identity.ciuser,
        ssh_authorized_key=identity.ssh_authorized_key,
        tailscale_auth_key=identity.tailscale_auth_key,
        owner=owner,
    )


__all__ = [
    "InviteCaps",
    "RedemptionIdentity",
    "build_provision_request",
    "caps_from_row",
]
