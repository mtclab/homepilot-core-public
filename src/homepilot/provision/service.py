from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from typing import Any

from ..adapters.proxmox import ProxmoxClient
from ..background import DEFAULT_DRAIN_TIMEOUT, drain_tasks
from ..db.repository import Repository
from ..tasks.repository import TaskRepository
from .defaults import ProvisioningDefaults, provisioning_defaults
from .guest_network import (
    DesiredGuestNetwork,
    desired_from_settings,
    fence_rule_writes,
    gateway_for,
)
from .models import ProvisionRequest

logger = logging.getLogger(__name__)

# Tailscale's own installer: the only thing that knows how to add their
# repository across distributions, and what their docs tell you to run. It
# needs the guest to reach the internet, which a fenced guest still does.
# `set -e` so a failed download cannot look like a successful install.
_TAILSCALE_INSTALL_SCRIPT = (
    "set -e; export DEBIAN_FRONTEND=noninteractive; "
    "curl -fsSL https://tailscale.com/install.sh | sh"
)

# What inventory refresh writes for a qemu guest. A provisioned VM MUST use the
# same host_type + proxmox_id, because refresh_inventory matches existing rows by
# proxmox_id alone: any other convention would leave the reconciler creating a
# second, duplicate row for the same VM on its next pass.
HOST_TYPE = "qemu"


class ProvisionConflictError(Exception):
    """A provision for the same guest name is already in flight."""


