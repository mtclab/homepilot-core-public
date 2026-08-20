"""Install and enrol hp-agent inside a guest over qemu-guest-agent (ADR-004 S4).

HomePilot already drives guests through the guest-agent channel (that is how a
provision joins a tailnet), so the same channel can run the installer the
operator would otherwise paste into a root shell on every host. This adds a
path; the manual one-liner stays.

Two rules shape the code:

* The enrolment token and the hub's certificate pin NEVER reach an argv. They
  are staged to a tmpfs file, sourced into the environment, and deleted by the
  same shell that reads them - the `_join_tailnet` precedent (an argv is
  readable by every process in the guest, and PVE echoes the command back
  inside task errors).
* Enrolment is proven, not assumed. The installer exiting 0 means the unit was
  written and started; it does not mean the agent reached the hub. The task only
  succeeds once THAT agent is in the registry.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import json
import logging
import re
import time
from typing import TYPE_CHECKING, Any

from ..adapters.proxmox import ProxmoxClient
from ..db.repository import Repository
from ..tasks.repository import TaskRepository

if TYPE_CHECKING:
    from .registry import AgentRegistry

logger = logging.getLogger(__name__)

# The action this work is tracked under. Admitted by the tasks CHECK constraint
# in migration 18, and artifactless like 'provision'.
ACTION = "install_agent"

# The SAME installer the UI's one-liner fetches. A second install path that
# installed the agent a different way would be two products to keep correct.
INSTALLER_URL = (
    "https://github.com/mtclab/homepilot-core-public/releases/latest/download/install-agent.sh"
)

# /run is tmpfs: the staged credentials never reach the guest's disk.
_ENV_PATH = "/run/hp-agent-enroll.env"
_INSTALLER_PATH = "/run/hp-agent-install.sh"

# install-agent.sh always writes the agent id it resolved to /etc/homepilot/agent.id
# (it reuses an existing id rather than minting one, so the id is not ours to
# predict). The wrapper echoes that file back with this marker, which is what
# lets verification wait for THIS agent instead of guessing which registration
# was ours from the hostname.
_AGENT_ID_MARKER = "hp-agent-id="
_AGENT_ID_RE = re.compile(rf"{_AGENT_ID_MARKER}(\S+)")

# Values written into a file that a shell SOURCES. Anything outside this set
# (whitespace, quotes, `$`, a newline) would be executed rather than assigned,
# so it is refused here rather than escaped later.
_SHELL_SAFE_RE = re.compile(r"^[A-Za-z0-9._:@/+=-]+$")

# Host types that have a qemu-guest-agent channel at all. An LXC container does
# not, whatever it has installed inside it.
_QEMU_HOST_TYPE = "qemu"


class EnrollPreconditionError(Exception):
    """A host that cannot be enrolled this way, with the reason an operator needs.

    Carries a stable `code` for the UI and a message that names what to do. Raised
    BEFORE any task is created, so a refusal is an answer rather than a failed
    task an operator has to open to understand.
    """

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


class EnrollConflictError(Exception):
    """An install for the same host is already in flight."""


class AgentEnrollService:
    """Install hp-agent into a guest and prove it enrolled, as an 'install_agent' task.

    The caller gets a task id immediately; the run ALWAYS lands the record in a
    terminal state (#386 - a task stranded in 'running' never stops being "in
    flight").
    """

    def __init__(
        self,
        proxmox: ProxmoxClient | None,
        task_repo: TaskRepository,
        repo: Repository,
        registry: AgentRegistry | None,
        install_timeout_s: float = 600.0,
        poll_interval: float = 3.0,
        connect_wait_s: float = 120.0,
        connect_interval: float = 2.0,
    ):
        self.proxmox = proxmox
        self.task_repo = task_repo
        self.repo = repo
        self.registry = registry
        self.install_timeout_s = install_timeout_s
        self.poll_interval = poll_interval
        self.connect_wait_s = connect_wait_s
        self.connect_interval = connect_interval
        self._running_tasks: set[asyncio.Task[Any]] = set()
        # Host ids with an install in flight IN THIS PROCESS. Two concurrent
        # installs would race over the same guest's /etc/homepilot and mint two
        # bootstrap tokens, one of which could never be used.
        self._inflight_hosts: set[str] = set()

    def _track_task(self, task: asyncio.Task[Any]) -> None:
        # Hold a strong reference until completion: asyncio keeps only a weak one.
        self._running_tasks.add(task)
        task.add_done_callback(self._running_tasks.discard)

    def is_inflight(self, host_id: str) -> bool:
        return host_id in self._inflight_hosts

    async def check(self, host: dict[str, Any], hub_host: str) -> None:
        """Refuse, with the reason, every host this channel cannot enrol.

        Checked in the order an operator would: what HomePilot itself is missing
        first, then what the host is, then what the guest is doing right now.
        Raises EnrollPreconditionError; returns None when the host qualifies.
        """
        hostname = str(host.get("hostname") or "")

        registry = self.registry
        hub = getattr(registry, "hub_server", None) if registry is not None else None
        if registry is None or hub is None:
            raise EnrollPreconditionError(
                "hub_unavailable",
                "The agent hub is not running, so an agent would have nothing to "
                "enrol against. Enable HP_AGENT_HUB_ENABLED and restart HomePilot.",
            )

        from .router import _INVALID_HUB_HOST_PLACEHOLDER, _is_valid_hub_host

        if hub_host == _INVALID_HUB_HOST_PLACEHOLDER or not _is_valid_hub_host(hub_host):
            raise EnrollPreconditionError(
                "hub_address_unresolved",
                f"The hub address HomePilot would hand the agent ({hub_host!r}) is not a "
                "usable hostname or IP, so the agent could never dial back. Set "
                "HP_AGENT_HUB_ADVERTISE_HOST and retry.",
            )

        if self.proxmox is None:
            raise EnrollPreconditionError(
                "proxmox_unavailable",
                "Proxmox is not configured, so HomePilot has no guest-agent channel "
                "into this host. Add the Proxmox address and token in Settings, or run "
                "scripts/install-agent.sh on the host by hand.",
            )

        node = host.get("node")
        vmid = host.get("proxmox_id")
        if host.get("host_type") != _QEMU_HOST_TYPE or not node or vmid is None:
            raise EnrollPreconditionError(
                "not_a_qemu_guest",
                f"{hostname or 'This host'} is not a Proxmox QEMU guest HomePilot knows a "
                "node and VMID for, so there is no qemu-guest-agent channel to install "
                "through. Run scripts/install-agent.sh on the host instead.",
            )

        if registry.is_connected(hostname):
            agent = registry.get_by_hostname(hostname)
            agent_id = agent.agent_id if agent is not None else "unknown"
            raise EnrollPreconditionError(
                "already_enrolled",
                f"{hostname} already has a live agent ({agent_id}). Revoke it on the "
                "Agents page, or stop hp-agent on the host, before enrolling again.",
            )

        status = await self._guest_status(str(node), int(vmid))
        if status != "running":
            raise EnrollPreconditionError(
                "not_running",
                f"{hostname} is {status} on Proxmox. Start the guest, then install the agent.",
            )

        if not await self.proxmox.agent_ping(str(node), int(vmid)):
            raise EnrollPreconditionError(
                "no_guest_agent",
                f"{hostname} does not answer on qemu-guest-agent, so HomePilot cannot run "
                "anything inside it. Install and start qemu-guest-agent in the guest (and "
                "enable the Agent option on the VM), or run scripts/install-agent.sh on the "
                "host by hand.",
            )

    async def _guest_status(self, node: str, vmid: int) -> str:
        """The guest's live PVE status, or a stand-in when PVE will not say.

        Live, never the cached hosts.pve_status: a precondition that reports a
        stale row is not a precondition.
        """
        proxmox = self.proxmox
        if proxmox is None:
            return "unknown"
        try:
            result = await proxmox.get_vm_current(node, vmid)
        except Exception:
            logger.warning("Could not read PVE status for vmid %s", vmid, exc_info=True)
            return "unreachable on Proxmox"
        data = result.get("data", result)
        if not isinstance(data, dict):
            return "unknown"
        return str(data.get("status") or "unknown")

    async def start(
        self,
        host: dict[str, Any],
        hub_host: str,
        hub_port: int,
        actor: str = "system",
    ) -> str:
        """Check the preconditions, then queue the install as a tracked task."""
        host_id = str(host["id"])
        if host_id in self._inflight_hosts:
            raise EnrollConflictError(
                f"An agent install for {host.get('hostname') or host_id!r} is already in flight"
            )
        await self.check(host, hub_host)
        self._inflight_hosts.add(host_id)
        try:
            task_id = await self.task_repo.create_task(None, ACTION)
        except Exception:
            self._inflight_hosts.discard(host_id)
            raise
        self._track_task(asyncio.create_task(self._run(task_id, host, hub_host, hub_port, actor)))
        return task_id

    async def _run(
        self,
        task_id: str,
        host: dict[str, Any],
        hub_host: str,
        hub_port: int,
        actor: str,
    ) -> None:
        # `step` names the stage a failure happened at, so the task's error tells
        # an operator WHERE the install died, not just that it did.
        step = "start"
        host_id = str(host["id"])
        hostname = str(host.get("hostname") or "")
        node = str(host.get("node"))
        vmid = int(host.get("proxmox_id") or 0)
        token = ""
        try:
            await self.task_repo.update_task_status(task_id, "running")
            proxmox = self.proxmox
            registry = self.registry
            hub = getattr(registry, "hub_server", None) if registry is not None else None
            if proxmox is None or registry is None or hub is None:
                raise RuntimeError("Proxmox or the agent hub went away before the install started")

            step = "mint_token"
            # A single-use bootstrap token, exactly what the UI's "Enroll Agent"
            # button mints. The agent trades it for a durable per-agent credential
            # on first connect, so the value staged in the guest stops being
            # usable the moment enrolment succeeds.
            token = await hub._token_store.create()
            tls = bool(getattr(hub, "tls_enabled", False))
            pin = str(getattr(hub, "cert_fingerprint", "") or "")

            step = "stage_credentials"
            env_body = _env_file(hub_host, hub_port, token, tls, pin)
            await proxmox.agent_write_file(node, vmid, _ENV_PATH, env_body)

            step = "install"
            exit_code, stdout, stderr = await self._install(node, vmid)
            if exit_code != 0:
                raise RuntimeError(
                    f"the installer exited {exit_code} in the guest: "
                    f"{_tail(_redact(stderr or stdout, token))}"
                )

            step = "verify"
            agent_id = _parse_agent_id(stdout)
            enrolled_id, verified_by = await self._wait_for_enrolment(agent_id, hostname)

            result = {
                "host_id": host_id,
                "hostname": hostname,
                "node": node,
                "vmid": vmid,
                "agent_id": enrolled_id,
                # True only because the agent is IN the registry - never because
                # the installer exited 0.
                "connected": True,
                "verified_by": verified_by,
            }
            await self.task_repo.update_task_status(
                task_id, "succeeded", result_json=json.dumps(result)
            )
            await self._audit(actor, hostname, "install_agent", result)
        except Exception as exc:
            # Never log or store the exception body raw: a PVE error echoes the
            # command back, and captured guest output could carry the token.
            error = f"{step}: {_redact(str(exc), token)}"
            logger.error("Agent install task %s failed at step %s", task_id, step)
            await self._cleanup(node, vmid)
            try:
                await self.task_repo.update_task_status(task_id, "failed", error=error)
                await self._audit(actor, hostname, "install_agent_failed", {"error": error})
            except Exception:
                logger.error("Could not mark agent-install task %s failed", task_id, exc_info=True)
        finally:
            self._inflight_hosts.discard(host_id)

    async def _install(self, node: str, vmid: int) -> tuple[int, str, str]:
        """Run the installer inside the guest and wait for it to actually exit.

        PVE's agent exec is fire-and-forget - it returns a pid, not a result - so
        a caller that does not poll exec-status has no idea whether the installer
        ran at all.
        """
        proxmox = self.proxmox
        if proxmox is None:
            raise RuntimeError("Proxmox not configured")
        started = await proxmox.agent_exec(
            node, vmid, ["sh", "-c", _install_script()], capture_output=True
        )
        pid = _exec_pid(started)
        if pid is None:
            raise RuntimeError("the guest agent accepted the command but returned no pid")
        deadline = time.monotonic() + self.install_timeout_s
        while True:
            status = await proxmox.agent_exec_status(node, vmid, pid)
            data = status.get("data", status)
            if isinstance(data, dict) and data.get("exited"):
                return (
                    int(data.get("exitcode") or 0),
                    _as_text(data.get("out-data")),
                    _as_text(data.get("err-data")),
                )
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"the installer did not finish within {self.install_timeout_s:.0f}s"
                )
            await asyncio.sleep(self.poll_interval)

    async def _wait_for_enrolment(self, agent_id: str | None, hostname: str) -> tuple[str, str]:
        """Wait until the installed agent is CONNECTED to this hub.

        This is the outcome the action promises. `agent_id` is what the installer
        reported; when it could not be read (an older guest agent that returns no
        output) the hostname is the fallback, which the already-enrolled
        precondition makes unambiguous - nothing was connected under it before.
        """
        registry = self.registry
        if registry is None:
            raise RuntimeError("the agent hub went away before enrolment could be verified")
        deadline = time.monotonic() + self.connect_wait_s
        while True:
            if agent_id:
                agent = registry.get(agent_id)
                if agent is not None:
                    return agent.agent_id, "agent_id"
            else:
                by_host = registry.get_by_hostname(hostname)
                if by_host is not None:
                    return by_host.agent_id, "hostname"
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"the installer finished but no agent connected to the hub within "
                    f"{self.connect_wait_s:.0f}s. Check `journalctl -u hp-agent` on {hostname}."
                )
            await asyncio.sleep(self.connect_interval)

    async def _cleanup(self, node: str, vmid: int) -> None:
        """Best effort: the wrapper deletes the staged file itself, but if the exec
        never ran the credentials would otherwise stay in the guest's tmpfs."""
        proxmox = self.proxmox
        if proxmox is None:
            return
        with contextlib.suppress(Exception):
            await proxmox.agent_exec(node, vmid, ["rm", "-f", _ENV_PATH, _INSTALLER_PATH])

    async def _audit(self, actor: str, hostname: str, action: str, details: dict[str, Any]) -> None:
        try:
            await self.repo.log_audit(
                user_id=actor,
                source="agent_enroll",
                action=action,
                target_host=hostname,
                details_json=json.dumps(details),
            )
        except Exception:
            # Audit is a record, never a gate.
            logger.warning("Could not write agent-install audit row", exc_info=True)


def _assert_shell_safe(name: str, value: str) -> str:
    if not _SHELL_SAFE_RE.match(value):
        raise ValueError(f"{name} contains characters that cannot be staged into a shell env file")
    return value


def _env_file(hub_host: str, hub_port: int, token: str, tls: bool, pin: str) -> str:
    """The env file the guest sources. Every value is shell-safe by validation,
    because `.` EXECUTES this file rather than parsing it."""
    lines = [
        f"HUB={_assert_shell_safe('hub host', f'{hub_host}:{hub_port}')}",
        f"TOKEN={_assert_shell_safe('enrollment token', token)}",
    ]
    if tls:
        lines.append("USE_TLS=true")
        if pin:
            lines.append(f"TLS_PIN=sha256:{_assert_shell_safe('certificate pin', pin)}")
    return "\n".join(lines) + "\n"


def _install_script() -> str:
    """The wrapper run inside the guest.

    `set -a` exports what the env file assigns so install-agent.sh - a child
    process - reads HUB/TOKEN/USE_TLS/TLS_PIN from its environment exactly as the
    documented `HUB=… TOKEN=… curl … | bash` form does. The file is deleted
    before the installer is even fetched, so the credentials exist on the guest
    for the length of one `.` and appear in no argv at any point.
    """
    return (
        "set -e; "
        "umask 077; "
        "set -a; "
        f". {_ENV_PATH}; "
        "set +a; "
        f"rm -f {_ENV_PATH}; "
        f"curl -fsSL {INSTALLER_URL} -o {_INSTALLER_PATH}; "
        f"bash {_INSTALLER_PATH}; "
        f"rm -f {_INSTALLER_PATH}; "
        f"printf '{_AGENT_ID_MARKER}%s\\n' \"$(cat /etc/homepilot/agent.id)\""
    )


def _exec_pid(payload: dict[str, Any] | None) -> int | None:
    if not payload:
        return None
    data = payload.get("data", payload)
    if not isinstance(data, dict):
        return None
    pid = data.get("pid")
    if pid is None:
        return None
    try:
        return int(pid)
    except (TypeError, ValueError):
        return None


def _as_text(value: Any) -> str:
    """Guest output as text. PVE hands stdout back as a string, but a guest agent
    that base64s it must not turn into an unreadable error message."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _parse_agent_id(stdout: str) -> str | None:
    match = _AGENT_ID_RE.search(stdout)
    if match:
        return match.group(1)
    # A guest agent that hands stdout back base64-encoded rather than decoded.
    with contextlib.suppress(binascii.Error, ValueError, UnicodeDecodeError):
        decoded = base64.b64decode(stdout.strip(), validate=True).decode("utf-8", errors="replace")
        match = _AGENT_ID_RE.search(decoded)
        if match:
            return match.group(1)
    return None


def _redact(text: str, token: str) -> str:
    """The installer never prints the token, but an error path we do not control
    might. Nothing carrying it reaches a task record or a log line."""
    if token and token in text:
        return text.replace(token, "<redacted>")
    return text


def _tail(text: str, limit: int = 400) -> str:
    cleaned = text.strip()
    if len(cleaned) <= limit:
        return cleaned
    return "…" + cleaned[-limit:]
