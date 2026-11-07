from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


def format_validation_error(exc: Any) -> str:
    """A pydantic ValidationError as text that names fields, never values (#697).

    ``str(ValidationError)`` includes ``input_value=<what was sent>`` for every
    error, and this product takes credentials as arguments (a tailnet auth key,
    an approval code): stringifying the error put the key into the MCP
    transcript and every log the reply touched. Only ``loc`` and ``msg`` are
    used - which is all a caller needs to fix the call; the validators in this
    codebase name the FIELD in their messages rather than quoting the value.

    Every place a ValidationError becomes user-facing text goes through here;
    tests/test_697_validation_error_no_echo.py holds every MCP tool to it.
    """
    parts: list[str] = []
    for error in exc.errors(include_url=False, include_input=False, include_context=False):
        loc = ".".join(str(p) for p in error.get("loc", ()) if p != "__root__")
        msg = str(error.get("msg", "invalid value")).removeprefix("Value error, ")
        parts.append(f"{loc}: {msg}" if loc else msg)
    return "; ".join(parts) or "invalid input"


def write_private(path: Path | str, data: bytes | str) -> None:
    """Write a secret file: 0600 from the moment it exists, and atomically.

    The pattern this replaces - `write_text()` then `chmod(0o600)` - leaves the
    file readable under the process umask (typically 0644) until the chmod
    lands, and a crash in that window leaves it that way for good (#690; the
    same shape was fixed for the vault in #431). A partial write is the other
    half: the only copy of a key truncated by a crash or a full disk.

    So: a temp file in the SAME directory (os.replace is only atomic within a
    filesystem), created 0600 by mkstemp, fsync'd, then renamed over the target.
    The target therefore never exists with any other mode or partial content -
    including when it already existed with a looser mode, since the rename
    replaces the inode rather than rewriting it.
    """
    target = Path(path)
    payload = data.encode("utf-8") if isinstance(data, str) else data
    fd, tmp_name = tempfile.mkstemp(
        dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp"
    )
    tmp = Path(tmp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(tmp), str(target))
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def redact_endpoint(raw: str) -> str:
    """Reduce an address to scheme/host/port - never credentials or a path.

    Configured endpoints routinely CARRY the credential: an n8n webhook path is
    minted per workflow, a git remote can embed ``user:token@``, and an embedding
    URL can carry ``?key=``. Logs and diagnostics get this form, so the whole
    class is dropped rather than pattern-matched per secret.
    """
    value = raw.strip()
    if not value:
        return ""
    parts = urlsplit(value)
    if parts.scheme and parts.netloc:
        host = parts.hostname or ""
        if not host:
            return parts.scheme + "://"
        if ":" in host:  # IPv6 literal
            host = f"[{host}]"
        port = f":{parts.port}" if parts.port else ""
        return f"{parts.scheme}://{host}{port}"
    # scp-style git remote (git@host:path) or a bare host[:port].
    remainder = value.rsplit("@", 1)[-1]
    return remainder.split("/", 1)[0].split(":", 1)[0]


class SlidingWindowLimiter:
    """Per-key sliding-window counter, the shape already used for admin token
    creation and MCP approvals, extracted so there is ONE implementation.

    Process-local by design (same caveat as the HTTP middleware's limiter: with
    several workers each holds its own counters). ``max_keys`` bounds memory
    under a flood of distinct keys - the oldest keys are dropped, which costs an
    attacker nothing they did not already have.
    """

    def __init__(self, limit: int, window_seconds: float, max_keys: int = 10_000):
        self.limit = limit
        self.window_seconds = window_seconds
        self.max_keys = max_keys
        self._hits: dict[str, list[float]] = {}

    def allow(self, key: str) -> bool:
        """Record an attempt and report whether it is within the limit.

        A refused attempt is NOT recorded, so a client hammering a full window
        cannot extend its own penalty indefinitely.
        """
        now = time.time()
        window = [ts for ts in self._hits.get(key, []) if now - ts < self.window_seconds]
        if len(window) >= self.limit:
            self._hits[key] = window
            return False
        window.append(now)
        self._hits[key] = window
        if len(self._hits) > self.max_keys:
            oldest = sorted(self._hits, key=lambda k: self._hits[k][-1])
            for stale in oldest[: len(self._hits) - self.max_keys]:
                del self._hits[stale]
        return True

    def clear(self) -> None:
        self._hits.clear()


class APIError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400, detail: str | None = None):
        self.code = code
        self.message = message
        self.status_code = status_code
        self.detail = detail
        super().__init__(message)

    def to_dict(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.detail is not None:
            error["detail"] = self.detail
        return {"error": error}
