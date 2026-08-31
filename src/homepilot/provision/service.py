from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
from enum import StrEnum
from typing import Any

from ..adapters.proxmox import ProxmoxClient, ProxmoxError
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
from .ip_allocation import AllocatedAddress, allocate_address
from .models import ProvisionRequest

logger = logging.getLogger(__name__)

# Tailscale's own installer: the only thing that knows how to add their
# repository across distributions, and what their docs tell you to run.
_TAILSCALE_INSTALL_URL = "https://tailscale.com/install.sh"  # nosec B105 - a URL
_TAILSCALE_INSTALL_HOST = "tailscale.com"

# Is the tailscale CLI in this guest? Its own command, so "not installed" and
# "we could not ask" stay different answers - the whole point of #642.
#
# The guest's OWN PATH is used, never one we invent. qemu-guest-agent runs as a
# systemd service and systemd hands services the standard root PATH, so naming
# the directories ourselves would be a no-op on every image this runs on - and
# on the image where it would NOT be a no-op, quietly widening the search would
# hide the fact that the guest's environment is broken. If tailscale is not
# findable, we say exactly that and let it be diagnosed.
_TAILSCALE_PROBE_SCRIPT = "command -v tailscale >/dev/null 2>&1"

# Exit codes the install script hands back so a failure can be NAMED to the
# person who asked for the join. Deliberately above the range a package manager
# or the vendor installer uses, so they cannot be confused with one.
_RC_NO_DOWNLOADER = 90
_RC_DOWNLOAD_FAILED = 91
_RC_DNS_FAILED = 92
_RC_INSTALLED_NOTHING = 93
# The guest's package manager was still busy when the wait ran out. Its own
# code, because "apt was locked" and "the install failed" send an operator to
# different places - and the first one is not a fault at all, just a guest that
# was still finishing its own boot.
_RC_PACKAGE_MANAGER_BUSY = 94

# How long to wait for the guest's own package manager to be free. Generous:
# unattended-upgrades on a fresh cloud image routinely holds the lock for
# minutes, and waiting is far cheaper than handing someone a machine with no
# tailscale on it.
_PACKAGE_LOCK_WAIT_S = 300
# The guest agent itself is forbidden to reach the network, whatever the network
# says. Proven live on dev 2026-08-29: on a Fedora guest, qemu-guest-agent runs
# as the CONFINED SELinux domain `virt_qemu_ga_t`, and a connect() from it to an
# http/https port returns EPERM before a packet leaves the guest - while DNS, and
# even TCP to 1.1.1.1:53, work fine. Every SELinux-enforcing distribution
# (Fedora, RHEL, Rocky, Alma, CentOS) confines it the same way.
#
# Without this code the operator is told "the guest could not download the
# installer ... the route out is the thing to look at" and goes hunting a network
# fault that does not exist. That is the #642 mistake in its most expensive form:
# a real failure attributed to the wrong cause, sending someone to fix the one
# thing that was already working.
_RC_AGENT_CONFINED = 94

# Four things this script exists to fix, all of them found by running the 3.6.12
# code against a real guest on the dev cluster:
#
#  1. `curl -fsSL ... | sh` CANNOT FAIL. A pipeline's exit status is the LAST
#     command's, so a download that 404s, is refused by DNS or is cut off feeds
#     `sh` an empty script and `sh` exits 0 - a failed install reported as a
#     successful one, under a `set -e` that never sees it. The old comment here
#     claimed `set -e` covered that; it did not. Fetch to a file, check the file
#     is non-empty, then run it.
#  2. curl is NOT a given. A cloud image ships the fetcher its distribution
#     chose, and the images that ship qemu-guest-agent are not the same set as
#     the images that ship curl. So the installer takes whichever fetcher the
#     guest has and says so plainly when it has none, instead of dying on a
#     missing binary and reporting "install failed".
#  3. A failed download and a failed NAME LOOKUP are different problems with
#     different fixes, and the second one is about to get commoner: the static-IP
#     work adds a nameserver setting that defaults to EMPTY, so a guest can get
#     an address and no resolver. Attributing that to "the download failed" would
#     send the operator hunting the wrong thing. The DNS verdict is only ever
#     given when the lookup was actually RUN (#642): on an image with no
#     `getent` we say the download failed, which is the part we did observe.
#  4. `sh install.sh` exiting 0 is not evidence that tailscale is installed
#     (#642 again - a verdict from a proxy signal). The same shell that ran the
#     installer looks for the binary afterwards and says so if it is not there.
_TAILSCALE_INSTALL_SCRIPT = f"""set -e
export DEBIAN_FRONTEND=noninteractive
out=/tmp/hp-tailscale-install.sh
trap 'rm -f "$out"' EXIT
fetch_failed() {{
  # SELinux first: it is the only one of the three that is not about the
  # network at all, and it looks exactly like a download failure from here.
  # `virt_qemu_ga_t` is the domain qemu-guest-agent runs in on an enforcing
  # system, and it may not open http/https - so saying "check the route out"
  # would send the operator to fix something that is not broken.
  if command -v id >/dev/null 2>&1 && id -Z 2>/dev/null | grep -q virt_qemu_ga_t; then
    exit {_RC_AGENT_CONFINED}
  fi
  if command -v getent >/dev/null 2>&1; then
    getent hosts {_TAILSCALE_INSTALL_HOST} >/dev/null 2>&1 || exit {_RC_DNS_FAILED}
  fi
  exit {_RC_DOWNLOAD_FAILED}
}}
if command -v curl >/dev/null 2>&1; then
  curl -fsSL {_TAILSCALE_INSTALL_URL} -o "$out" || fetch_failed
elif command -v wget >/dev/null 2>&1; then
  wget -nv -O "$out" {_TAILSCALE_INSTALL_URL} || fetch_failed
elif command -v python3 >/dev/null 2>&1; then
  python3 -c 'import urllib.request,sys
sys.stdout.buffer.write(urllib.request.urlopen("{_TAILSCALE_INSTALL_URL}").read())' \
    > "$out" || fetch_failed
else
  exit {_RC_NO_DOWNLOADER}
fi
[ -s "$out" ] || fetch_failed
# WAIT FOR THE PACKAGE LOCK. cloud-init finishing is not the same as apt being
# free: `apt-daily` and `unattended-upgrades` start on their own timers after
# it, so an install seconds into a guest's life races them and apt exits 100.
# Bounded, and it degrades to "carry on" on an image without flock or dpkg
# rather than refusing to install on one it cannot inspect.
if command -v dpkg >/dev/null 2>&1 && command -v flock >/dev/null 2>&1; then
  flock -w {_PACKAGE_LOCK_WAIT_S} /var/lib/dpkg/lock-frontend true \
    || exit {_RC_PACKAGE_MANAGER_BUSY}
fi
sh "$out"
command -v tailscale >/dev/null 2>&1 || exit {_RC_INSTALLED_NOTHING}
"""

