"""Gate: shipped MCP tool descriptions must not advertise removed transports (#432).

Every tool description in ``_TOOL_DEFINITIONS`` is sent verbatim to every
connected LLM client. Three of them still described the SSH/jump-server
transport that was removed in #327:

* ``read_file_on_guest``  - "via SFTP through the jump server"
* ``exec_on_guest_readonly`` - "on a guest via SSH"
* ``refresh_inventory`` - "Proxmox API and SSH-to-guests"

A wrong description is worse than a missing one: the model plans around a
capability that does not exist, and ``orchestrator.host_adapter`` raises
"no host adapter - the agent hub is required for host operations" when it tries.

Teeth: put "via SSH" or "jump server" back into any tool description and this
fails naming the tool.
"""

from __future__ import annotations

import re

import pytest

from homepilot.mcp.server import _TOOL_DEFINITIONS

# Transports that no longer exist. Matched case-insensitively as whole words so
# "sshd_config" in an allowlist example would not trip it.
_REMOVED_TRANSPORTS = (
    r"\bssh\b",
    r"\bsftp\b",
    r"\bscp\b",
    r"\bjump[ -]server\b",
    r"\bproxycommand\b",
)


def _descriptions() -> list[tuple[str, str]]:
    return [(t.get("name", "<unnamed>"), t.get("description", "")) for t in _TOOL_DEFINITIONS]


def test_tool_definitions_are_present() -> None:
    """Guard the guard: an empty list would make the real test vacuously pass."""
    names = [n for n, _ in _descriptions()]
    assert len(names) >= 5, f"expected the shipped MCP tool set, got {names}"


@pytest.mark.parametrize("pattern", _REMOVED_TRANSPORTS)
def test_no_tool_description_advertises_a_removed_transport(pattern: str) -> None:
    offenders = [
        f"{name}: {desc!r}"
        for name, desc in _descriptions()
        if re.search(pattern, desc, re.IGNORECASE)
    ]
    assert not offenders, (
        f"MCP tool description(s) advertise the removed {pattern!r} transport "
        f"to every LLM client: {offenders}"
    )
