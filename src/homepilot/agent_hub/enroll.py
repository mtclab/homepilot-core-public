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
from ..background import DEFAULT_DRAIN_TIMEOUT, drain_tasks
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

    async def drain(self, timeout: float = DEFAULT_DRAIN_TIMEOUT) -> None:
        """Let in-flight jobs finish before the database goes away (#496).

        These run behind an already-accepted HTTP request, so nothing else
        awaits them; a shutdown that closes the database under one leaves the
        task row it was about to finish saying "running" forever - and can kill
        aiosqlite's worker thread mid-write.
        """
        await drain_tasks(self._running_tasks, "agent enrolment job(s)", timeout)

    def is_inflight(self, host_id: str) -> bool:
        return host_id in self._inflight_hosts

    async def check(self, host: dict[str, Any], hub_host: str, upgrade: bool = False) -> None:
        """Refuse, with the reason, every host this channel cannot enrol.

        ``upgrade`` turns the live-agent rule around: an install is refused over a
        connected agent, an upgrade is refused without one.

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

        if registry.is_connected(hostname) and not upgrade:
            agent = registry.get_by_hostname(hostname)
            agent_id = agent.agent_id if agent is not None else "unknown"
            # Not "revoke it first": revoking leaves a dead credential on the host,
            # which is the very state a reinstall then has to dig it out of (#815).
            raise EnrollPreconditionError(
                "already_enrolled",
                f"{hostname} already has a live agent ({agent_id}). To put the current "
                "agent on it, use Upgrade agent: the same agent stays enrolled.",
            )
        if upgrade and not registry.is_connected(hostname):
            raise EnrollPreconditionError(
                "not_connected",
                f"{hostname} has no live agent to upgrade. Use Install agent instead.",
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
        upgrade: bool = False,
    ) -> str:
        """Check the preconditions, then queue the install as a tracked task.

        ``upgrade`` reinstalls over a LIVE agent (binary and config, same agent
        id): the task succeeds only once that agent has reconnected on a new
        connection, and if it never does the previous binary is put back."""
        host_id = str(host["id"])
        if host_id in self._inflight_hosts:
            raise EnrollConflictError(
                f"An agent install for {host.get('hostname') or host_id!r} is already in flight"
            )
        await self.check(host, hub_host, upgrade=upgrade)
        self._inflight_hosts.add(host_id)
        try:
            task_id = await self.task_repo.create_task(None, ACTION)
        except Exception:
            self._inflight_hosts.discard(host_id)
            raise
        self._track_task(
            asyncio.create_task(self._run(task_id, host, hub_host, hub_port, actor, upgrade))
        )
        return task_id

    async def _run(
        self,
        task_id: str,
        host: dict[str, Any],
        hub_host: str,
        hub_port: int,
        actor: str,
        upgrade: bool = False,
    ) -> None:
        # `step` names the stage a failure happened at, so the task's error tells
        # an operator WHERE the install died, not just that it did.
        step = "start"
        host_id = str(host["id"])
        hostname = str(host.get("hostname") or "")
        node = str(host.get("node"))
        vmid = int(host.get("proxmox_id") or 0)
        token = ""
        # The live agent an upgrade replaces: its id, version and the connection
        # record it holds now, so verification can tell the NEW connection apart.
        prior: Any = None
        swapped = False
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
            token = await hub._token_store.create(
                minted_by=f"zero-touch install on {hostname or host_id}"
            )
            tls = bool(getattr(hub, "tls_enabled", False))
            pin = str(getattr(hub, "cert_fingerprint", "") or "")

            step = "stage_credentials"
            # The guest reaches HomePilot's API on the same host it reaches the
            # hub on - that address is already resolved for this guest's network -
            # so the binary comes from the control plane rather than the internet
            # (#464). A guest on an egress-limited VLAN can then still enrol.
            from .dist import api_base_for

            api_base = api_base_for(hub_host)
            env_body = _env_file(hub_host, hub_port, token, tls, pin, api_base)
            await proxmox.agent_write_file(node, vmid, _ENV_PATH, env_body)

            step = "install"
            from .join_requests import install_started_at

            started_at = install_started_at()
            if upgrade and registry is not None:
                prior = registry.get_by_hostname(hostname)
            exit_code, stdout, stderr = await self._install(node, vmid)
            # The binary-swap block announces the swap; only then is there a
            # previous binary of THIS run to put back.
            swapped = "Installed to " in (stdout or "")
            if exit_code != 0:
                raise RuntimeError(
                    f"the installer exited {exit_code} in the guest: "
                    f"{_tail(_redact(stderr or stdout, token))}"
                )

            step = "verify"
            agent_id = _parse_agent_id(stdout)
            if prior is not None:
                enrolled_id, verified_by = await self._wait_for_upgrade(
                    prior, agent_id, hostname, since=started_at
                )
            else:
                enrolled_id, verified_by = await self._wait_for_enrolment(
                    agent_id, hostname, since=started_at
                )

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
            if prior is not None:
                result["upgrade"] = True
                result["previous_version"] = _agent_version(prior)
                result["agent_version"] = _agent_version(registry.get(enrolled_id))
            await self.task_repo.update_task_status(
                task_id, "succeeded", result_json=json.dumps(result)
            )
            await self._audit(actor, hostname, "install_agent", result)
        except Exception as exc:
            # Never log or store the exception body raw: a PVE error echoes the
            # command back, and captured guest output could carry the token.
            error = f"{step}: {_redact(str(exc), token)}"
            if prior is not None:
                error += " " + await self._roll_back(node, vmid, prior, swapped)
            logger.error("Agent install task %s failed at step %s", task_id, step)
            await self._cleanup(node, vmid)
            try:
                # WHICH host failed, on the task itself: an install task has no
                # artifact, so without this a failure can never be matched to
                # the later install (or the connected agent) that made it moot,
                # and it stays on the Overview until it ages off the page.
                failed_on = {"host_id": host_id, "hostname": hostname, "step": step}
                await self.task_repo.update_task_status(
                    task_id, "failed", error=error, result_json=json.dumps(failed_on)
                )
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
        return await proxmox.agent_run(
            node,
            vmid,
            _install_script(),
            timeout_s=self.install_timeout_s,
            poll_interval=self.poll_interval,
        )

    async def _wait_for_enrolment(
        self, agent_id: str | None, hostname: str, since: str | None = None
    ) -> tuple[str, str]:
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
                refused = await self._refusal_since(hostname, since)
                if refused is not None:
                    # The host DID dial in and was turned away: it presented a
                    # credential other than the bootstrap token this install
                    # handed it (#815). Say that, with the hub's own reason,
                    # rather than send the operator to a journal on the guest.
                    raise RuntimeError(
                        f"the agent on {hostname} presented an old credential that this hub "
                        f"refused, so the new enrolment token was never used (the hub said: "
                        f"{refused.get('reason') or refused.get('reason_code')}). Press Install "
                        "again: the installer moves the old credential aside to "
                        "agent.token.prev and enrols with a fresh token."
                    )
                raise RuntimeError(
                    f"the installer finished but no agent connected to the hub within "
                    f"{self.connect_wait_s:.0f}s. Check `journalctl -u hp-agent` on {hostname}."
                )
            await asyncio.sleep(self.connect_interval)

    async def _wait_for_upgrade(
        self, prior: Any, agent_id: str | None, hostname: str, since: str | None
    ) -> tuple[str, str]:
        """Wait until the upgraded agent is back: the SAME agent id, on a NEW
        connection (a different registry record than the one it held before the
        installer ran), and no longer older than this control plane.

        The old connection being still there proves nothing - it is the old
        binary - so it never satisfies this.
        """
        from .version_skew import control_plane_version, is_behind

        registry = self.registry
        if registry is None:
            raise RuntimeError("the agent hub went away before the upgrade could be verified")
        expected_id = agent_id or prior.agent_id
        if expected_id != prior.agent_id:
            raise RuntimeError(
                f"the installer reported agent id {expected_id}, not the live agent "
                f"{prior.agent_id}; refusing to treat a second agent as the upgrade"
            )
        deadline = time.monotonic() + self.connect_wait_s
        while True:
            agent = registry.get(expected_id)
            if agent is not None and agent is not prior:
                version = _agent_version(agent)
                control = control_plane_version()
                if is_behind(version, control) is True:
                    raise RuntimeError(
                        f"the agent on {hostname} reconnected on {version}, still older than "
                        f"HomePilot {control}: the binary this control plane served is not "
                        "the current one"
                    )
                return expected_id, "upgrade"
            if time.monotonic() >= deadline:
                refused = await self._refusal_since(hostname, since)
                why = (
                    f" The hub refused it: {refused.get('reason') or refused.get('reason_code')}."
                    if refused is not None
                    else ""
                )
                raise RuntimeError(
                    f"the upgraded agent on {hostname} did not reconnect within "
                    f"{self.connect_wait_s:.0f}s.{why}"
                )
            await asyncio.sleep(self.connect_interval)

    async def _roll_back(self, node: str, vmid: int, prior: Any, swapped: bool) -> str:
        """After a failed upgrade, leave the host with a working agent.

        Nothing to do while the OLD connection is still live: the installer never
        restarted the service, so the old agent is running and stays enrolled.
        Otherwise - and only when this run swapped the binary, so hp-agent.prev
        is the one that was running - put the previous binary back, restore the
        previous credential if the new agent never got one, restart, and wait
        for the agent to return. Returns the sentence the task error ends with.
        """
        registry = self.registry
        if registry is None or self.proxmox is None:
            return "The previous agent could not be checked."
        if registry.get(prior.agent_id) is prior:
            return (
                f"The previous agent ({_agent_version(prior)}) was never stopped and is "
                "still connected; nothing was rolled back."
            )
        if not swapped:
            return "The binary was not replaced, so there was nothing to roll back."
        try:
            exit_code, _out, _err = await self.proxmox.agent_run(
                node,
                vmid,
                _rollback_script(),
                timeout_s=self.install_timeout_s,
                poll_interval=self.poll_interval,
            )
        except Exception:
            logger.warning("Agent upgrade rollback on vmid %s failed", vmid, exc_info=True)
            return "Putting the previous agent back failed; see the host's journal."
        if exit_code != 0:
            return f"Putting the previous agent back exited {exit_code}."
        deadline = time.monotonic() + self.connect_wait_s
        while time.monotonic() < deadline:
            agent = registry.get(prior.agent_id)
            if agent is not None and agent is not prior:
                return (
                    f"HomePilot put the previous agent back and it is connected again "
                    f"({_agent_version(agent)})."
                )
            await asyncio.sleep(self.connect_interval)
        return "HomePilot put the previous agent back, but it has not reconnected yet."

    async def _refusal_since(self, hostname: str, since: str | None) -> dict[str, Any] | None:
        """The hub's latest refusal of this host since the install began, if any.
        Best effort: a store that cannot answer leaves the generic message."""
        if since is None:
            return None
        from .join_requests import refusal_since

        try:
            return await refusal_since(self.repo, hostname, since)
        except Exception:
            logger.warning("Could not read enrolment refusals for %s", hostname, exc_info=True)
            return None

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


def _env_file(
    hub_host: str, hub_port: int, token: str, tls: bool, pin: str, api_base: str = ""
) -> str:
    """The env file the guest sources. Every value is shell-safe by validation,
    because `.` EXECUTES this file rather than parsing it."""
    lines = [
        f"HUB={_assert_shell_safe('hub host', f'{hub_host}:{hub_port}')}",
        f"TOKEN={_assert_shell_safe('enrollment token', token)}",
    ]
    if api_base:
        # Points the installer at THIS control plane for the binary, so a guest
        # with no internet still enrols and the agent matches the hub (#464).
        lines.append(f"HP_API={_assert_shell_safe('control plane address', api_base)}")
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
        # The installer comes from the control plane too when one is reachable,
        # for the same reason the binary does: an isolated guest has no route to
        # GitHub, and this way the script and the agent it installs are from one
        # image (#464). $HP_API and $TOKEN are set by the sourced env file.
        f'if [ -n "${{HP_API:-}}" ]; then '
        f'curl -fsSL -H "x-hp-agent-token: $TOKEN" "$HP_API/agents/dist/install-agent.sh" '
        f"-o {_INSTALLER_PATH}; else "
        f"curl -fsSL {INSTALLER_URL} -o {_INSTALLER_PATH}; fi; "
        f"bash {_INSTALLER_PATH}; "
        f"rm -f {_INSTALLER_PATH}; "
        f"printf '{_AGENT_ID_MARKER}%s\\n' \"$(cat /etc/homepilot/agent.id)\""
    )


def _rollback_script() -> str:
    """Put the agent that was running before a failed upgrade back.

    The binary the installer kept as hp-agent.prev replaces the new one; the
    credential it kept as agent.token.prev comes back only if the new agent never
    persisted one (if it did, that new credential is the live one on the hub and
    works with either binary). Paths default to the installer's; INSTALL_DIR and
    CONF_DIR exist so a test can run this very text in a sandbox.
    """
    return (
        'I="${INSTALL_DIR:-/usr/local/bin}"; C="${CONF_DIR:-/etc/homepilot}"; '
        'if [ -f "$I/hp-agent.prev" ]; then mv -f "$I/hp-agent.prev" "$I/hp-agent"; fi; '
        'if [ ! -s "$C/agent.token" ] && [ -s "$C/agent.token.prev" ]; then '
        'cp -p "$C/agent.token.prev" "$C/agent.token"; chmod 600 "$C/agent.token"; fi; '
        "systemctl restart hp-agent"
    )


def _agent_version(agent: Any) -> str:
    """What a connected agent reported about its build, for task text."""
    info = getattr(agent, "system_info", None) or {}
    version = info.get("agent_version") if isinstance(info, dict) else None
    return str(version) if version else "no version reported"


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
