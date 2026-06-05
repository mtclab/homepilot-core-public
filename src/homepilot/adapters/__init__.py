from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from homepilot.adapters.agent import (
    AgentAdapter,
    AgentAdapterError,
    GuestHostError,
    ReadOnlyCommandError,
)
from homepilot.adapters.proxmox import ProxmoxClient, ProxmoxError
from homepilot.adapters.ssh import SSHAdapter, SSHAdapterError


@runtime_checkable
class HostAdapter(Protocol):
    """Protocol for host command execution adapters (SSH or Agent)."""

    async def exec(self, host: str, command: str, timeout: int = 30) -> tuple[int, str, str]: ...
    async def exec_readonly(self, host: str, command: str) -> tuple[int, str, str]: ...
    async def read_file(self, host: str, path: str) -> str: ...
    async def write_file(self, host: str, path: str, content: str) -> dict[str, Any]: ...


__all__ = [
    "AgentAdapter",
    "AgentAdapterError",
    "GuestHostError",
    "HostAdapter",
    "ProxmoxClient",
    "ProxmoxError",
    "ReadOnlyCommandError",
    "SSHAdapter",
    "SSHAdapterError",
]
