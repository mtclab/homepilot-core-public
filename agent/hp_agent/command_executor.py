from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from typing import ClassVar

logger = logging.getLogger(__name__)


class CommandAllowlist:
    _SHELL_METACHAR_RE = re.compile(r"[|;&`$<>!#()\[\]]")

    _SAFE_COMMANDS: ClassVar[dict[str, re.Pattern[str]]] = {
        # --- Read-only system info ---
        "cat": re.compile(r"^cat\s+[^\s]+$"),
        "ls": re.compile(r"^ls(\s+-[aAlrStZ]+)*(\s+[a-zA-Z0-9_./-]+)*$"),
        "ps": re.compile(r"^ps(\s+[a-zA-Z0-9_-]+)*$"),
        "hostname": re.compile(r"^hostname$"),
        "uname": re.compile(r"^uname(\s+-[aAmnrsv]+)*$"),
        "df": re.compile(r"^df(\s+-[hHiTkP]+)*(\s+[a-zA-Z0-9_./-]+)?$"),
        "free": re.compile(r"^free(\s+-[hkmgb]+)*$"),
        "uptime": re.compile(r"^uptime$"),
        "ip": re.compile(
            r"^ip\s+(addr|link|route|neigh)"
            r"(\s+[a-zA-Z0-9_./-]+)*$"
        ),
        "ss": re.compile(
            r"^ss(\s+-[a-zA-Z0-9]+)*"
            r"(\s+[a-zA-Z0-9_./-]+)?$"
        ),
        "systemctl": re.compile(
            r"^systemctl\s+"
            r"(status|is-active|is-enabled|daemon-reload)"
            r"(\s+[a-zA-Z0-9_.-]+(\.service)?)?$"
        ),
        "journalctl": re.compile(r"^journalctl(\s+-[a-zA-Z0-9]+(\s+\S+)?)*$"),
        "dpkg": re.compile(r"^dpkg\s+-[lSs](\s+[a-zA-Z0-9_.+-]+)*$"),
        # --- Docker (read-only) ---
        "docker": re.compile(
            r"^docker\s+"
            r"(ps|images|inspect|logs|stats|version|info)"
            r"(\s+[a-zA-Z0-9_.:/-]+)*$"
        ),
    }

    _PRIVILEGED_COMMANDS: ClassVar[dict[str, re.Pattern[str]]] = {
        # --- Package management ---
        "apt-get": re.compile(
            r"^apt-get\s+(install|update|upgrade)\s+"
            r"-y\s+[a-zA-Z0-9_.+-]+(\s+[a-zA-Z0-9_.+-]+)*$"
        ),
        "apt": re.compile(
            r"^apt\s+(install|update|upgrade)\s+"
            r"-y\s+[a-zA-Z0-9_.+-]+(\s+[a-zA-Z0-9_.+-]+)*$"
        ),
        # --- Docker management ---
        "docker-run": re.compile(
            r"^docker\s+run\s+"
            r"(\s+--[a-zA-Z]+[= ][a-zA-Z0-9_.:/-]+)*\s+"
            r"[a-zA-Z0-9_.:/-]+(\s+[a-zA-Z0-9_./-]+)*$"
        ),
        "docker-pull": re.compile(r"^docker\s+pull\s+[a-zA-Z0-9_.:/-]+$"),
        "docker-compose-up": re.compile(
            r"^docker\s+compose\s+"
            r"(-f\s+[a-zA-Z0-9_./-]+\s+)?"
            r"(up|down)\s+(-d\s+)?(--build\s+)?[a-zA-Z0-9_.-]*$"
        ),
        "docker-stop": re.compile(r"^docker\s+(stop|rm|restart)\s+[a-zA-Z0-9_.:-]+$"),
        # --- Systemctl management (start/stop/restart/enable/disable) ---
        "systemctl": re.compile(
            r"^systemctl\s+"
            r"(daemon-reload|start|stop|restart|enable|disable)"
            r"(\s+[a-zA-Z0-9_.-]+(\.service)?)?$"
        ),
        # --- File operations ---
        "mkdir": re.compile(r"^mkdir\s+-p\s+[a-zA-Z0-9_./-]+$"),
        "chmod": re.compile(r"^chmod\s+[0-7]{3,4}\s+[a-zA-Z0-9_./-]+$"),
        "cp": re.compile(r"^cp\s+[a-zA-Z0-9_./-]+\s+[a-zA-Z0-9_./-]+$"),
        "mv": re.compile(r"^mv\s+[a-zA-Z0-9_./-]+\s+[a-zA-Z0-9_./-]+$"),
    }

    _CAT_ALLOWED_PREFIXES: ClassVar[tuple[str, ...]] = (
        "/var/log/",
        "/etc/homepilot/",
        "/etc/hostname",
        "/etc/os-release",
        "/etc/resolv.conf",
        "/etc/hosts",
        "/etc/systemd/",
        "/opt/",
    )

    _ALLOWED_SUDO_COMMANDS: ClassVar[frozenset[str]] = frozenset(
        {
            "systemctl start",
            "systemctl stop",
            "systemctl restart",
            "systemctl enable",
            "systemctl disable",
            "apt-get install",
            "apt-get update",
            "apt install",
            "apt update",
            "docker pull",
            "docker run",
            "docker compose",
            "docker stop",
            "docker rm",
        }
    )

    def __init__(self, privileged: bool = False) -> None:
        self._privileged = privileged

    @classmethod
    def default(cls, privileged: bool = False) -> CommandAllowlist:
        return cls(privileged=privileged)

    def is_allowed(self, command: str) -> tuple[bool, str]:
        stripped = command.strip()
        if not stripped:
            return False, "empty command"

        if self._SHELL_METACHAR_RE.search(stripped):
            return False, f"shell metacharacters forbidden: {command}"

        uses_sudo = stripped.startswith("sudo ")
        actual_cmd = stripped[5:].strip() if uses_sudo else stripped

        if uses_sudo:
            if not self._privileged:
                return False, f"sudo requires privileged mode: {command}"
            allowed = any(actual_cmd.startswith(prefix) for prefix in self._ALLOWED_SUDO_COMMANDS)
            if not allowed:
                return False, f"sudo command not in allowlist: {actual_cmd}"
            return True, ""

        cmd_base = actual_cmd.split()[0] if actual_cmd.split() else ""

        # Check privileged patterns first (if privileged mode is on)
        if self._privileged:
            parts = actual_cmd.split()
            # Disambiguate docker subcommands
            if cmd_base == "docker" and len(parts) >= 2:
                sub = parts[1]
                sub_key = f"docker-{sub}" if sub in ("pull", "run") else None
                if sub == "compose":
                    sub_key = "docker-compose-up"
                if sub in ("stop", "rm", "restart"):
                    sub_key = "docker-stop"
                if sub_key and sub_key in self._PRIVILEGED_COMMANDS:
                    pattern = self._PRIVILEGED_COMMANDS[sub_key]
                    if pattern.match(actual_cmd):
                        return True, ""

            # Check other privileged commands by base command
            if cmd_base in self._PRIVILEGED_COMMANDS:
                pattern = self._PRIVILEGED_COMMANDS[cmd_base]
                if pattern.match(actual_cmd):
                    return True, ""

            # Also allow safe docker commands in privileged mode
            if cmd_base == "docker":
                pattern = self._SAFE_COMMANDS.get("docker")
                if pattern and pattern.match(actual_cmd):
                    return True, ""

        # Check safe commands
        pattern = self._SAFE_COMMANDS.get(cmd_base)
        if pattern is not None:
            if pattern.match(actual_cmd):
                if cmd_base == "cat":
                    return self._validate_cat_path(actual_cmd)
                return True, ""
            return False, f"command pattern not allowed: {actual_cmd}"

        if self._privileged:
            return False, f"privileged command not in allowlist: {actual_cmd}"

        return False, f"command not in allowlist: {cmd_base}"

    def _validate_cat_path(self, actual_cmd: str) -> tuple[bool, str]:
        parts = actual_cmd.split()
        if len(parts) < 2:
            return False, "cat requires a path argument"
        cat_path = parts[1]
        if ".." in cat_path:
            return False, f"path traversal forbidden: {cat_path}"
        if not any(cat_path == p or cat_path.startswith(p) for p in self._CAT_ALLOWED_PREFIXES):
            return False, f"cat path not in allowed prefixes: {cat_path}"
        return True, ""


class CommandExecutor:
    def __init__(self, allowlist: CommandAllowlist | None = None) -> None:
        self._allowlist = allowlist or CommandAllowlist.default()

    async def exec(self, command: str, timeout: int = 30) -> tuple[int, str, str]:
        allowed, reason = self._allowlist.is_allowed(command)
        if not allowed:
            return -1, "", f"command blocked: {reason}"

        try:
            proc = await asyncio.create_subprocess_exec(
                *command.split(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return (
                proc.returncode or 0,
                stdout_bytes.decode("utf-8", errors="replace"),
                stderr_bytes.decode("utf-8", errors="replace"),
            )
        except FileNotFoundError:
            return -1, "", f"command not found: {command.split()[0]}"
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            return -1, "", f"command timed out after {timeout}s"
        except Exception as exc:
            return -1, "", str(exc)
