from __future__ import annotations

import base64
import binascii
import re
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field, field_validator, model_validator

if TYPE_CHECKING:  # pragma: no cover - import cycle: defaults reads the registry
    from .defaults import ProvisioningDefaults

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


class ProvisionRequestIn(ProvisionRequest):
    """The same request, with the two infra fields allowed to be OMITTED (#553 C3).

    A SUBCLASS rather than a parallel model on purpose: every other field, every
    validator and every future addition come from ProvisionRequest itself, so
    the API surface cannot drift from what provisioning actually accepts. Only
    ``node`` and ``template_vmid`` are loosened, and only because this instance
    may already know them - ``resolve`` turns this back into a strict
    ProvisionRequest or refuses, naming the setting that would have filled the
    gap.
    """

    node: str = ""
    template_vmid: int = 0

    def resolve(self, defaults: ProvisioningDefaults) -> ProvisionRequest:
        from .defaults import (
            resolve_ipconfig,
            resolve_node,
            resolve_pool,
            resolve_template_vmid,
        )

        payload = self.model_dump()
        payload["node"] = resolve_node(self.node, defaults)
        payload["template_vmid"] = resolve_template_vmid(self.template_vmid, defaults)
        payload["pool"] = resolve_pool(self.pool, defaults)
        payload["ipconfig0"] = resolve_ipconfig(self.ipconfig0, defaults)
        return ProvisionRequest(**payload)


# ── Guest-template creation (#594) ───────────────────────────────────────────

# A PVE storage id, as PVE itself accepts it. Validated here because it is
# interpolated into an API PATH (/storage/{id}, /nodes/{n}/storage/{id}/...),
# where a slash or a '..' would address something else entirely.
STORAGE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{0,62}$")

# A volume id as PVE prints it: '<storage>:<content>/<filename>'. The staged
# cloud image is named this way whether it sits under `import` or `iso` content.
VOLID_RE = re.compile(
    r"^[A-Za-z][A-Za-z0-9._-]{0,62}:[A-Za-z0-9]+/[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
)

# The file name a download is stored under. It becomes the tail of a volid and a
# file on the node, so no path separators and no leading dot.
IMAGE_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _image_filename_from_url(url: str) -> str:
    """The file name a cloud-image URL will be stored under on the node.

    Derived rather than asked for: an operator pasting a download URL should not
    also have to name the file, and a name derived from the URL is the one that
    matches what they see on the storage afterwards. Anything that does not come
    out as a plain file name is refused rather than sanitised — a silently
    rewritten name would put the image somewhere the caller did not expect.
    """
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("download_url must be an http(s) URL")
    name = parsed.path.rsplit("/", 1)[-1]
    if not IMAGE_FILENAME_RE.match(name):
        raise ValueError(
            "download_url must end in a plain image file name "
            "(letters, digits, dot, dash, underscore), e.g. .../ubuntu-24.04.qcow2"
        )
    return name


class GuestTemplateRequest(BaseModel):
    """A request to BUILD the cloud-init template that provisioning clones (#594).

    Provisioning has always needed a template to exist and had no way to make
    one: building it by hand needs root on the node, which HomePilot's scoped
    PVE token deliberately does not have. This request describes the API-only
    path instead — stage (or download) a cloud image as `import` content, create
    a shell, import the disk into it, add the cloud-init drive, convert.
    """

    name: str = "ubuntu-2404-cloudinit"
    node: str = Field(min_length=1)
    template_vmid: int = Field(gt=0)
    storage: str = "local"
    # Exactly one of these two. A volid points at an image already staged on the
    # storage; a URL has the NODE fetch one. Both is ambiguous (which wins?) and
    # neither leaves nothing to import, so the validator refuses either.
    source_volid: str | None = None
    download_url: str | None = None
    memory_mb: int = Field(default=2048, ge=256, le=65536)
    cores: int = Field(default=2, ge=1, le=32)

    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str) -> str:
        return _validate_name(v, "name")

    @field_validator("storage")
    @classmethod
    def _check_storage(cls, v: str) -> str:
        if not STORAGE_RE.match(v):
            raise ValueError("storage must be a PVE storage id (letters, digits, dot, dash, _)")
        return v

    @field_validator("source_volid")
    @classmethod
    def _check_volid(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if not VOLID_RE.match(v):
            raise ValueError(
                "source_volid must look like '<storage>:<content>/<file>', "
                "e.g. local:import/ubuntu-24.04.qcow2"
            )
        return v

    @field_validator("download_url")
    @classmethod
    def _check_url(cls, v: str | None) -> str | None:
        if v is None:
            return None
        _image_filename_from_url(v)
        return v

    @model_validator(mode="after")
    def _exactly_one_source(self) -> GuestTemplateRequest:
        if bool(self.source_volid) == bool(self.download_url):
            raise ValueError(
                "give exactly one of source_volid (an image already on the storage) "
                "or download_url (an image for the node to fetch)"
            )
        return self

    @property
    def image_filename(self) -> str | None:
        """The file the download will land as, or None when a volid was given."""
        return None if self.download_url is None else _image_filename_from_url(self.download_url)


class GuestTemplateRequestIn(GuestTemplateRequest):
    """The same request with node/template_vmid allowed to be OMITTED.

    A subclass for the same reason ProvisionRequestIn is one: every field and
    validator comes from the strict model, so the callable surface cannot drift
    from what template creation actually accepts. ``resolve`` turns it back into
    a strict request or refuses, naming the setting that would have filled the
    gap — and it is the SAME pair of settings provisioning resolves, so the
    template an instance builds is by construction the template it clones.
    """

    node: str = ""
    template_vmid: int = 0

    def resolve(self, defaults: ProvisioningDefaults) -> GuestTemplateRequest:
        from .defaults import resolve_node, resolve_template_vmid

        payload = self.model_dump()
        payload["node"] = resolve_node(self.node, defaults)
        payload["template_vmid"] = resolve_template_vmid(self.template_vmid, defaults)
        return GuestTemplateRequest(**payload)
