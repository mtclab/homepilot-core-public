from __future__ import annotations

import hashlib
import logging
from typing import Any

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

    async def exec_readonly(self, host: str, command: str) -> tuple[int, str, str]:
        refusal = readonly_command_refusal(command)
        if refusal is not None:
            raise ReadOnlyCommandError(f"command not in read-only whitelist: {refusal}")
        return await self.exec(host=host, command=command)

    @staticmethod
    def _validate_readonly_command(command: str) -> bool:
        """Whether ``command`` may run at the READ tier (see readonly_policy)."""
        return readonly_command_refusal(command) is None
