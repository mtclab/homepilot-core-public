"""Operator power controls for Proxmox guests (lane B3, #781).

Start, shutdown, reboot and stop for VMs and containers, with the guards that
make them safe to press, and a result that says what really happened.

Who may act, and how:

* an operator (an admin credential - a person, see ``auth/principal.py``) acts
  DIRECTLY through ``POST /api/inventory/{host_id}/power``: no artifact, one
  audit row naming the actor, the guest, the action, the PVE task and the
  outcome;
* an assistant never acts. The ``power_guest`` MCP tool proposes a one-step
  ``guest-power`` artifact; only the owner's approval turns it into an action,
  and the apply runs THIS module, so it meets the same guards at apply time
  (the world may have changed since the proposal).

The guards, each refusing with a reason and sending nothing:

* the guest is not in inventory, is gone (``absent_since``), is a template, or
  is not a Proxmox guest at all;
* the machine at (node, VMID) is not the one the row names - PVE reuses VMIDs,
  so the name is asked live before anything is sent (the guest portal's #706
  pattern, ``same_guest``);
* the guest is not in a state the action applies to (start a running guest);
* Start, and a Reboot that applies a pending ``ipconfigN``/``netN`` change,
  would take an address a RUNNING guest holds - lane B2's
  ``InventoryService.address_conflicts_for_start``, which names the holder;
* Start, and a Reboot that attaches a pending disk, would open a physical disk
  (``/dev/...`` passthrough) that a running guest on the same node already has
  attached - the stopped VM that still referenced a running VM's SSD;
* shutdown / reboot / stop of the machine HomePilot runs on (``is_self``) or of
  a protected guest takes the guest's name typed back, and is never done
  through an artifact at all.

Outcomes (``PowerResult.outcome``), honest by construction:

* ``ok``        the PVE task ended and the guest is in the state the action
                promised (running after start/reboot, stopped after
                shutdown/stop);
* ``timed_out`` a shutdown or reboot the guest did not honour in time: it is
                still running, nothing was forced, and Stop is offered;
* ``failed``    the task failed, or the guest is not where it should be;
* ``unknown``   HomePilot stopped following a task that PVE still runs, or
                could not read the guest's state afterwards (#774): neither
                verdict is claimed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from ..adapters.proxmox import (
    GUEST_POWER_ACTIONS,
    ProxmoxTaskStillRunningError,
    task_warnings,
)

logger = logging.getLogger(__name__)

POWER_ACTIONS: tuple[str, ...] = GUEST_POWER_ACTIONS
# The actions that take a machine down (for a while or for good). On the self
# guest or a protected one they need the guest's name typed back.
OFF_ACTIONS: tuple[str, ...] = ("shutdown", "reboot", "stop")

OUTCOME_OK = "ok"
OUTCOME_FAILED = "failed"
OUTCOME_TIMED_OUT = "timed_out"
OUTCOME_UNKNOWN = "unknown"

# How long a guest gets to shut down (or to shut down for a reboot) before the
# action is reported as timed out. Sent to PVE as the task's own `timeout`.
DEFAULT_SHUTDOWN_TIMEOUT_S = 120
MIN_SHUTDOWN_TIMEOUT_S = 10
MAX_SHUTDOWN_TIMEOUT_S = 600
# How long a start or a forced stop is followed. Both are quick on PVE; a task
# still running after this is an unknown outcome, never a failure (#774).
START_STOP_WAIT_S = 180.0
# Following a shutdown/reboot task past PVE's own timeout: the task needs a
# moment to notice its timeout, and a reboot then starts the guest again.
TASK_WAIT_MARGIN_S = 120.0
POLL_INTERVAL_S = 2.0
# A QEMU reboot task ends once the guest has STOPPED; Proxmox's event daemon
# then starts it again. The guest is given this long to be running again
# before the result says it has not come back.
REBOOT_COMEBACK_S = 90.0

# The env whose machines are protected unless the operator says otherwise.
_PROTECTED_ENVS = ("infra",)

_ADDRESS_KEY_RE = re.compile(r"^(ipconfig|net)\d+$")
_QEMU_DISK_KEY_RE = re.compile(r"^(scsi|sata|virtio|ide)\d+$")
_LXC_DISK_KEY_RE = re.compile(r"^(rootfs|mp\d+)$")


class PowerRefusedError(Exception):
    """A guard refused the action; nothing was sent to Proxmox.

    ``code`` is stable and machine-readable; the message is the sentence an
    operator reads. ``status`` is the HTTP status the REST route answers with.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int = 409,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.status = status
        self.details = dict(details or {})
        super().__init__(message)

    def detail(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, **self.details}


