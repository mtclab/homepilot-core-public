"""The read-only policy the control plane shares with the host agent (#682, #691).

There used to be two hand-kept copies of "what a READ-tier caller may run or read
on a managed host": regexes in ``adapters/agent.py`` and ``agent/go/allowlist.go``
for commands, and prefix/denylist tuples in ``mcp/tools/system_tools.py`` and
``agent/go/fileops.go`` for file reads. They drifted, and both were written as
"allow the command, deny a few bad arguments", which is how ``ip link set eth0
down``, ``ss -K``, ``systemctl daemon-reload``, ``cat /etc/homepilot/agent.env``
and ``/etc/ssh/ssh_host_ed25519_key`` all ended up readable or runnable at the
READ tier.

Now there is one policy, ``readonly_policy.json``. The Go agent embeds its copy
(``agent/go/readonly_policy.json``); this package ships a byte-identical one, and
a test on each side fails on any drift. Commands are positive-only (the whole
command must match its pattern) and file reads are default-deny (a path must be
allowlisted and must not hit a deny rule; deny always wins).

This module is the control plane's half: it cannot resolve symlinks on the host,
so its path check is LEXICAL. The agent re-checks the resolved path and is the
enforcement point; this layer is the control plane declining to ask.
"""

from __future__ import annotations

import json
import posixpath
import re
from dataclasses import dataclass
from importlib import resources
from typing import Any

POLICY_FILE = "readonly_policy.json"


@dataclass(frozen=True)
class _Command:
    pattern: re.Pattern[str]
    read_tier: bool
    path_checked: bool


@dataclass(frozen=True)
class _ReadPaths:
    allow_prefixes: tuple[str, ...]
    allow_exact: frozenset[str]
    write_prefixes: tuple[str, ...]
    deny_exact: frozenset[str]
    deny_prefixes: tuple[str, ...]
    deny_path: tuple[re.Pattern[str], ...]
    deny_basename: tuple[re.Pattern[str], ...]


def policy_text() -> str:
    """The raw policy file as shipped in this package."""
    return resources.files(__package__).joinpath(POLICY_FILE).read_text(encoding="utf-8")


def _load() -> tuple[re.Pattern[str], dict[str, _Command], _ReadPaths]:
    raw: dict[str, Any] = json.loads(policy_text())
    meta = re.compile("[" + re.escape(raw["shell_metachars"]) + "]")
    commands = {
        name: _Command(
            pattern=re.compile(spec["pattern"]),
            read_tier=bool(spec["read_tier"]),
            path_checked=bool(spec.get("path_checked", False)),
        )
        for name, spec in raw["commands"].items()
    }
    rp = raw["read_paths"]
    paths = _ReadPaths(
        allow_prefixes=tuple(rp["allow_prefixes"]),
        allow_exact=frozenset(rp["allow_exact"]),
        write_prefixes=tuple(rp["default_write_prefixes"]),
        deny_exact=frozenset(rp["deny_exact"]),
        deny_prefixes=tuple(rp["deny_prefixes"]),
        deny_path=tuple(re.compile(p) for p in rp["deny_path_regex"]),
        deny_basename=tuple(re.compile(p) for p in rp["deny_basename_regex"]),
    )
    return meta, commands, paths


_SHELL_META, _COMMANDS, _PATHS = _load()


def _under(path: str, prefixes: tuple[str, ...]) -> bool:
    """Boundary-aware prefix match: "/etc/x/" matches "/etc/x" and "/etc/x/y",
    never "/etc/xy"."""
    for prefix in prefixes:
        pre = prefix.rstrip("/")
        if pre and (path == pre or path.startswith(pre + "/")):
            return True
    return False


def read_path_refusal(path: str) -> str | None:
    """Why the READ tier may not read ``path`` on a managed host, or ``None``.

    Default-deny: the path must be one of the allowlisted files or sit under an
    allowlisted prefix (the agent's default write prefixes count, since what
    HomePilot wrote it must be able to read back), and it must not hit any deny
    rule - private keys, shadow files, PVE and HomePilot credentials, /root,
    process environments, dotfile credential stores. The phrases "parent
    traversal", "denied" and "not under an allowed read prefix" are relied on by
    callers and tests.
    """
    if not path:
        return "an empty path is not a file"
    if ".." in path.split("/"):
        return f"path '{path}' contains parent traversal"
    norm = posixpath.normpath(path)
    if not norm.startswith("/"):
        return f"path '{path}' must be an absolute path"
    # posixpath keeps a leading '//' (POSIX leaves it implementation-defined);
    # Linux does not, so neither may a match against '/etc/...'.
    norm = "/" + norm.lstrip("/")

    base = posixpath.basename(norm)
    if (
        norm in _PATHS.deny_exact
        or _under(norm, _PATHS.deny_prefixes)
        or any(rx.fullmatch(norm) for rx in _PATHS.deny_path)
        or any(rx.fullmatch(base) for rx in _PATHS.deny_basename)
    ):
        return f"path '{path}' is denied (credential or private key material)"

    if norm in _PATHS.allow_exact or _under(norm, _PATHS.allow_prefixes + _PATHS.write_prefixes):
        return None
    return (
        f"path '{path}' not under an allowed read prefix: the read tier serves logs, "
        "unit files, a few identity files and what HomePilot itself wrote"
    )


def readonly_command_refusal(command: str) -> str | None:
    """Why ``command`` may not run at the READ tier, or ``None`` if it may.

    Positive-only: the command's first word must name a read-tier entry of the
    shared policy and the WHOLE command must match that entry's pattern. A
    path-checked command (``cat``) additionally passes its path through
    :func:`read_path_refusal`, the same check ``read_file_on_guest`` applies.
    """
    stripped = command.strip()
    if not stripped:
        return "empty command"
    if _SHELL_META.search(stripped):
        return f"shell metacharacters forbidden: {command}"
    base = stripped.split()[0]
    spec = _COMMANDS.get(base)
    if spec is None or not spec.read_tier:
        return f"command not in the read-only allowlist: {base}"
    if not spec.pattern.fullmatch(stripped):
        return f"command form not allowed at the read tier: {stripped}"
    if spec.path_checked:
        return read_path_refusal(stripped.split()[1])
    return None


def read_tier_commands() -> frozenset[str]:
    """The command names the READ tier offers (for tool descriptions and tests)."""
    return frozenset(name for name, spec in _COMMANDS.items() if spec.read_tier)
