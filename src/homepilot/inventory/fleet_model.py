"""What Proxmox already knows about a guest, as the fleet model reads it (lane B1).

Review 02 steps 1-2. The sync used to read only each node's guest listing (name,
VMID, status) and the config's description. Everything else the operator had
already told Proxmox - that a guest is a template, which pool it is in, its
tags, how big it was made - was invisible, so templates sat in the adoption
queue as "offline" guests and "which VMs are dev?" had no answer anywhere.

Pure functions only: the sync gathers the rows, these decide what they mean.

Environment ("env") vocabulary and where it comes from:

* an operator sets it (pinned: a later sync never overwrites it);
* otherwise a PVE tag ``hp-env-<value>`` on the guest;
* otherwise ``template`` for a Proxmox template;
* otherwise nothing - no guess. A wrong "prod" is worse than a blank.

The tag is spelled with a hyphen because Proxmox tags may only contain
``a-z 0-9 _ - + .`` - the ``hp-env:<value>`` spelling the review sketched cannot
be stored by Proxmox at all. ``hp-env.<value>`` and ``hp-env_<value>`` (and the
colon form, should a Proxmox ever accept it) are read too, so a near-miss
spelling is not silently ignored.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

# The environments a machine can belong to. MUST equal the CHECK constraint on
# hosts.env (migration 37) and ENVS in web/src/lib/fleet.ts.
ENVS: tuple[str, ...] = ("prod", "dev", "infra", "friend", "lab", "template")

# Who decided a host's env. MUST equal the CHECK constraint on hosts.env_source.
ENV_SOURCE_TAG = "pve_tag"
ENV_SOURCE_TEMPLATE = "pve_template"
ENV_SOURCE_USER = "user"

_ENV_TAG_RE = re.compile(r"^hp-env[-_.:](?P<env>[a-z]+)$", re.IGNORECASE)
_TAG_SPLIT_RE = re.compile(r"[;,\s]+")

_MIB = 1024 * 1024
_GIB = 1024 * 1024 * 1024


def parse_pve_tags(raw: Any) -> list[str]:
    """PVE's tag string as a list, in PVE's order, without blanks or repeats.

    PVE answers ``"a;b"``, and accepts ``,`` and spaces as separators on input,
    so all three are split on.
    """
    out: list[str] = []
    for part in _TAG_SPLIT_RE.split(str(raw or "")):
        tag = part.strip()
        if tag and tag not in out:
            out.append(tag)
    return out


def env_from_tags(tags: list[str]) -> str | None:
    """The env the guest's PVE tags name, or None.

    Two DIFFERENT env tags on one guest name no env: picking one would be a
    guess presented as the operator's word.
    """
    found = {
        m.group("env").lower()
        for m in (_ENV_TAG_RE.match(t) for t in tags)
        if m is not None and m.group("env").lower() in ENVS
    }
    return found.pop() if len(found) == 1 else None


def imported_env(tags: list[str], is_template: bool) -> tuple[str | None, str | None]:
    """``(env, env_source)`` as Proxmox states it, before any operator override."""
    tagged = env_from_tags(tags)
    if tagged is not None:
        return tagged, ENV_SOURCE_TAG
    if is_template:
        return "template", ENV_SOURCE_TEMPLATE
    return None, None


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def guest_facts(
    listing: dict[str, Any], resource: dict[str, Any] | None, resources_read: bool
) -> dict[str, Any]:
    """The hosts-table columns one guest's Proxmox rows establish.

    ``listing`` is the guest's row from ``/nodes/<node>/<type>``; ``resource``
    its row from ``/cluster/resources?type=vm`` (None when that read failed or
    did not list it). ``resources_read`` says whether that read succeeded at
    all.

    Only what was actually READ is returned. ``pool`` exists only in
    /cluster/resources, so when that read failed the key is left out and the
    stored pool stands - a failed read is not "the guest left its pool". When
    it succeeded and the guest carries no pool, ``pool`` is None: it left.
    Template flag, tags and sizes are in both rows; the cluster row wins.
    """
    src: dict[str, Any] = {**listing, **(resource or {})}
    facts: dict[str, Any] = {}
    template = _int(src.get("template"))
    facts["is_template"] = 1 if template else 0
    tags = parse_pve_tags(src.get("tags"))
    facts["pve_tags"] = ";".join(tags) or None
    cpus = _int(src.get("maxcpu")) or _int(src.get("cpus"))
    if cpus is not None:
        facts["maxcpu"] = cpus
    maxmem = _int(src.get("maxmem"))
    if maxmem is not None:
        facts["maxmem_mb"] = maxmem // _MIB
    maxdisk = _int(src.get("maxdisk"))
    if maxdisk is not None:
        facts["maxdisk_gb"] = round(maxdisk / _GIB, 1)
    if resources_read and resource is not None:
        facts["pool"] = str(resource.get("pool") or "") or None
    return facts


def env_update(
    row: dict[str, Any] | None, facts: dict[str, Any], pinned: set[str]
) -> dict[str, Any]:
    """The env columns a sync may write for this guest: ``{}`` when it may not.

    An operator's env is pinned (and marked ``user``) and stays put whatever
    the tags now say. Otherwise the imported value replaces the stored one -
    including with None, so removing the tag in Proxmox removes the env.
    """
    if "env" in pinned or (row or {}).get("env_source") == ENV_SOURCE_USER:
        return {}
    tags = parse_pve_tags(facts.get("pve_tags"))
    env, source = imported_env(tags, bool(facts.get("is_template")))
    return {"env": env, "env_source": source}


def proxmox_ui_url(proxmox: Any, host: dict[str, Any]) -> str | None:
    """The Proxmox web UI page for this host, or None when there is none to open.

    Built from the address HomePilot itself talks to Proxmox on - the one place
    it knows the hypervisor is reachable - and the guest's VMID, which is what
    the PVE UI leads with. A host Proxmox no longer reports has no page.
    """
    if proxmox is None or host.get("absent_since"):
        return None
    api_host = getattr(proxmox, "api_host", None)
    if not isinstance(api_host, str) or not api_host:
        return None
    port = getattr(proxmox, "api_port", 8006)
    if not isinstance(port, int):
        port = 8006
    netloc = f"[{api_host}]" if ":" in api_host else api_host
    base = f"https://{netloc}:{port}/"
    host_type = host.get("host_type")
    vmid = _int(host.get("proxmox_id"))
    if host_type in ("qemu", "lxc") and vmid is not None:
        return f"{base}#v1:0:={host_type}%2F{vmid}"
    if host_type == "node" and host.get("hostname"):
        return f"{base}#v1:0:=node%2F{quote(str(host['hostname']), safe='')}"
    return None


def proxmox_console_url(proxmox: Any, host: dict[str, Any]) -> str | None:
    """The guest's console in the Proxmox web UI, or None when there is none to open.

    noVNC for a VM, xterm.js for a container - the two consoles PVE's own UI
    opens. Only a guest that exists, is not a template (a template never runs)
    and has a node and VMID has one; the operator still logs in to Proxmox
    there, HomePilot hands over no credential.
    """
    if proxmox is None or host.get("absent_since") or host.get("is_template"):
        return None
    base = proxmox_ui_url(proxmox, host)
    host_type = host.get("host_type")
    vmid = _int(host.get("proxmox_id"))
    node = host.get("node")
    if base is None or host_type not in ("qemu", "lxc") or vmid is None or not node:
        return None
    root = base.split("#", 1)[0]
    kind = "kvm&novnc=1" if host_type == "qemu" else "lxc&xtermjs=1"
    name = quote(str(host.get("hostname") or ""), safe="")
    return (
        f"{root}?console={kind}&vmid={vmid}&vmname={name}"
        f"&node={quote(str(node), safe='')}&resize=off&cmd="
    )


__all__ = [
    "ENVS",
    "ENV_SOURCE_TAG",
    "ENV_SOURCE_TEMPLATE",
    "ENV_SOURCE_USER",
    "env_from_tags",
    "env_update",
    "guest_facts",
    "imported_env",
    "parse_pve_tags",
    "proxmox_console_url",
    "proxmox_ui_url",
]