class ProvisionService:
    """Clone a Proxmox template into a running guest, tracked as a 'provision' task.

    The caller gets a task id immediately; everything else happens in a tracked
    background task that ALWAYS lands the record in a terminal state (#386 — a
    task stranded in 'running' is worse than a failed one, because it never stops
    being "in flight").
    """

    def __init__(
        self,
        proxmox: ProxmoxClient | None,
        task_repo: TaskRepository,
        repo: Repository,
        poll_interval: float = 2.0,
        task_timeout_s: float = 600.0,
        ip_wait_s: float = 60.0,
        ip_interval: float = 3.0,
        defaults_source: Any = None,
        tailscale_install: bool = True,
        tailscale_timeout_s: float = 120.0,
        tailscale_install_timeout_s: float = 300.0,
    ):
        self.proxmox = proxmox
        # Where the provisioning defaults are read from at RUN time (#553 C3):
        # an app state, a resolver, or None for the process-wide one. Never a
        # snapshot - the settings are hot-reloadable and a cached copy would
        # make that claim false.
        self.defaults_source = defaults_source
        # The PVE firewall gateway (the estate's proxmox_mcp library) the per-VM
        # fence is written through. Built from the Proxmox client's own
        # credentials when it is needed; settable so a fence journey can be
        # driven against a fake cluster at that boundary.
        self.sdn_gateway: Any = None
        self.task_repo = task_repo
        self.repo = repo
        self.poll_interval = poll_interval
        self.task_timeout_s = task_timeout_s
        self.ip_wait_s = ip_wait_s
        self.ip_interval = ip_interval
        # Whether a guest with no tailscale gets it installed before the join.
        # Off is for an image that ships its own (or an air-gapped guest, where
        # the vendor installer cannot reach the internet anyway).
        self.tailscale_install = tailscale_install
        self.tailscale_timeout_s = tailscale_timeout_s
        # The vendor installer adds a repo and pulls packages: minutes, not
        # seconds, on a cold cloud image.
        self.tailscale_install_timeout_s = tailscale_install_timeout_s
        self._running_tasks: set[asyncio.Task[Any]] = set()
        # task_id → in-flight asyncio.Task, so cancel() can reach the coroutine
        # that is actually talking to Proxmox (#452). Without it, a cancel only
        # marked the row and the still-running job overwrote it seconds later.
        self._task_by_id: dict[str, asyncio.Task[Any]] = {}
        # Names with a provision in flight IN THIS PROCESS. The DB cannot answer
        # this cheaply (a provision task carries no artifact_id, and the name only
        # appears in the result), and a duplicate would clone a second VM under
        # the same hostname.
        self._inflight_names: set[str] = set()

    def _track_task(self, task: asyncio.Task[Any], task_id: str | None = None) -> None:
        # Hold a strong reference until completion: asyncio keeps only a weak one,
        # so an untracked background task can be garbage-collected mid-flight.
        self._running_tasks.add(task)
        if task_id is None:
            task.add_done_callback(self._running_tasks.discard)
            return
        self._task_by_id[task_id] = task

        def _done(t: asyncio.Task[Any]) -> None:
            self._running_tasks.discard(t)
            # Only drop the mapping if it still points at THIS task — a fresh
            # task for a reused id (shouldn't happen: ids are uuid4) must win.
            if self._task_by_id.get(task_id) is t:
                del self._task_by_id[task_id]

        task.add_done_callback(_done)

    async def drain(self, timeout: float = DEFAULT_DRAIN_TIMEOUT) -> None:
        """Let in-flight jobs finish before the database goes away (#496).

        These run behind an already-accepted HTTP request, so nothing else
        awaits them; a shutdown that closes the database under one leaves the
        task row it was about to finish saying "running" forever - and can kill
        aiosqlite's worker thread mid-write.
        """
        await drain_tasks(self._running_tasks, "provision job(s)", timeout)

    def is_inflight(self, name: str) -> bool:
        return name in self._inflight_names

    async def start(self, request: ProvisionRequest, actor: str = "system") -> str:
        if request.name in self._inflight_names:
            raise ProvisionConflictError(f"A provision for {request.name!r} is already in flight")
        self._inflight_names.add(request.name)
        try:
            task_id = await self.task_repo.create_task(None, "provision")
        except Exception:
            self._inflight_names.discard(request.name)
            raise
        self._track_task(asyncio.create_task(self._run(task_id, request, actor)), task_id)
        return task_id

    async def cancel(self, task_id: str) -> dict[str, Any] | None:
        """Stop an in-flight provision for real, and mark the record (#452).

        Two halves, deliberately split. The ROW is marked here and now, so the
        API answers immediately and the task stops counting as in flight; the
        atomic status guard inside `TaskRepository.cancel_task` keeps a run that
        finished a moment ago from being clobbered to 'cancelled'. The UNWIND
        (stop the PVE task, destroy the half-created guest) happens in the
        cancelled coroutine's own handler and lands on the row afterwards
        through `record_cancel_outcome`, because it takes as long as Proxmox
        takes and nobody should hold an HTTP request open for it.

        Returns the task row, or None when the task id is unknown.
        """
        running_task = self._task_by_id.get(task_id)
        before = await self.task_repo.get_task(task_id)
        if before is None:
            return None
        row = await self.task_repo.cancel_task(task_id)
        # The row is marked FIRST, and only then is the coroutine cancelled.
        # Both orders stop the provision, but the other one loses the outcome:
        # the cancelled coroutine's cleanup writes `WHERE status = 'cancelled'`,
        # and it can reach that write while this one is still awaiting its own
        # UPDATE - so the unwind's result would silently match no row.
        if (
            running_task is not None
            and not running_task.done()
            and row is not None
            and row["status"] == "cancelled"
        ):
            running_task.cancel()
        if (
            running_task is None
            and before["status"] == "running"
            and row is not None
            and row["status"] == "cancelled"
        ):
            # The row says a provision was mid-flight, but this process is not
            # the one running it (a restart, most likely). Nothing will ever
            # write the real outcome, so say so rather than leaving a blank row
            # that reads like a clean cancel.
            row = await self.task_repo.record_cancel_outcome(
                task_id, error="process restarted; in-flight PVE state unknown"
            )
        return row

    async def _run(self, task_id: str, request: ProvisionRequest, actor: str) -> None:
        # `step` names the stage a failure happened at, so the task's error tells
        # an operator WHERE the provision died, not just that it did.
        step = "start"
        # What this run has already put on Proxmox, kept where the cancel
        # handler below can read it (#452). A CancelledError can arrive at any
        # await, so the unwind is driven by these rather than by `step` alone.
        vmid: int | None = None
        clone_issued = False
        inflight_upid: str | None = None
        # Set once a path has already taken the guest back (today: the fence
        # failure below, which destroys the unfenced guest and raises). The
        # generic failure handler reads it so it does NOT try to destroy a guest
        # that is already gone - which would turn a clean unwind into a spurious
        # "cleanup FAILED" on a vmid that no longer exists (#595).
        guest_unwound = False
        # The per-VM firewall this run actually wrote, for the provision record.
        # None means no fence was applied - the result says so rather than
        # leaving the question open.
        fence: DesiredGuestNetwork | None = None
        applied_rules: list[dict[str, Any]] | None = None
        try:
            await self.task_repo.update_task_status(task_id, "running")
            proxmox = self.proxmox
            if proxmox is None:
                raise RuntimeError("Proxmox not configured")

            step = "next_vmid"
            vmid = await proxmox.next_vmid(request.node)

            step = "clone"
            # Set BEFORE the call, not after: if a cancel lands while the clone
            # request is in flight we cannot know whether PVE took it, and an
            # attempted destroy of a VM that was never created is a cheap,
            # reported failure - a leaked guest is not.
            clone_issued = True
            upid = await proxmox.clone_vm(
                node=request.node,
                template_vmid=request.template_vmid,
                new_vmid=vmid,
                name=request.name,
                full=request.full,
                pool=request.pool,
                # None means "inherit the template's storage" all the way down
                # to the PVE body, which then carries no storage key at all.
                storage=request.storage,
            )
            inflight_upid = upid
            await proxmox.wait_for_task(
                request.node, upid, timeout_s=self.task_timeout_s, poll_interval=self.poll_interval
            )
            inflight_upid = None

            step = "configure"
            defaults = await provisioning_defaults(self.defaults_source)
            # Resolved BEFORE the NIC is written, because the fence decides
            # whether net0 carries `firewall=1` at all - a NIC configured
            # without it cannot be fenced afterwards without a second write.
            fence = await self._resolve_fence(defaults)
            config = self._build_config(request, defaults, fence is not None)
            if config:
                await proxmox.set_vm_config(request.node, vmid, config)

            # The fence goes on BEFORE the guest boots. A guest that comes up on
            # the guest vnet with no rules is on the operator's LAN for as long
            # as it takes to notice, and "we added the rules a second later" is
            # not a property anybody can rely on.
            if fence is not None:
                step = "fence"
                try:
                    applied_rules = await self._fence_guest(request, vmid, fence)
                except Exception as exc:
                    # LOUD, and the guest goes away. Anything else ships a
                    # machine onto the guest wire with the operator's LAN in
                    # reach, which is the one outcome the guest network exists
                    # to make impossible.
                    outcome = await self._destroy_unfenced(request, vmid)
                    guest_unwound = True
                    raise RuntimeError(
                        f"could not write the guest firewall rules ({exc}); {outcome}"
                    ) from exc

            if request.disk_gb is not None:
                # PVE resize is grow-only and refuses a shrink; we cannot know the
                # template's disk size cheaply, so a too-small request surfaces as
                # the PVE error on a failed task rather than a silent no-op.
                step = "resize_disk"
                await proxmox.resize_disk(request.node, vmid, request.disk, f"{request.disk_gb}G")

            step = "start_vm"
            start_upid = await proxmox.start_vm(request.node, vmid)
            inflight_upid = start_upid
            await proxmox.wait_for_task(
                request.node,
                start_upid,
                timeout_s=self.task_timeout_s,
                poll_interval=self.poll_interval,
            )
            inflight_upid = None

            # Best-effort only: a template without qemu-guest-agent never answers,
            # and that must not fail the provision — the inventory reconciler
            # fills the IP in on a later pass.
            step = "discover_ip"
            ip = await self._discover_ip(request.node, vmid)

            step = "record_host"
            host_id = await self._record_host(request, vmid, ip)

            # Best-effort like the IP discovery above: a tailnet that did not
            # come up is reported to the requester, never a failed provision.
            step = "tailscale_join"
            tailnet = await self._join_tailnet(request, vmid)

            result = {
                "vmid": vmid,
                "name": request.name,
                "node": request.node,
                "ip": ip,
                "host_id": host_id,
                # The login the guest was actually built with. Recorded so a
                # caller can tell the requester how to get in without
                # re-deriving (and possibly mis-stating) it.
                "ciuser": request.ciuser,
                "tailnet": tailnet,
                # What fences this guest, in full. An operator asking "is my
                # friend's box walled off" gets the ruleset that was applied,
                # not a boolean they have to trust.
                "guest_network_fence": (
                    None
                    if fence is None or applied_rules is None
                    else {"vnet": fence.vnet, "rules": applied_rules}
                ),
            }
            await self.task_repo.update_task_status(
                task_id, "succeeded", result_json=json.dumps(result)
            )
            await self._audit(actor, request, "provision", result)
        except asyncio.CancelledError:
            # CancelledError is a BaseException, so the `except Exception` below
            # never sees it - which is exactly right: a cancel must NOT be
            # recorded as a failure, and must not overwrite the 'cancelled' row
            # `cancel()` has already written. What it must do is unwind whatever
            # this run put on Proxmox, and say so on the record.
            cleanup = asyncio.create_task(
                self._cleanup_after_cancel(
                    task_id, request, actor, vmid, clone_issued, inflight_upid
                )
            )
            # Tracked (so a shutdown drains it instead of orphaning a half-run
            # destroy) and shielded (so THIS coroutine's cancellation cannot
            # take it down mid-unwind).
            self._track_task(cleanup)
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                # We are already being cancelled; the cleanup task survives on
                # its own and drain() waits for it.
                pass
            except Exception:  # pragma: no cover - the cleanup swallows its own
                logger.warning("Provision cancel cleanup raised", exc_info=True)
            raise
        except Exception as exc:
            logger.exception("Provision task %s failed at step %s", task_id, step)
            # A failure AFTER the clone leaves a guest on the node that this run
            # created and nothing else will remove - clone_vm already made VM N,
            # and a failure at (say) start_vm strands it, stopped, forever (#595:
            # reproduced live, a start_vm failure left VM 101 orphaned). A
            # failure BEFORE the clone has nothing to unwind, and the fence path
            # has already taken its guest back, so neither destroys here.
            error = f"failed at {step}: {exc}"
            if clone_issued and vmid is not None and not guest_unwound:
                error = f"{error}; {await self._destroy_after_failure(request, vmid)}"
            try:
                await self.task_repo.update_task_status(task_id, "failed", error=error)
                await self._audit(actor, request, "provision_failed", {"error": error})
            except Exception:
                logger.error("Could not mark provision task %s failed", task_id, exc_info=True)
        finally:
            self._inflight_names.discard(request.name)

    async def _cleanup_after_cancel(
        self,
        task_id: str,
        request: ProvisionRequest,
        actor: str,
        vmid: int | None,
        clone_issued: bool,
        inflight_upid: str | None,
    ) -> None:
        """Unwind a cancelled provision and record what actually happened (#452).

        Every Proxmox call here is best-effort with a DISTINCT outcome: the
        point of a cancel is that the operator knows the state of their cluster
        afterwards, so "we destroyed it", "there was nothing to destroy" and
        "we tried and failed, guest N may still be on node X" must never look
        alike on the record.
        """
        node = request.node
        outcome: dict[str, Any] = {"cancelled": True}
        error: str | None = None

        if not clone_issued or self.proxmox is None:
            outcome["cleanup"] = "nothing_created"
        else:
            proxmox = self.proxmox
            outcome["vmid"] = vmid
            if inflight_upid is not None:
                try:
                    await proxmox.stop_task(node, inflight_upid)
                    outcome["stop_task"] = "stopped"
                except Exception as exc:
                    # A PVE task that cannot be stopped (or already finished)
                    # does not stop us destroying what it made.
                    logger.warning("Could not stop PVE task %s: %s", inflight_upid, exc)
                    outcome["stop_task"] = "failed"
            else:
                outcome["stop_task"] = "not_needed"

            if vmid is None:
                outcome["cleanup"] = "nothing_created"
            else:
                try:
                    # Stop-then-destroy, waiting on both tasks: a cancel lands
                    # on a guest that may already be RUNNING, and PVE refuses
                    # to destroy one of those (#626).
                    await self._stop_then_destroy(node, vmid)
                    outcome["cleanup"] = "deleted"
                except Exception as exc:
                    logger.warning("Could not destroy guest vmid %s on %s", vmid, node)
                    outcome["cleanup"] = "failed"
                    error = (
                        f"cancelled but cleanup failed at delete_vm: {exc}; "
                        f"guest vmid {vmid} may remain on {node}"
                    )

        try:
            await self.task_repo.record_cancel_outcome(
                task_id, result_json=json.dumps(outcome), error=error
            )
        except Exception:
            logger.error(
                "Could not record the cancel outcome for provision task %s", task_id, exc_info=True
            )
        await self._audit(actor, request, "provision_cancelled", outcome)

    async def _resolve_fence(self, defaults: ProvisioningDefaults) -> DesiredGuestNetwork | None:
        """The guest network this provision must fence against, or None.

        None - do not fence - is the answer in two ordinary cases: this
        instance describes no guest network, or the guest is not being put on
        the guest vnet (an operator VM on vmbr0 must NOT get a fence that
        would cut it off).

        An EMPTY isolate list is NOT one of them. The code default is empty
        (a shipped default cannot name any particular operator's LAN - the
        public build's scrub proved that by silently rewriting one), so empty
        is what an unconfigured instance looks like, and provisioning onto
        the guest wire unfenced is the one outcome this exists to prevent.
        So: guest vnet + nothing to isolate = REFUSE, and the message says
        which setting to fill. Unusable settings raise for the same reason.
        """
        desired = await desired_from_settings(self.defaults_source)
        if desired is None:
            return None
        if (defaults.bridge or "").strip() != desired.vnet:
            return None
        if not desired.isolate_cidrs:
            raise ValueError(
                "refusing to provision onto the guest vnet "
                f"{desired.vnet!r} with no isolation CIDRs configured - set "
                "guest_network_isolate_cidrs (typically the operator LAN) "
                "before provisioning guests, or provision onto another bridge"
            )
        return desired

    async def _fence_guest(
        self,
        request: ProvisionRequest,
        vmid: int,
        fence: DesiredGuestNetwork,
    ) -> list[dict[str, Any]]:
        """Write the per-VM firewall that actually holds, and fail loudly if it does not.

        This is the enforced fence. Vnet firewall rules are the tidier place for
        the same intent, but the legacy iptables stack this estate runs stores
        them without applying them to vnet forward traffic; the per-VM (tap
        level) rules are enforced by both stacks, so they are what stands
        between a friend's machine and the operator's LAN.

        `policy_out: ACCEPT` with explicit DROPs, rather than a default-DROP
        policy, on purpose: the guest must reach the internet. What it must not
        reach is enumerated, in order, and the ACCEPTs for DHCP and DNS to the
        gateway come first because the DROPs below cover the gateway too.

        Any failure raises. A half-fenced guest is the exact thing this exists
        to prevent, and the caller destroys the guest rather than starting it.
        """
        gateway = self.sdn_gateway or gateway_for(self.proxmox)
        if gateway is None:  # pragma: no cover - the caller checked
            raise RuntimeError("Proxmox not configured")
        await gateway.set_vm_firewall_options(
            node=request.node, vmid=vmid, enable=1, policy_out="ACCEPT"
        )
        # POST in reverse, each pinned to pos=0: PVE prepends every rule create,
        # so writing the fence straight through left the gateway DROP above the
        # DNS/DHCP ACCEPTs it shadows and broke a fenced guest's DNS/DHCP (#599).
        # `applied` is recorded in the FINAL compiled order (by final_pos), which
        # is what the provision report shows an operator, not the write order.
        writes = fence_rule_writes(fence, "out")
        applied: list[dict[str, Any]] = [{} for _ in writes]
        for final_pos, body in writes:
            await gateway.create_vm_firewall_rule(node=request.node, vmid=vmid, **body)
            applied[final_pos] = body
        return applied

    async def _stop_then_destroy(self, node: str, vmid: int) -> None:
        """Stop a guest if it is running, then destroy it, waiting on both tasks.

        PVE refuses to destroy a RUNNING guest, and both `stop` and `destroy`
        are asynchronous - they answer with a UPID the moment the work is
        accepted. A cleanup that fired the destroy without stopping first, or
        that took the destroy's UPID for a finished destroy, reported a guest
        gone while it was still on the node. Cancelling a provision did exactly
        that on dev: "cancelled but cleanup failed at delete_vm: VM 101 is
        running - destroy failed", and the guest stayed up (#626).

        Raises whatever Proxmox raises on the destroy: the CALLER decides how a
        failed cleanup is reported, because a cancel and a failed provision say
        it in different words.
        """
        proxmox = self.proxmox
        if proxmox is None:  # pragma: no cover - every caller checks first
            raise RuntimeError("Proxmox not configured")
        try:
            current = await proxmox.get_vm_current(node, vmid)
            status = str((current.get("data") or current).get("status") or "")
            if status == "running":
                stop_upid = await proxmox.stop_vm(node, vmid)
                with contextlib.suppress(Exception):
                    await proxmox.wait_for_task(
                        node,
                        stop_upid,
                        timeout_s=self.task_timeout_s,
                        poll_interval=self.poll_interval,
                    )
        except Exception as exc:
            # A status read or stop that failed does not stop us trying the
            # destroy - PVE may still take a stopped-enough guest, and if it
            # refuses, the destroy's own error is what the caller reports.
            logger.warning("Could not stop guest %s on %s before destroy: %s", vmid, node, exc)

        destroy_upid = await proxmox.delete_vm(node, vmid)
        if destroy_upid:
            await proxmox.wait_for_task(
                node,
                destroy_upid,
                timeout_s=self.task_timeout_s,
                poll_interval=self.poll_interval,
            )

    async def _destroy_after_failure(self, request: ProvisionRequest, vmid: int) -> str:
        """Take back a guest a post-clone failure would otherwise strand (#595).

        Unlike :meth:`_destroy_unfenced` (whose guest is always still stopped,
        because the fence runs before the boot), a generic failure can land with
        the guest already running - a start_vm that returned before the wait
        timed out, a later step that raised. PVE refuses to destroy a running
        guest, so this stops it first. Every call is best-effort and swallows its
        own error: the point is that the recorded provision error names the
        cleanup OUTCOME, so an operator knows whether a guest is still on the
        node, rather than the cleanup itself becoming a second failure.
        """
        if self.proxmox is None:  # pragma: no cover - the caller checked clone_issued
            return f"no Proxmox client to destroy with, vmid {vmid} may remain"
        node = request.node
        try:
            await self._stop_then_destroy(node, vmid)
        except Exception as exc:
            logger.error("Could not destroy the orphaned guest %s on %s: %s", vmid, node, exc)
            return f"cleanup FAILED ({exc}), vmid {vmid} may remain on node {node}"
        return f"destroyed guest vmid {vmid}"

    async def _destroy_unfenced(self, request: ProvisionRequest, vmid: int) -> str:
        """Take back a guest whose fence could not be written.

        The guest is still stopped at this point (the fence runs before the
        start), so this is a plain destroy. If it fails, the failure rides into
        the task error by name: an unfenced guest left on the guest wire is
        something an operator must be told about in words, not a log line.
        """
        proxmox = self.proxmox
        if proxmox is None:  # pragma: no cover - the caller checked
            return "no Proxmox client to destroy with"
        try:
            await proxmox.delete_vm(request.node, vmid)
        except Exception as exc:
            logger.error("Could not destroy the unfenced guest %s: %s", vmid, exc)
            return (
                f"and the guest could NOT be destroyed ({exc}); vmid {vmid} may still "
                f"be on {request.node}, unfenced"
            )
        return "the half-made guest was destroyed"

    def _build_config(
        self,
        request: ProvisionRequest,
        defaults: ProvisioningDefaults | None = None,
        fenced: bool = False,
    ) -> dict[str, Any]:
        config: dict[str, Any] = {
            "name": request.name,
            "ciuser": request.ciuser,
            "ipconfig0": request.ipconfig0,
        }
        # net0 is touched ONLY when this instance has a default bridge (#553 C3).
        # Before C3 the template's NIC was cloned untouched, which is precisely
        # why a guest VLAN could not be enforced; with no bridge configured that
        # behaviour is preserved exactly, because an instance that has said
        # nothing about its network must not start rewriting NICs.
        net0 = defaults.net0 if defaults is not None else None
        if net0 is not None:
            # `firewall=1` on the NIC is what makes PVE attach the per-VM rules
            # to the tap at all: without it the rules are stored and inert, and
            # the guest is on the operator's LAN with a firewall page that looks
            # right.
            config["net0"] = f"{net0},firewall=1" if fenced else net0
        if request.ssh_authorized_key is not None:
            config["sshkeys"] = request.ssh_authorized_key
        if request.cores is not None:
            config["cores"] = request.cores
        if request.memory_mb is not None:
            config["memory"] = request.memory_mb
        return config

    async def _join_tailnet(self, request: ProvisionRequest, vmid: int) -> str | None:
        """Join the guest to the requester's tailnet. Returns None / 'joined' / 'failed'.

        NOT part of _build_config on purpose: PVE's cloud-init drive exposes no
        free-form user-data field over the API. The only cloud-init key that
        could carry a `tailscale up` invocation is `cicustom`, which points at a
        snippet FILE on node storage, and the PVE API cannot write snippets
        (/storage/{id}/upload takes iso/vztmpl/import only). Writing an invented
        config key would silently drop the requester's key, so the join runs
        through the guest agent after boot instead, and its outcome is reported.
        """
        key = request.tailscale_auth_key
        if key is None:
            return None
        proxmox = self.proxmox
        if proxmox is None:
            return "failed"

        # NOTHING PUT TAILSCALE IN THE GUEST. The join ran `tailscale up`
        # against a stock cloud image that has never heard of it, so the first
        # real guest recorded tailnet "failed" and no amount of retrying could
        # have helped (#628). Install it if it is missing, unless the operator
        # has turned that off for an image that ships its own.
        # Read at USE time, like every other provisioning default, so the
        # setting can be flipped without a restart and mean it.
        defaults = await provisioning_defaults(self.defaults_source)
        install_allowed = self.tailscale_install and defaults.tailscale_install
        if not await self._ensure_tailscale(request, vmid, install_allowed):
            return "failed"

        # The key never goes on the command line. Tailscale's own guidance is to
        # pass it through the environment for exactly this reason: an argv is
        # readable in the guest's process list and is echoed back inside PVE task
        # errors. So it is written to a tmpfs file, read into the environment by
        # the shell, and deleted before `tailscale up` is even invoked.
        key_path = "/run/hp-tailscale.key"
        script = (
            f'set -e; TS_AUTHKEY="$(cat {key_path})"; rm -f {key_path}; '
            f'tailscale up --auth-key="$TS_AUTHKEY" --hostname="{request.name}"'
        )
        try:
            await proxmox.agent_write_file(request.node, vmid, key_path, key)
        except Exception:
            logger.warning("Could not stage the tailnet key for vmid %s (key redacted)", vmid)
            return "failed"
        try:
            # WAIT for it. agent_exec answers with a pid, so returning "joined"
            # on the call alone reported a join that had not happened yet and
            # might never (#628) - a `tailscale up` that exits 1 on a bad key
            # looked identical to success.
            rc, _out, _err = await proxmox.agent_run(
                request.node, vmid, script, timeout_s=self.tailscale_timeout_s
            )
        except Exception:
            # Never log the exception body: a PVE error echoes the command back,
            # and the command is built around the key file.
            logger.warning("Tailnet join failed for vmid %s (key redacted)", vmid)
            # Best effort: the shell deletes the file itself, but if the exec
            # never ran the key would otherwise stay on the guest's disk.
            with contextlib.suppress(Exception):
                await proxmox.agent_exec(request.node, vmid, ["rm", "-f", key_path])
            return "failed"
        if rc != 0:
            # Same reason: the stderr of this command can quote the key back.
            logger.warning("Tailnet join for vmid %s exited %s (output withheld)", vmid, rc)
            with contextlib.suppress(Exception):
                await proxmox.agent_exec(request.node, vmid, ["rm", "-f", key_path])
            return "failed"
        return "joined"

    async def _ensure_tailscale(
        self, request: ProvisionRequest, vmid: int, install_allowed: bool
    ) -> bool:
        """Is tailscale in the guest? Install it if not. True when it is there.

        The installer is the vendor's own, which is the only thing that knows
        how to add their repo across distributions. It needs the guest to reach
        the internet - a fenced guest may leave the LAN alone but still routes
        out - and it is skipped entirely when the operator has said the image
        provides tailscale itself.
        """
        proxmox = self.proxmox
        if proxmox is None:
            return False
        try:
            rc, _out, _err = await proxmox.agent_run(
                request.node, vmid, "command -v tailscale", timeout_s=self.tailscale_timeout_s
            )
        except Exception:
            logger.warning("Could not ask vmid %s whether tailscale is installed", vmid)
            return False
        if rc == 0:
            return True
        if not install_allowed:
            logger.warning(
                "vmid %s has no tailscale and installing it is disabled; join cannot succeed", vmid
            )
            return False
        try:
            rc, _out, err = await proxmox.agent_run(
                request.node,
                vmid,
                _TAILSCALE_INSTALL_SCRIPT,
                timeout_s=self.tailscale_install_timeout_s,
            )
        except Exception as exc:
            logger.warning("Installing tailscale in vmid %s failed: %s", vmid, exc)
            return False
        if rc != 0:
            logger.warning("Installing tailscale in vmid %s exited %s: %s", vmid, rc, err[:200])
            return False
        return True

    async def _discover_ip(self, node: str, vmid: int) -> str | None:
        proxmox = self.proxmox
        if proxmox is None:
            return None
        deadline = time.monotonic() + self.ip_wait_s
        while True:
            data = await proxmox.get_vm_agent_network(node, vmid)
            ip = _first_ipv4(data)
            if ip is not None:
                return ip
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(self.ip_interval)

    async def _record_host(self, request: ProvisionRequest, vmid: int, ip: str | None) -> str:
        # source='hp_created' (not 'discovered') is the load-bearing choice: it
        # keeps refresh_inventory from stamping import_state='pending' on a guest
        # HomePilot itself created, so a provisioned VM never shows up in the
        # "unimported, please adopt" queue. managed_by records the provenance.
        return await self.repo.create_host(
            hostname=request.name,
            host_type=HOST_TYPE,
            proxmox_id=vmid,
            node=request.node,
            ip_address=ip,
            ip_source="pve" if ip else None,
            managed_by="provisioned",
            managed=True,
            source="hp_created",
            role_source="inferred",
            pve_status="running",
            status="online" if ip else "unknown",
            cpu_cores=request.cores,
            memory_mb=request.memory_mb,
            disk_gb=request.disk_gb,
            owner=request.owner,
        )

    async def _audit(
        self,
        actor: str,
        request: ProvisionRequest,
        action: str,
        details: dict[str, Any],
    ) -> None:
        try:
            await self.repo.log_audit(
                user_id=actor,
                source="provision",
                action=action,
                target_host=request.name,
                details_json=json.dumps({"node": request.node, **details}),
            )
        except Exception:
            # Audit is a record, never a gate: a failed audit write must not turn
            # a succeeded provision into a failed task.
            logger.warning("Could not write provision audit row", exc_info=True)


def _first_ipv4(payload: dict[str, Any] | None) -> str | None:
    """Pull the first non-loopback IPv4 out of a guest-agent interface listing."""
    if not payload:
        return None
    data = payload.get("data", payload)
    if isinstance(data, dict):
        data = data.get("result", data)
    if not isinstance(data, list):
        return None
    for iface in data:
        if not isinstance(iface, dict) or iface.get("name") == "lo":
            continue
        for addr in iface.get("ip-addresses") or []:
            if not isinstance(addr, dict):
                continue
            ip = str(addr.get("ip-address", ""))
            if ip and ":" not in ip and not ip.startswith("127."):
                return ip
    return None
