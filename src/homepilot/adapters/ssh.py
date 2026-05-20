from __future__ import annotations

import hashlib
import logging
import re
from typing import Any

from homepilot.jumpserver.client import JumpServerClient, JumpServerError

logger = logging.getLogger(__name__)

_SHELL_METACHAR_RE = re.compile(r"[|;&`$<>!#\(\){}\[\]]")

_CAT_ALLOWED_PREFIXES = (
    "/var/log/",
    "/etc/homepilot/",
    "/etc/hostname",
    "/etc/os-release",
    "/etc/resolv.conf",
    "/etc/hosts",
)

_SERVICE_NAME_RE = re.compile(r"^[a-zA-Z0-9_.:-]+\.service$")


class SSHAdapterError(Exception):
    pass


class GuestHostError(SSHAdapterError):
    pass


class ReadOnlyCommandError(SSHAdapterError):
    pass


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


class SSHAdapter:
    def __init__(self, jump_client: JumpServerClient):
        self._client = jump_client

    async def exec(self, host: str, command: str, timeout: int = 30) -> tuple[int, str, str]:
        try:
            result = await self._client.exec(host=host, command=command, timeout=timeout)
        except JumpServerError as exc:
            raise SSHAdapterError(str(exc)) from exc
        except ConnectionError as exc:
            raise SSHAdapterError(f"jump server connection lost: {exc}") from exc
        return (result["exit_code"], result["stdout"], result["stderr"])

    async def test_connection(self, host: str) -> bool:
        try:
            result = await self._client.exec(host=host, command="true", timeout=5)
            exit_code: Any = result["exit_code"]
            return bool(exit_code == 0)
        except (JumpServerError, ConnectionError, OSError):
            return False

    async def read_file(self, host: str, path: str) -> str:
        try:
            result = await self._client.read_file(host=host, path=path)
        except JumpServerError as exc:
            raise SSHAdapterError(str(exc)) from exc
        except ConnectionError as exc:
            raise SSHAdapterError(f"jump server connection lost: {exc}") from exc
        content: Any = result.get("content", "")
        return str(content)

    async def write_file(self, host: str, path: str, content: str) -> dict[str, Any]:
        before_hash: str | None = None
        try:
            existing = await self._client.read_file(host=host, path=path)
            before_hash = hashlib.sha256(existing.get("content", "").encode()).hexdigest()
        except (JumpServerError, ConnectionError):
            pass

        try:
            await self._client.write_file(host=host, path=path, content=content)
        except JumpServerError as exc:
            raise SSHAdapterError(str(exc)) from exc
        except ConnectionError as exc:
            raise SSHAdapterError(f"jump server connection lost: {exc}") from exc

        after_hash = hashlib.sha256(content.encode()).hexdigest()
        changed = before_hash != after_hash
        return {"before_hash": before_hash, "after_hash": after_hash, "changed": changed}

    async def exec_readonly(self, host: str, command: str) -> tuple[int, str, str]:
        if not self._validate_readonly_command(command):
            raise ReadOnlyCommandError(f"command not in read-only whitelist: {command}")
        return await self.exec(host=host, command=command)

    def _validate_guest_only(self, host: str, pve_nodes: list[str]) -> None:
        host_lower = host.lower().strip()
        for node in pve_nodes:
            if host_lower == node.lower().strip():
                raise GuestHostError(
                    f"SSH to PVE node '{host}' is forbidden — use Proxmox API instead"
                )

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
        if cmd_base == "systemctl":
            parts = stripped.split()
            if len(parts) >= 3:
                svc = parts[2]
                if not svc.endswith(".service"):
                    svc = f"{svc}.service"
                if not _SERVICE_NAME_RE.match(svc):
                    return False
                if ".." in svc:
                    return False
        return True
