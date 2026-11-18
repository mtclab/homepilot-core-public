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
    # The safe form a refusal names (#817), e.g. ``ps -eo pid,user,comm,...``.
    hint: str


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


@dataclass(frozen=True)
class _UnitFiles:
    """The policy's ``unit_files`` section (#853). Which files are unit-shaped
    (``dirs``, the name patterns) and the secret-source keys are the agent's
    alone - it sees the resolved path and scans the host's units; they are loaded
    here so a malformed policy fails on both sides. The rewrite keys and the
    mark drive the mirror below."""

    dirs: tuple[str, ...]
    unit_basename: re.Pattern[str]
    dropin_dir: re.Pattern[str]
    env_keys: frozenset[str]
    credential_keys: frozenset[str]
    secret_keys: frozenset[str]
    exec_keys: frozenset[str]
    file_keys: frozenset[str]
    credential_file_keys: frozenset[str]
    mark: str
    args_mark: str


def _load_units(raw: dict[str, Any]) -> _UnitFiles:
    u = raw["unit_files"]

    def lower(items: list[str]) -> frozenset[str]:
        return frozenset(item.lower() for item in items)

    return _UnitFiles(
        dirs=tuple(u["dirs"]),
        unit_basename=re.compile(u["unit_basename_regex"]),
        dropin_dir=re.compile(u["dropin_dir_regex"]),
        env_keys=lower(u["env_keys"]),
        credential_keys=lower(u["credential_keys"]),
        secret_keys=lower(u["secret_keys"]),
        exec_keys=lower(u["exec_keys"]),
        file_keys=lower(u["file_keys"]),
        credential_file_keys=lower(u["credential_file_keys"]),
        mark=str(u["mark"]),
        args_mark=str(u["args_mark"]),
    )


