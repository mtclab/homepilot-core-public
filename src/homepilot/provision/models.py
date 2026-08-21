from __future__ import annotations

import base64
import binascii
import re

from pydantic import BaseModel, Field, field_validator

# A guest name that is simultaneously a valid PVE VM name and a valid DNS label:
# lowercase alnum, inner hyphens allowed, 3-63 chars, never starting or ending
# with a hyphen.
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")

# Disk PVE will accept for a resize on a qemu guest.
DISK_RE = re.compile(r"^(scsi|virtio|sata|ide)\d{1,2}$")

# Key types an authorized_keys line may open with. Deliberately narrow: no
# ssh-dss (disabled by default in modern OpenSSH), no bare 'ssh-' wildcard.
_KEY_TYPE_RE = re.compile(r"^(ssh-ed25519|ssh-rsa|ecdsa-sha2-[A-Za-z0-9.@-]+|sk-[A-Za-z0-9.@-]+)$")

# Tailscale auth keys are 'tskey-' + a kind + opaque base62 ('tskey-auth-...',
# 'tskey-client-...', and the older bare 'tskey-<blob>'). The shape check is a
# guard against a value that is not a key at all (a password, a shell fragment)
# reaching a command line, NOT an authenticity check — only Tailscale can say
# whether a key is real.
_TAILSCALE_KEY_RE = re.compile(r"^tskey-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*$")


def _validate_name(value: str, field: str) -> str:
    if not NAME_RE.match(value):
        msg = (
            f"{field} must be 3-63 lowercase alphanumeric characters or hyphens, "
            "starting and ending with an alphanumeric"
        )
        raise ValueError(msg)
    return value


def validate_guest_name(value: str, field: str = "name") -> str:
    """Public entry point for the guest-name/ciuser rule, for callers outside
    this module (the invite portal validates the redeemer's fields with it)."""
    return _validate_name(value, field)


def validate_ssh_authorized_key(value: str) -> str:
    """Accept exactly one authorized_keys line: '<type> <base64 blob> [comment]'.

    Rejecting embedded newlines is the point: a multi-line value smuggled into
    the cloud-init sshkeys field would inject arbitrary extra keys into the
    guest's authorized_keys.
    """
    if "\n" in value or "\r" in value:
        raise ValueError("ssh_authorized_key must be a single line (no newlines)")
    parts = value.strip().split()
    if len(parts) < 2:
        raise ValueError("ssh_authorized_key must look like '<type> <base64 key> [comment]'")
    key_type, body = parts[0], parts[1]
    if not _KEY_TYPE_RE.match(key_type):
        raise ValueError(f"Unsupported SSH key type: {key_type!r}")
    try:
        decoded = base64.b64decode(body, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("ssh_authorized_key body is not valid base64") from exc
    if not decoded:
        raise ValueError("ssh_authorized_key body is empty")
    return value.strip()


def validate_tailscale_auth_key(value: str) -> str:
    """Accept one 'tskey-...' auth key and nothing else.

    The key is passed to a command run inside the guest, so anything that is not
    a plain key token — whitespace, quotes, shell metacharacters, newlines — must
    be refused here rather than escaped later.
    """
    stripped = value.strip()
    if not (16 <= len(stripped) <= 200):
        raise ValueError("tailscale_auth_key must be 16-200 characters")
    if not _TAILSCALE_KEY_RE.match(stripped):
        raise ValueError("tailscale_auth_key must look like 'tskey-...' (letters, digits, hyphens)")
    return stripped


class ProvisionRequest(BaseModel):
    """A request to clone a Proxmox template into a running, reachable guest."""

    name: str
    node: str = Field(min_length=1)
    template_vmid: int = Field(gt=0)
    cores: int | None = Field(default=None, ge=1, le=32)
    memory_mb: int | None = Field(default=None, ge=256, le=65536)
    disk_gb: int | None = Field(default=None, ge=1, le=2000)
    disk: str = "scsi0"
    ciuser: str = "friend"
    ssh_authorized_key: str | None = None
    # The requester's OWN tailnet key. Never persisted anywhere (not in the
    # invites table, not in the task result, not in the audit row) and never
    # logged: it is carried in memory for the length of one provision.
    tailscale_auth_key: str | None = None
    ipconfig0: str = "ip=dhcp"
    owner: str | None = Field(default=None, max_length=64)
    pool: str | None = None
    full: bool = True

    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str) -> str:
        return _validate_name(v, "name")

    @field_validator("ciuser")
    @classmethod
    def _check_ciuser(cls, v: str) -> str:
        return _validate_name(v, "ciuser")

    @field_validator("disk")
    @classmethod
    def _check_disk(cls, v: str) -> str:
        if not DISK_RE.match(v):
            raise ValueError("disk must be a PVE disk name such as 'scsi0' or 'virtio0'")
        return v

    @field_validator("ssh_authorized_key")
    @classmethod
    def _check_key(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return validate_ssh_authorized_key(v)

    @field_validator("tailscale_auth_key")
    @classmethod
    def _check_tailscale_key(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return validate_tailscale_auth_key(v)