# cloud-init is still running when qemu-guest-agent starts answering: the agent
# comes up early in boot and cloud-init is what writes resolv.conf and finishes
# the package database. Installing tailscale before it finishes races the
# distribution's own package lock and can run before the guest can resolve
# anything. `cloud-init status --wait` is the vendor-supported way to wait for
# it; an image without cloud-init (or one where it errored) must not block the
# join, so this only ever reports 0.
_CLOUD_INIT_WAIT_SCRIPT = (
    "command -v cloud-init >/dev/null 2>&1 || exit 0; "
    "cloud-init status --wait >/dev/null 2>&1 || true"
)

# A tailscale auth key, anywhere in a string. Guest output is never shown to
# anybody without passing through this: the key itself is substituted out by
# value, and this catches a DIFFERENT key (an older one still on the guest, one
# a distribution tool echoed) that the value substitution would miss.
_TSKEY_RE = re.compile(r"tskey-[A-Za-z0-9-]+")

# How much of the guest's own words a failure detail may carry. Long enough for
# tailscale's actual sentence ("invalid key: unknown key"), short enough that a
# task result cannot become a log dump.
_DETAIL_LIMIT = 700


def _safe_detail(text: str, key: str | None) -> str:
    """One line of guest output with every auth key taken out of it.

    The reason a join failed is the single most useful thing the requester can
    be told - "your key was already used" is actionable, "join failed" is not -
    and it comes from the guest, which is untrusted text. Two rules make it
    safe to show: the key we sent is replaced by value, and anything else
    SHAPED like a key is replaced too.
    """
    cleaned = text
    if key:
        cleaned = cleaned.replace(key, "<redacted>")
    cleaned = _TSKEY_RE.sub("<redacted>", cleaned)
    cleaned = " ".join(cleaned.split())
    # The TAIL, not the head. Tailscale's installer runs under `set -x`, so its
    # output is a command trace followed by the actual error - and keeping the
    # first N characters kept the trace and threw away the reason. A real
    # redeemer's install failed with apt exit 100 and all anyone could see was
    # "+ mkdir ... + curl ... + tee ... + curl", which sent the operator
    # hunting the template. A reason truncated before the reason is not a
    # reason (#648).
    if len(cleaned) <= _DETAIL_LIMIT:
        return cleaned
    return "..." + cleaned[-_DETAIL_LIMIT:]


# What inventory refresh writes for a qemu guest. A provisioned VM MUST use the
# same host_type + proxmox_id, because refresh_inventory matches existing rows by
# proxmox_id alone: any other convention would leave the reconciler creating a
# second, duplicate row for the same VM on its next pass.
HOST_TYPE = "qemu"


class TailnetOutcome(StrEnum):
    """What a tailnet join ESTABLISHED, which is not the same as what it wanted.

    Modelled on DriftState (#425), for the reason quoted at the top of
    `reconciler/verify.py`: "I looked and it says no" and "I could not look" are
    different answers, and a tool that gives them the same word is confidently
    wrong exactly where it matters (#642).

    JOINED   - `tailscale up` ran in the guest and exited 0.
    FAILED   - something was READ that settles it: `tailscale up` exited
               non-zero (a used or expired key, a refused device), the vendor
               installer ran and failed, or the guest has no tailscale and
               installing it is switched off. A fresh key may fix some of these.
    UNKNOWN  - nothing was established: the guest agent never answered, PVE
               refused the exec, the install or the join ran out of time (and
               may yet be finishing inside the guest). A fresh key fixes none of
               these, and the reason says which one it was.

    A join that was never asked for is not in here at all: the task result
    carries `tailnet: null`, meaning no key was supplied.
    """

    JOINED = "joined"
    FAILED = "failed"
    UNKNOWN = "unknown"


class ProvisionConflictError(Exception):
    """A provision for the same guest name is already in flight."""


