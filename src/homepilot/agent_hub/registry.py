from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from ..background import DEFAULT_DRAIN_TIMEOUT, drain_tasks
from .audit import AuditLog
from .replay import ReplaySession

if TYPE_CHECKING:
    from .server import AgentHubServer

logger = logging.getLogger(__name__)


def _conn_is_live(writer: asyncio.StreamWriter | None) -> bool:
    """True when a stored connection is still usable.

    A ``None`` writer (persisted/last-known entry, or a test registration) is not
    a live connection. ``is_closing()`` catches sockets that have begun teardown."""
    return writer is not None and not writer.is_closing()


@dataclass
class ConnectedAgent:
    agent_id: str
    hostname: str
    system_info: dict[str, Any] = field(default_factory=dict)
    state: dict[str, Any] = field(default_factory=dict)
    connected_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_heartbeat: datetime = field(default_factory=lambda: datetime.now(UTC))
    writer: asyncio.StreamWriter | None = None
    # Per-connection identity: a fresh id is minted for every accepted socket so
    # teardown of a stale/half-open connection can be told apart from the live
    # one that replaced it (compare-and-delete on unregister).
    conn_id: str | None = None
    # Per-connection replay session, set only when the connection negotiated
    # app-layer replay protection (#362 slice 3). Command dispatch reads it to
    # stamp outbound frames with a seq+MAC; None on unprotected connections.
    replay: ReplaySession | None = None
    _result_futures: dict[str, asyncio.Future[Any]] = field(default_factory=dict)


