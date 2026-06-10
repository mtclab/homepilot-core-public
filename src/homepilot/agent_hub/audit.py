from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

logger = logging.getLogger(__name__)

ActionType = Literal["exec", "read_file", "write_file"]
ResultType = Literal["success", "blocked", "error"]


@dataclass
class AuditEntry:
    timestamp: str
    agent_id: str
    action: ActionType
    command_or_path: str
    result: ResultType
    exit_code: int | None = None


class AuditLog:
    def __init__(self, max_entries: int = 1000) -> None:
        self._entries: deque[AuditEntry] = deque(maxlen=max_entries)

    def log(
        self,
        agent_id: str,
        action: ActionType,
        command_or_path: str,
        result: ResultType,
        exit_code: int | None = None,
    ) -> AuditEntry:
        ts = datetime.now(UTC).isoformat()
        entry = AuditEntry(
            timestamp=ts,
            agent_id=agent_id,
            action=action,
            command_or_path=command_or_path,
            result=result,
            exit_code=exit_code,
        )
        self._entries.append(entry)
        logger.info(
            "audit: agent=%s action=%s target=%s result=%s exit_code=%s",
            entry.agent_id,
            entry.action,
            entry.command_or_path,
            entry.result,
            entry.exit_code,
        )
        return entry

    def query(self, limit: int = 100) -> list[dict[str, Any]]:
        entries = list(self._entries)
        recent = entries[-limit:]
        return [
            {
                "timestamp": e.timestamp,
                "agent_id": e.agent_id,
                "action": e.action,
                "command_or_path": e.command_or_path,
                "result": e.result,
                "exit_code": e.exit_code,
            }
            for e in recent
        ]

    def clear(self) -> None:
        self._entries.clear()