class TailnetJoinConflictError(Exception):
    """A tailnet join is already running against that guest.

    Not politeness: every join stages its key at the SAME path in the guest
    (`/run/hp-tailscale.key`, deleted by the shell that reads it). Two joins in
    flight on one guest would overwrite each other's key file, so one of them
    would `tailscale up` with the other's key - or with nothing, because the
    other shell had already deleted it. Refusing the second is the only answer
    that cannot silently use the wrong secret.
    """


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
        agent_wait_s: float = 180.0,
        agent_interval: float = 3.0,
        cloud_init_wait_s: float = 300.0,
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
        # How long a guest gets to bring qemu-guest-agent up before HomePilot
        # gives up on running anything inside it. Generous because it covers a
        # cold boot of a cloud image; bounded because an image with no agent at
        # ALL never answers, and "your template has no guest agent" is a far
        # better answer than an unbounded wait.
        self.agent_wait_s = agent_wait_s
        self.agent_interval = agent_interval
        # cloud-init's own `status --wait`. Its default first boot on a cloud
        # image is tens of seconds; a package-installing one is minutes.
        self.cloud_init_wait_s = cloud_init_wait_s
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
        # Guests with a tailnet join in flight IN THIS PROCESS, as (node, vmid).
        # See TailnetJoinConflictError: two joins on one guest fight over the
        # same staged key file, so the second is refused rather than served.
        self._joining: set[tuple[str, int]] = set()

    def _reserve_join(self, node: str, vmid: int) -> bool:
        """Claim the join slot for one guest. False when somebody already holds it.

        Check-then-add with NO await between the two, which is what makes it a
        latch on a single-threaded event loop rather than a suggestion.
        """
        target = (node, vmid)
        if target in self._joining:
            return False
        self._joining.add(target)
        return True

    def _release_join(self, node: str, vmid: int) -> None:
        self._joining.discard((node, vmid))

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

    async def start_tailnet_join(
        self, node: str, vmid: int, hostname: str, key: str, actor: str = "system"
    ) -> str:
        """Retry a tailnet join against a guest that ALREADY EXISTS (#628).

        Tracked as its own 'tailnet_join' task rather than folded into
        'provision', because it is not one: nothing is cloned, nothing is
        configured, no host row is written. A failed join used to be terminal -
        the status page told the redeemer to run `tailscale up` themselves on a
        machine they had only just been handed - and the commonest cause (an
        expired or already-used key) is fixed by a FRESH key, which is exactly
        what the original provision cannot be given twice.

        `key` lives in this call's arguments and in the guest's tmpfs, and
        nowhere else: it is not in the task row, not in the audit row, and not
        in any log line.

        Raises TailnetJoinConflictError when a join is already running against
        this guest - including the one a provision is running right now.
        """
        if not self._reserve_join(node, vmid):
            raise TailnetJoinConflictError(
                f"A tailnet join is already running for vmid {vmid} on {node}. "
                "Wait for it to finish and read its result before sending another key."
            )
        try:
            task_id = await self.task_repo.create_task(None, "tailnet_join")
        except Exception:
            self._release_join(node, vmid)
            raise
        self._track_task(
            asyncio.create_task(self._run_tailnet_join(task_id, node, vmid, hostname, key, actor)),
            task_id,
        )
        return task_id

    async def _run_tailnet_join(
        self, task_id: str, node: str, vmid: int, hostname: str, key: str, actor: str
    ) -> None:
        """The tracked body of a re-join. ALWAYS lands the task in a terminal state.

        A 'failed' JOIN is a SUCCEEDED task carrying `tailnet: failed` - the same
        shape a provision reports - because the retry itself did what it was
        asked: it ran, and it found out. The task only fails when HomePilot
        could not run it at all.
        """
        result: dict[str, Any] = {"vmid": vmid, "node": node, "name": hostname}
        try:
            tailnet, detail = await self.join_tailnet(
                node=node, vmid=vmid, hostname=hostname, key=key
            )
        except asyncio.CancelledError:
            # Nothing on the cluster was created by this run, so there is nothing
            # to unwind - but the row must not be left saying "running" (#386).
            with contextlib.suppress(Exception):
                await self.task_repo.record_cancel_outcome(
                    task_id, result_json=json.dumps({**result, "cancelled": True}), error=None
                )
            raise
        except Exception as exc:
            # str(exc) and nothing more: an exception body from this path can
            # quote the command back, and the command is built around the key
            # file. `logger.exception` is safe - it is the message, not the
            # guest's echo of it - but the task row gets the class and message
            # only.
            logger.exception("Tailnet re-join task %s failed", task_id)
            with contextlib.suppress(Exception):
                await self.task_repo.update_task_status(
                    task_id,
                    "failed",
                    result_json=json.dumps(
                        {
                            **result,
                            # The retry did not establish anything, so it must not
                            # say "failed" as if it had asked and been told no
                            # (#642). "unknown" is the honest word for a join that
                            # never ran.
                            "tailnet": "unknown",
                            "tailnet_detail": _safe_detail(str(exc), key)
                            or "The re-join could not be run.",
                        }
                    ),
                    error=_safe_detail(str(exc), key),
                )
            return
        finally:
            self._release_join(node, vmid)
        result["tailnet"] = tailnet
        result["tailnet_detail"] = detail or None
        await self.task_repo.update_task_status(
            task_id, "succeeded", result_json=json.dumps(result)
        )
        try:
            await self.repo.log_audit(
                user_id=actor,
                source="provision",
                action="tailnet_join",
                target_host=hostname,
                # The key is NOT here, and neither is anything derived from it.
                details_json=json.dumps({"node": node, "vmid": vmid, "tailnet": tailnet}),
            )
        except Exception:
            logger.warning("Could not write tailnet re-join audit row", exc_info=True)

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
        unwind_cleanup = ""
        # The per-VM firewall this run actually wrote, for the provision record.
        # None means no fence was applied - the result says so rather than
        # leaving the question open.
        fence: DesiredGuestNetwork | None = None
        applied_rules: list[dict[str, Any]] | None = None
        # The address this run allocated for the guest (#630), or None when the
        # guest is addressed some other way (an explicit static, DHCP mode, a
        # guest that is not on the guest network at all).
        allocation: AllocatedAddress | None = None
        try:
            await self.task_repo.update_task_status(task_id, "running")
            proxmox = self.proxmox
            if proxmox is None:
                raise RuntimeError("Proxmox not configured")

            # EVERYTHING that can refuse this provision happens BEFORE the
            # clone. The settings read, the fence decision and the address
            # allocation are all reads, and each of them can say no - a
            # subnet with no free address, a guest vnet with no isolate list.
            # Refusing after the clone would mean a guest on the node that
            # nobody asked for and that only the unwind path removes (#595,
            # #630).
            step = "settings"
            defaults = await provisioning_defaults(self.defaults_source)
            # Resolved BEFORE the NIC is written, because the fence decides
            # whether net0 carries `firewall=1` at all - a NIC configured
            # without it cannot be fenced afterwards without a second write.
            fence = await self._resolve_fence(defaults)

            step = "allocate_ip"
            allocation = await self._allocate_address(defaults, fence, request.ipconfig0)

            step = "next_vmid"
            vmid = await self._next_vmid(request.node, defaults)

            step = "clone"
            # Set BEFORE the call, not after: if a cancel lands while the clone
            # request is in flight we cannot know whether PVE took it, and an
            # attempted destroy of a VM that was never created is a cheap,
            # reported failure - a leaked guest is not.
            clone_issued = True
            try:
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
            except ProxmoxError as exc:
                # PVE authorises BEFORE it acts, so a refused request is one
                # that never ran: nothing was created and there is nothing to
                # take back. Every other failure - a timeout, a 500, a dropped
                # connection - leaves the clone's fate genuinely unknown, which
                # is the case `clone_issued = True` above exists for. Collapsing
                # the two made a stale write token (401, the #625 shape) look
                # like a guest that might be stranded, so the friend's invite
                # stayed burned for a request PVE had refused outright.
                if exc.status_code in (401, 403):
                    clone_issued = False
                raise
            inflight_upid = upid
            await proxmox.wait_for_task(
                request.node, upid, timeout_s=self.task_timeout_s, poll_interval=self.poll_interval
            )
            inflight_upid = None

            step = "configure"
            config = self._build_config(request, defaults, fence is not None, allocation)
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
                    # Prose in a raised message cannot be read by anything: the
                    # verdict is recorded structurally too, so a caller can
                    # establish whether a guest remains instead of parsing
                    # English (#648 tranche 9).
                    unwind_cleanup = (
                        "deleted" if outcome == "the half-made guest was destroyed" else "failed"
                    )
                    raise RuntimeError(
                        f"could not write the guest firewall rules ({exc}); {outcome}"
                    ) from exc

            if request.disk_gb is not None:
                step = "resize_disk"
                await self._resize_disk(request, vmid)

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

            step = "discover_ip"
            ip: str | None
            if allocation is not None:
                # We CHOSE this address and wrote it into cloud-init, so there
                # is nothing to discover and nothing to wait for (#630). This is
                # the half of the fix the friend actually sees: a bare cloud
                # image may not run qemu-guest-agent at all, and before this the
                # portal showed such a guest "no address yet" forever.
                ip = allocation.address
            else:
                # Best-effort only: a template without qemu-guest-agent never
                # answers, and that must not fail the provision — the inventory
                # reconciler fills the IP in on a later pass.
                ip = await self._discover_ip(request.node, vmid)

            step = "record_host"
            host_id = await self._record_host(request, vmid, ip)

            # Best-effort like the IP discovery above: a tailnet that did not
            # come up is reported to the requester, never a failed provision.
            step = "tailscale_join"
            tailnet, tailnet_detail = await self._join_tailnet(request, vmid)

            result = {
                "vmid": vmid,
                "name": request.name,
                "node": request.node,
                "ip": ip,
                # How the guest was addressed, in the guest's own words (#630).
                # "ip=dhcp" on this line is now a STATEMENT - this instance was
                # asked to leave the address to a DHCP server - rather than the
                # unexamined default it used to be.
                "ipconfig0": allocation.ipconfig0 if allocation else request.ipconfig0,
                "host_id": host_id,
                # The login the guest was actually built with. Recorded so a
                # caller can tell the requester how to get in without
                # re-deriving (and possibly mis-stating) it.
                "ciuser": request.ciuser,
                "tailnet": tailnet,
                # WHY the join did not happen, in the requester's language and
                # with every auth key scrubbed out. A bare "failed" is what the
                # first live run left behind, and nobody - not the redeemer, not
                # the operator reading the task row - could act on it.
                "tailnet_detail": tailnet_detail or None,
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
            # The CANCEL path has always recorded its unwind structurally -
            # nothing_created / deleted / failed - precisely so an operator can
            # tell "we took it back" from "a guest may still be on the node".
            # The FAILURE path, doing the identical unwind, said it only in
            # English inside `error`, so nothing downstream could read it and the
            # portal could not tell whether a failed build had left a machine
            # behind (#648 tranche 9, and what makes #625 safe).
            failure_outcome: dict[str, Any] = {"failed": True, "step": step}
            if clone_issued and vmid is not None and not guest_unwound:
                failure_outcome["vmid"] = vmid
                cleanup_state, note = await self._destroy_after_failure(request, vmid)
                failure_outcome["cleanup"] = cleanup_state
                error = f"{error}; {note}"
            elif guest_unwound:
                failure_outcome["vmid"] = vmid
                failure_outcome["cleanup"] = unwind_cleanup or "failed"
            else:
                failure_outcome["cleanup"] = "nothing_created"
            try:
                await self.task_repo.update_task_status(
                    task_id, "failed", error=error, result_json=json.dumps(failure_outcome)
                )
                await self._audit(
                    actor, request, "provision_failed", {"error": error, **failure_outcome}
                )
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

    async def _allocate_address(
        self,
        defaults: ProvisioningDefaults,
        fence: DesiredGuestNetwork | None,
        requested_ipconfig: str,
    ) -> AllocatedAddress | None:
        """The address this guest gets, or None to leave ipconfig0 alone (#630).

        Four ways to get None, and each is a deliberate "somebody else already
        decided this":

        * the request (or provision_default_ipconfig) names a concrete
          ipconfig - an operator-written static WINS, always;
        * provision_ip_mode is 'dhcp' - the operator has said something on the
          wire answers, and the pre-#630 behaviour is restored exactly;
        * this instance describes no guest network, or the guest is going onto
          another bridge - there is no subnet to allocate out of, and an
          operator VM on vmbr0 must never be handed a guest-subnet address.
          (This is the same condition the fence uses, and it is READ from the
          fence rather than recomputed, so the guest that gets an address and
          the guest that gets rules can never be two different guests.)

        Anything else allocates, and a failure to allocate RAISES - before the
        clone. Falling back to ip=dhcp here would rebuild the exact silent gap
        this exists to close.
        """
        if (requested_ipconfig or "").strip() != "ip=dhcp":
            return None
        if not defaults.allocates_addresses:
            return None
        if fence is None:
            return None
        return await allocate_address(self.proxmox, fence, defaults.nameserver)

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

    async def _next_vmid(self, node: str, defaults: ProvisioningDefaults) -> int:
        """The VMID for a new guest - never one that has been used before.

        PVE's `/cluster/nextid` hands back the LOWEST free id, so destroying a
        guest and provisioning another gives the new machine the dead one's
        number. `hosts` then carries two rows for one id, and on prod a third
        from an unrelated machine imported months earlier - which is how a live
        guest came to be marked absent three minutes after it was built (#648).

        With a range configured, ids are taken HIGHEST-FIRST inside it, so a
        guest's number is never reused and can never be one of the operator's
        own machines. Without one, this is exactly the previous behaviour.
        """
        proxmox = self.proxmox
        if proxmox is None:  # pragma: no cover - the caller checked
            raise RuntimeError("Proxmox not configured")
        span = (defaults.vmid_range or "").strip()
        if not span:
            return int(await proxmox.next_vmid(node))
        low_s, _, high_s = span.partition("-")
        low, high = int(low_s), int(high_s)
        taken = await proxmox.cluster_vmids()
        used = [v for v in taken if low <= v <= high]
        candidate = (max(used) + 1) if used else low
        if candidate > high:
            # REFUSE, loudly. Falling back to lowest-free here would quietly
            # reintroduce the reuse this range exists to prevent, on the day
            # the range fills - which is the worst day to discover it.
            raise RuntimeError(
                f"the guest VMID range {span} is full: {len(used)} id(s) in use and the "
                f"highest is {max(used)}. Widen provision_vmid_range before provisioning "
                "another guest."
            )
        return candidate

    async def _resize_disk(self, request: ProvisionRequest, vmid: int) -> None:
        """Grow the clone's disk to the requested size, and WAIT to find out.

        Three things were wrong here at once, and a real redeemer got all of
        them (#648). PVE resize is a TASK: `resize_disk` answers with a UPID, so
        firing it and moving on is "acceptance is not completion" - the fifth
        site of that mistake in this codebase. PVE also refuses to SHRINK, and
        the old comment justified not checking by saying the template's size
        could not be known cheaply. It can: the guest's own config carries it,
        and this method has just cloned that guest.

        So: a request SMALLER than the disk the template gave is not an error
        and not an attempt - the machine already exceeds what was promised, and
        the invite that promised less is the thing to fix. Anything larger is
        attempted and WAITED for, and a refusal fails the provision rather than
        handing someone a disk they were told they would not get.
        """
        proxmox = self.proxmox
        if proxmox is None:  # pragma: no cover - the caller checked
            return
        if request.disk_gb is None:  # pragma: no cover - the caller checked
            return
        wanted_bytes = request.disk_gb * 1024 * 1024 * 1024
        current = await self._disk_size_bytes(request.node, vmid, request.disk)
        if current is not None and wanted_bytes <= current:
            logger.info(
                "vmid %s already has %d bytes on %s, at or above the %dG asked for; "
                "not resizing (PVE cannot shrink)",
                vmid,
                current,
                request.disk,
                request.disk_gb,
            )
            return
        result = await proxmox.resize_disk(request.node, vmid, request.disk, f"{request.disk_gb}G")
        upid = proxmox.upid_of(result)
        if upid:
            await proxmox.wait_for_task(
                request.node,
                upid,
                timeout_s=self.task_timeout_s,
                poll_interval=self.poll_interval,
            )

    async def _disk_size_bytes(self, node: str, vmid: int, disk: str) -> int | None:
        """The size PVE currently reports for one of a guest's disks, in bytes.

        None when it cannot be read or parsed - and None means "do not decide",
        so an unreadable config lets the resize be attempted rather than
        silently skipped.
        """
        proxmox = self.proxmox
        if proxmox is None:  # pragma: no cover - the caller checked
            return None
        try:
            config = await proxmox.get_vm_config(node, vmid)
        except Exception as exc:
            logger.warning("Could not read vmid %s config to size %s: %s", vmid, disk, exc)
            return None
        data = config.get("data", config) if isinstance(config, dict) else {}
        spec = str((data or {}).get(disk) or "")
        match = re.search(r"\bsize=(\d+)([KMGT]?)", spec)
        if not match:
            return None
        units = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}
        return int(match.group(1)) * units[match.group(2)]

    async def _destroy_after_failure(self, request: ProvisionRequest, vmid: int) -> tuple[str, str]:
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
            return "failed", f"no Proxmox client to destroy with, vmid {vmid} may remain"
        node = request.node
        try:
            await self._stop_then_destroy(node, vmid)
        except Exception as exc:
            logger.error("Could not destroy the orphaned guest %s on %s: %s", vmid, node, exc)
            return "failed", f"cleanup FAILED ({exc}), vmid {vmid} may remain on node {node}"
        return "deleted", f"destroyed guest vmid {vmid}"

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
            # WAIT for it. delete_vm answers with a UPID - the destroy has been
            # accepted, not done - and this sentence is the assurance that an
            # UNFENCED guest is off the guest wire. Claiming that off an
            # acceptance is the #626 mistake on the one path where being wrong
            # means a guest nobody has walled off is still running (#642).
            destroy_upid = await proxmox.delete_vm(request.node, vmid)
            if destroy_upid:
                await proxmox.wait_for_task(
                    request.node,
                    destroy_upid,
                    timeout_s=self.task_timeout_s,
                    poll_interval=self.poll_interval,
                )
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
        allocation: AllocatedAddress | None = None,
    ) -> dict[str, Any]:
        config: dict[str, Any] = {
            "name": request.name,
            "ciuser": request.ciuser,
            # The allocated address REPLACES ip=dhcp (#630). It only ever
            # exists when the request asked for dhcp and this instance
            # allocates, so an explicit ipconfig cannot be overwritten here.
            "ipconfig0": allocation.ipconfig0 if allocation else request.ipconfig0,
        }
        if allocation is not None and allocation.nameserver:
            # An address with no resolver is half a network. Nothing hands one
            # out on a subnet with no DHCP server, so cloud-init has to carry it.
            config["nameserver"] = allocation.nameserver
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

    async def _join_tailnet(self, request: ProvisionRequest, vmid: int) -> tuple[str | None, str]:
        """The provision run's tailnet join: (None | 'joined' | 'failed', detail).

        A thin adapter over `join_tailnet`, which is the reusable one - a join
        that failed can be retried against the same guest later, with a fresh
        key, without re-provisioning it (#628 second half).

        Nothing raised in here may reach the caller. The guest EXISTS by this
        point - cloned, configured, fenced, booted and written to the inventory -
        and the join is the last, best-effort step; letting an exception out of
        it would turn a built machine into a 'failed' provision and send the
        cleanup path at a guest somebody is about to be given. The join's own
        failures already come back as ('failed', reason); this catches the ones
        it did not anticipate and says so rather than guessing.
        """
        if request.tailscale_auth_key is None:
            return None, ""
        if not self._reserve_join(request.node, vmid):  # pragma: no cover - a fresh vmid
            return "failed", "A tailnet join is already running for this guest."
        try:
            return await self.join_tailnet(
                node=request.node,
                vmid=vmid,
                hostname=request.name,
                key=request.tailscale_auth_key,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Tailnet join for vmid %s raised", vmid)
            # "unknown", not "failed": nothing was established about this guest's
            # tailnet, and a confident "failed" would be a verdict from a read
            # that never completed (#642).
            return "unknown", _safe_detail(
                f"The tailnet join could not be completed: {exc}", request.tailscale_auth_key
            )
        finally:
            self._release_join(request.node, vmid)

    async def join_tailnet(self, node: str, vmid: int, hostname: str, key: str) -> tuple[str, str]:
        """Join ONE guest to a tailnet with ONE key. Returns (TailnetOutcome, detail).

        NOT part of _build_config on purpose: PVE's cloud-init drive exposes no
        free-form user-data field over the API. The only cloud-init key that
        could carry a `tailscale up` invocation is `cicustom`, which points at a
        snippet FILE on node storage, and the PVE API cannot write snippets
        (/storage/{id}/upload takes iso/vztmpl/import only). Writing an invented
        config key would silently drop the requester's key, so the join runs
        through the guest agent after boot instead, and its outcome is reported.

        `detail` is the reason, in words the requester can act on ("your key was
        already used"), with every auth key scrubbed out of it. "failed" with no
        reason at all is what the first live run of this code gave the operator,
        and it cost a rebuilt guest to find out why.
        """
        proxmox = self.proxmox
        if proxmox is None:
            # UNKNOWN, not FAILED: nothing was asked of the guest, so nothing
            # about its tailnet was established (#642).
            return TailnetOutcome.UNKNOWN, "Proxmox is not configured on this HomePilot."

        # NOTHING PUT TAILSCALE IN THE GUEST. The join ran `tailscale up`
        # against a stock cloud image that has never heard of it, so the first
        # real guest recorded tailnet "failed" and no amount of retrying could
        # have helped (#628). Install it if it is missing, unless the operator
        # has turned that off for an image that ships its own.
        # Read at USE time, like every other provisioning default, so the
        # setting can be flipped without a restart and mean it.
        defaults = await provisioning_defaults(self.defaults_source)
        install_allowed = self.tailscale_install and defaults.tailscale_install
        outcome, detail = await self._ensure_tailscale(node, vmid, install_allowed)
        if outcome is not None:
            return outcome, detail

        # The key never goes on the command line. Tailscale's own guidance is to
        # pass it through the environment for exactly this reason: an argv is
        # readable in the guest's process list and is echoed back inside PVE task
        # errors. So it is written to a tmpfs file, read into the environment by
        # the shell, and deleted before `tailscale up` is even invoked.
        key_path = "/run/hp-tailscale.key"
        script = (
            f'set -e; TS_AUTHKEY="$(cat {key_path})"; rm -f {key_path}; '
            f'tailscale up --auth-key="$TS_AUTHKEY" --hostname="{hostname}"'
        )
        try:
            await proxmox.agent_write_file(node, vmid, key_path, key)
        except Exception:
            logger.warning("Could not stage the tailnet key for vmid %s (key redacted)", vmid)
            return (
                TailnetOutcome.UNKNOWN,
                "The auth key could not be written into the guest, so the join never ran.",
            )
        try:
            # WAIT for it. agent_exec answers with a pid, so returning "joined"
            # on the call alone reported a join that had not happened yet and
            # might never (#628) - a `tailscale up` that exits 1 on a bad key
            # looked identical to success.
            rc, out, err = await proxmox.agent_run(
                node, vmid, script, timeout_s=self.tailscale_timeout_s
            )
        except TimeoutError:
            # UNKNOWN on purpose: `tailscale up` is still running in there and
            # may well succeed a second after we stopped watching. Calling that
            # "failed" would send the redeemer after a fresh key they do not
            # need - and would burn the one they just used.
            await self._shred_key_file(node, vmid, key_path)
            return (
                TailnetOutcome.UNKNOWN,
                f"`tailscale up` had not finished after {self.tailscale_timeout_s:.0f}s. It may "
                "still be running inside the guest; check the machine before sending a new key.",
            )
        except Exception:
            # Never log the exception body: a PVE error echoes the command back,
            # and the command is built around the key file.
            logger.warning("Tailnet join failed for vmid %s (key redacted)", vmid)
            # Best effort: the shell deletes the file itself, but if the exec
            # never ran the key would otherwise stay on the guest's disk.
            await self._shred_key_file(node, vmid, key_path)
            return (
                TailnetOutcome.UNKNOWN,
                "The guest agent would not run `tailscale up`, so nothing was tried.",
            )
        if rc != 0:
            # Same reason: the stderr of this command can quote the key back, so
            # it goes through _safe_detail before anyone sees it.
            logger.warning("Tailnet join for vmid %s exited %s (output withheld)", vmid, rc)
            await self._shred_key_file(node, vmid, key_path)
            reason = _safe_detail(err or out, key)
            return TailnetOutcome.FAILED, (
                f"`tailscale up` exited {rc}: {reason}"
                if reason
                else f"`tailscale up` exited {rc} and said nothing."
            )
        # rc 0 from `tailscale up` is a COMPLETION, not an acceptance: the
        # command blocks until the backend reaches Running or gives up, unlike
        # the guest-agent pid and the PVE UPID this code has twice mistaken for
        # an outcome. What it does not prove is anything about the tailnet's own
        # side of the transaction, which is the requester's to check.
        return TailnetOutcome.JOINED, ""

    async def _shred_key_file(self, node: str, vmid: int, key_path: str) -> None:
        """Make sure the staged key file is gone, and WAIT to find out.

        `agent_exec` answers with a pid, not a result: firing a `rm` and walking
        away is the same "acceptance is not completion" mistake the join itself
        was built on, and what is left behind here is the requester's auth key
        on a disk they never asked us to write it to.
        """
        proxmox = self.proxmox
        if proxmox is None:  # pragma: no cover - the caller checked
            return
        try:
            rc, _out, _err = await proxmox.agent_run(
                node, vmid, f"rm -f {key_path}", timeout_s=self.tailscale_timeout_s
            )
        except Exception:
            logger.warning(
                "Could not remove the staged tailnet key from vmid %s; it may remain at %s",
                vmid,
                key_path,
            )
            return
        if rc != 0:
            logger.warning(
                "Removing the staged tailnet key from vmid %s exited %s; it may remain at %s",
                vmid,
                rc,
                key_path,
            )

    async def _ensure_tailscale(
        self, node: str, vmid: int, install_allowed: bool
    ) -> tuple[TailnetOutcome | None, str]:
        """Is tailscale in the guest? Install it if not.

        Returns `(None, "")` when the binary is THERE - the one answer that lets
        the join go ahead. Anything else is the outcome to report and the reason
        for it, split the way #642 asks: FAILED when something was read that
        settles it, UNKNOWN when nothing could be read at all.

        The installer is the vendor's own, which is the only thing that knows
        how to add their repo across distributions. It needs the guest to reach
        the internet, which a fenced guest MAY NOT: whether it does is a
        property of the operator's guest network, not of the fence, and on the
        dev cluster it does not (curl gets "Could not connect to server" to
        tailscale.com:443 while name resolution works). So the installer's
        failure names which of the two it was, and the join is skipped entirely
        when the operator has said the image provides tailscale itself.
        """
        proxmox = self.proxmox
        if proxmox is None:  # pragma: no cover - the caller checked
            return TailnetOutcome.UNKNOWN, "Proxmox is not configured on this HomePilot."

        # The guest agent is the whole channel. Asking it for anything before it
        # answers is what the first live run of the 3.6.12 code did: the guest
        # booted, an IP came back from one agent call, `command -v tailscale`
        # raised on the next, and the provision recorded tailnet "failed" 28
        # seconds after it started. Waiting is the fix; a BOUNDED wait, because
        # an image with no qemu-guest-agent at all never answers and the
        # requester must be told that rather than left watching a spinner.
        if not await self._wait_for_agent(node, vmid):
            # ASK WHY before naming a cause. Silence from the agent has more than
            # one explanation and they send an operator to entirely different
            # places: a template without the package, a guest that is switched
            # off, or - the case this was written for - a machine that no longer
            # exists at all. A redeemer retried a join against a guest destroyed
            # three days earlier and was told his TEMPLATE needed
            # qemu-guest-agent, on a template whose agent had demonstrably run
            # commands. The operator then went looking at the image. #642's
            # shape: a conclusion drawn from a read that only established
            # silence.
            return TailnetOutcome.UNKNOWN, await self._why_no_agent(node, vmid)

        try:
            rc, _out, _err = await proxmox.agent_run(
                node, vmid, _TAILSCALE_PROBE_SCRIPT, timeout_s=self.tailscale_timeout_s
            )
        except Exception as exc:
            # Safe to name: this command carries no key, and "we could not ask"
            # with no reason attached is exactly what made the first live
            # failure undiagnosable from the logs. Some builds of
            # qemu-guest-agent ship with guest-exec switched off entirely, and
            # that refusal arrives here - it is a different problem from "the
            # guest has no tailscale" and must not be reported as one.
            logger.warning("Could not ask vmid %s whether tailscale is installed: %s", vmid, exc)
            return TailnetOutcome.UNKNOWN, _safe_detail(
                "The guest agent answered a ping but would not run a command: "
                f"{exc}. Some images ship qemu-guest-agent with guest-exec disabled.",
                None,
            )
        if rc == 0:
            return None, ""
        if not install_allowed:
            logger.warning(
                "vmid %s has no tailscale and installing it is disabled; join cannot succeed", vmid
            )
            # FAILED, not UNKNOWN: we asked the guest and it told us there is no
            # tailscale, and the operator has told us not to put one there.
            # Both halves were read; the verdict is settled.
            return TailnetOutcome.FAILED, (
                "The guest has no tailscale and installing it is switched off "
                "(provision_tailscale_install)."
            )

        # cloud-init is usually still running at this point; the installer must
        # not race it for the package lock or start before DNS exists.
        await self._wait_for_cloud_init(node, vmid)

        try:
            rc, _out, err = await proxmox.agent_run(
                node,
                vmid,
                _TAILSCALE_INSTALL_SCRIPT,
                timeout_s=self.tailscale_install_timeout_s,
            )
        except TimeoutError:
            # The installer is still going in there - a cold cloud image pulling
            # a repo can outlast our patience without failing. Nothing is
            # settled, so nothing is asserted.
            return TailnetOutcome.UNKNOWN, (
                "Installing tailscale had not finished after "
                f"{self.tailscale_install_timeout_s:.0f}s. It may still be running in the guest."
            )
        except Exception as exc:
            logger.warning("Installing tailscale in vmid %s failed: %s", vmid, exc)
            return (
                TailnetOutcome.UNKNOWN,
                "The guest agent would not run the tailscale installer.",
            )
        # The fetcher's own words, whenever it left any. The named exit codes
        # below say WHICH stage failed; only the guest can say why, and
        # "Connection timed out" and "SSL certificate problem" send an operator
        # to two different places. `curl -fsSL` keeps -S precisely so it speaks
        # up, and wget runs at -nv for the same reason.
        said = _safe_detail(err, None)
        because = f" The guest said: {said}" if said else ""
        if rc == _RC_NO_DOWNLOADER:
            return TailnetOutcome.FAILED, (
                "The guest has no curl, wget or python3, so the tailscale installer could "
                "not be fetched. Install one of them in the guest, or use an image that "
                "ships tailscale."
            )
        if rc == _RC_AGENT_CONFINED:
            # NOT a network fault, and the reason must not read like one.
            # Proven live: TCP from this guest to 1.1.1.1:53 succeeds while
            # :443 returns EPERM, because the agent's SELinux domain forbids
            # it - so the route out is fine and there is nothing to fix there.
            return TailnetOutcome.FAILED, (
                "The guest's qemu-guest-agent is confined by SELinux (domain "
                "virt_qemu_ga_t) and is not allowed to open http/https, so it cannot fetch "
                "the tailscale installer. The guest's own network is fine - this is a guest "
                "policy limit, not a route. Put tailscale in the image, or install it in the "
                "guest yourself; every SELinux-enforcing distribution (Fedora, RHEL, Rocky, "
                "Alma, CentOS) confines the agent this way." + because
            )
        if rc == _RC_DNS_FAILED:
            # The one case the installer probed for itself. Named separately
            # because its fix is a resolver, not a route - and because a guest
            # with a static address and no nameserver is about to become a
            # commoner shape than it is today.
            return TailnetOutcome.FAILED, (
                f"The guest could not resolve {_TAILSCALE_INSTALL_HOST}: it has no working DNS. "
                "Check the guest's nameserver (a static address with no resolver does this)."
                + because
            )
        if rc == _RC_DOWNLOAD_FAILED:
            return TailnetOutcome.FAILED, (
                "The guest could not download the tailscale installer. Name resolution was "
                "not shown to be the problem, so the route out is the thing to look at." + because
            )
        if rc == _RC_INSTALLED_NOTHING:
            # The vendor installer exited 0 and left no tailscale behind. Before
            # this check the join went on to run `tailscale up` against a guest
            # that had never gained the binary, off an exit code that meant
            # nothing (#642).
            return TailnetOutcome.FAILED, (
                "The tailscale installer ran and exited cleanly, but `tailscale` is still "
                "not on the guest's PATH. Nothing here says why - the vendor installer may "
                "not support this distribution, or the guest's own PATH may be wrong."
            )
        if rc != 0:
            # The installer's own words. It never sees the auth key, but it goes
            # through the same scrub as everything else quoted back from a guest.
            logger.warning("Installing tailscale in vmid %s exited %s: %s", vmid, rc, err[:200])
            reason = _safe_detail(err, None)
            return TailnetOutcome.FAILED, (
                f"Installing tailscale exited {rc}: {reason}"
                if reason
                else f"Installing tailscale exited {rc}."
            )
        return None, ""

    async def _why_no_agent(self, node: str, vmid: int) -> str:
        """Why the guest agent said nothing, in the redeemer's own words.

        Only what can be ESTABLISHED. When the cluster cannot be asked either,
        the answer says the agent did not answer and stops there rather than
        naming a cause it has not checked.
        """
        proxmox = self.proxmox
        if proxmox is None:  # pragma: no cover - the caller checked
            return "The guest's qemu-guest-agent never answered."
        try:
            current = await proxmox.get_vm_current(node, vmid)
        except ProxmoxError as exc:
            if exc.status_code in (404, 500, 501, 595):
                # PVE answers a missing guest with an error, not an empty
                # record. This is the destroyed-machine case.
                return (
                    f"This machine no longer exists on {node} - there is nothing to join to "
                    "a tailnet. If you expected it to be here, ask the operator; a new "
                    "invite gets you a fresh machine."
                )
            return (
                "The guest's qemu-guest-agent never answered, and the cluster could not be "
                f"asked why ({exc})."
            )
        except Exception as exc:
            return (
                "The guest's qemu-guest-agent never answered, and the cluster could not be "
                f"asked why ({exc})."
            )
        status = str((current.get("data") or current).get("status") or "").strip()
        if status and status != "running":
            return (
                f"This machine is {status}, so nothing inside it can be reached. Start it "
                "and try again."
            )
        # It is here and running, so the template really is the thing to look at.
        return (
            "The guest's qemu-guest-agent never answered, so HomePilot could not run "
            "anything inside it. The machine is running, so the template needs "
            "qemu-guest-agent installed, started, and allowed to run commands."
        )

    async def _wait_for_agent(self, node: str, vmid: int) -> bool:
        """Poll until qemu-guest-agent answers, or the wait runs out.

        `agent_ping` and nothing else: PVE happily accepts `agent: enabled=1` on
        a guest whose OS has no agent installed, so the config says yes and the
        guest says nothing. Only a ping that comes back proves a command can run.
        """
        proxmox = self.proxmox
        if proxmox is None:  # pragma: no cover - the caller checked
            return False
        deadline = time.monotonic() + self.agent_wait_s
        while True:
            if await proxmox.agent_ping(node, vmid):
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(self.agent_interval)

    async def _wait_for_cloud_init(self, node: str, vmid: int) -> None:
        """Best effort: let cloud-init finish before touching the package manager.

        Never fatal. An image with no cloud-init, one where cloud-init errored,
        and one that simply takes longer than we are willing to wait all end the
        same way - we go on and let the installer report what actually happens.
        """
        proxmox = self.proxmox
        if proxmox is None:  # pragma: no cover - the caller checked
            return
        try:
            await proxmox.agent_run(
                node, vmid, _CLOUD_INIT_WAIT_SCRIPT, timeout_s=self.cloud_init_wait_s
            )
        except Exception as exc:
            logger.info("Not waiting for cloud-init in vmid %s: %s", vmid, exc)

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


class TailnetJoinTargetError(Exception):
    """The guest a re-join was asked for cannot be located."""


async def resolve_join_target(
    service: ProvisionService,
    vmid: int,
    node: str | None = None,
    hostname: str | None = None,
    defaults_source: Any = None,
) -> tuple[str, str]:
    """Where a re-join should run: (node, hostname), from the guest's own row.

    ONE implementation, called by BOTH the HTTP route and the MCP tool. They
    used to carry a copy each - same chain, restated - and a fallback that means
    something different over one transport than the other is the exact trap
    tests/test_mcp_read_parity.py exists to catch elsewhere. A caller that
    already knows the node and the hostname never reaches the inventory at all.

    Raises TailnetJoinTargetError naming the field that could not be filled.
    """
    if node is None or hostname is None:
        # The guest's own inventory row is the source of truth for both. A guest
        # HomePilot provisioned always has one (source='hp_created'), and one it
        # merely discovered has one too after a refresh.
        host = None
        with contextlib.suppress(Exception):
            host = await service.repo.get_host_by_proxmox_id(vmid)
        if host is not None:
            node = node or (str(host["node"]) if host["node"] else None)
            hostname = hostname or (str(host["hostname"]) if host["hostname"] else None)
    if node is None:
        defaults = await provisioning_defaults(
            defaults_source if defaults_source is not None else service.defaults_source
        )
        node = defaults.node or None
    if node is None:
        raise TailnetJoinTargetError(
            f"No node known for VMID {vmid}: it is not in the inventory and "
            "provision_default_node is unset. Pass 'node'."
        )
    if hostname is None:
        raise TailnetJoinTargetError(
            f"No hostname known for VMID {vmid}: it is not in the inventory. "
            "Pass 'hostname' - it is what the machine will be called on the tailnet."
        )
    return node, hostname