class AgentRegistry:
    def __init__(self, repo: Any = None, metrics_repo: Any = None) -> None:
        self._agents: dict[str, ConnectedAgent] = {}
        self._hostname_index: dict[str, str] = {}
        self.hub_server: AgentHubServer | None = None
        # Share the persistence repo with the audit log so the fleet-root command
        # trail is durable + attributable (#381), not just an in-memory deque.
        self.audit_log: AuditLog = AuditLog(repo=repo)
        # Optional Repository for persistence so agents survive a backend restart
        # (Agents view + coverage show last-known agents instead of flapping to
        # empty). Best-effort: never blocks or fails the hub.
        self._repo = repo
        # Optional MetricsRepository. None in unit tests that build a bare
        # registry; metric frames are then validated and acked but not stored.
        self.metrics_repo = metrics_repo
        # Outstanding fire-and-forget persistence writes (#496).
        self._persist_tasks: set[asyncio.Task[Any]] = set()

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
        # TRACKED, not merely fired: an untracked write still in aiosqlite's queue
        # when the database is closed can leave that close waiting forever (#496).
        # `drain()` is what lets a shutdown - or a test teardown - wait for these
        # instead of racing them.
        self._persist_tasks.add(task)

        def _log_err(t: asyncio.Task[Any]) -> None:
            self._persist_tasks.discard(t)
            if not t.cancelled() and t.exception():
                logger.warning("agent persistence failed: %s", t.exception())

        task.add_done_callback(_log_err)

    async def drain(self, timeout: float = DEFAULT_DRAIN_TIMEOUT) -> None:
        """Wait for outstanding persistence writes, then give up on the rest.

        Called before the hub's database goes away.
        """
        await drain_tasks(self._persist_tasks, "agent persistence write(s)", timeout)
        await self.audit_log.drain(timeout)

    def register(
        self,
        agent_id: str,
        hostname: str,
        system_info: dict[str, Any] | None = None,
        writer: asyncio.StreamWriter | None = None,
        conn_id: str | None = None,
    ) -> bool:
        """Register (or replace) an agent connection.

        Returns ``False`` and leaves the existing registration untouched when the
        registration would hijack another LIVE agent's identity:

        * the claimed ``hostname`` already maps to a *different* live agent, or
        * the ``agent_id`` is live on a different host (a stolen id replayed
          from elsewhere).

        A re-register with the same ``agent_id`` from the same ``hostname`` is a
        legitimate reconnect and replaces the prior (now stale) entry."""
        holder_id = self._hostname_index.get(hostname)
        if holder_id is not None and holder_id != agent_id:
            holder = self._agents.get(holder_id)
            if holder is not None and _conn_is_live(holder.writer):
                logger.warning(
                    "register rejected: hostname %s held by live agent %s, refusing "
                    "claim from agent %s",
                    hostname,
                    holder_id,
                    agent_id,
                )
                return False

        existing = self._agents.get(agent_id)
        if (
            existing is not None
            and existing.hostname != hostname
            and _conn_is_live(existing.writer)
        ):
            logger.warning(
                "register rejected: agent_id %s live on host %s, refusing claim from host %s",
                agent_id,
                existing.hostname,
                hostname,
            )
            return False

        # A reconnect replaces a prior entry for the same agent_id; make sure a
        # now-orphaned hostname index entry from that prior registration is not
        # left dangling if the hostname changed.
        if (
            existing is not None
            and existing.hostname != hostname
            and self._hostname_index.get(existing.hostname) == agent_id
        ):
            self._hostname_index.pop(existing.hostname, None)

        agent = ConnectedAgent(
            agent_id=agent_id,
            hostname=hostname,
            system_info=system_info or {},
            writer=writer,
            conn_id=conn_id,
        )
        self._agents[agent_id] = agent
        self._hostname_index[hostname] = agent_id
        logger.info("registered agent %s (hostname=%s)", agent_id, hostname)
        if self._repo is not None:
            self._persist(
                self._repo.upsert_agent(agent_id, hostname, system_info or {}, {}, connected=True)
            )
        return True

    def unregister(
        self, agent_id: str, conn_id: str | None = None, reason: str | None = None
    ) -> None:
        """Remove an agent, but only if the caller owns the current connection.

        ``reason`` is why the connection ended, persisted as the agent's last
        error so a dark host can explain itself (#430). It is optional because
        the operator-driven paths (forget an agent) are not a disconnect anyone
        needs a reason for.

        Compare-and-delete: when ``conn_id`` is supplied it must match the id
        stored on the current registration. A stale/half-open socket closing must
        not evict an agent that has already reconnected under a new connection."""
        current = self._agents.get(agent_id)
        if current is None:
            return
        if conn_id is not None and current.conn_id is not None and current.conn_id != conn_id:
            logger.info(
                "unregister skipped for %s: stale connection %s (current %s)",
                agent_id,
                conn_id,
                current.conn_id,
            )
            return
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
                self._persist(self._repo.mark_agent_disconnected(agent_id, reason))

    def disconnect(self, agent_id: str, reason: str) -> bool:
        """Close an agent's live channel now. Returns True if one was open (#430).

        Revocation used to take effect only on the agent's NEXT connect - and the
        connection is long-lived by design, so "next" could be never. A revoked
        agent therefore kept a fleet-root exec/write channel open for as long as
        its socket survived, which is the opposite of what an operator pressing
        Revoke is asking for.

        Closing the writer is what ends the channel; the connection's own
        teardown then runs `unregister`, so the reason recorded here is the one
        the fleet list shows. The in-memory entry is dropped immediately so the
        agent reads as gone the moment the operator acts, rather than when the
        socket finishes closing."""
        agent = self._agents.get(agent_id)
        if agent is None:
            return False
        writer = agent.writer
        self.unregister(agent_id, reason=reason)
        if writer is None or not _conn_is_live(writer):
            return False
        try:
            writer.close()
        except Exception as exc:  # pragma: no cover - a closed transport is fine
            logger.warning("could not close the channel for agent %s: %s", agent_id, exc)
            return False
        logger.warning("closed the live channel for agent %s: %s", agent_id, reason)
        return True

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
        """Fold the agent's freshest reported values into its live state.

        Called from ONE place: the metrics-frame ingest (ADR-004 S5). Metric
        frames arrive on an interval, which is the right cadence to persist a
        fresh heartbeat without writing on every keepalive — and, unlike the
        removed ``report_state`` action, it is a channel the agent actually
        uses, so the persisted ``last_heartbeat`` moves (#430)."""
        agent = self._agents.get(agent_id)
        if agent:
            agent.state.update(state)
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