@dataclass
class PowerResult:
    """What one power action did, in the words the operator reads."""

    action: str
    outcome: str
    message: str
    host_id: str
    hostname: str
    vmid: int
    node: str
    guest_type: str
    upid: str | None = None
    status_before: str | None = None
    status_after: str | None = None
    task_exitstatus: str | None = None
    # What the operator can do next (e.g. ["stop"] after a timed-out shutdown).
    next_actions: list[str] = field(default_factory=list)
    # After a reboot: the pending changes PVE still lists (should be none).
    pending_after: list[dict[str, Any]] | None = None

    @property
    def ok(self) -> bool:
        return self.outcome == OUTCOME_OK

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# ── pure helpers ─────────────────────────────────────────────────────────────


def protection(host: Mapping[str, Any]) -> tuple[bool, str | None]:
    """Is ``host`` protected, and why - ``(protected, reason)``.

    The operator's own answer wins (``hosts.protected`` 0/1). Without one, the
    machine HomePilot runs on and every ``env = infra`` machine are protected.
    The self guest needs the typed name whatever ``protected`` says: switching
    it off switches HomePilot off.
    """
    if host.get("is_self"):
        return True, "self"
    explicit = host.get("protected")
    if explicit is not None:
        return (bool(int(explicit)), "protected" if int(explicit) else None)
    if str(host.get("env") or "") in _PROTECTED_ENVS:
        return True, "protected"
    return False, None


def needs_typed_confirmation(host: Mapping[str, Any], action: str) -> bool:
    return action in OFF_ACTIONS and protection(host)[0]


def confirmation_matches(host: Mapping[str, Any], typed: str | None) -> bool:
    expected = str(host.get("hostname") or "").strip().lower()
    return bool(expected) and str(typed or "").strip().lower() == expected


