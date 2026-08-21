from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlsplit


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
