from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from .audit import AuditLog

if TYPE_CHECKING:
    from .server import AgentHubServer

logger = logging.getLogger(__name__)


@dataclass
class ConnectedAgent:
    agent_id: str
    hostname: str
    system_info: dict[str, Any] = field(default_factory=dict)
    state: dict[str, Any] = field(default_factory=dict)
    connected_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_heartbeat: datetime = field(default_factory=lambda: datetime.now(UTC))
    writer: asyncio.StreamWriter | None = None
    _result_futures: dict[str, asyncio.Future[Any]] = field(default_factory=dict)


class AgentRegistry:
    def __init__(self, repo: Any = None) -> None:
        self._agents: dict[str, ConnectedAgent] = {}
        self._hostname_index: dict[str, str] = {}
        self.hub_server: AgentHubServer | None = None
        self.audit_log: AuditLog = AuditLog()
        # Optional Repository for persistence so agents survive a backend restart
        # (Agents view + coverage show last-known agents instead of flapping to
        # empty). Best-effort: never blocks or fails the hub.
        self._repo = repo

    def _persist(self, coro: Any) -> None:
        if self._repo is None:
            coro.close()
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            coro.close()  # no running loop (e.g. unit test) — skip
            return
        task = loop.create_task(coro)

        def _log_err(t: asyncio.Task[Any]) -> None:
            if not t.cancelled() and t.exception():
                logger.warning("agent persistence failed: %s", t.exception())

        task.add_done_callback(_log_err)

    def register(
        self,
        agent_id: str,
        hostname: str,
        system_info: dict[str, Any] | None = None,
        writer: asyncio.StreamWriter | None = None,
    ) -> None:
        agent = ConnectedAgent(
            agent_id=agent_id,
            hostname=hostname,
            system_info=system_info or {},
            writer=writer,
        )
        self._agents[agent_id] = agent
        self._hostname_index[hostname] = agent_id
        logger.info("registered agent %s (hostname=%s)", agent_id, hostname)
        if self._repo is not None:
            self._persist(
                self._repo.upsert_agent(agent_id, hostname, system_info or {}, {}, connected=True)
            )

    def unregister(self, agent_id: str) -> None:
        agent = self._agents.pop(agent_id, None)
        if agent:
            # Only clear the hostname index if it still points at THIS agent —
            # a newer agent with the same hostname may have replaced it.
            if self._hostname_index.get(agent.hostname) == agent_id:
                self._hostname_index.pop(agent.hostname, None)
            for fut in agent._result_futures.values():
                if not fut.done():
                    fut.set_exception(ConnectionError(f"agent {agent_id} disconnected"))
            logger.info("unregistered agent %s", agent_id)
            if self._repo is not None:
                self._persist(self._repo.mark_agent_disconnected(agent_id))

    def get(self, agent_id: str) -> ConnectedAgent | None:
        return self._agents.get(agent_id)

    def get_by_hostname(self, hostname: str) -> ConnectedAgent | None:
        agent_id = self._hostname_index.get(hostname)
        if agent_id:
            return self._agents.get(agent_id)
        return None

    def is_connected(self, hostname: str) -> bool:
        return hostname in self._hostname_index

    def update_heartbeat(self, agent_id: str) -> None:
        agent = self._agents.get(agent_id)
        if agent:
            agent.last_heartbeat = datetime.now(UTC)

    def update_state(self, agent_id: str, state: dict[str, Any]) -> None:
        agent = self._agents.get(agent_id)
        if agent:
            agent.state.update(state)
            # State pushes are periodic — a good cadence to persist fresh
            # metrics + heartbeat without writing on every keepalive.
            if self._repo is not None:
                self._persist(self._repo.touch_agent(agent_id, agent.state))

    def store_command_result(self, agent_id: str, msg: dict[str, Any]) -> None:
        agent = self._agents.get(agent_id)
        if not agent:
            return
        request_id = msg.get("request_id", "")
        if not request_id:
            return
        fut = agent._result_futures.pop(request_id, None)
        if fut and not fut.done():
            fut.set_result(msg)

    async def wait_for_result(self, agent_id: str, request_id: str) -> dict[str, Any]:
        agent = self._agents.get(agent_id)
        if not agent:
            raise ConnectionError(f"agent {agent_id} not connected")
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        agent._result_futures[request_id] = fut
        try:
            return await fut
        finally:
            # Don't leak the future if the caller's timeout fired before a result
            agent._result_futures.pop(request_id, None)

    def list_connected(self) -> list[dict[str, Any]]:
        now = datetime.now(UTC)
        result = []
        for agent in self._agents.values():
            result.append(
                {
                    "agent_id": agent.agent_id,
                    "hostname": agent.hostname,
                    "system_info": agent.system_info,
                    "state": agent.state,
                    "connected_at": agent.connected_at.isoformat(),
                    "last_heartbeat": agent.last_heartbeat.isoformat(),
                    "stale_seconds": int((now - agent.last_heartbeat).total_seconds()),
                }
            )
        return result
