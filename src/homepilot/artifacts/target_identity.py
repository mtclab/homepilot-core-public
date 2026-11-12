"""Which machine a change is for: recorded when it is written, checked before it runs (#813).

PVE reuses VMIDs. A change approved for a throw-away probe guest at VMID N was
found on prod two months later, one click from deleting a friend's machine
that had since been given the same number; the typed confirmation asked for
the VMID - exactly what the wrong machine shared. A VMID names a slot, not a
machine.

So every change that acts on a guest records WHO the guest is when it is
proposed, and every path that executes it asks Proxmox again, before anything
is sent, whether the machine at that VMID is still that one:

* **name** - what HomePilot gave a guest it provisioned and what it recorded
  for one it discovered; the comparison is ``inventory.service.same_guest``,
  the one power and the guest portal already use (#706), not a second rule;
* **type** - a container where a VM was is a different machine;
* **creation time** (QEMU only) - PVE writes ``meta: ...,ctime=<epoch>`` into a
  VM's config when the VM is created (read with ``current=1``). It survives a
  rename, a migration and a snapshot rollback, and a new VM at the same number
  gets a new one - so two machines that share a name are still told apart.
  ``vmgenid`` is deliberately NOT used: it changes on every snapshot rollback
  and backup restore of the SAME machine (that is its purpose), so it would
  refuse the right machine after a routine rollback. Containers have no
  equivalent - PVE records no creation stamp for an LXC - so a container is
  known by its name and type.

A change that CREATES a guest at a VMID (``POST /nodes/<n>/qemu|lxc`` with a
``vmid`` - a create or a restore, which with ``force`` overwrites - or a clone's
``newid``) records that number as one that must be free, and is refused when
it is taken.

Read-only: nothing here writes anything. The lifecycle stores what
``read_identity`` returns; the executor refuses on what ``check`` says.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import yaml

from .models import ArtifactKind

logger = logging.getLogger(__name__)

# A guest, or a guest collection, in a PVE API path.
_GUEST_PATH = re.compile(
    r"^/?nodes/(?P<node>[^/]+)/(?P<type>qemu|lxc)(?:/(?P<vmid>[^/?]+)(?P<rest>/[^?]*)?)?/?$"
)
_TARGET_TYPES = {"vm": "qemu", "lxc": "lxc"}
# Node-level operations that name their guests in the BODY, not the path:
# ``POST /nodes/<n>/vzdump`` with ``vmid: "101,102"`` (a comma list).
_NODE_GUEST_OP = re.compile(r"^/?nodes/(?P<node>[^/]+)/vzdump$")
# Kinds that run on a host through its agent and never call Proxmox. With no
# Proxmox configured there is nobody to ask who is at their target's VMID, and
# they do not address the guest by that number (see ``check``).
_AGENT_ONLY_KINDS = frozenset(
    {
        ArtifactKind.ANSIBLE_PLAYBOOK.value,
        ArtifactKind.SHELL_SCRIPT.value,
        ArtifactKind.HOST_PROVISION.value,
    }
)
_FENCES = ("proxmox-api-spec", "proxmox-api-rollback")

# What `check` answers with, so callers and the UI can tell the cases apart.
CODE_UNCONFIRMED = "unconfirmed"  # nothing recorded for a guest it acts on
CODE_CHANGED = "changed"  # a different machine holds the VMID
CODE_GONE = "gone"  # the recorded machine no longer exists
CODE_TAKEN = "taken"  # a VMID this change creates is in use
CODE_UNREADABLE = "unreadable"  # Proxmox could not be asked
CODE_STALE = "stale"  # the approval is older than the setting allows


class TargetUnreadableError(Exception):
    """Proxmox could not say who is at a VMID; nothing may be decided on that."""


@dataclass(frozen=True)
class GuestRef:
    """A guest a change acts on. `creates`: the change makes it, so it must be free."""

    vmid: int
    node: str | None = None
    guest_type: str | None = None
    creates: bool = False


@dataclass
class Verdict:
    ok: bool
    code: str | None = None
    message: str | None = None
    # Per guest: vmid, recorded, live, problem.
    guests: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "code": self.code, "message": self.message, "guests": self.guests}


def _vmid(value: Any) -> int | None:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _fence_steps(body: str, fence: str) -> list[dict[str, Any]]:
    m = re.search(rf"```yaml\s+{fence}\s*\n(.*?)```", body, re.DOTALL)
    if not m:
        return []
    try:
        parsed = yaml.safe_load(m.group(1).strip())
    except yaml.YAMLError:
        return []
    if not isinstance(parsed, dict) or not isinstance(parsed.get("steps"), list):
        return []
    return [s for s in parsed["steps"] if isinstance(s, dict)]


def guest_refs(fm: dict[str, Any], body: str) -> list[GuestRef]:
    """Every guest this change acts on, by VMID, and which of them it creates.

    The target's VMID, and for a ``proxmox-api-sequence`` every guest a
    writing step (or rollback step) addresses - in its path
    (``/nodes/<n>/qemu|lxc/<vmid>/...``) or, for a node-level backup
    (``POST /nodes/<n>/vzdump``), in its body's ``vmid`` list - with
    ``{{ target.* }}`` rendered the way the executor renders it. A step whose path cannot be
    rendered here names no guest that can be known before it runs; the target
    is still checked.
    """
    from homepilot.executor.jinja_utils import (
        InterpolationError,
        _interpolate,
        _interpolate_obj,
        interpolation_context,
    )

    target = fm.get("target") or {}
    seen: dict[int, GuestRef] = {}
    created: set[int] = set()

    def _add(vmid: int | None, node: Any, guest_type: str | None) -> None:
        if vmid is None:
            return
        known = seen.get(vmid)
        seen[vmid] = GuestRef(
            vmid,
            (known.node if known else None) or (str(node) if node else None),
            (known.guest_type if known else None) or guest_type,
        )

    if fm.get("kind") == ArtifactKind.PROXMOX_API_SEQUENCE.value:
        context = interpolation_context(target, fm)
        for fence in _FENCES:
            for step in _fence_steps(body, fence):
                method = str(step.get("method", "GET")).upper()
                if method == "GET":
                    continue
                try:
                    path = _interpolate(str(step.get("path", "")), context)
                    step_body = _interpolate_obj(step.get("body"), context)
                except InterpolationError:
                    continue
                bare = path.split("?", 1)[0].rstrip("/")
                payload = step_body if isinstance(step_body, dict) else {}
                node_op = _NODE_GUEST_OP.match(bare)
                if node_op:
                    for part in str(payload.get("vmid") or "").split(","):
                        _add(_vmid(part), node_op["node"], None)
                    continue
                m = _GUEST_PATH.match(bare)
                if not m:
                    continue
                if m["vmid"] is None:
                    # POST /nodes/<n>/qemu|lxc: a create, or a restore from an archive.
                    new = _vmid(payload.get("vmid")) if method == "POST" else None
                    if new is not None:
                        created.add(new)
                        _add(new, m["node"], m["type"])
                    continue
                _add(_vmid(m["vmid"]), m["node"], m["type"])
                if (m["rest"] or "").strip("/") == "clone" and method == "POST":
                    new = _vmid(payload.get("newid"))
                    if new is not None:
                        created.add(new)
                        _add(new, payload.get("target") or m["node"], m["type"])

    _add(_vmid(target.get("vmid")), target.get("node"), _TARGET_TYPES.get(str(target.get("kind"))))
    return [
        GuestRef(r.vmid, r.node, r.guest_type, creates=r.vmid in created)
        for r in sorted(seen.values(), key=lambda r: r.vmid)
    ]


def _ctime(config: dict[str, Any]) -> int | None:
    """``meta: creation-qemu=9.0.2,ctime=1715000000`` -> 1715000000."""
    meta = str(config.get("meta") or "")
    for part in meta.split(","):
        key, _, value = part.partition("=")
        if key.strip() == "ctime":
            return _vmid(value)
    return None


_GUEST_TYPES = ("qemu", "lxc")


def _config_absent(exc: Exception) -> bool:
    """Did PVE say there is no such guest config, rather than fail to answer?"""
    from homepilot.adapters.proxmox import ProxmoxError

    return (
        isinstance(exc, ProxmoxError)
        and exc.status_code in (400, 404, 500)
        and "does not exist" in str(exc.body).lower()
    )


async def _config_at(proxmox: Any, node: str, guest_type: str, vmid: int) -> dict[str, Any] | None:
    """The guest's config as it is NOW at (node, type), or None when PVE says
    there is none there. Any other failure is unreadable, never "absent"."""
    try:
        return dict(await proxmox.get_vm_config(node, vmid, guest_type, current=True))
    except Exception as exc:  # a read that failed says nothing about who is there
        if _config_absent(exc):
            return None
        raise TargetUnreadableError(
            f"could not read the config of VMID {vmid} on {node} ({exc})"
        ) from exc


async def _vmid_is_free(proxmox: Any, vmid: int) -> bool:
    """PVE's own answer, from its live VMID list: ``/cluster/nextid?vmid=N``
    answers N when N is free and refuses ("already exists") when it is taken."""
    try:
        await proxmox.read("/cluster/nextid", query={"vmid": vmid})
        return True
    except Exception as exc:  # only "already exists" means taken
        if "already exists" in str(getattr(exc, "body", exc)).lower():
            return False
        raise TargetUnreadableError(f"could not ask whether VMID {vmid} is free ({exc})") from exc


async def _node_names(proxmox: Any) -> list[str]:
    try:
        resp = await proxmox.read("/nodes")
    except Exception as exc:  # cannot search the cluster: who is there is not known
        raise TargetUnreadableError(f"could not list the cluster's nodes ({exc})") from exc
    rows = resp.get("data", resp) if isinstance(resp, dict) else resp
    return [str(r.get("node")) for r in rows or [] if isinstance(r, dict) and r.get("node")]


async def read_live(
    proxmox: Any,
    vmids: list[int],
    hints: dict[int, tuple[str | None, str | None]] | None = None,
) -> dict[int, dict[str, Any] | None]:
    """Who is at each VMID now, cluster-wide; None for a free number (#813).

    Identity comes from the guest's CONFIG, read with ``current=1`` - never
    from ``/cluster/resources``, whose names, types and members are refreshed
    by pvestatd every few seconds: right after a rename, or a destroy and
    re-create at the same number, it still describes the previous machine
    (seen on dev 3.11.0: an approval written for a renamed container ran). The
    list only LOCATES a guest (node, type) and supplies the running state the
    confirm dialog shows. Every disagreement resolves towards refusing:

    * listed, but no config there -> asked again: free (PVE's live VMID list
      says so) is "gone"; taken is searched for on every node and type;
    * not listed -> free only when PVE's VMID list says free (a guest created
      a moment ago may not be listed yet); taken is searched for as above;
    * a VMID taken but whose config cannot be found, or any read that fails,
      raises ``TargetUnreadableError`` - a failed read is never "nobody is there".

    One config read per guest in the usual case. ``hints`` maps a VMID to the
    (node, type) it was recorded at, tried before a cluster-wide search.
    """
    if not vmids:
        return {}
    try:
        resp = await proxmox.read("/cluster/resources", query={"type": "vm"})
    except Exception as exc:  # any failure: who is there is not known
        raise TargetUnreadableError(f"could not list the cluster's guests ({exc})") from exc
    rows = resp.get("data", resp) if isinstance(resp, dict) else resp
    if not isinstance(rows, list):
        raise TargetUnreadableError("Proxmox answered the guest list with something unreadable")
    by_vmid: dict[int, dict[str, Any]] = {}
    for row in rows:
        if isinstance(row, dict) and _vmid(row.get("vmid")) is not None:
            by_vmid[int(row["vmid"])] = row
    nodes: list[str] | None = None
    out: dict[int, dict[str, Any] | None] = {}
    for vmid in vmids:
        row = by_vmid.get(vmid)
        tried: set[tuple[str, str]] = set()
        found: tuple[str, str, dict[str, Any]] | None = None
        first = [(str(row.get("node")), str(row.get("type")))] if row else []
        hint = (hints or {}).get(vmid)
        if hint and hint[0] and hint[1]:
            first.append((str(hint[0]), str(hint[1])))
        for node, guest_type in first:
            if (node, guest_type) in tried or guest_type not in _GUEST_TYPES:
                continue
            tried.add((node, guest_type))
            config = await _config_at(proxmox, node, guest_type, vmid)
            if config is not None:
                found = (node, guest_type, config)
                break
        if found is None:
            if await _vmid_is_free(proxmox, vmid):
                out[vmid] = None
                continue
            if nodes is None:
                nodes = await _node_names(proxmox)
            for node in nodes:
                for guest_type in _GUEST_TYPES:
                    if (node, guest_type) in tried:
                        continue
                    config = await _config_at(proxmox, node, guest_type, vmid)
                    if config is not None:
                        found = (node, guest_type, config)
                        break
                if found:
                    break
            if found is None:
                raise TargetUnreadableError(
                    f"VMID {vmid} is taken, but its config was found on no node"
                )
        node, guest_type, config = found
        listed = row if row and str(row.get("node")) == node else {}
        live: dict[str, Any] = {
            "vmid": vmid,
            "node": node,
            "type": guest_type,
            "name": config.get("hostname" if guest_type == "lxc" else "name"),
            # The running state is not identity: the list's view of it serves
            # the confirm dialog, and is absent while the list lags.
            "status": listed.get("status"),
            "template": bool(config.get("template")),
            "cpus": listed.get("maxcpu"),
            "maxmem": listed.get("maxmem"),
            "maxdisk": listed.get("maxdisk"),
        }
        if guest_type == "qemu":
            live["ctime"] = _ctime(config)
        out[vmid] = live
    return out


def _recorded_entry(ref: GuestRef, live: dict[str, Any] | None) -> dict[str, Any]:
    if live is None:
        entry: dict[str, Any] = {"vmid": ref.vmid, "absent": True}
    else:
        entry = {
            "vmid": ref.vmid,
            "node": live.get("node"),
            "type": live.get("type"),
            "name": live.get("name"),
        }
        if live.get("ctime") is not None:
            entry["ctime"] = live["ctime"]
    if ref.creates:
        entry["creates"] = True
    return entry


async def read_identity(proxmox: Any, refs: list[GuestRef]) -> dict[str, Any]:
    """What to record on an artifact: who each guest it acts on is, right now."""
    live = await read_live(
        proxmox, [r.vmid for r in refs], {r.vmid: (r.node, r.guest_type) for r in refs}
    )
    return {
        "recorded_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "guests": {str(r.vmid): _recorded_entry(r, live.get(r.vmid)) for r in refs},
    }


def describe(live: dict[str, Any] | None) -> str:
    """``'friend-web' (VM, running, on pve1)``, or ``nothing`` for a free number."""
    if live is None:
        return "nothing"
    word = {"qemu": "VM", "lxc": "container"}.get(str(live.get("type")), "guest")
    bits = [word]
    if live.get("status"):
        bits.append(str(live["status"]))
    if live.get("node"):
        bits.append(f"on {live['node']}")
    return f"{live.get('name')!r} ({', '.join(bits)})"


def _problem(recorded: dict[str, Any], live: dict[str, Any] | None) -> tuple[str, str] | None:
    """(code, sentence) when the machine at this VMID is not the recorded one."""
    from homepilot.inventory.service import same_guest

    vmid = recorded.get("vmid")
    if recorded.get("creates"):
        if live is not None:
            return CODE_TAKEN, (
                f"VMID {vmid} is taken by {describe(live)}; this change creates a guest "
                "there and would overwrite or collide with it"
            )
        return None
    if recorded.get("absent"):
        if live is not None:
            return CODE_CHANGED, (
                f"VMID {vmid} was free when this change was written; {describe(live)} is there now"
            )
        return None
    name = recorded.get("name")
    if live is None:
        return CODE_GONE, (
            f"{name!r} (VMID {vmid}) no longer exists; the number is free, and anything "
            "created there later would be a different machine"
        )
    if str(live.get("type")) != str(recorded.get("type")) or not same_guest(
        {"hostname": name}, live.get("name")
    ):
        return CODE_CHANGED, (
            f"VMID {vmid} is now {describe(live)}, not {name!r} - Proxmox reused the number "
            "or the guest was renamed"
        )
    if recorded.get("ctime") is not None and live.get("ctime") != recorded.get("ctime"):
        return CODE_CHANGED, (
            f"VMID {vmid} holds a VM named {name!r}, but not the one this change was written "
            "for: it was created at a different time (a new machine under the same name)"
        )
    return None


def _first(problems: list[tuple[str, str]]) -> tuple[str, str]:
    # The most decisive reason leads: a different machine outranks a free number.
    order = [CODE_CHANGED, CODE_TAKEN, CODE_GONE, CODE_UNCONFIRMED]
    return sorted(problems, key=lambda p: order.index(p[0]) if p[0] in order else 99)[0]


async def check(proxmox: Any, fm: dict[str, Any], body: str) -> Verdict:
    """Is every guest this change acts on still the machine it was written for?

    A guest with nothing recorded (an artifact proposed before 3.11, or whose
    target an edit changed) is ``unconfirmed``: it cannot be run until its
    approval is renewed against the live machine. Read-only.
    """
    refs = guest_refs(fm, body)
    if not refs:
        return Verdict(ok=True)
    recorded_all = (fm.get("target_identity") or {}).get("guests") or {}
    if proxmox is None:
        if fm.get("kind") in _AGENT_ONLY_KINDS:
            # Nobody to ask, and nothing here is sent to Proxmox: the agent
            # reaches the host, not a VMID. (Today no executor is built without
            # Proxmox, and a VM target's default pre-apply snapshot needs it;
            # this keeps an agent-only change from being refused for a check
            # that cannot apply to it.)
            return Verdict(ok=True)
        return Verdict(
            ok=False,
            code=CODE_UNREADABLE,
            message="no Proxmox is configured, so nobody can say which machine is at "
            + ", ".join(f"VMID {r.vmid}" for r in refs),
        )
    try:
        hints: dict[int, tuple[str | None, str | None]] = {}
        for r in refs:
            rec = recorded_all.get(str(r.vmid))
            if isinstance(rec, dict) and rec.get("node") and rec.get("type"):
                hints[r.vmid] = (str(rec["node"]), str(rec["type"]))
            else:
                hints[r.vmid] = (r.node, r.guest_type)
        live = await read_live(proxmox, [r.vmid for r in refs], hints)
    except TargetUnreadableError as exc:
        return Verdict(ok=False, code=CODE_UNREADABLE, message=f"{exc}")
    guests: list[dict[str, Any]] = []
    problems: list[tuple[str, str]] = []
    for ref in refs:
        recorded = recorded_all.get(str(ref.vmid))
        now = live.get(ref.vmid)
        problem: tuple[str, str] | None
        if not isinstance(recorded, dict):
            problem = (
                CODE_UNCONFIRMED,
                f"nothing records which machine VMID {ref.vmid} was when this change was "
                f"written (it predates that record, or an edit changed its target); "
                f"{describe(now)} is there now",
            )
        else:
            problem = _problem(recorded, now)
        guests.append(
            {
                "vmid": ref.vmid,
                "creates": ref.creates,
                "recorded": recorded if isinstance(recorded, dict) else None,
                "live": now,
                "problem": problem[1] if problem else None,
            }
        )
        if problem:
            problems.append(problem)
    if not problems:
        return Verdict(ok=True, guests=guests)
    code, message = _first(problems)
    return Verdict(ok=False, code=code, message=message, guests=guests)


# ── how old an approval is ───────────────────────────────────────────────────


def approval_age_days(fm: dict[str, Any], now: datetime | None = None) -> float | None:
    """Days since the artifact was approved; None when it carries no approval time."""
    at = (fm.get("approved_by") or {}).get("at")
    if not at:
        return None
    try:
        when = datetime.fromisoformat(str(at).replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, ((now or datetime.now(UTC)) - when).total_seconds() / 86400)


def stale_reason(fm: dict[str, Any], limit_days: int, now: datetime | None = None) -> str | None:
    """Why this approval is too old to run on, or None. ``limit_days`` 0 = never."""
    if limit_days <= 0:
        return None
    age = approval_age_days(fm, now)
    if age is None:
        # An approved artifact with no approval time cannot show it is recent.
        return "it carries no approval time, so how old the approval is cannot be told"
    if age <= limit_days:
        return None
    return (
        f"it was approved {int(age)} days ago and approvals expire after {limit_days} "
        f"day{'s' if limit_days != 1 else ''} (setting approval_max_age_days); the cluster "
        "may have changed since - review it again"
    )


async def approval_limit_days() -> int:
    """``approval_max_age_days`` as resolved NOW (env, else the Settings page, else 7)."""
    from homepilot.app_settings import effective
    from homepilot.config import Settings, get_settings

    try:
        return int(await effective("approval_max_age_days", get_settings()))
    except Exception as exc:  # a bad value falls back to the code default, never to "never"
        logger.warning("Could not resolve approval_max_age_days (%s); using the default", exc)
        return int(Settings.model_fields["approval_max_age_days"].default)