def _load() -> tuple[re.Pattern[str], dict[str, _Command], _ReadPaths]:
    raw: dict[str, Any] = json.loads(policy_text())
    meta = re.compile("[" + re.escape(raw["shell_metachars"]) + "]")
    commands = {
        name: _Command(
            pattern=re.compile(spec["pattern"]),
            read_tier=bool(spec["read_tier"]),
            path_checked=bool(spec.get("path_checked", False)),
            hint=str(spec.get("hint", "")),
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
_UNITS = _load_units(json.loads(policy_text()))


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
        "unit files, a few identity files, web server, certbot renewal and firewall "
        "configuration, and what HomePilot itself wrote"
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
        if spec.hint:
            return f"command form not allowed at the read tier: {stripped} ({spec.hint})"
        return f"command form not allowed at the read tier: {stripped}"
    if spec.path_checked:
        return read_path_refusal(stripped.split()[1])
    return None


def read_tier_commands() -> frozenset[str]:
    """The command names the READ tier offers (for tool descriptions and tests)."""
    return frozenset(name for name, spec in _COMMANDS.items() if spec.read_tier)


# --- systemd unit files (#853) ----------------------------------------------
#
# The agent returns every unit file and drop-in with its Environment= values
# (and SetCredential=, PrivateKey=, ...) redacted; the secret never leaves the
# host. This is the control plane's MIRROR of that rewrite (agent/go/
# unitfiles.go), byte for byte - the shared corpus's "unit_redaction" cases run
# against both. The control plane needs it to read a redacted file honestly:
# drift compares the desired content redacted the same way, and a rollback
# snapshot refuses to "restore" bytes that are only placeholders.

_UNIT_KEY = re.compile(r"([ \t]*(?:[#;][#; \t]*)?([A-Za-z][A-Za-z0-9]*)[ \t]*=)(.*)", re.DOTALL)
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_CREDENTIAL_ID = re.compile(r"[A-Za-z0-9_.@-]+")
_UNIT_TRIM = " \t\r"


def _is_unit_comment(line: str) -> bool:
    return line.lstrip(" \t").startswith(("#", ";"))


def _continues(line: str) -> bool:
    return line.rstrip("\r").endswith("\\")


def _logical_lines(text: str) -> list[tuple[list[str], str, str, str]]:
    """(physical lines, lower-cased key, prefix up to '=', value) per logical
    line; key is "" when the line is not an assignment."""
    lines = text.split("\n")
    out: list[tuple[list[str], str, str, str]] = []
    i = 0
    while i < len(lines):
        group = [lines[i]]
        if not _is_unit_comment(lines[i]) and _continues(lines[i]):
            while i + len(group) < len(lines):
                nxt = lines[i + len(group)]
                group.append(nxt)
                if not _is_unit_comment(nxt) and not _continues(nxt):
                    break
        i += len(group)
        joined = ""
        for j, line in enumerate(group):
            if j > 0 and _is_unit_comment(line):
                continue
            line = line.rstrip("\r")
            if line.endswith("\\"):
                line = line[:-1] + " "
            joined += line
        m = _UNIT_KEY.fullmatch(joined)
        if m:
            out.append((group, m.group(2).lower(), m.group(1), m.group(3)))
        else:
            out.append((group, "", "", ""))
    return out


def _unit_words(value: str) -> list[tuple[str, str]]:
    """Split an Environment= value into (unquoted text, opening quote) words."""
    words: list[tuple[str, str]] = []
    cur: list[str] = []
    quote = opened = ""
    in_word = False
    i = 0
    while i < len(value):
        c = value[i]
        if not quote:
            if c in " \t\r":
                if in_word:
                    words.append(("".join(cur), opened))
                    cur, in_word, opened = [], False, ""
            elif c in "\"'":
                if not in_word:
                    opened = c
                in_word = True
                quote = c
            elif c == "\\":
                in_word = True
                if i + 1 < len(value):
                    i += 1
                    cur.append(value[i])
            else:
                in_word = True
                cur.append(c)
        elif c == quote:
            quote = ""
        elif c == "\\" and quote == '"' and i + 1 < len(value):
            i += 1
            cur.append(value[i])
        else:
            cur.append(c)
        i += 1
    if in_word:
        words.append(("".join(cur), opened))
    return words


def _redact_env_word(text: str, quote: str) -> str:
    name, eq, _ = text.partition("=")
    out = f"{name}={_UNITS.mark}" if eq and _ENV_NAME.fullmatch(name) else _UNITS.mark
    return f"{quote}{out}{quote}" if quote else out


_EXEC_PREFIX_CHARS = "-@:+!|"


def _redact_exec(value: str) -> str | None:
    """An Exec*= value with its prefix characters and program kept and every
    argument dropped, or None when there is no argument to drop."""
    rest = value.lstrip(_EXEC_PREFIX_CHARS)
    prefix = value[: len(value) - len(rest)]
    words = _unit_words(rest)
    if len(words) < 2:
        return None
    program, quote = words[0]
    if quote or any(c in program for c in " \t\"'\\"):
        program = '"' + program.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return f"{prefix}{program} {_UNITS.args_mark}"


def redact_unit_text(text: str) -> tuple[str, int]:
    """The agent's unit-file rewrite: (redacted text, values replaced)."""
    count = 0
    out: list[str] = []
    for physical, key, prefix, raw_value in _logical_lines(text):
        value = raw_value.strip(_UNIT_TRIM)
        if key and key in _UNITS.env_keys:
            words = _unit_words(value)
            count += len(words)
            out.append(prefix + " ".join(_redact_env_word(w, q) for w, q in words))
        elif key and key in _UNITS.credential_keys:
            if not value:
                out.append(prefix)
                continue
            count += 1
            cred_id, colon, _ = value.partition(":")
            if colon and _CREDENTIAL_ID.fullmatch(cred_id):
                out.append(f"{prefix}{cred_id}:{_UNITS.mark}")
            else:
                out.append(prefix + _UNITS.mark)
        elif key and key in _UNITS.secret_keys:
            if not value:
                out.append(prefix)
                continue
            count += 1
            out.append(prefix + _UNITS.mark)
        elif key and key in _UNITS.exec_keys and (exec_line := _redact_exec(value)) is not None:
            count += 1
            out.append(prefix + exec_line)
        else:
            out.append(physical[0])
            for line in physical[1:]:
                if _is_unit_comment(line):
                    line, n = redact_unit_text(line)
                    count += n
                out.append(line)
    return "\n".join(out), count


def unit_read_is_redacted(content: str) -> bool:
    """Whether ``content``, read through the agent, carries the agent's
    placeholders instead of real values - so it is NOT the bytes on the host and
    must never be written back as if it were.

    The rewrite is a fixed point: text the agent redacted comes back unchanged
    from redacting it again, while text holding a real value does not (an
    older agent that did not redact returns the real bytes, which ARE
    restorable). Deliberately not keyed on the path: the agent also redacts
    under the unit directories an operator adds (HP_AGENT_UNIT_DIRS), which this
    side cannot see, and calling a non-unit file that happens to hold
    ``Environment=X=<redacted>`` unrestorable is the safe mistake."""
    redacted, count = redact_unit_text(content)
    return count > 0 and redacted == content
