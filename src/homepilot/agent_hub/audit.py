from __future__ import annotations

import asyncio
import logging
from collections import deque
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from ..background import DEFAULT_DRAIN_TIMEOUT, drain_tasks

logger = logging.getLogger(__name__)

ActionType = Literal[
    "exec",
    "read_file",
    "write_file",
    "register",
    "register_rejected",
    "disconnect",
    # #397 phase-B1 native provisioning actions.
    "install_package",
    "manage_service",
    "write_config",
    # #468: move an already-enrolled agent onto TLS without touching its host.
    # Audited like any other fleet-wide instruction - it changes how a machine
    # authenticates its control plane, which is worth a record of who asked.
    "set_transport",
]
ResultType = Literal["success", "blocked", "error"]

# Caller identity for the in-flight command-dispatch request. The admin-authed
# API endpoints (agent_hub/router.py) set this from the authenticated token so
# the audit records WHO issued a fleet-root command, without threading the
# identity through send_command/adapter signatures. Unset (None) means the call
# did not originate from an attributed API request (server lifecycle, MCP, a
# unit test) and is recorded as "unknown".
_audit_caller_var: ContextVar[str | None] = ContextVar("audit_caller", default=None)


def set_audit_caller(caller: str | None) -> None:
    """Set the caller identity for audit entries logged in this context."""
    _audit_caller_var.set(caller)


def get_audit_caller() -> str | None:
    """The caller identity for the current context, or ``None`` when unset."""
    return _audit_caller_var.get()


@dataclass
class AuditEntry:
    timestamp: str
    agent_id: str
    action: ActionType
    command_or_path: str
    result: ResultType
    exit_code: int | None = None
    hostname: str | None = None
    caller: str = "unknown"


class AuditLog:
    def __init__(self, max_entries: int = 1000, repo: Any = None) -> None:
        self._entries: deque[AuditEntry] = deque(maxlen=max_entries)
        # Optional Repository for durable persistence (#381). When present, each
        # entry is mirrored to the agent_audit table best-effort (fire-and-forget)
        # so the fleet-root command trail survives a backend restart and carries
        # caller attribution. With no repo (or no running loop) the log is
        # in-memory only, unchanged.
        self._repo = repo
        # Outstanding fire-and-forget audit writes, so a shutdown can drain them
        # rather than close the database under them (#496).
        self._persist_tasks: set[asyncio.Task[Any]] = set()

    def log(
        self,
        agent_id: str,
        action: ActionType,
        command_or_path: str,
        result: ResultType,
        exit_code: int | None = None,
        hostname: str | None = None,
        caller: str | None = None,
    ) -> AuditEntry:
        ts = datetime.now(UTC).isoformat()
        # Resolve the caller synchronously at log time so the persisted value is
        # correct regardless of the fire-and-forget task's context. An explicit
        # caller (e.g. "system" for lifecycle events) wins; otherwise read the
        # request-scoped contextvar, defaulting to "unknown".
        resolved_caller = caller if caller is not None else (_audit_caller_var.get() or "unknown")
        entry = AuditEntry(
            timestamp=ts,
            agent_id=agent_id,
            action=action,
            command_or_path=command_or_path,
            result=result,
            exit_code=exit_code,
            hostname=hostname,
            caller=resolved_caller,
        )
        self._entries.append(entry)
        logger.info(
            "audit: caller=%s agent=%s host=%s action=%s target=%s result=%s exit_code=%s",
            entry.caller,
            entry.agent_id,
            entry.hostname,
            entry.action,
            entry.command_or_path,
            entry.result,
            entry.exit_code,
        )
        self._persist(entry)
        return entry

    def _persist(self, entry: AuditEntry) -> None:
        """Best-effort mirror of an entry to the DB. Never blocks or raises into
        the caller: with no repo / no running loop it is a no-op (in-memory
        deque remains the record); a persistence failure is logged, not raised."""
        if self._repo is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no running loop (e.g. unit test) — in-memory only
        coro = self._repo.log_agent_audit(
            agent_id=entry.agent_id,
            action=entry.action,
            target=entry.command_or_path,
            result=entry.result,
            exit_code=entry.exit_code,
            hostname=entry.hostname,
            caller=entry.caller,
            ts=entry.timestamp,
        )
        task = loop.create_task(coro)
        # TRACKED, like the registry's writes (#496). An audit write is a DB
        # write like any other: left in flight when the loop goes away it kills
        # aiosqlite's worker thread. Most survive incidentally because a
        # registry write is queued behind them and drained - but a rejection
        # audit on a banned peer has no registry write to hide behind.
        self._persist_tasks.add(task)

        def _log_err(t: asyncio.Task[Any]) -> None:
            self._persist_tasks.discard(t)
            if not t.cancelled() and t.exception():
                logger.warning("agent audit persistence failed: %s", t.exception())

        task.add_done_callback(_log_err)

    async def drain(self, timeout: float = DEFAULT_DRAIN_TIMEOUT) -> None:
        """Wait for outstanding audit writes before the database goes away."""
        await drain_tasks(self._persist_tasks, "agent audit write(s)", timeout)

    def _entry_to_dict(self, e: AuditEntry) -> dict[str, Any]:
        return {
            "timestamp": e.timestamp,
            "agent_id": e.agent_id,
            "hostname": e.hostname,
            "action": e.action,
            "command_or_path": e.command_or_path,
            "result": e.result,
            "exit_code": e.exit_code,
            "caller": e.caller,
        }

    def query(self, limit: int = 100) -> list[dict[str, Any]]:
        """Synchronous in-memory view (fast fallback). Prefer :meth:`query_persisted`
        on an async caller when a repo is wired so the durable trail is returned."""
        entries = list(self._entries)
        recent = entries[-limit:]
        return [self._entry_to_dict(e) for e in recent]

    async def query_persisted(
        self,
        limit: int = 100,
        agent_id: str | None = None,
        action: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return audit entries, newest-first, preferring the durable DB record
        when a repo is available (so the trail survives a restart). Falls back to
        the in-memory deque when there is no repo or the DB read fails.

        ``agent_id`` / ``action`` narrow the trail. The repository has always
        implemented them and nothing passed them through (#430), which is why
        "why did THIS host get turned away" meant reading 100 mixed rows."""
        if self._repo is not None:
            try:
                rows = await self._repo.query_agent_audit(
                    limit=limit, agent_id=agent_id, action=action
                )
            except Exception as exc:  # pragma: no cover - defensive fallback
                logger.warning("agent audit DB query failed, using in-memory log: %s", exc)
            else:
                return [
                    {
                        "timestamp": r.get("ts"),
                        "agent_id": r.get("agent_id"),
                        "hostname": r.get("hostname"),
                        "action": r.get("action"),
                        "command_or_path": r.get("target"),
                        "result": r.get("result"),
                        "exit_code": r.get("exit_code"),
                        "caller": r.get("caller"),
                    }
                    for r in rows
                ]
        # No repo (or DB read failed): newest-first view of the deque. Filter
        # BEFORE the limit, so a narrowed query returns `limit` matching rows and
        # not "however many of the last `limit` rows happened to match".
        entries = [
            e
            for e in self._entries
            if (agent_id is None or e.agent_id == agent_id)
            and (action is None or e.action == action)
        ]
        recent = entries[-limit:]
        return [self._entry_to_dict(e) for e in reversed(recent)]

    def clear(self) -> None:
        self._entries.clear()
