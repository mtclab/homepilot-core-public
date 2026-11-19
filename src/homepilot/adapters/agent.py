from __future__ import annotations

import hashlib
import logging
from typing import Any, Final

from homepilot.adapters.readonly_policy import readonly_command_refusal

logger = logging.getLogger(__name__)

# The READ-tier command gate lives in the policy shared with the Go agent
# (adapters/readonly_policy.json, a byte-identical copy of
# agent/go/readonly_policy.json). The regex tables that used to sit here drifted
# from the agent's and let mutating verbs through at the read tier (#682).


class AgentAdapterError(Exception):
    pass


class GuestHostError(AgentAdapterError):
    pass


def is_pve_node(host: str, pve_nodes: list[str]) -> bool:
    """True if ``host`` names a Proxmox hypervisor node rather than a guest.

    Shared by the callers that hold a ``pve_nodes`` list of their own (the
    ansible executor and the drift verifier) so they do not have to reach into
    ``AgentAdapter`` privates. Reaching in is how the guard silently died: both
    called ``_validate_guest_only``, a method that no longer exists, one behind a
    ``hasattr`` that therefore never fired (#388).
    """
    host_lower = host.lower().strip()
    return any(host_lower == node.lower().strip() for node in pve_nodes)


class ReadOnlyCommandError(AgentAdapterError):
    pass


class AgentTooOldError(AgentAdapterError):
    """The connected agent predates an action it was asked for."""


#: The agent action (and system-info capability) of the host description (#818).
DESCRIBE_CAPABILITY: Final = "describe_host"
#: The agent action (and capability) that returns the exact bytes write_config is
#: about to replace, for a rollback snapshot (#853).
SNAPSHOT_CAPABILITY: Final = "snapshot_file"


