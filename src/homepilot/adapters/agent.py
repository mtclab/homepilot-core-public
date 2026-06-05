from __future__ import annotations

import hashlib
import logging
import re
from typing import Any

from homepilot.jumpserver.client import JumpServerClient, JumpServerError

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
    "systemctl": re.compile(r"^systemctl\s+status\s+[a-zA-Z0-9_.-]+(\.service)?$"),
    "journalctl": re.compile(r"^journalctl(\s+-[a-zA-Z0-9]+(\s+\S+)?)*$"),
    "dpkg": re.compile(r"^dpkg\s+-[lSs](\s+[a-zA-Z0-9_.+-]+)*$"),
}

_CAT_ALLOWED_PREFIXES = (
    "/var/log/",
    "/etc/homepilot/",
    "/etc/hostname",
    "/etc/os-release",
    "/etc/resolv.conf",
    "/etc/hosts",
)


class AgentAdapterError(Exception):
    pass


class GuestHostError(AgentAdapterError):
    pass


class ReadOnlyCommandError(AgentAdapterError):
    pass


class AgentAdapter:
    """Drop-in replacement for SSHAdapter that routes through the agent hub.

    The agent hub server is the central point where connected agents
    register. When this adapter needs to execute a command on a host,
    it checks if an agent is connected for that hostname. If so, it
    routes the command through the hub. If not, it falls back to the
    SSH-based JumpServerClient.
    """

    def __init__(
        self,
        hub_server: Any = None,
        jump_client: JumpServerClient | None = None,
        pve_nodes: list[str] | None = None,
    ) -> None:
        self._hub = hub_server
        self._jump_client = jump_client
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
                logger.warning("agent exec failed for %s, falling back to SSH: %s", host, exc)

        if self._jump_client:
            try:
                result = await self._jump_client.exec(host=host, command=command, timeout=timeout)
                return (
                    result.get("exit_code", -1),
                    result.get("stdout", ""),
                    result.get("stderr", ""),
                )
            except JumpServerError as exc:
                raise AgentAdapterError(str(exc)) from exc
            except ConnectionError as exc:
                raise AgentAdapterError(f"jump server connection lost: {exc}") from exc

        raise AgentAdapterError(f"no agent or SSH connection available for {host}")

    async def test_connection(self, host: str) -> bool:
        agent_id = self._resolve_agent_id(host)
        if agent_id:
            return True
        if self._jump_client:
            try:
                result = await self._jump_client.exec(host=host, command="true", timeout=5)
                return bool(result.get("exit_code") == 0)
            except (JumpServerError, ConnectionError, OSError):
                return False
        return False

    async def read_file(self, host: str, path: str) -> str:
        self._check_guest_only(host)

        agent_id = self._resolve_agent_id(host)
        if agent_id:
            try:
                result = await self._hub.send_read_file(agent_id, path)
                return str(result.get("content", ""))
            except (TimeoutError, ConnectionError) as exc:
                logger.warning("agent read_file failed for %s, falling back to SSH: %s", host, exc)

        if self._jump_client:
            try:
                result = await self._jump_client.read_file(host=host, path=path)
                return str(result.get("content", ""))
            except JumpServerError as exc:
                raise AgentAdapterError(str(exc)) from exc
            except ConnectionError as exc:
                raise AgentAdapterError(f"jump server connection lost: {exc}") from exc

        raise AgentAdapterError(f"no agent or SSH connection available for {host}")

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
        if agent_id:
            try:
                await self._hub.send_write_file(agent_id, path, content)
            except (TimeoutError, ConnectionError) as exc:
                logger.warning("agent write_file failed for %s, falling back to SSH: %s", host, exc)
                if self._jump_client:
                    try:
                        await self._jump_client.write_file(host=host, path=path, content=content)
                    except JumpServerError as exc2:
                        raise AgentAdapterError(str(exc2)) from exc2
                    except ConnectionError as exc2:
                        raise AgentAdapterError(f"jump server connection lost: {exc2}") from exc2
        elif self._jump_client:
            try:
                await self._jump_client.write_file(host=host, path=path, content=content)
            except JumpServerError as exc:
                raise AgentAdapterError(str(exc)) from exc
            except ConnectionError as exc:
                raise AgentAdapterError(f"jump server connection lost: {exc}") from exc
        else:
            raise AgentAdapterError(f"no agent or SSH connection available for {host}")

        after_hash = hashlib.sha256(content.encode()).hexdigest()
        changed = before_hash != after_hash
        return {"before_hash": before_hash, "after_hash": after_hash, "changed": changed}

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
