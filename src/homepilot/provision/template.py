"""Build the cloud-init template provisioning clones, over the API alone (#594).

The wall this exists to remove: ``provision_guest`` clones ``template_vmid``, so
a cloud-init template must ALREADY exist - and HomePilot had no way to make one.
The manual route (``qm importdisk`` / ``import-from=/absolute/path``) needs root
on the node, which HomePilot's scoped PVE token deliberately does not have
("Only root can pass arbitrary filesystem paths"), so provisioning was
undeliverable on any cluster that had never been hand-prepared.

The recipe below was validated live on dev pve1 with HomePilot's OWN non-root
token, and every step of it is an ordinary API call:

1. the target storage must declare ``import`` content - the scoped token may add
   it, and this DOES add it when it is missing (recorded on the result, and left
   in place afterwards: it is an additive declaration another job may already be
   relying on by the time we finish);
2. the cloud image is either already staged (``source_volid``) or fetched onto
   the storage by the node itself (``download_url`` -> the download-url endpoint,
   which takes iso/vztmpl/import content);
3. ``POST /nodes/{node}/qemu`` creates the shell;
4. ``scsi0 = {storage}:0,import-from={volid}`` imports the disk into it;
5. the cloud-init drive, boot order, serial console and guest agent go on;
6. ``POST .../template`` converts it.

Two properties are not negotiable, and both have gates:

* **No overwrite.** Converting to a template is one-way and a create at a taken
  vmid would land on somebody else's machine, so a vmid already in use anywhere
  in the CLUSTER is refused before anything is written.
* **No orphan.** Once the shell exists, EVERY failure path destroys it (#595
  reproduced exactly this class live for provisioning: a failure after the
  create left a half-made VM on the node forever). The recorded error names the
  cleanup outcome, so an operator knows whether anything is still there.

Why these PVE endpoints live on ``ProxmoxClient`` and not behind the
``proxmox_mcp`` library the SDN/firewall work delegates to (``adapters/pve_sdn``):
the library's own helpers cannot express this recipe. ``lifecycle.create_vm``
takes ``disk_size``/``storage`` and has no way to pass ``import-from``;
``templates.download_template`` hardcodes ``content=vztmpl`` (a qcow2 is
``import``); and ``storage.get_storage`` returns a formatted human sentence, not
the ``content`` list this must read. They are the same "needs a structured core
upstream" gap that module already documents - so the calls stay on the qemu/
storage client that already owns clone, config, resize, start and destroy.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from ..adapters.proxmox import ProxmoxClient
from ..background import DEFAULT_DRAIN_TIMEOUT, drain_tasks
from ..db.repository import Repository
from ..tasks.repository import TaskRepository
from .models import GuestTemplateRequest

logger = logging.getLogger(__name__)

# The task action. Artifactless like 'provision': it creates infrastructure
# rather than applying authored intent (migration 28 admits it).
ACTION = "create_guest_template"


class GuestTemplateConflictError(Exception):
    """A template build for the same vmid is already in flight."""


class GuestTemplateExistsError(Exception):
    """The vmid is taken. Refused rather than overwritten."""


def upid_of(result: Any) -> str | None:
    """The UPID inside a PVE response, or None when the call was synchronous.

    PVE answers some of these calls with a task id and others with ``null`` (a
    config write that needed no worker). Treating "no UPID" as an error would
    fail perfectly good runs; waiting on a non-UPID string would hang. So: a
    string that starts with ``UPID:`` is a task, anything else is not.
    """
    data = result.get("data", result) if isinstance(result, dict) else result
    if isinstance(data, str) and data.startswith("UPID:"):
        return data
    return None


class GuestTemplateService:
    """Build a cloud-init template, tracked as a 'create_guest_template' task.

    The caller gets a task id immediately; the build runs as a tracked
    background job that ALWAYS lands the record in a terminal state - the same
    contract ProvisionService carries (#386), for the same reason: a task
    stranded in 'running' never stops being "in flight".
    """

    def __init__(
        self,
        proxmox: ProxmoxClient | None,
        task_repo: TaskRepository,
        repo: Repository,
        poll_interval: float = 2.0,
        task_timeout_s: float = 600.0,
        download_timeout_s: float = 1800.0,
        defaults_source: Any = None,
    ):
        self.proxmox = proxmox
        self.task_repo = task_repo
        self.repo = repo
        self.poll_interval = poll_interval
        self.task_timeout_s = task_timeout_s
        # Its own, much longer budget: fetching a cloud image and importing it
        # into a disk are the two steps that move gigabytes, and timing them out
        # at the config-call budget would destroy a build that was working.
        self.download_timeout_s = download_timeout_s
        self.defaults_source = defaults_source
        self._running_tasks: set[asyncio.Task[Any]] = set()
        self._task_by_id: dict[str, asyncio.Task[Any]] = {}
        # vmids with a build in flight IN THIS PROCESS. Two builds onto one vmid
        # would race the create, and the loser's failure handler would destroy
        # the winner's template.
        self._inflight_vmids: set[int] = set()

    # ── task plumbing (mirrors ProvisionService) ─────────────────────────────

    def _track_task(self, task: asyncio.Task[Any], task_id: str | None = None) -> None:
        # A strong reference until completion: asyncio keeps only a weak one, so
        # an untracked background task can be garbage-collected mid-flight.
        self._running_tasks.add(task)
        if task_id is None:
            task.add_done_callback(self._running_tasks.discard)
            return
        self._task_by_id[task_id] = task

        def _done(t: asyncio.Task[Any]) -> None:
            self._running_tasks.discard(t)
            if self._task_by_id.get(task_id) is t:
                del self._task_by_id[task_id]

        task.add_done_callback(_done)

    async def drain(self, timeout: float = DEFAULT_DRAIN_TIMEOUT) -> None:
        """Let in-flight builds finish before the database goes away (#496)."""
        await drain_tasks(self._running_tasks, "guest-template build(s)", timeout)

    def is_inflight(self, vmid: int) -> bool:
        return vmid in self._inflight_vmids

    async def start(self, request: GuestTemplateRequest, actor: str = "system") -> str:
        if request.template_vmid in self._inflight_vmids:
            raise GuestTemplateConflictError(
                f"A template build for vmid {request.template_vmid} is already in flight"
            )
        self._inflight_vmids.add(request.template_vmid)
        try:
            task_id = await self.task_repo.create_task(None, ACTION)
        except Exception:
            self._inflight_vmids.discard(request.template_vmid)
            raise
        self._track_task(asyncio.create_task(self._run(task_id, request, actor)), task_id)
        return task_id

    async def cancel(self, task_id: str) -> dict[str, Any] | None:
        """Stop an in-flight build for real, and mark the record.

        The same two halves as a provision cancel (#452), for the same reason:
        marking the row while the job keeps writing to PVE is a cancel that
        cancels nothing, and the job would overwrite the row seconds later. The
        row is marked FIRST (so the caller answers immediately and the atomic
        guard in ``cancel_task`` cannot clobber a run that just finished), then
        the coroutine is cancelled and unwinds on its own clock.
        """
        running_task = self._task_by_id.get(task_id)
        before = await self.task_repo.get_task(task_id)
        if before is None:
            return None
        row = await self.task_repo.cancel_task(task_id)
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
            # The row says a build was mid-flight but this process is not the one
            # running it (a restart). Nothing will ever write the real outcome,
            # so say so rather than leaving a blank row that reads like a clean
            # cancel.
            row = await self.task_repo.record_cancel_outcome(
                task_id, error="process restarted; in-flight PVE state unknown"
            )
        return row

    # ── the build ────────────────────────────────────────────────────────────

    async def _run(self, task_id: str, request: GuestTemplateRequest, actor: str) -> None:
        # `step` names the stage a failure happened at, so the task error tells an
        # operator WHERE the build died, not just that it did.
        step = "start"
        node = request.node
        vmid = request.template_vmid
        # Set the moment the create is ISSUED, not after it returns: if the call
        # is in flight when something goes wrong we cannot know whether PVE took
        # it, and an attempted destroy of a VM that was never created is a cheap,
        # reported failure - a leaked shell is not.
        create_issued = False
        content_added = False
        source_volid = request.source_volid
        inflight_upid: str | None = None
        try:
            await self.task_repo.update_task_status(task_id, "running")
            proxmox = self.proxmox
            if proxmox is None:
                raise RuntimeError("Proxmox not configured")

            step = "check_vmid"
            if vmid in await proxmox.cluster_vmids():
                # No overwrite, ever. Converting to a template is one-way, and
                # the id may belong to somebody's running machine.
                raise GuestTemplateExistsError(
                    f"vmid {vmid} is already in use on this cluster; refusing to "
                    "overwrite it. Pick a free template_vmid (or destroy the "
                    "existing guest yourself first)."
                )

            step = "storage_content"
            content_added = await self._ensure_import_content(request.storage)

            if request.download_url is not None:
                step = "download_image"
                filename = request.image_filename
                if filename is None:  # pragma: no cover - the model guarantees one
                    raise RuntimeError("download_url carries no usable image file name")
                upid = await proxmox.download_url_to_storage(
                    node=node, storage=request.storage, url=request.download_url, filename=filename
                )
                inflight_upid = upid or None
                await self._wait(upid, node, self.download_timeout_s)
                inflight_upid = None
                source_volid = f"{request.storage}:import/{filename}"
            if source_volid is None:  # pragma: no cover - the model guarantees one
                raise RuntimeError("no image source: give source_volid or download_url")

            step = "create_vm"
            create_issued = True
            upid = await proxmox.create_vm(
                node,
                vmid,
                {
                    "name": request.name,
                    "memory": request.memory_mb,
                    "cores": request.cores,
                    "sockets": 1,
                    # A Linux cloud image. PVE's unset default is 'other', which
                    # picks weaker device defaults for a guest we know is Linux.
                    "ostype": "l26",
                    # The controller the imported disk attaches to; scsi0 below
                    # is meaningless without it.
                    "scsihw": "virtio-scsi-pci",
                },
            )
            inflight_upid = upid or None
            await self._wait(upid, node, self.task_timeout_s)
            inflight_upid = None

            step = "import_disk"
            # `{storage}:0` is PVE's "allocate a disk of the size the imported
            # image needs" form; import-from does the copy, and the config write
            # comes back as a task because it moves the image's bytes.
            result = await proxmox.set_vm_config(
                node, vmid, {"scsi0": f"{request.storage}:0,import-from={source_volid}"}
            )
            import_upid = upid_of(result)
            inflight_upid = import_upid
            if import_upid is not None:
                await proxmox.wait_for_task(
                    node,
                    import_upid,
                    timeout_s=self.download_timeout_s,
                    poll_interval=self.poll_interval,
                )
            inflight_upid = None

            step = "cloud_init"
            # Everything that makes the clone of this template actually usable:
            # the cloud-init drive PVE writes ciuser/sshkeys/ipconfig0 into, a
            # boot order that names the imported disk (a template whose boot
            # order still points at an absent CD-ROM boots nothing), a serial
            # console because that is what Ubuntu's cloud images expect, and the
            # guest agent - which is how provisioning discovers the guest's IP
            # and joins a tailnet.
            await proxmox.set_vm_config(
                node,
                vmid,
                {
                    "ide2": f"{request.storage}:cloudinit",
                    "boot": "order=scsi0",
                    "serial0": "socket",
                    "vga": "serial0",
                    "agent": "enabled=1",
                },
            )

            step = "convert_template"
            upid = await proxmox.convert_vm_to_template(node, vmid)
            inflight_upid = upid or None
            await self._wait(upid, node, self.task_timeout_s)
            inflight_upid = None

            result_payload = {
                "vmid": vmid,
                "name": request.name,
                "node": node,
                "storage": request.storage,
                "source_volid": source_volid,
                # Which of the two sources was used, stated plainly rather than
                # left to be inferred from the volid.
                "downloaded_from": request.download_url,
                # A CONFIG CHANGE this run made to the operator's cluster and did
                # not undo. It belongs on the record, not in a log line.
                "storage_import_content_added": content_added,
                "template": True,
            }
            await self.task_repo.update_task_status(
                task_id, "succeeded", result_json=json.dumps(result_payload)
            )
            await self._audit(actor, request, "guest_template_created", result_payload)
        except asyncio.CancelledError:
            # CancelledError is a BaseException, so `except Exception` below never
            # sees it - which is right: a cancel must not be recorded as a
            # failure, and must not overwrite the 'cancelled' row cancel() wrote.
            # What it MUST do is unwind what this run put on PVE.
            cleanup = asyncio.create_task(
                self._cleanup_after_cancel(
                    task_id, request, actor, create_issued, inflight_upid, content_added
                )
            )
            self._track_task(cleanup)
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                pass
            except Exception:  # pragma: no cover - the cleanup swallows its own
                logger.warning("Guest-template cancel cleanup raised", exc_info=True)
            raise
        except Exception as exc:
            logger.exception("Guest-template task %s failed at step %s", task_id, step)
            error = f"failed at {step}: {exc}"
            # A failure AFTER the create leaves a shell on the node that nothing
            # else will remove (#595's class, on this path). A failure BEFORE it
            # has nothing to unwind.
            if create_issued:
                error = f"{error}; {await self._destroy_partial(node, vmid)}"
            if content_added:
                error = f"{error}; note: 'import' content was added to storage {request.storage!r}"
            try:
                await self.task_repo.update_task_status(task_id, "failed", error=error)
                await self._audit(actor, request, "guest_template_create_failed", {"error": error})
            except Exception:
                logger.error("Could not mark template task %s failed", task_id, exc_info=True)
        finally:
            self._inflight_vmids.discard(vmid)

    async def _cleanup_after_cancel(
        self,
        task_id: str,
        request: GuestTemplateRequest,
        actor: str,
        create_issued: bool,
        inflight_upid: str | None,
        content_added: bool,
    ) -> None:
        """Unwind a cancelled build and record what actually happened.

        Every call here is best-effort with a DISTINCT outcome: the point of a
        cancel is that the operator knows the state of their cluster afterwards,
        so "we destroyed it", "there was nothing to destroy" and "we tried and
        failed, vmid N may still be there" must never look alike on the record.
        """
        node = request.node
        vmid = request.template_vmid
        outcome: dict[str, Any] = {"cancelled": True, "vmid": vmid}
        error: str | None = None

        if not create_issued or self.proxmox is None:
            outcome["cleanup"] = "nothing_created"
        else:
            if inflight_upid is not None:
                try:
                    await self.proxmox.stop_task(node, inflight_upid)
                    outcome["stop_task"] = "stopped"
                except Exception as exc:
                    logger.warning("Could not stop PVE task %s: %s", inflight_upid, exc)
                    outcome["stop_task"] = "failed"
            else:
                outcome["stop_task"] = "not_needed"
            unwound = await self._destroy_partial(node, vmid)
            if unwound.startswith("cleanup FAILED"):
                outcome["cleanup"] = "failed"
                error = f"cancelled but {unwound}"
            else:
                outcome["cleanup"] = "deleted"
        outcome["storage_import_content_added"] = content_added

        try:
            await self.task_repo.record_cancel_outcome(
                task_id, result_json=json.dumps(outcome), error=error
            )
        except Exception:
            logger.error(
                "Could not record the cancel outcome for template task %s", task_id, exc_info=True
            )
        await self._audit(actor, request, "guest_template_cancelled", outcome)

    # ── the pieces ───────────────────────────────────────────────────────────

    async def _ensure_import_content(self, storage: str) -> bool:
        """Make the storage declare `import` content. True when this had to add it.

        Why it is allowed to: with no import-capable storage the whole API-only
        path is impossible (#594 - "template creation needs root on the node or
        an `import`-content storage"), and the scoped token CAN make this one
        change. It is ADDITIVE: the existing content types are sent back with it,
        because PVE takes the whole list and dropping one would un-declare
        content the storage is already holding.

        Not undone afterwards, on purpose. It is a declaration, not a
        reservation; another job may be relying on it by the time we finish, and
        removing it could break a storage that is now serving imports. What it
        does get is a place on the RECORD, so the operator sees the change.
        """
        proxmox = self.proxmox
        if proxmox is None:  # pragma: no cover - the caller checked
            raise RuntimeError("Proxmox not configured")
        config = await proxmox.get_storage(storage)
        content = [c.strip() for c in str(config.get("content") or "").split(",") if c.strip()]
        if "import" in content:
            return False
        await proxmox.set_storage_content(storage, ",".join([*content, "import"]))
        logger.info("Added 'import' content type to storage %s", storage)
        return True

    async def _wait(self, upid: str | None, node: str, timeout_s: float) -> None:
        """Wait for a PVE task, when the call produced one.

        Several of these endpoints answer synchronously (``data: null``). Waiting
        on that would hang on a URL that is not a task; failing on it would fail
        a working build. So an absent or non-UPID answer is simply nothing to wait for.
        """
        proxmox = self.proxmox
        if proxmox is None or not upid or not upid.startswith("UPID:"):
            return
        await proxmox.wait_for_task(
            node, upid, timeout_s=timeout_s, poll_interval=self.poll_interval
        )

    async def _destroy_partial(self, node: str, vmid: int) -> str:
        """Take back the half-made template a failure would otherwise strand.

        The guest is never started on this path, so a plain destroy is enough.
        Best-effort and swallows its own error: the point is that the RECORDED
        error names the cleanup outcome, so an operator knows whether anything is
        still on the node - the cleanup must not become a second failure.
        """
        proxmox = self.proxmox
        if proxmox is None:  # pragma: no cover - the caller checked create_issued
            return f"no Proxmox client to destroy with, vmid {vmid} may remain"
        try:
            await proxmox.delete_vm(node, vmid)
        except Exception as exc:
            logger.error("Could not destroy the half-made template %s on %s: %s", vmid, node, exc)
            return f"cleanup FAILED ({exc}), vmid {vmid} may remain on node {node}"
        return f"destroyed the half-made template vmid {vmid}"

    async def _audit(
        self,
        actor: str,
        request: GuestTemplateRequest,
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
            # a succeeded build into a failed task.
            logger.warning("Could not write guest-template audit row", exc_info=True)