class AgentAdapter:
    """Routes host commands through the agent hub — the only host-management
    transport (the SSH/jump server was removed). An operation on a host with
    no connected agent raises AgentAdapterError.
    """

    def __init__(
        self,
        hub_server: Any = None,
        pve_nodes: list[str] | None = None,
    ) -> None:
        self._hub = hub_server
        self._pve_nodes = pve_nodes or []

    def _check_guest_only(self, host: str) -> None:
        host_lower = host.lower().strip()
        for node in self._pve_nodes:
            if host_lower == node.lower().strip():
                raise GuestHostError(f"PVE node '{host}' — use Proxmox API instead")

    def _resolve_agent_id(self, host: str) -> str | None:
        if not self._hub:
            return None
        agent = self._hub.registry.get_by_hostname(host)
        return agent.agent_id if agent else None

    async def exec(self, host: str, command: str, timeout: int = 30) -> tuple[int, str, str]:
        self._check_guest_only(host)

        agent_id = self._resolve_agent_id(host)
        if agent_id:
            try:
                result = await self._hub.send_command(agent_id, command, timeout)
                return (
                    result.get("exit_code", -1),
                    result.get("stdout", ""),
                    result.get("stderr", ""),
                )
            except (TimeoutError, ConnectionError) as exc:
                raise AgentAdapterError(f"agent exec failed for {host}: {exc}") from exc

        raise AgentAdapterError(f"no agent connected for {host}")

    async def test_connection(self, host: str) -> bool:
        return self._resolve_agent_id(host) is not None

    async def read_file(self, host: str, path: str) -> str:
        self._check_guest_only(host)

        agent_id = self._resolve_agent_id(host)
        if agent_id:
            try:
                result = await self._hub.send_read_file(agent_id, path)
                return str(result.get("content", ""))
            except (TimeoutError, ConnectionError) as exc:
                raise AgentAdapterError(f"agent read_file failed for {host}: {exc}") from exc

        raise AgentAdapterError(f"no agent connected for {host}")

    async def write_file(self, host: str, path: str, content: str) -> dict[str, Any]:
        self._check_guest_only(host)

        before_hash: str | None = None
        try:
            existing = await self.read_file(host, path)
            before_hash = hashlib.sha256(existing.encode()).hexdigest()
        except Exception:
            # Best-effort probe for the "changed" flag. A missing file now
            # raises (AgentCommandError "file not found") instead of returning
            # ""; treat any probe failure as "no prior content".
            before_hash = None

        agent_id = self._resolve_agent_id(host)
        if not agent_id:
            raise AgentAdapterError(f"no agent connected for {host}")
        try:
            await self._hub.send_write_file(agent_id, path, content)
        except (TimeoutError, ConnectionError) as exc:
            raise AgentAdapterError(f"agent write_file failed for {host}: {exc}") from exc

        after_hash = hashlib.sha256(content.encode()).hexdigest()
        changed = before_hash != after_hash
        return {"before_hash": before_hash, "after_hash": after_hash, "changed": changed}

    async def _provision(self, host: str, action: str, params: dict[str, Any]) -> dict[str, Any]:
        """Shared dispatch for the #397 phase-B1 provisioning actions.

        Resolves the agent by host (with the PVE-node guard), sends the action via
        the hub, and returns the parsed ``{changed, detail}`` structure. A missing
        agent or transport failure raises :class:`AgentAdapterError`."""
        self._check_guest_only(host)
        agent_id = self._resolve_agent_id(host)
        if not agent_id:
            raise AgentAdapterError(f"no agent connected for {host}")
        try:
            result = await self._hub.send_action(agent_id, action, params)
        except (TimeoutError, ConnectionError) as exc:
            raise AgentAdapterError(f"agent {action} failed for {host}: {exc}") from exc
        return {
            "changed": bool(result.get("changed", False)),
            "detail": str(result.get("detail", "")),
        }

    async def install_package(self, host: str, name: str) -> dict[str, Any]:
        """Idempotently install an apt package on ``host`` via the agent."""
        return await self._provision(host, "install_package", {"name": name})

    async def manage_service(self, host: str, name: str, state: str) -> dict[str, Any]:
        """Bring a systemd unit on ``host`` to ``state`` (started/stopped/
        restarted/enabled/disabled) idempotently via the agent."""
        return await self._provision(host, "manage_service", {"name": name, "state": state})

    async def write_config(
        self, host: str, path: str, content: str, mode: str = "0644"
    ) -> dict[str, Any]:
        """Idempotently write a config file on ``host`` at ``mode`` via the agent.

        The agent enforces the same write allowlist + symlink safety as
        write_file; identical content is a no-op (``changed: False``)."""
        return await self._provision(
            host, "write_config", {"path": path, "content": content, "mode": mode}
        )

    async def snapshot_file(self, host: str, path: str) -> str:
        """The exact bytes at ``path`` on ``host``, for a rollback snapshot (#853).

        The read tier (``read_file``) redacts a unit file's ``Environment=``
        values and ``Exec*=`` arguments, so its answer is not something a revoke
        can write back. This is the write path's own read: a privileged agent
        answers it for a path ``write_config`` could replace (never a denied
        credential path, never through a symlink). Only the pre-apply capture
        calls it; no read-tier tool or route does.

        Raises :class:`AgentTooOldError` when the agent does not advertise the
        action (an older agent would drop the frame and leave this waiting), and
        the agent's own refusal (unprivileged, "file not found", denied path)
        as :class:`~homepilot.agent_hub.server.AgentCommandError`.
        """
        self._check_guest_only(host)
        if not self._hub:
            raise AgentAdapterError(f"no agent connected for {host}")
        agent = self._hub.registry.get_by_hostname(host)
        if agent is None:
            raise AgentAdapterError(f"no agent connected for {host}")
        capabilities = (agent.system_info or {}).get("capabilities") or []
        if SNAPSHOT_CAPABILITY not in capabilities:
            version = (agent.system_info or {}).get("agent_version") or "unknown"
            raise AgentTooOldError(
                f"the agent on {host} (version {version}) cannot snapshot a file "
                "for rollback: upgrade it"
            )
        try:
            result = await self._hub.send_action(
                agent.agent_id, SNAPSHOT_CAPABILITY, {"path": path}
            )
        except (TimeoutError, ConnectionError) as exc:
            raise AgentAdapterError(f"agent snapshot_file failed for {host}: {exc}") from exc
        content = result.get("content")
        if not isinstance(content, str):
            raise AgentAdapterError(f"the agent on {host} answered without the file's content")
        return content

    async def describe_host(self, host: str, timeout: int = 60) -> dict[str, Any]:
        """The agent's host description (#818): how the machine is built.

        Read-only on the host: the agent runs only its fixed collectors and
        takes no parameters from this frame. An agent that does not advertise
        ``describe_host`` in its system info is refused here with the reason -
        an older agent would silently drop the frame and leave the caller
        waiting out the timeout.
        """
        self._check_guest_only(host)
        if not self._hub:
            raise AgentAdapterError(f"no agent connected for {host}")
        agent = self._hub.registry.get_by_hostname(host)
        if agent is None:
            raise AgentAdapterError(f"no agent connected for {host}")
        capabilities = (agent.system_info or {}).get("capabilities") or []
        if DESCRIBE_CAPABILITY not in capabilities:
            version = (agent.system_info or {}).get("agent_version") or "unknown"
            raise AgentTooOldError(
                f"the agent on {host} (version {version}) cannot describe its host: "
                "upgrade it to report how the host is built"
            )
        # A TimeoutError propagates as itself: "did not answer" is its own answer.
        try:
            result = await self._hub.send_action(
                agent.agent_id, DESCRIBE_CAPABILITY, {}, timeout=timeout
            )
        except ConnectionError as exc:
            raise AgentAdapterError(f"agent describe_host failed for {host}: {exc}") from exc
        description = result.get("description")
        if not isinstance(description, dict):
            raise AgentAdapterError(f"the agent on {host} answered without a description")
        return description

    async def exec_readonly(self, host: str, command: str) -> tuple[int, str, str]:
        refusal = readonly_command_refusal(command)
        if refusal is not None:
            raise ReadOnlyCommandError(f"command not in read-only whitelist: {refusal}")
        return await self.exec(host=host, command=command)

    @staticmethod
    def _validate_readonly_command(command: str) -> bool:
        """Whether ``command`` may run at the READ tier (see readonly_policy)."""
        return readonly_command_refusal(command) is None
