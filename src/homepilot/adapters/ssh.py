"""Host-adapter error types and shared validation.

The SSH/JumpServer transport was removed — the agent hub (AgentAdapter)
replaced it as the only host-management path. This module survives as the
home of the shared error hierarchy (still raised by the agent path and the
ansible executor) and the hostname validator that rejects injection.
"""

from __future__ import annotations

import re


class SSHAdapterError(Exception):
    """Base error for host-adapter operations (historical name kept for the
    ansible executor and tests that import it)."""


class GuestHostError(SSHAdapterError):
    """Raised when an operation targets a PVE node directly — guests only."""


class ReadOnlyCommandError(SSHAdapterError):
    """Raised when a command is not in the read-only allowlist."""


_HOSTNAME_RE = re.compile(r"^[a-zA-Z0-9]([a-zA-Z0-9.-]{0,253}[a-zA-Z0-9])?$")


def _validate_hostname(host: str) -> None:
    """Reject hostnames carrying shell/path-injection (newlines, ';', '..',
    empty). Used wherever a hostname reaches a command or a path."""
    if not _HOSTNAME_RE.match(host):
        raise GuestHostError(f"Invalid hostname: {host!r}")