def pending_changes(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The keys a ``/pending`` listing says will change at the next boot.

    Each comes back as ``{key, current, pending}`` or ``{key, current,
    delete: True}`` - what the operator is shown before a reboot.
    """
    out: list[dict[str, Any]] = []
    for row in rows:
        key = str(row.get("key") or "")
        if not key:
            continue
        if row.get("delete"):
            out.append({"key": key, "current": row.get("value"), "delete": True})
        elif "pending" in row:
            out.append({"key": key, "current": row.get("value"), "pending": row["pending"]})
    return out


def passthrough_disks(config: Mapping[str, Any], guest_type: str) -> dict[str, str]:
    """Every host device a guest's config attaches directly: ``{/dev/... path: key}``.

    A disk line is ``<volume>[,opt=...]``; a volume that is a host path under
    ``/dev/`` (``/dev/disk/by-id/...``) is a physical disk handed to the guest,
    which two running guests must never share. Storage volumes
    (``local-lvm:vm-100-disk-0``) are owned by one guest and are not compared.
    """
    key_re = _LXC_DISK_KEY_RE if guest_type == "lxc" else _QEMU_DISK_KEY_RE
    out: dict[str, str] = {}
    for key, value in config.items():
        if not key_re.match(str(key)):
            continue
        volume = str(value or "").split(",", 1)[0].strip()
        if volume.startswith("/dev/"):
            out.setdefault(volume, str(key))
    return out


def _data(payload: Any) -> Any:
    return payload.get("data", payload) if isinstance(payload, Mapping) else payload


# ── the service ──────────────────────────────────────────────────────────────


class PowerService:
    """Guards, sends and follows one power action; describes what is possible."""

    def __init__(
        self,
        repo: Any,
        proxmox: Any,
        *,
        poll_interval: float = POLL_INTERVAL_S,
        start_stop_wait_s: float = START_STOP_WAIT_S,
        wait_margin_s: float = TASK_WAIT_MARGIN_S,
        reboot_comeback_s: float = REBOOT_COMEBACK_S,
    ) -> None:
        self.repo = repo
        self.proxmox = proxmox
        self.poll_interval = poll_interval
        self.start_stop_wait_s = start_stop_wait_s
        self.wait_margin_s = wait_margin_s
        self.reboot_comeback_s = reboot_comeback_s

    # ── reads ────────────────────────────────────────────────────────────────

    async def describe(self, host_id: str) -> dict[str, Any]:
        """The guest's power state, pending changes and what each action needs.

        A read: nothing is sent. A failed read is reported as such - the
        state is ``unknown``, never guessed from the inventory row.
        """
        host = await self._host(host_id)
        protected, why = protection(host)
        out: dict[str, Any] = {
            "host_id": host["id"],
            "hostname": host.get("hostname"),
            "vmid": host.get("proxmox_id"),
            "node": host.get("node"),
            "guest_type": host.get("host_type"),
            "is_self": bool(host.get("is_self")),
            "protected": protected,
            "protected_reason": why,
            "status": "unknown",
            "pending": [],
            "actions": {},
            "error": None,
        }
        try:
            node, vmid, guest_type = self._guest_ref(host)
        except PowerRefusedError as exc:
            out["error"] = exc.message
            return out
        try:
            status = await self.proxmox.get_guest_status(node, vmid, guest_type)
            pending = await self.proxmox.get_guest_pending(node, vmid, guest_type)
        except Exception as exc:  # a read that failed: say so, guess nothing
            out["error"] = f"could not read the guest's state from Proxmox ({exc})"
            return out
        state = str(status.get("status") or "unknown")
        out["status"] = state
        out["pve_name"] = status.get("name")
        out["name_matches"] = _same_guest(host, status.get("name"))
        out["pending"] = pending_changes(pending)
        for action in POWER_ACTIONS:
            available, why_not = self._state_allows(action, state)
            out["actions"][action] = {
                "available": available and out["name_matches"] and not host.get("absent_since"),
                "reason": why_not,
                "needs_typed_confirmation": needs_typed_confirmation(host, action),
            }
        return out

    async def check(
        self,
        host_id: str,
        action: str,
        *,
        timeout_s: int | None = None,
        via_artifact: bool = False,
    ) -> dict[str, Any]:
        """Run every guard WITHOUT acting; raises ``PowerRefusedError`` like ``execute``.

        What ``power_guest`` asks before it proposes: a proposal the apply
        would refuse right now is refused at once, with the same reason.
        """
        host = await self._host(host_id)
        return await self._guard(
            host, action, confirm_name=None, timeout_s=timeout_s, via_artifact=via_artifact
        )

    # ── the action ───────────────────────────────────────────────────────────

    async def execute(
        self,
        host_id: str,
        action: str,
        *,
        actor: str,
        source: str,
        confirm_name: str | None = None,
        timeout_s: int | None = None,
        via_artifact: bool = False,
        artifact_id: str | None = None,
    ) -> PowerResult:
        """Guard, send, follow and audit one power action.

        Raises ``PowerRefusedError`` (audited) when a guard refuses; otherwise the
        action WAS sent and the result says how it ended.
        """
        host = await self._host(host_id)
        try:
            ctx = await self._guard(
                host,
                action,
                confirm_name=confirm_name,
                timeout_s=timeout_s,
                via_artifact=via_artifact,
            )
        except PowerRefusedError as refused:
            await self._audit(
                actor,
                source,
                "guest_power_refused",
                host,
                artifact_id,
                {"action": action, "code": refused.code, "reason": refused.message},
            )
            raise
        result = await self._send_and_follow(host, action, ctx)
        await self._audit(
            actor,
            source,
            f"guest_{action}",
            host,
            artifact_id,
            {
                "vmid": result.vmid,
                "node": result.node,
                "guest_type": result.guest_type,
                "upid": result.upid,
                "outcome": result.outcome,
                "status_after": result.status_after,
                "task_exitstatus": result.task_exitstatus,
                "message": result.message,
            },
        )
        return result

    # ── guards ───────────────────────────────────────────────────────────────

    async def _host(self, host_id: str) -> dict[str, Any]:
        row = await self.repo.get_host(host_id)
        if row is None:
            raise PowerRefusedError("not_found", f"no host {host_id!r} in inventory", status=404)
        return dict(row)

    def _guest_ref(self, host: Mapping[str, Any]) -> tuple[str, int, str]:
        node = str(host.get("node") or "")
        guest_type = str(host.get("host_type") or "")
        vmid = host.get("proxmox_id")
        if guest_type not in ("qemu", "lxc") or not node or vmid is None:
            raise PowerRefusedError(
                "not_a_guest",
                f"{host.get('hostname') or 'this host'} is not a Proxmox VM or container "
                "with a known node and VMID, so HomePilot cannot power it",
            )
        if self.proxmox is None:
            raise PowerRefusedError(
                "no_proxmox", "no Proxmox is configured, so nothing can be powered", status=503
            )
        return node, int(vmid), guest_type

    @staticmethod
    def _state_allows(action: str, state: str) -> tuple[bool, str | None]:
        if action == "start":
            if state == "stopped":
                return True, None
            return False, f"the guest is {state}, not stopped"
        if state == "running":
            return True, None
        return False, f"the guest is {state}, not running"

    async def _guard(
        self,
        host: dict[str, Any],
        action: str,
        *,
        confirm_name: str | None,
        timeout_s: int | None,
        via_artifact: bool,
    ) -> dict[str, Any]:
        name = host.get("hostname") or host.get("id")
        if action not in POWER_ACTIONS:
            raise PowerRefusedError(
                "bad_action",
                f"action must be one of: {', '.join(POWER_ACTIONS)}",
                status=400,
            )
        if timeout_s is not None and (
            isinstance(timeout_s, bool)
            or not isinstance(timeout_s, int)
            or not MIN_SHUTDOWN_TIMEOUT_S <= timeout_s <= MAX_SHUTDOWN_TIMEOUT_S
        ):
            raise PowerRefusedError(
                "bad_timeout",
                f"timeout_s must be a whole number of seconds from {MIN_SHUTDOWN_TIMEOUT_S} "
                f"to {MAX_SHUTDOWN_TIMEOUT_S}",
                status=400,
            )
        if host.get("absent_since"):
            # The VMID may already be somebody else's: PVE reuses numbers (#706).
            raise PowerRefusedError(
                "gone",
                f"{name} no longer exists on Proxmox (gone since {host['absent_since']}), "
                "so it cannot be powered; nothing was sent",
            )
        if host.get("is_template"):
            raise PowerRefusedError(
                "template", f"{name} is a Proxmox template; templates never run"
            )
        node, vmid, guest_type = self._guest_ref(host)

        # Who is at (node, vmid) NOW, and in what state - asked before anything
        # is sent. A read that fails is not a licence to act.
        try:
            status = await self.proxmox.get_guest_status(node, vmid, guest_type)
        except Exception as exc:  # any failure: the guest's identity is not known
            raise PowerRefusedError(
                "unreadable",
                f"could not ask Proxmox who is at VMID {vmid} on {node} ({exc}); nothing was sent",
            ) from exc
        pve_name = status.get("name")
        if not _same_guest(host, pve_name):
            raise PowerRefusedError(
                "name_mismatch",
                f"VMID {vmid} on {node} is now {pve_name!r}, not {host.get('hostname')!r} - "
                "Proxmox reused the number or the guest was renamed; nothing was sent. "
                "Refresh the inventory and try again.",
                details={"pve_name": pve_name},
            )
        state = str(status.get("status") or "unknown")
        if status.get("lock"):
            raise PowerRefusedError(
                "locked",
                f"cannot {action} {name}: Proxmox has it locked ({status['lock']}) - a "
                "backup, snapshot or migration is running. Try again when it finishes; "
                "nothing was sent.",
                details={"lock": status["lock"]},
            )
        allowed, why_not = self._state_allows(action, state)
        if not allowed:
            raise PowerRefusedError(
                "state",
                f"cannot {action} {name}: {why_not}; nothing was sent",
                details={"status": state},
            )

        if needs_typed_confirmation(host, action):
            _protected, why = protection(host)
            what = (
                "the machine HomePilot runs on - HomePilot goes down with it"
                if why == "self"
                else "a protected guest"
            )
            if via_artifact:
                raise PowerRefusedError(
                    "confirmation_required",
                    f"{name} is {what}. A {action} of it needs its name typed back by a "
                    "person on the Hosts page; it is never done through a proposed "
                    "change.",
                    details={"protected_reason": why},
                )
            if not confirmation_matches(host, confirm_name):
                raise PowerRefusedError(
                    "confirmation_required",
                    f"{name} is {what}. Type its name to confirm the {action}.",
                    status=428,
                    details={"protected_reason": why},
                )

        pending: list[dict[str, Any]] = []
        if action in ("start", "reboot"):
            try:
                pending = await self.proxmox.get_guest_pending(node, vmid, guest_type)
            except Exception as exc:  # what it would boot with is not known: refuse
                raise PowerRefusedError(
                    "unreadable",
                    f"could not read the pending config of {name} ({exc}), so what it "
                    "would boot with is not known; nothing was sent",
                ) from exc
            changed = pending_changes(pending)
            changed_keys = {c["key"] for c in changed}
            if action == "start" or any(_ADDRESS_KEY_RE.match(k) for k in changed_keys):
                await self._refuse_address_clash(host, action)
            await self._refuse_shared_disk(
                host, action, node, vmid, guest_type, pending, changed_keys
            )
        return {
            "node": node,
            "vmid": vmid,
            "guest_type": guest_type,
            "status_before": state,
            "timeout_s": timeout_s,
        }

    async def _refuse_address_clash(self, host: dict[str, Any], action: str) -> None:
        from .addresses import AddressCheckError
        from .service import InventoryService

        name = host.get("hostname")
        try:
            holders = await InventoryService(self.repo, self.proxmox).address_conflicts_for_start(
                host
            )
        except AddressCheckError as exc:
            raise PowerRefusedError(
                "address_unknown",
                f"cannot {action} {name}: whether its addresses are free could not be "
                f"established ({exc}); nothing was sent",
            ) from exc
        if holders:
            named = "; ".join(
                f"{h.address} is held by {h.hostname}"
                + (f" (VMID {h.proxmox_id}, {h.pve_status})" if h.proxmox_id is not None else "")
                for h in holders
            )
            raise PowerRefusedError(
                "address_clash",
                f"cannot {action} {name}: {named}. Starting it would take that address "
                "over on the LAN and cut the running machine off; nothing was sent.",
                details={
                    "holders": [
                        {
                            "address": h.address,
                            "hostname": h.hostname,
                            "vmid": h.proxmox_id,
                            "node": h.node,
                            "source": h.source,
                        }
                        for h in holders
                    ]
                },
            )

    async def _refuse_shared_disk(
        self,
        host: dict[str, Any],
        action: str,
        node: str,
        vmid: int,
        guest_type: str,
        pending: Sequence[Mapping[str, Any]],
        changed_keys: set[str],
    ) -> None:
        """Refuse when a /dev passthrough this guest would open is attached to a running guest.

        Start: every passthrough disk of the config it boots with. Reboot: only
        the disks a pending change attaches - the ones it has open now are not
        news. The OTHER guests are read with ``current=1`` (what is really
        attached; PVE's plain GET would answer with their pending changes).
        """
        from .addresses import effective_pending_config

        boot = effective_pending_config(pending)
        disks = passthrough_disks(boot, guest_type)
        if action == "reboot":
            disks = {path: key for path, key in disks.items() if key in changed_keys}
        if not disks:
            return
        name = host.get("hostname")
        try:
            listing = await self.proxmox.read("/cluster/resources", {"type": "vm"})
        except Exception as exc:  # the scan cannot complete: refuse
            raise PowerRefusedError(
                "disk_unknown",
                f"cannot {action} {name}: could not list the cluster's guests to check its "
                f"passthrough disk(s) ({exc}); nothing was sent",
            ) from exc
        rows = _data(listing)
        if not isinstance(rows, list):
            raise PowerRefusedError(
                "disk_unknown",
                f"cannot {action} {name}: the cluster's guest list could not be read; "
                "nothing was sent",
            )
        for row in rows:
            if not isinstance(row, Mapping) or row.get("template"):
                continue
            other_type = str(row.get("type") or "")
            try:
                other_vmid = int(row.get("vmid"))  # type: ignore[arg-type]
            except (TypeError, ValueError):
                continue
            # A /dev path names a disk on ONE machine: the same path on another
            # node is another disk.
            if (
                other_vmid == vmid
                or str(row.get("node") or "") != node
                or other_type not in ("qemu", "lxc")
                or str(row.get("status") or "") != "running"
            ):
                continue
            try:
                other = await self.proxmox.get_vm_config(node, other_vmid, other_type, current=True)
            except Exception as exc:  # the scan cannot complete: refuse
                raise PowerRefusedError(
                    "disk_unknown",
                    f"cannot {action} {name}: could not read the config of running guest "
                    f"{other_vmid} to check for a shared passthrough disk ({exc}); "
                    "nothing was sent",
                ) from exc
            theirs = passthrough_disks(other, other_type)
            shared = sorted(set(disks) & set(theirs))
            if shared:
                other_name = row.get("name") or other.get("name") or other.get("hostname")
                disk = shared[0]
                raise PowerRefusedError(
                    "disk_shared",
                    f"cannot {action} {name}: its {disks[disk]} is the physical disk {disk}, "
                    f"which running guest {other_name} (VMID {other_vmid}) has attached as "
                    f"{theirs[disk]}. Two machines writing one disk corrupts it; detach it "
                    "from one of them first. Nothing was sent.",
                    details={
                        "disk": disk,
                        "holder": {"hostname": other_name, "vmid": other_vmid, "key": theirs[disk]},
                    },
                )

    # ── send and follow ──────────────────────────────────────────────────────

    async def _send_and_follow(
        self, host: dict[str, Any], action: str, ctx: dict[str, Any]
    ) -> PowerResult:
        node, vmid, guest_type = ctx["node"], ctx["vmid"], ctx["guest_type"]
        name = str(host.get("hostname") or "")
        timeout_s = ctx["timeout_s"]
        if action in ("shutdown", "reboot") and timeout_s is None:
            timeout_s = DEFAULT_SHUTDOWN_TIMEOUT_S
        result = PowerResult(
            action=action,
            outcome=OUTCOME_UNKNOWN,
            message="",
            host_id=str(host["id"]),
            hostname=name,
            vmid=vmid,
            node=node,
            guest_type=guest_type,
            status_before=ctx["status_before"],
        )
        try:
            result.upid = await self.proxmox.power_guest(
                node, vmid, action, guest_type=guest_type, timeout_s=timeout_s
            )
        except Exception as exc:  # PVE refused the request itself
            result.outcome = OUTCOME_FAILED
            result.message = f"Proxmox refused the {action} of {name}: {exc}"
            return result

        task_ok = True
        if result.upid:
            wait = (
                float(timeout_s) + self.wait_margin_s
                if action in ("shutdown", "reboot")
                else self.start_stop_wait_s
            )
            try:
                task = await self.proxmox.wait_until_stopped(
                    node, result.upid, timeout_s=wait, poll_interval=self.poll_interval
                )
            except ProxmoxTaskStillRunningError:
                result.message = (
                    f"the {action} of {name} was sent, but its Proxmox task {result.upid} is "
                    f"still running after {wait:.0f}s; the outcome is not known yet"
                )
                return result
            except Exception as exc:  # the task could not be followed
                result.message = (
                    f"the {action} of {name} was sent, but its Proxmox task could not be "
                    f"followed ({exc}); the outcome is not known"
                )
                return result
            result.task_exitstatus = str(task.get("exitstatus") or "")
            task_ok = task_warnings(task.get("exitstatus")) is not None

        try:
            after = await self.proxmox.get_guest_status(node, vmid, guest_type)
            result.status_after = str(after.get("status") or "unknown")
            if action == "reboot" and task_ok and result.status_after != "running":
                result.status_after = await self._await_comeback(node, vmid, guest_type)
        except Exception as exc:  # the result cannot be confirmed
            result.message = (
                f"the {action} of {name} was sent and its task ended "
                f"({result.task_exitstatus or 'no task'}), but the guest's state could not "
                f"be read afterwards ({exc}); the outcome is not known"
            )
            return result

        self._judge(result, action, task_ok, timeout_s)
        if action == "reboot" and result.outcome == OUTCOME_OK:
            try:
                result.pending_after = pending_changes(
                    await self.proxmox.get_guest_pending(node, vmid, guest_type)
                )
            except Exception:  # best effort: the reboot itself is settled
                result.pending_after = None
            if result.pending_after:
                keys = ", ".join(p["key"] for p in result.pending_after)
                result.message += f" Proxmox still lists pending changes: {keys}."
        return result

    async def _await_comeback(self, node: str, vmid: int, guest_type: str) -> str:
        """The guest's state once it is running again, or when the wait runs out."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.reboot_comeback_s
        state = "unknown"
        while True:
            status = await self.proxmox.get_guest_status(node, vmid, guest_type)
            state = str(status.get("status") or "unknown")
            if state == "running" or loop.time() >= deadline:
                return state
            await asyncio.sleep(self.poll_interval)

    @staticmethod
    def _judge(result: PowerResult, action: str, task_ok: bool, timeout_s: int | None) -> None:
        name = result.hostname
        after = result.status_after
        task = result.task_exitstatus or "no task"
        want = "running" if action in ("start", "reboot") else "stopped"
        if action == "start":
            if after == want:
                result.outcome = OUTCOME_OK
                result.message = f"{name} started and is running."
            else:
                result.outcome = OUTCOME_FAILED
                result.message = f"the start of {name} failed ({task}); it is {after}."
                result.next_actions = ["start"]
            return
        if action == "stop":
            if after == want:
                result.outcome = OUTCOME_OK
                result.message = f"{name} was stopped (forced)."
            else:
                result.outcome = OUTCOME_FAILED
                result.message = f"the forced stop of {name} failed ({task}); it is {after}."
                result.next_actions = ["stop"]
            return
        if action == "shutdown":
            if after == want:
                result.outcome = OUTCOME_OK
                result.message = f"{name} shut down."
            elif after == "running":
                result.outcome = OUTCOME_TIMED_OUT
                result.message = (
                    f"{name} did not shut down within {timeout_s}s - it ignored or did not "
                    "receive the shutdown request. It is still running; nothing was forced. "
                    "Stop (forced) is available."
                )
                result.next_actions = ["stop"]
            else:
                result.outcome = OUTCOME_FAILED
                result.message = f"the shutdown of {name} failed ({task}); it is {after}."
            return
        # reboot
        if task_ok and after == want:
            result.outcome = OUTCOME_OK
            result.message = f"{name} rebooted (a Proxmox reboot: pending config was applied)."
        elif task_ok:
            # The shutdown half finished; the guest has not come back (yet).
            # Proxmox starts it again on its own, so "failed" is not established.
            result.outcome = OUTCOME_UNKNOWN
            result.message = (
                f"{name} shut down for the reboot but is {after}, not running again yet; "
                "check it in a moment, and Start it if it stays down."
            )
            result.next_actions = ["start"] if after == "stopped" else []
        elif after == "running":
            result.outcome = OUTCOME_TIMED_OUT
            result.message = (
                f"{name} did not shut down within {timeout_s}s, so it was not rebooted and "
                "its pending changes are not applied. It is still running; nothing was "
                "forced. Stop (forced), then Start, is available."
            )
            result.next_actions = ["stop"]
        else:
            result.outcome = OUTCOME_FAILED
            result.message = (
                f"the reboot of {name} failed ({task}): it is {after} and did not come back."
            )
            result.next_actions = ["start"] if after == "stopped" else []

    # ── audit ────────────────────────────────────────────────────────────────

    async def _audit(
        self,
        actor: str,
        source: str,
        action: str,
        host: Mapping[str, Any],
        artifact_id: str | None,
        details: Mapping[str, Any],
    ) -> None:
        try:
            await self.repo.log_audit(
                user_id=actor,
                source=source,
                action=action,
                artifact_id=artifact_id,
                target_host=str(host.get("hostname") or host.get("id") or ""),
                details_json=json.dumps(dict(details), default=str),
            )
        except Exception:  # the action happened; a lost audit row must be loud
            logger.exception("could not audit %s on %s by %s", action, host.get("id"), actor)


def _same_guest(host: Mapping[str, Any], pve_name: Any) -> bool:
    from .service import same_guest

    return same_guest(dict(host), pve_name)


__all__ = [
    "DEFAULT_SHUTDOWN_TIMEOUT_S",
    "MAX_SHUTDOWN_TIMEOUT_S",
    "MIN_SHUTDOWN_TIMEOUT_S",
    "OFF_ACTIONS",
    "OUTCOME_FAILED",
    "OUTCOME_OK",
    "OUTCOME_TIMED_OUT",
    "OUTCOME_UNKNOWN",
    "POWER_ACTIONS",
    "PowerRefusedError",
    "PowerResult",
    "PowerService",
    "confirmation_matches",
    "needs_typed_confirmation",
    "passthrough_disks",
    "pending_changes",
    "protection",
]
