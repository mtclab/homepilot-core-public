from __future__ import annotations

import hashlib
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

_SHELL_METACHAR_RE = re.compile(r"[|;&`$<>!#\(\){}\[\]]")

_SAFE_READONLY_COMMANDS = {
    "cat": re.compile(r"^cat\s+[^\s]+$"),
    "ls": re.compile(r"^ls(\s+[a-zA-Z0-9_./-]+)*$"),
    "ps": re.compile(r"^ps(\s+[a-zA-Z0-9_-]+)*$"),
    "hostname": re.compile(r"^hostname$"),
    "uname": re.compile(r"^uname(\s+-[aAmnrsv]+)*$"),
    "df": re.compile(r"^df(\s+-[hHiTkP]+)*(\s+[a-zA-Z0-9_./-]+)?$"),
    "free": re.compile(r"^free(\s+-[hkmgb]+)*$"),
    "uptime": re.compile(r"^uptime$"),
    "ip": re.compile(r"^ip\s+(addr|link|route|neigh)(\s+[a-zA-Z0-9_./-]+)*$"),
    "ss": re.compile(r"^ss(\s+-[a-zA-Z0-9]+)*(\s+[a-zA-Z0-9_./-]+)?$"),
    # Kept in lockstep with agent/go/allowlist.go safeCommands["systemctl"] — the
    # adapter must not reject a read-only subcommand (is-active/is-enabled/
    # daemon-reload) the agent itself allows. Parity is gated by
    # tests/test_allowlist_parity.py.
    "systemctl": re.compile(
        r"^systemctl\s+(status|is-active|is-enabled|daemon-reload)"
        r"(\s+[a-zA-Z0-9_.-]+(\.service)?)?$"
    ),
    "journalctl": re.compile(r"^journalctl(\s+-[a-zA-Z0-9]+(\s+\S+)?)*$"),
    "dpkg": re.compile(r"^dpkg\s+-[lSs](\s+[a-zA-Z0-9_.+-]+)*$"),
}

# Kept in lockstep with agent/go/allowlist.go catAllowedPrefixes — gated by
# tests/test_allowlist_parity.py so the adapter never permits a cat path the
# agent would reject (subset), and does not reject one the agent allows.
_CAT_ALLOWED_PREFIXES = (
    "/var/log/",
    "/etc/homepilot/",
    "/etc/hostname",
    "/etc/os-release",
    "/etc/resolv.conf",
    "/etc/hosts",
    "/etc/systemd/",
    "/opt/",
)


class AgentAdapterError(Exception):
    pass


class GuestHostError(AgentAdapterError):
    pass


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
        if not self._validate_readonly_command(command):
            raise ReadOnlyCommandError(f"command not in read-only whitelist: {command}")
        return await self.exec(host=host, command=command)

    @staticmethod
    def _validate_readonly_command(command: str) -> bool:
        stripped = command.strip()
        if _SHELL_METACHAR_RE.search(stripped):
            return False
        cmd_base = stripped.split()[0] if stripped.split() else ""
        pattern = _SAFE_READONLY_COMMANDS.get(cmd_base)
        if pattern is None:
            return False
        if not pattern.match(stripped):
            return False
        if cmd_base == "cat":
            parts = stripped.split()
            if len(parts) < 2:
                return False
            cat_path = parts[1]
            if ".." in cat_path:
                return False
            if not any(cat_path == p or cat_path.startswith(p) for p in _CAT_ALLOWED_PREFIXES):
                return False
        return True
