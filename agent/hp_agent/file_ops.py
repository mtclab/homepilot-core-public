from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_ALLOWED_WRITE_PREFIXES = (
    "/etc/homepilot/",
    "/opt/homepilot/",
    "/tmp/homepilot/",
    "/etc/systemd/system/",
    "/etc/docker/",
    "/etc/nginx/",
    "/etc/zabbix/",
)

_ALLOWED_READ_PREFIXES = (
    "/var/log/",
    "/etc/",
    "/opt/homepilot/",
    "/proc/",
    "/sys/",
    "/tmp/homepilot/",
    "/home/",
    "/usr/local/bin/",
)


class FileOpsError(Exception):
    pass


class FileOps:
    def _validate_path(self, path: str, prefixes: tuple[str, ...]) -> None:
        resolved = Path(path).resolve()
        if ".." in path:
            raise FileOpsError(f"path traversal forbidden: {path}")
        if not str(resolved).startswith(prefixes):
            allowed = ", ".join(prefixes)
            raise FileOpsError(f"path not in allowed prefixes ({allowed}): {path}")

    async def read_file(self, path: str) -> str:
        resolved = Path(path).resolve()
        if ".." in path:
            raise FileOpsError(f"path traversal forbidden: {path}")
        if not resolved.exists():
            raise FileOpsError(f"file not found: {path}")
        if not resolved.is_file():
            raise FileOpsError(f"not a file: {path}")
        if not os.access(resolved, os.R_OK):
            raise FileOpsError(f"permission denied: {path}")

        def _read() -> str:
            return resolved.read_text(encoding="utf-8", errors="replace")

        return await asyncio.to_thread(_read)

    async def write_file(self, path: str, content: str) -> None:
        resolved = Path(path).resolve()
        self._validate_path(path, _ALLOWED_WRITE_PREFIXES)

        def _write() -> None:
            resolved.parent.mkdir(parents=True, exist_ok=True)
            resolved.write_text(content, encoding="utf-8")

        await asyncio.to_thread(_write)
