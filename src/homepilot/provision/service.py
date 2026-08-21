from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from typing import Any

from ..adapters.proxmox import ProxmoxClient
from ..db.repository import Repository
from ..tasks.repository import TaskRepository
from .models import ProvisionRequest

logger = logging.getLogger(__name__)

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
    ):
        self.proxmox = proxmox
        self.task_repo = task_repo
        self.repo = repo
        self.poll_interval = poll_interval
        self.task_timeout_s = task_timeout_s
        self.ip_wait_s = ip_wait_s
        self.ip_interval = ip_interval
        self._running_tasks: set[asyncio.Task[Any]] = set()
        # Names with a provision in flight IN THIS PROCESS. The DB cannot answer
        # this cheaply (a provision task carries no artifact_id, and the name only
        # appears in the result), and a duplicate would clone a second VM under
        # the same hostname.
        self._inflight_names: set[str] = set()

    def _track_task(self, task: asyncio.Task[Any]) -> None:
        # Hold a strong reference until completion: asyncio keeps only a weak one,
        # so an untracked background task can be garbage-collected mid-flight.
        self._running_tasks.add(task)
        task.add_done_callback(self._running_tasks.discard)

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
        self._track_task(asyncio.create_task(self._run(task_id, request, actor)))
        return task_id

    async def _run(self, task_id: str, request: ProvisionRequest, actor: str) -> None:
        # `step` names the stage a failure happened at, so the task's error tells
        # an operator WHERE the provision died, not just that it did.
        step = "start"
        try:
            await self.task_repo.update_task_status(task_id, "running")
            proxmox = self.proxmox
            if proxmox is None:
                raise RuntimeError("Proxmox not configured")

            step = "next_vmid"
            vmid = await proxmox.next_vmid(request.node)

            step = "clone"
            upid = await proxmox.clone_vm(
                node=request.node,
                template_vmid=request.template_vmid,
                new_vmid=vmid,
                name=request.name,
                full=request.full,
                pool=request.pool,
            )
            await proxmox.wait_for_task(
                request.node, upid, timeout_s=self.task_timeout_s, poll_interval=self.poll_interval
            )

            step = "configure"
            config = self._build_config(request)
            if config:
                await proxmox.set_vm_config(request.node, vmid, config)

            if request.disk_gb is not None:
                # PVE resize is grow-only and refuses a shrink; we cannot know the
                # template's disk size cheaply, so a too-small request surfaces as
                # the PVE error on a failed task rather than a silent no-op.
                step = "resize_disk"
                await proxmox.resize_disk(request.node, vmid, request.disk, f"{request.disk_gb}G")

            step = "start_vm"
            start_upid = await proxmox.start_vm(request.node, vmid)
            await proxmox.wait_for_task(
                request.node,
                start_upid,
                timeout_s=self.task_timeout_s,
                poll_interval=self.poll_interval,
            )

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
            }
            await self.task_repo.update_task_status(
                task_id, "succeeded", result_json=json.dumps(result)
            )
            await self._audit(actor, request, "provision", result)
        except Exception as exc:
            error = f"{step}: {exc}"
            logger.exception("Provision task %s failed at step %s", task_id, step)
            try:
                await self.task_repo.update_task_status(task_id, "failed", error=error)
                await self._audit(actor, request, "provision_failed", {"error": error})
            except Exception:
                logger.error("Could not mark provision task %s failed", task_id, exc_info=True)
        finally:
            self._inflight_names.discard(request.name)

    def _build_config(self, request: ProvisionRequest) -> dict[str, Any]:
        config: dict[str, Any] = {
            "name": request.name,
            "ciuser": request.ciuser,
            "ipconfig0": request.ipconfig0,
        }
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
            await proxmox.agent_exec(request.node, vmid, ["sh", "-c", script])
        except Exception:
            # Never log the exception body: a PVE error echoes the command back.
            logger.warning("Tailnet join failed for vmid %s (key redacted)", vmid)
            # Best effort: the shell deletes the file itself, but if the exec
            # never ran the key would otherwise stay on the guest's disk.
            with contextlib.suppress(Exception):
                await proxmox.agent_exec(request.node, vmid, ["rm", "-f", key_path])
            return "failed"
        return "joined"

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
