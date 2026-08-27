from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any, ClassVar

from .connection import Database

logger = logging.getLogger(__name__)

_HOST_COLUMN_TYPES: dict[str, type] = {
    "proxmox_id": int,
    "hostname": str,
    "node": str,
    "host_type": str,
    "role": str,
    "ip_address": str,
    "fqdn": str,
    "status": str,
    "tags": str,
    "managed_by": str,
    "managed": int,
    "storage_pool": str,
    "os_info": str,
    "cpu_cores": int,
    "memory_mb": int,
    "disk_gb": int,
    "network_bridge": str,
    "vlan_id": int,
    "pve_status": str,
    "source": str,
    "description": str,
    "artifact_id": str,
    "import_state": str,
    "role_source": str,
    "ip_source": str,
    "owner": str,
    "absent_since": str,
    "pinned_fields": str,
}

_SERVICE_COLUMN_TYPES: dict[str, type] = {
    "host_id": str,
    "name": str,
    "runtime": str,
    "version": str,
    "status": str,
    "managed_by": str,
    "config": str,
}


def _sanitize_audit_field(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return value
    return value.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")


_HOST_COLUMNS: frozenset[str] = frozenset(
    {
        "proxmox_id",
        "hostname",
        "node",
        "host_type",
        "role",
        "ip_address",
        "fqdn",
        "status",
        "tags",
        "managed_by",
        "managed",
        "storage_pool",
        "os_info",
        "cpu_cores",
        "memory_mb",
        "disk_gb",
        "network_bridge",
        "vlan_id",
        "pve_status",
        "source",
        "description",
        "artifact_id",
        "import_state",
        "role_source",
        "ip_source",
        "owner",
        "absent_since",
        "pinned_fields",
    }
)

_SERVICE_COLUMNS: frozenset[str] = frozenset(
    {
        "host_id",
        "name",
        "runtime",
        "version",
        "status",
        "managed_by",
        "config",
    }
)

_ARTIFACT_STATUS_COLUMNS: frozenset[str] = frozenset(
    {
        "approved_by_json",
        "applied_at",
        "failed_at",
        "failure_reason",
        "superseded_by",
        "rejected_by_json",
        "revoked_by_json",
        "hash",
    }
)

_DOC_METADATA_COLUMNS: frozenset[str] = frozenset(
    {
        "title",
        "content",
        "kind",
        "target",
    }
)


def _validated_set_clause(
    columns: dict[str, Any],
    allowlist: frozenset[str],
) -> tuple[str, list[Any]]:
    invalid = set(columns) - allowlist
    if invalid:
        raise ValueError(f"Invalid columns: {invalid}")
    parts = ", ".join(f"{k} = ?" for k in columns)
    values = list(columns.values())
    return parts, values


# What "find that box" means: the name, where it runs, how to reach it, what it
# is for. A constant tuple, never caller text - it is interpolated into SQL.
_HOST_SEARCH_COLUMNS: tuple[str, ...] = (
    "hostname",
    "fqdn",
    "ip_address",
    "node",
    "role",
    "tags",
    "description",
    "owner",
    "os_info",
)


def now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def uuid4() -> str:
    return str(uuid.uuid4())


class Repository:
    def __init__(self, db: Database):
        self.db = db

    async def create_user(self, display_name: str = "admin", auth_source: str = "api_token") -> str:
        user_id = uuid4()
        await self.db.execute(
            "INSERT INTO users (id, display_name, auth_source, created_at) VALUES (?, ?, ?, ?)",
            (user_id, display_name, auth_source, now()),
        )
        await self.db.conn.commit()
        return user_id

    async def get_user_by_id(self, user_id: str) -> dict[str, Any] | None:
        row = await self.db.fetchone("SELECT * FROM users WHERE id = ?", (user_id,))
        return dict(row) if row is not None else None

    async def create_api_token(
        self,
        user_id: str,
        token_type: str,
        prefix: str,
        hash: str,
        scope: str | None = None,
        expires_at: str | None = None,
        role: str | None = None,
        label: str | None = None,
        fingerprint: str | None = None,
    ) -> str:
        token_id = uuid4()
        cols = ["id", "user_id", "token_type", "prefix", "hash", "created_at"]
        vals: list[Any] = [token_id, user_id, token_type, prefix, hash, now()]
        if scope is not None:
            cols.append("scope")
            vals.append(scope)
        if expires_at is not None:
            cols.append("expires_at")
            vals.append(expires_at)
        if role is not None:
            cols.append("role")
            vals.append(role)
        if label is not None:
            cols.append("label")
            vals.append(label)
        if fingerprint is not None:
            cols.append("fingerprint")
            vals.append(fingerprint)
        placeholders = ", ".join("?" for _ in vals)
        col_names = ", ".join(cols)
        await self.db.execute(
            f"INSERT INTO api_tokens ({col_names}) VALUES ({placeholders})",
            vals,
        )
        await self.db.conn.commit()
        return token_id

    async def list_tokens_for_user(self, user_id: str) -> list[dict[str, Any]]:
        sql = (
            "SELECT id, prefix, scope, role, label, token_type, "
            "created_at, last_used_at, expires_at "
            "FROM api_tokens WHERE user_id = ? ORDER BY created_at DESC"
        )
        rows = await self.db.fetchall(sql, (user_id,))
        return [dict(r) for r in rows]

    async def list_all_tokens(self) -> list[dict[str, Any]]:
        sql = (
            "SELECT id, prefix, scope, role, label, token_type, "
            "created_at, last_used_at, expires_at "
            "FROM api_tokens ORDER BY created_at DESC"
        )
        rows = await self.db.fetchall(sql)
        return [dict(r) for r in rows]

    async def count_live_api_tokens(self) -> int:
        """How many API tokens can still authenticate right now.

        "Live" is the whole rule behind the bootstrap exception in `hp token
        create`: revocation DELETES the row, so a live token is any row that has
        not expired. An instance with zero of them has no admin to mint through
        and is the only case where an unauthenticated mint is allowed.
        """
        rows = await self.db.fetchall(
            "SELECT COUNT(*) AS n FROM api_tokens WHERE expires_at IS NULL OR expires_at > ?",
            (now(),),
        )
        return int(rows[0]["n"]) if rows else 0

    async def get_token_by_prefix(self, prefix: str) -> dict[str, Any] | None:
        row = await self.db.fetchone("SELECT * FROM api_tokens WHERE prefix = ?", (prefix,))
        return dict(row) if row is not None else None

    async def touch_token_last_used(self, token_id: str) -> None:
        await self.db.execute(
            "UPDATE api_tokens SET last_used_at = ? WHERE id = ?", (now(), token_id)
        )
        await self.db.conn.commit()

    async def delete_token(self, token_id: str) -> None:
        await self.db.execute("DELETE FROM api_tokens WHERE id = ?", (token_id,))
        await self.db.conn.commit()

    async def update_token_fingerprint(self, token_id: str, fingerprint: str) -> None:
        await self.db.execute(
            "UPDATE api_tokens SET fingerprint = ? WHERE id = ?", (fingerprint, token_id)
        )
        await self.db.conn.commit()

    # ── Agent registry persistence (survives backend restart/update) ──────────
    async def upsert_agent(
        self,
        agent_id: str,
        hostname: str,
        system_info: dict[str, Any] | None = None,
        state: dict[str, Any] | None = None,
        connected: bool = True,
    ) -> None:
        import json as _json

        ts = now()
        await self.db.execute(
            """INSERT INTO agents
                 (agent_id, hostname, system_info, state, connected,
                  first_seen, connected_at, last_heartbeat, disconnected_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
               ON CONFLICT(agent_id) DO UPDATE SET
                 hostname=excluded.hostname,
                 system_info=excluded.system_info,
                 state=excluded.state,
                 connected=excluded.connected,
                 connected_at=excluded.connected_at,
                 last_heartbeat=excluded.last_heartbeat,
                 disconnected_at=NULL,
                 -- An agent that is BACK has no outstanding reason to explain
                 -- (#430). Clearing it here rather than in a second statement
                 -- keeps "connected" and "no last error" one atomic fact, so a
                 -- fleet list can never show a live agent still carrying the
                 -- revocation it was re-enrolled out of.
                 last_error=NULL,
                 last_error_at=NULL""",
            (
                agent_id,
                hostname,
                _json.dumps(system_info or {}),
                _json.dumps(state or {}),
                int(connected),
                ts,
                ts,
                ts,
            ),
        )
        await self.db.conn.commit()
        # One noun: Host (#514 S1). An enrolled agent's machine must exist in
        # inventory - "connected agent, empty Inventory, Coverage 0%" was the
        # walk's headline defect. Same transaction boundary as the agent row so
        # the two facts cannot drift apart.
        await self.link_agent_host(agent_id, hostname, system_info)

    async def link_agent_host(
        self,
        agent_id: str,
        hostname: str,
        system_info: dict[str, Any] | None = None,
    ) -> str:
        """Create-or-link the host row for an enrolled agent. Returns the host id.

        Link rules, in order:
        * a host already carrying this agent_id wins (re-registration);
        * else an UNAMBIGUOUS hostname match is linked (two hosts with the same
          hostname = link nothing rather than guess);
        * else a new host row is created with source='agent'.

        Only `agent_id` (and, on create, agent-derived facts) are written.
        Role, description, status and every operator-settable field are left
        alone - automation must never overwrite operator intent (#424).
        """
        info = system_info or {}

        row = await self.db.fetchone("SELECT id FROM hosts WHERE agent_id = ?", (agent_id,))
        if row is not None:
            await self._sync_agent_facts(str(row["id"]), info, connected=True)
            return str(row["id"])

        matches = await self.db.fetchall(
            "SELECT id, agent_id FROM hosts WHERE hostname = ?", (hostname,)
        )
        if len(matches) == 1 and matches[0]["agent_id"] in (None, agent_id):
            await self.db.execute(
                "UPDATE hosts SET agent_id = ?, updated_at = ? WHERE id = ?",
                (agent_id, now(), matches[0]["id"]),
            )
            await self.db.conn.commit()
            await self._sync_agent_facts(str(matches[0]["id"]), info, connected=True)
            return str(matches[0]["id"])
        if matches:
            # Ambiguous or already claimed by a different agent: refuse to guess.
            return ""

        os_name = info.get("os") or None
        os_version = info.get("os_version") or None
        os_info = f"{os_name} {os_version}".strip() if (os_name or os_version) else None
        cpu = info.get("cpu_count")
        mem_gb = None
        memory = info.get("memory")
        if isinstance(memory, dict):
            mem_gb = memory.get("total_gb")
        host_id = await self.create_host(
            hostname=hostname,
            host_type="physical",
            managed_by="agent",
            source="agent",
            os_info=os_info,
            cpu_cores=int(cpu) if isinstance(cpu, (int, float)) else None,
            memory_mb=int(float(mem_gb) * 1024) if isinstance(mem_gb, (int, float)) else None,
        )
        await self.db.execute(
            "UPDATE hosts SET agent_id = ?, updated_at = ? WHERE id = ?",
            (agent_id, now(), host_id),
        )
        await self.db.conn.commit()
        await self._sync_agent_facts(host_id, info, connected=True)
        return host_id

    async def _sync_agent_facts(
        self, host_id: str, info: dict[str, Any], *, connected: bool | None = None
    ) -> None:
        """Let the agent's report fill what nothing else has claimed.

        Two rules keep this on the right side of #424:
        * facts (os/cpu/memory) are written only into NULL columns - the agent
          fills gaps, it never overwrites Proxmox's or an operator's answer;
        * status follows the agent's channel only when 'status' is not pinned
          by an operator. "unknown" next to a green "agent connected" chip is
          the kind of lie P6 exists to kill.
        """
        host = await self.get_host(host_id)
        if host is None:
            return
        host = dict(host)
        updates: dict[str, Any] = {}

        os_name = info.get("os") or None
        os_version = info.get("os_version") or None
        os_info = f"{os_name} {os_version}".strip() if (os_name or os_version) else None
        if host.get("os_info") in (None, "") and os_info:
            updates["os_info"] = os_info
        cpu = info.get("cpu_count")
        if host.get("cpu_cores") is None and isinstance(cpu, (int, float)):
            updates["cpu_cores"] = int(cpu)
        memory = info.get("memory")
        mem_gb = memory.get("total_gb") if isinstance(memory, dict) else None
        if host.get("memory_mb") is None and isinstance(mem_gb, (int, float)):
            updates["memory_mb"] = int(float(mem_gb) * 1024)
        disk = info.get("disk")
        disk_gb = disk.get("total_gb") if isinstance(disk, dict) else None
        if host.get("disk_gb") is None and isinstance(disk_gb, (int, float)):
            updates["disk_gb"] = int(float(disk_gb))

        if connected is not None and "status" not in self._pinned_fields(host):
            wanted = "online" if connected else "offline"
            if host.get("status") != wanted:
                updates["status"] = wanted

        if not updates:
            return
        sets = ", ".join(f"{k} = ?" for k in updates)
        await self.db.execute(
            f"UPDATE hosts SET {sets}, updated_at = ? WHERE id = ?",
            (*updates.values(), now(), host_id),
        )
        await self.db.conn.commit()

    async def touch_agent(self, agent_id: str, state: dict[str, Any] | None = None) -> None:
        import json as _json

        if state is not None:
            await self.db.execute(
                "UPDATE agents SET last_heartbeat = ?, state = ? WHERE agent_id = ?",
                (now(), _json.dumps(state), agent_id),
            )
        else:
            await self.db.execute(
                "UPDATE agents SET last_heartbeat = ? WHERE agent_id = ?", (now(), agent_id)
            )
        await self.db.conn.commit()

    async def mark_agent_disconnected(self, agent_id: str, reason: str | None = None) -> None:
        """Mark an agent disconnected, optionally recording WHY (#430).

        ``reason`` is stored as the agent's last error so a dark host can explain
        itself. It is only written when given: an ordinary clean disconnect
        should not erase the rejection reason that a later reconnect attempt
        recorded, and vice versa.
        """
        if reason:
            await self.db.execute(
                "UPDATE agents SET connected = 0, disconnected_at = ?, "
                "last_error = ?, last_error_at = ? WHERE agent_id = ?",
                (now(), reason, now(), agent_id),
            )
        else:
            await self.db.execute(
                "UPDATE agents SET connected = 0, disconnected_at = ? WHERE agent_id = ?",
                (now(), agent_id),
            )
        await self.db.conn.commit()
        # The linked host's status follows the channel (#514 S2), pinning respected.
        linked = await self.db.fetchone("SELECT id FROM hosts WHERE agent_id = ?", (agent_id,))
        if linked is not None:
            await self._sync_agent_facts(str(linked["id"]), {}, connected=False)

    async def record_agent_error(
        self, agent_id: str, reason: str, hostname: str | None = None
    ) -> bool:
        """Record why the hub refused an agent (#430). Returns True if a row matched.

        Matches on ``agent_id`` first and falls back to ``hostname``, because the
        interesting rejections are exactly the ones where the claimed id is NOT
        the one the hub knows - a host that came back with a fresh id, or one
        replaying someone else's. Nothing is inserted for an unknown agent: a
        rejected stranger must not be able to grow the agents table (that table
        is the credential store), so an unmatched rejection lives only in the
        audit log.
        """
        cursor = await self.db.execute(
            "UPDATE agents SET last_error = ?, last_error_at = ? WHERE agent_id = ?",
            (reason, now(), agent_id),
        )
        if cursor.rowcount == 0 and hostname:
            cursor = await self.db.execute(
                "UPDATE agents SET last_error = ?, last_error_at = ? WHERE hostname = ?",
                (reason, now(), hostname),
            )
        await self.db.conn.commit()
        return cursor.rowcount > 0

    async def set_agent_credential(
        self, agent_id: str, hostname: str, credential_hash: str
    ) -> None:
        """Store (or replace) an agent's per-agent credential hash.

        Upserts so a mint can run before the agent row exists (register
        persistence is fire-and-forget). Re-minting on re-enrollment clears any
        prior ``revoked_at`` — a bootstrap/shared re-enrollment restores access.
        """
        ts = now()
        await self.db.execute(
            """INSERT INTO agents
                 (agent_id, hostname, credential_hash, credential_set_at, revoked_at)
               VALUES (?, ?, ?, ?, NULL)
               ON CONFLICT(agent_id) DO UPDATE SET
                 hostname=excluded.hostname,
                 credential_hash=excluded.credential_hash,
                 credential_set_at=excluded.credential_set_at,
                 revoked_at=NULL""",
            (agent_id, hostname, credential_hash, ts),
        )
        await self.db.conn.commit()

    async def get_agent_credential(self, agent_id: str) -> dict[str, Any] | None:
        """Return the credential row for an agent (hostname, credential_hash,
        credential_set_at, revoked_at) or ``None`` if unknown."""
        return await self.db.fetchone(
            "SELECT agent_id, hostname, credential_hash, credential_set_at, revoked_at "
            "FROM agents WHERE agent_id = ?",
            (agent_id,),
        )

    async def count_agents(self) -> int:
        """How many agents this install has, ever enrolled or not.

        The enrolment window (#537) exempts a genuinely FRESH install so the
        zero-touch first rollout still needs zero operator input: zero rows here
        is what "fresh" means. A refused stranger never creates a row, so this
        cannot be inflated by the attempts the window exists to refuse.
        """
        row = await self.db.fetchone("SELECT COUNT(*) AS n FROM agents")
        return int(row["n"]) if row else 0

    async def agent_hostname_known(self, hostname: str) -> bool:
        """Whether this install already has an agent row for ``hostname``.

        Includes revoked and credential-less rows on purpose: the question is
        "is this host already part of the fleet", not "can it authenticate".
        Re-enrolling a host the operator already added is the documented
        recovery path; enrolling a hostname nobody has ever seen is the thing
        the enrolment window gates (#537).
        """
        if not hostname:
            return False
        row = await self.db.fetchone(
            "SELECT 1 AS present FROM agents WHERE hostname = ? LIMIT 1", (hostname,)
        )
        return row is not None

    async def get_agent_credentials_by_hostname(self, hostname: str) -> list[dict[str, Any]]:
        """Return the live (non-revoked, credentialed) rows issued to ``hostname``,
        newest credential first.

        Used to recognise a host whose ``agent_id`` changed between restarts: the
        presented token still has to match one of these hashes, so this identifies
        the host by POSSESSION of a credential issued to it, never by hostname
        alone. Revoked rows are excluded so a revoked credential can never be
        laundered back in through this path.
        """
        if not hostname:
            return []
        return await self.db.fetchall(
            "SELECT agent_id, hostname, credential_hash, credential_set_at, revoked_at "
            "FROM agents "
            "WHERE hostname = ? AND credential_hash IS NOT NULL AND revoked_at IS NULL "
            "ORDER BY credential_set_at DESC",
            (hostname,),
        )

    async def rebind_agent_credential(self, old_agent_id: str, new_agent_id: str) -> bool:
        """Move a per-agent credential from ``old_agent_id`` to ``new_agent_id``,
        keeping the same hash so the agent's token stays valid. Returns ``True``
        when a credential was moved.

        The credential follows the agent identity that presented it: the old id
        is left with no credential, so exactly one identity can authenticate with
        that token afterwards.
        """
        if not old_agent_id or not new_agent_id or old_agent_id == new_agent_id:
            return False
        old = await self.get_agent_credential(old_agent_id)
        if not old or not old.get("credential_hash"):
            return False
        existing = await self.db.fetchone(
            "SELECT agent_id FROM agents WHERE agent_id = ?", (new_agent_id,)
        )
        if existing is None:
            # No row under the new id: carry the whole agent row across (history,
            # system_info and all) by renaming its primary key.
            await self.db.execute(
                "UPDATE agents SET agent_id = ? WHERE agent_id = ?",
                (new_agent_id, old_agent_id),
            )
        else:
            # A row already exists under the new id (the agent re-registered
            # before): copy the credential onto it and strip it from the old one
            # so the retired identity cannot authenticate.
            await self.db.execute(
                "UPDATE agents SET hostname = ?, credential_hash = ?, "
                "credential_set_at = ?, revoked_at = NULL WHERE agent_id = ?",
                (
                    old.get("hostname"),
                    old["credential_hash"],
                    old.get("credential_set_at"),
                    new_agent_id,
                ),
            )
            await self.db.execute(
                "UPDATE agents SET credential_hash = NULL, credential_set_at = NULL "
                "WHERE agent_id = ?",
                (old_agent_id,),
            )
        await self.db.conn.commit()
        return True

    async def revoke_agent_credential(self, agent_id: str) -> bool:
        """Mark an agent's credential revoked. Returns ``True`` if a matching,
        credentialed agent row was updated, ``False`` otherwise."""
        cursor = await self.db.execute(
            "UPDATE agents SET revoked_at = ? "
            "WHERE agent_id = ? AND credential_hash IS NOT NULL AND revoked_at IS NULL",
            (now(), agent_id),
        )
        await self.db.conn.commit()
        return cursor.rowcount > 0

    async def delete_agent(self, agent_id: str) -> bool:
        """Forget an agent entirely. Returns ``True`` if a row was removed.

        Deleting the ROW is what makes this a real removal rather than a hidden
        one: the `agents` table doubles as the per-agent credential store
        (#362 slice 2), so a decommissioned host whose row survives keeps a
        credential that still authenticates. Revoking alone leaves the row - and
        its hostname - available to the credential-rebind path (#418), which is
        exactly the door a scrapped box should not still have (#415).
        """
        cursor = await self.db.execute("DELETE FROM agents WHERE agent_id = ?", (agent_id,))
        # The host outlives the agent, but not the LINK (#514 S3): a dangling
        # agent_id makes the fleet list claim "agent enrolled, not connected"
        # about a credential that no longer exists. The host also stops
        # pretending to be online on the strength of a deleted channel -
        # unless an operator pinned status.
        host = await self.db.fetchone(
            "SELECT id, status, pinned_fields FROM hosts WHERE agent_id = ?", (agent_id,)
        )
        if host is not None:
            await self.db.execute(
                "UPDATE hosts SET agent_id = NULL, updated_at = ? WHERE id = ?",
                (now(), host["id"]),
            )
            if host["status"] == "online" and "status" not in self._pinned_fields(dict(host)):
                await self.db.execute(
                    "UPDATE hosts SET status = 'unknown', updated_at = ? WHERE id = ?",
                    (now(), host["id"]),
                )
        await self.db.conn.commit()
        return cursor.rowcount > 0

    async def list_agents(self) -> list[dict[str, Any]]:
        import json as _json

        rows = await self.db.fetchall("SELECT * FROM agents ORDER BY hostname")
        out = []
        for r in rows:
            d = dict(r)
            for f in ("system_info", "state"):
                try:
                    d[f] = _json.loads(d[f]) if d.get(f) else {}
                except (ValueError, TypeError):
                    d[f] = {}
            out.append(d)
        return out

    async def log_agent_audit(
        self,
        agent_id: str,
        action: str,
        target: str = "",
        result: str = "success",
        exit_code: int | None = None,
        hostname: str | None = None,
        caller: str | None = None,
        ts: str | None = None,
    ) -> None:
        """Persist one agent-hub audit entry (#381).

        Durable + attributable record of a fleet-root command/lifecycle event.
        ``ts`` is accepted so the persisted timestamp matches the in-memory
        AuditEntry the caller already stamped.
        """
        await self.db.execute(
            """INSERT INTO agent_audit
               (ts, agent_id, hostname, action, target, result, exit_code, caller)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                ts or now(),
                agent_id,
                hostname,
                action,
                _sanitize_audit_field(target),
                result,
                exit_code,
                caller,
            ),
        )
        await self.db.conn.commit()

    async def query_agent_audit(
        self,
        limit: int = 100,
        agent_id: str | None = None,
        action: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return agent-hub audit rows, newest-first, capped at ``limit``."""
        limit = max(min(limit, 10000), 1)
        clauses: list[str] = []
        params: list[Any] = []
        if agent_id is not None:
            clauses.append("agent_id = ?")
            params.append(agent_id)
        if action is not None:
            clauses.append("action = ?")
            params.append(action)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        rows = await self.db.fetchall(
            f"SELECT * FROM agent_audit{where} ORDER BY id DESC LIMIT ?",
            params,
        )
        return [dict(r) for r in rows]

    async def create_host(
        self,
        hostname: str,
        host_type: str,
        proxmox_id: int | None = None,
        node: str | None = None,
        ip_address: str | None = None,
        tags: str | None = None,
        managed_by: str = "user",
        managed: bool = False,
        storage_pool: str | None = None,
        os_info: str | None = None,
        cpu_cores: int | None = None,
        memory_mb: int | None = None,
        disk_gb: int | None = None,
        role: str = "guest",
        network_bridge: str | None = None,
        vlan_id: int | None = None,
        fqdn: str | None = None,
        pve_status: str | None = None,
        source: str = "discovered",
        description: str | None = None,
        artifact_id: str | None = None,
        import_state: str | None = None,
        role_source: str = "inferred",
        ip_source: str | None = None,
        status: str | None = None,
        owner: str | None = None,
    ) -> str:
        host_id = uuid4()
        ts = now()
        await self.db.execute(
            """INSERT INTO hosts
               (id, proxmox_id, hostname, node, host_type, role, ip_address, fqdn,
                status, tags, managed_by, managed, storage_pool, os_info,
                cpu_cores, memory_mb, disk_gb, network_bridge, vlan_id,
                pve_status, source, description, artifact_id, import_state,
                role_source, ip_source, owner, created_at, updated_at)
               VALUES (
                   ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                   ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
               )""",
            (
                host_id,
                proxmox_id,
                hostname,
                node,
                host_type,
                role,
                ip_address,
                fqdn,
                status or "unknown",
                tags,
                managed_by,
                int(managed),
                storage_pool,
                os_info,
                cpu_cores,
                memory_mb,
                disk_gb,
                network_bridge,
                vlan_id,
                pve_status,
                source,
                description,
                artifact_id,
                import_state,
                role_source,
                ip_source,
                owner,
                ts,
                ts,
            ),
        )
        await self.db.conn.commit()
        return host_id

    async def list_hosts(
        self,
        managed: bool | None = None,
        role: str | None = None,
        source: str | None = None,
        import_state: str | None = None,
        pve_status: str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
        q: str | None = None,
    ) -> list[dict[str, Any]]:
        limit = min(limit, 1000)
        offset = max(offset, 0)
        where, params = self._host_where(
            managed=managed,
            status=status,
            role=role,
            source=source,
            import_state=import_state,
            pve_status=pve_status,
            q=q,
        )
        params.extend([limit, offset])
        rows = await self.db.fetchall(f"SELECT * FROM hosts{where} LIMIT ? OFFSET ?", params)
        return [dict(r) for r in rows]

    @classmethod
    def _host_where(
        cls,
        managed: bool | None = None,
        status: str | None = None,
        role: str | None = None,
        source: str | None = None,
        import_state: str | None = None,
        pve_status: str | None = None,
        q: str | None = None,
    ) -> tuple[str, list[Any]]:
        """The filter clauses, in ONE place, for both the list and the count.

        Two copies drifting is a lie the pager tells: `items` and `total` come
        from different queries, so a filter applied to one and not the other
        reports a count for a different set of rows than the page shows.
        """
        clauses: list[str] = []
        params: list[Any] = []
        if managed is not None:
            clauses.append("managed = ?")
            params.append(int(managed))
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if role is not None:
            clauses.append("role = ?")
            params.append(role)
        if source is not None:
            clauses.append("source = ?")
            params.append(source)
        if import_state is not None:
            clauses.append("import_state = ?")
            params.append(import_state)
        if pve_status is not None:
            clauses.append("pve_status = ?")
            params.append(pve_status)
        if q:
            # Free text over what an operator types when looking for a machine:
            # its name, where it lives, what it is for. Searched in SQL rather
            # than in the page so a match on host 400 of 500 is findable at all -
            # the list is paginated, and a client-side filter can only ever see
            # the page it already has (#445 A4). The column list is a constant.
            like = f"%{q}%"
            ors = " OR ".join(f"{c} LIKE ?" for c in _HOST_SEARCH_COLUMNS)
            clauses.append(f"({ors})")
            params.extend([like] * len(_HOST_SEARCH_COLUMNS))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        return where, params

    async def count_hosts(self, **filters: Any) -> int:
        """How many hosts match, ignoring pagination (#428).

        `list_hosts()` pages, so `len(list_hosts(...))` is the PAGE SIZE. The
        inventory route returned that as `total`, which capped the UI at 100 with
        no way to reach page 2 and told the operator their estate was smaller
        than it is.
        """
        where, params = self._host_where(**filters)
        row = await self.db.fetchone(f"SELECT COUNT(*) as cnt FROM hosts{where}", params)
        return int(row["cnt"]) if row else 0

    async def all_host_ids(self) -> set[str]:
        """Every host id, unpaginated (#428).

        The inventory reconciler compared "what exists" against "what Proxmox
        reported" using `list_hosts()`, whose default limit is 100 - so on an
        estate with more than a hundred hosts the absent/changed sets were
        computed from an arbitrary first page.
        """
        rows = await self.db.fetchall("SELECT id FROM hosts")
        return {str(r["id"]) for r in rows}

    async def get_host(self, host_id: str) -> dict[str, Any] | None:
        row = await self.db.fetchone("SELECT * FROM hosts WHERE id = ?", (host_id,))
        return dict(row) if row is not None else None

    async def get_host_by_hostname(self, hostname: str) -> dict[str, Any] | None:
        row = await self.db.fetchone("SELECT * FROM hosts WHERE hostname = ?", (hostname,))
        return dict(row) if row is not None else None

    async def get_host_by_proxmox_id(self, proxmox_id: int) -> dict[str, Any] | None:
        row = await self.db.fetchone("SELECT * FROM hosts WHERE proxmox_id = ?", (proxmox_id,))
        return dict(row) if row is not None else None

    async def update_host(self, host_id: str, **kwargs: Any) -> None:
        if not kwargs:
            return
        sets, set_vals = _validated_set_clause(kwargs, _HOST_COLUMNS)
        vals = [*set_vals, now(), host_id]
        await self.db.execute(f"UPDATE hosts SET {sets}, updated_at = ? WHERE id = ?", vals)
        await self.db.conn.commit()

    # Fields an operator can set by hand, and which automation must therefore not
    # overwrite once they have. `status` is here because `PATCH /inventory/{id}`
    # accepts it - enrich re-derived it every cycle, which made that PATCH field
    # a lie (#424).
    PINNABLE_HOST_FIELDS: frozenset[str] = frozenset(
        {"role", "ip_address", "description", "status", "tags", "owner", "fqdn"}
    )

    async def pin_host_fields(self, host_id: str, fields: set[str]) -> None:
        """Record that an operator set these fields (#424)."""
        import json as _json

        pinnable = {f for f in fields if f in self.PINNABLE_HOST_FIELDS}
        if not pinnable:
            return
        host = await self.get_host(host_id)
        if host is None:
            return
        current = self._pinned_fields(host)
        merged = sorted(current | pinnable)
        await self.db.execute(
            "UPDATE hosts SET pinned_fields = ?, updated_at = ? WHERE id = ?",
            (_json.dumps(merged), now(), host_id),
        )
        await self.db.conn.commit()

    @staticmethod
    def _pinned_fields(host: dict[str, Any]) -> set[str]:
        import json as _json

        raw = host.get("pinned_fields")
        if not raw:
            return set()
        try:
            values = _json.loads(raw)
        except (ValueError, TypeError):
            return set()
        return {str(v) for v in values} if isinstance(values, list) else set()

    async def update_host_from_automation(self, host_id: str, **kwargs: Any) -> list[str]:
        """Update a host from a sync/enrich pass, LEAVING pinned fields alone.

        The single door automation goes through (#424). Before this, refresh and
        enrich each wrote whatever they had computed: a node refresh overwrote an
        operator's role and ip_address (and stamped `role_source="user"` over its
        own guess), every cycle clobbered an operator's description with the PVE
        blurb, and enrich re-derived `status` over anything set by PATCH.

        Returns the field names it skipped, so a caller can log what it did not
        touch instead of silently doing nothing.
        """
        host = await self.get_host(host_id)
        if host is None:
            return []
        pinned = self._pinned_fields(host)
        skipped = sorted(f for f in kwargs if f in pinned)
        allowed = {k: v for k, v in kwargs.items() if k not in pinned}
        # Automation never claims a value came from a person. `role_source="user"`
        # written by a sync is the exact forgery #424 names.
        # `role_source` is constrained to inferred/user/artifact, so automation's
        # value is "inferred" - it decided, a person did not. `ip_source` has its
        # own vocabulary where "pve" is the automation answer.
        if allowed.get("role_source") == "user":
            allowed["role_source"] = "inferred"
        if allowed.get("ip_source") == "user":
            allowed["ip_source"] = "pve"
        if allowed:
            await self.update_host(host_id, **allowed)
        return skipped

    async def delete_host(self, host_id: str) -> None:
        """Remove a host and everything that only exists because of it (#445 A5).

        The services and the as-found observation note exist only because of the
        host; deleting the host alone leaves rows pointing at an id nothing
        resolves, which is how a "forgotten" machine keeps showing up in service
        listings and coverage counts. One commit, so a partial delete cannot
        leave that state.
        """
        await self.db.execute("DELETE FROM services WHERE host_id = ?", (host_id,))
        # The as-found observation note is keyed `introspect:<host_id>` in its
        # SOURCE (its target is the hostname), so that is what identifies it -
        # matching on the hostname alone would take out unrelated notes about a
        # machine that merely shares a name.
        await self.db.execute(
            "DELETE FROM doc_metadata WHERE source = ?", (f"introspect:{host_id}",)
        )
        await self.db.execute("DELETE FROM hosts WHERE id = ?", (host_id,))
        await self.db.conn.commit()

    async def mark_hosts_absent(self, seen_ids: set[str], sources: tuple[str, ...]) -> int:
        """Stamp hosts the hypervisor no longer reports, clear the ones it does.

        Only hosts whose ``source`` is in ``sources`` are considered: Proxmox has
        no opinion about a machine an operator added by hand, so a manual host
        must never be marked absent by a sync that never looked for it (#445 A5).

        Returns the number newly marked. ``absent_since`` is set ONCE - a host
        that has been gone for a week must keep the date it went missing, not be
        restamped every cycle - and cleared the moment it is seen again.
        """
        placeholders = ", ".join("?" for _ in sources)
        seen = list(seen_ids)
        seen_clause = ""
        params: list[Any] = list(sources)
        if seen:
            seen_clause = f" AND id NOT IN ({', '.join('?' for _ in seen)})"
            params.extend(seen)
        cursor = await self.db.execute(
            f"UPDATE hosts SET absent_since = ?, updated_at = ? "
            f"WHERE source IN ({placeholders}) AND absent_since IS NULL{seen_clause}",
            [now(), now(), *params],
        )
        newly_absent = cursor.rowcount
        if seen:
            await self.db.execute(
                f"UPDATE hosts SET absent_since = NULL, updated_at = ? "
                f"WHERE absent_since IS NOT NULL AND id IN ({', '.join('?' for _ in seen)})",
                [now(), *seen],
            )
        await self.db.conn.commit()
        return newly_absent

    async def create_service(
        self,
        host_id: str,
        name: str,
        runtime: str,
        version: str | None = None,
        status: str = "unknown",
        managed_by: str = "user",
        config: str | None = None,
    ) -> str:
        svc_id = uuid4()
        ts = now()
        await self.db.execute(
            """INSERT INTO services
               (id, host_id, name, runtime, version, status,
                managed_by, config, deployed_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (svc_id, host_id, name, runtime, version, status, managed_by, config, ts, ts),
        )
        await self.db.conn.commit()
        return svc_id

    async def list_services(self, host_id: str | None = None) -> list[dict[str, Any]]:
        if host_id is not None:
            rows = await self.db.fetchall("SELECT * FROM services WHERE host_id = ?", (host_id,))
        else:
            rows = await self.db.fetchall("SELECT * FROM services")
        return [dict(r) for r in rows]

    async def get_service(self, service_id: str) -> dict[str, Any] | None:
        row = await self.db.fetchone("SELECT * FROM services WHERE id = ?", (service_id,))
        return dict(row) if row is not None else None

    async def update_service(self, service_id: str, **kwargs: Any) -> None:
        if not kwargs:
            return
        sets, set_vals = _validated_set_clause(kwargs, _SERVICE_COLUMNS)
        vals = [*set_vals, now(), service_id]
        await self.db.execute(f"UPDATE services SET {sets}, updated_at = ? WHERE id = ?", vals)
        await self.db.conn.commit()

    async def delete_service(self, service_id: str) -> None:
        await self.db.execute("DELETE FROM services WHERE id = ?", (service_id,))
        await self.db.conn.commit()

    async def log_audit(
        self,
        user_id: str | None = None,
        source: str | None = None,
        action: str | None = None,
        artifact_id: str | None = None,
        target_host: str | None = None,
        target_service: str | None = None,
        command: str | None = None,
        exit_code: int | None = None,
        snapshot_id: str | None = None,
        duration_ms: int | None = None,
        details_json: str | None = None,
    ) -> None:
        await self.db.execute(
            """INSERT INTO audit_log
               (user_id, source, action, artifact_id, target_host, target_service,
                command, exit_code, snapshot_id, duration_ms, details_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                user_id,
                source,
                action,
                artifact_id,
                _sanitize_audit_field(target_host),
                target_service,
                _sanitize_audit_field(command),
                exit_code,
                snapshot_id,
                duration_ms,
                details_json,
            ),
        )
        await self.db.conn.commit()

    # The audit filter clauses live in ONE place because two copies drifting is a
    # lie the UI shows the operator: `items` and `total` come from different
    # queries, so a filter added to the list and forgotten in the count reports
    # "50 of 4000" for a search that matched 50 things (#445 A4).
    _AUDIT_SEARCH_COLUMNS = (
        "artifact_id",
        "target_host",
        "target_service",
        "command",
        "user_id",
        "action",
        "details_json",
    )

    @classmethod
    def _audit_where(
        cls,
        action: str | None,
        artifact_id: str | None,
        target_host: str | None,
        source: str | None,
        q: str | None,
    ) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if action is not None:
            clauses.append("action = ?")
            params.append(action)
        if artifact_id is not None:
            clauses.append("artifact_id = ?")
            params.append(artifact_id)
        if target_host is not None:
            clauses.append("target_host = ?")
            params.append(target_host)
        if source is not None:
            clauses.append("source = ?")
            params.append(source)
        if q:
            # Free text over the columns an operator would recognise an entry by.
            # The column list is a constant, never caller text.
            like = f"%{q}%"
            ors = " OR ".join(f"{c} LIKE ?" for c in cls._AUDIT_SEARCH_COLUMNS)
            clauses.append(f"({ors})")
            params.extend([like] * len(cls._AUDIT_SEARCH_COLUMNS))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        return where, params

    async def query_audit_log(
        self,
        action: str | None = None,
        artifact_id: str | None = None,
        target_host: str | None = None,
        source: str | None = None,
        limit: int = 100,
        offset: int = 0,
        q: str | None = None,
    ) -> list[dict[str, Any]]:
        limit = min(limit, 10000)
        offset = max(offset, 0)
        where, params = self._audit_where(action, artifact_id, target_host, source, q)
        params.extend([limit, offset])
        rows = await self.db.fetchall(
            f"SELECT * FROM audit_log{where} ORDER BY id DESC LIMIT ? OFFSET ?",
            params,
        )
        return [dict(r) for r in rows]

    async def count_audit_log(
        self,
        action: str | None = None,
        artifact_id: str | None = None,
        target_host: str | None = None,
        source: str | None = None,
        q: str | None = None,
    ) -> int:
        where, params = self._audit_where(action, artifact_id, target_host, source, q)
        row = await self.db.fetchone(f"SELECT COUNT(*) as cnt FROM audit_log{where}", params)
        return row["cnt"] if row else 0

    async def create_artifact(
        self,
        id: str,
        kind: str,
        intent: str,
        status: str,
        mutating: bool,
        hash: str,
        target_json: str | None,
        idempotence: str | None,
        produced_by_json: str,
        file_path: str,
        supersedes_json: str | None = None,
        tags_json: str | None = None,
        rollback: bool = False,
        replay_safe: bool = True,
        requires_snapshot: bool = True,
        note_kind: str | None = None,
    ) -> str:
        ts = now()
        await self.db.execute(
            """INSERT INTO artifacts
               (id, kind, intent, status, mutating, target_json, idempotence, hash,
                produced_by_json, supersedes_json, tags_json, rollback, replay_safe,
                requires_snapshot, note_kind, file_path, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                id,
                kind,
                intent,
                status,
                int(mutating),
                target_json,
                idempotence,
                hash,
                produced_by_json,
                supersedes_json,
                tags_json,
                int(rollback),
                int(replay_safe),
                int(requires_snapshot),
                note_kind,
                file_path,
                ts,
                ts,
            ),
        )
        await self.db.conn.commit()
        return id

    async def get_artifact(self, artifact_id: str) -> dict[str, Any] | None:
        row = await self.db.fetchone("SELECT * FROM artifacts WHERE id = ?", (artifact_id,))
        return dict(row) if row is not None else None

    async def list_artifacts(
        self,
        status: str | None = None,
        kind: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.extend([limit, offset])
        rows = await self.db.fetchall(
            f"SELECT * FROM artifacts{where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            params,
        )
        return [dict(r) for r in rows]

    async def update_artifact_status(self, artifact_id: str, status: str, **kwargs: Any) -> None:
        sets_parts = ["status = ?"]
        vals: list[Any] = [status]
        for k, v in kwargs.items():
            if k not in _ARTIFACT_STATUS_COLUMNS:
                raise ValueError(f"Invalid column: {k}")
            sets_parts.append(f"{k} = ?")
            vals.append(v)
        sets_parts.append("updated_at = ?")
        vals.append(now())
        vals.append(artifact_id)
        await self.db.execute(f"UPDATE artifacts SET {', '.join(sets_parts)} WHERE id = ?", vals)
        await self.db.conn.commit()

    async def upsert_artifact(
        self,
        id: str,
        kind: str,
        intent: str,
        status: str,
        *,
        mutating: bool = True,
        hash: str | None = None,
        target_json: str | None = None,
        idempotence: str | None = None,
        produced_by_json: str | None = None,
        file_path: str | None = None,
        supersedes_json: str | None = None,
        tags_json: str | None = None,
        rollback: bool = False,
        replay_safe: bool = True,
        requires_snapshot: bool = True,
        note_kind: str | None = None,
        approved_by_json: str | None = None,
        applied_at: str | None = None,
        failed_at: str | None = None,
        failure_reason: str | None = None,
        superseded_by: str | None = None,
        rejected_by_json: str | None = None,
        revoked_by_json: str | None = None,
    ) -> None:
        ts = now()
        await self.db.execute(
            """INSERT INTO artifacts
               (id, kind, intent, status, mutating, target_json, idempotence, hash,
                produced_by_json, approved_by_json, applied_at, failed_at, failure_reason,
                supersedes_json, superseded_by, rejected_by_json, revoked_by_json,
                tags_json, rollback, replay_safe, requires_snapshot, note_kind,
                file_path, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 status=excluded.status,
                 hash=excluded.hash,
                 file_path=excluded.file_path,
                 approved_by_json=excluded.approved_by_json,
                 applied_at=excluded.applied_at,
                 failed_at=excluded.failed_at,
                 failure_reason=excluded.failure_reason,
                 superseded_by=excluded.superseded_by,
                 rejected_by_json=excluded.rejected_by_json,
                 revoked_by_json=excluded.revoked_by_json,
                 updated_at=excluded.updated_at""",
            (
                id,
                kind,
                intent,
                status,
                int(mutating),
                target_json,
                idempotence,
                hash,
                produced_by_json,
                approved_by_json,
                applied_at,
                failed_at,
                failure_reason,
                supersedes_json,
                superseded_by,
                rejected_by_json,
                revoked_by_json,
                tags_json,
                int(rollback),
                int(replay_safe),
                int(requires_snapshot),
                note_kind,
                file_path or "",
                ts,
                ts,
            ),
        )
        await self.db.conn.commit()

    async def get_setting(self, key: str) -> dict[str, Any] | None:
        row = await self.db.fetchone("SELECT * FROM settings WHERE key = ?", (key,))
        return dict(row) if row is not None else None

    async def set_setting(self, key: str, value: str) -> None:
        await self.db.execute(
            """INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE
               SET value=excluded.value, updated_at=excluded.updated_at""",
            (key, value, now()),
        )
        await self.db.conn.commit()

    async def create_doc_metadata(
        self,
        source: str,
        title: str,
        content: str,
        kind: str = "note",
        target: str | None = None,
        url: str | None = None,
        version: str | None = None,
    ) -> int | None:
        cursor = await self.db.execute(
            """INSERT OR IGNORE INTO doc_metadata
               (source, kind, target, title, content, url, version, embedded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (source, kind, target, title, content, url, version, now()),
        )
        if cursor.lastrowid == 0 or cursor.rowcount == 0:
            return None
        await self.db.conn.commit()
        return cursor.lastrowid

    async def list_doc_metadata(
        self,
        kind: str | None = None,
        target: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        """One page of KB documents plus the TRUE total for the same filters.

        The total counts every matching row, not the page: a `len(items)` count
        against a LIMIT saturates the UI's entry count and makes later documents
        unreachable. Shared by GET /kb and the `list_kb` MCP tool.
        """
        clauses: list[str] = []
        params: list[Any] = []
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind)
        if target is not None:
            clauses.append("target = ?")
            params.append(target)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        count_row = await self.db.fetchone(
            f"SELECT COUNT(*) AS cnt FROM doc_metadata{where}",  # nosec B608
            list(params),
        )
        total = int(count_row["cnt"]) if count_row else 0
        rows = await self.db.fetchall(
            f"SELECT * FROM doc_metadata{where} ORDER BY embedded_at DESC LIMIT ? OFFSET ?",  # nosec B608
            [*params, limit, offset],
        )
        return [dict(r) for r in rows], total

    async def get_doc_metadata(self, doc_id: int) -> dict[str, Any] | None:
        row = await self.db.fetchone("SELECT * FROM doc_metadata WHERE id = ?", (doc_id,))
        return dict(row) if row is not None else None

    async def update_doc_metadata(self, doc_id: int, **fields: Any) -> dict[str, Any] | None:
        sets: list[str] = []
        vals: list[Any] = []
        for k, v in fields.items():
            if k not in _DOC_METADATA_COLUMNS:
                raise ValueError(f"Invalid column: {k}")
            sets.append(f"{k} = ?")
            vals.append(v)
        if not sets:
            return await self.get_doc_metadata(doc_id)
        vals.append(doc_id)
        await self.db.execute(f"UPDATE doc_metadata SET {', '.join(sets)} WHERE id = ?", vals)
        await self.db.conn.commit()
        return await self.get_doc_metadata(doc_id)

    async def delete_doc_metadata(self, doc_id: int) -> bool:
        cursor = await self.db.execute("DELETE FROM doc_metadata WHERE id = ?", (doc_id,))
        await self.db.conn.commit()
        return cursor.rowcount > 0

    async def search_docs_by_source(self, source: str, limit: int = 50) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            "SELECT * FROM doc_metadata WHERE source = ? LIMIT ?",
            (source, limit),
        )
        return [dict(r) for r in rows]

    async def add_artifact_dependency(
        self,
        composite_id: str,
        sub_artifact_id: str,
        depends_on: str | None = None,
        on_error: str = "halt",
        step_order: int = 0,
    ) -> None:
        await self.db.execute(
            """INSERT INTO artifact_dependencies
               (composite_id, sub_artifact_id, depends_on, on_error, step_order)
               VALUES (?, ?, ?, ?, ?)""",
            (composite_id, sub_artifact_id, depends_on, on_error, step_order),
        )
        await self.db.conn.commit()

    async def get_artifact_dependencies(self, composite_id: str) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            "SELECT * FROM artifact_dependencies WHERE composite_id = ? ORDER BY step_order",
            (composite_id,),
        )
        return [dict(r) for r in rows]

    # ── Approval codes (human-relay MCP approval, #385 follow-up) ────────────
    # These rows live in artifact_approval_codes, a table NO MCP read touches, so
    # the code is reachable only from operator surfaces (web/CLI/webhook) and can
    # never leak into an agent-facing response.

    async def set_approval_code(self, artifact_id: str, code: str) -> None:
        """Store (or replace) the approval code for an artifact, resetting the
        failed-attempt counter and unlocking it."""
        ts = now()
        await self.db.execute(
            """INSERT INTO artifact_approval_codes
               (artifact_id, code, failed_attempts, locked, created_at, updated_at)
               VALUES (?, ?, 0, 0, ?, ?)
               ON CONFLICT(artifact_id) DO UPDATE SET
                 code=excluded.code,
                 failed_attempts=0,
                 locked=0,
                 updated_at=excluded.updated_at""",
            (artifact_id, code, ts, ts),
        )
        await self.db.conn.commit()

    async def get_approval_code_row(self, artifact_id: str) -> dict[str, Any] | None:
        """The full approval-code row (code, failed_attempts, locked) or None."""
        row = await self.db.fetchone(
            "SELECT * FROM artifact_approval_codes WHERE artifact_id = ?",
            (artifact_id,),
        )
        return dict(row) if row is not None else None

    async def record_failed_approval(self, artifact_id: str, lock_threshold: int) -> dict[str, Any]:
        """Count one wrong-code attempt; lock the artifact at the threshold.

        Returns {failed_attempts, locked} after the increment. A missing row (no
        code was ever issued) returns {failed_attempts: 0, locked: 0} - there is
        nothing to brute-force."""
        row = await self.get_approval_code_row(artifact_id)
        if row is None:
            return {"failed_attempts": 0, "locked": 0}
        attempts = int(row["failed_attempts"]) + 1
        locked = 1 if attempts >= lock_threshold else int(row["locked"])
        await self.db.execute(
            """UPDATE artifact_approval_codes
               SET failed_attempts = ?, locked = ?, updated_at = ?
               WHERE artifact_id = ?""",
            (attempts, locked, now(), artifact_id),
        )
        await self.db.conn.commit()
        return {"failed_attempts": attempts, "locked": locked}

    async def reset_approval_lock(self, artifact_id: str) -> bool:
        """Operator reset: clear the failed-attempt counter and unlock.

        Returns True if a code row existed to reset."""
        cursor = await self.db.execute(
            """UPDATE artifact_approval_codes
               SET failed_attempts = 0, locked = 0, updated_at = ?
               WHERE artifact_id = ?""",
            (now(), artifact_id),
        )
        await self.db.conn.commit()
        return cursor.rowcount > 0

    async def clear_approval_code(self, artifact_id: str) -> None:
        """Delete the approval code once the artifact leaves PROPOSED."""
        await self.db.execute(
            "DELETE FROM artifact_approval_codes WHERE artifact_id = ?",
            (artifact_id,),
        )
        await self.db.conn.commit()

    async def upsert_drift_check(
        self,
        artifact_id: str,
        drifted: bool,
        details_json: str | None = None,
        state: str = "unknown",
    ) -> None:
        """Record a drift check. ``state`` is the real answer (#425).

        ``drifted`` is kept because it is what the existing filters and indexes
        read, but it cannot answer "was this checked at all" - which is the
        question a green tick was silently getting wrong. ``state`` defaults to
        ``unknown`` so a caller that does not say cannot accidentally assert
        health.
        """
        await self.db.execute(
            """INSERT INTO drift_checks (artifact_id, drifted, checked_at, details_json, state)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(artifact_id) DO UPDATE SET
                   drifted = excluded.drifted,
                   checked_at = excluded.checked_at,
                   details_json = excluded.details_json,
                   state = excluded.state""",
            (artifact_id, int(drifted), now(), details_json, state),
        )
        await self.db.conn.commit()

    async def save_host_state_capture(self, artifact_id: str, items_json: str) -> None:
        """Store what a host looked like before an artifact was applied (#426)."""
        await self.db.execute(
            """INSERT INTO host_state_captures (artifact_id, captured_at, items_json)
               VALUES (?, ?, ?)
               ON CONFLICT(artifact_id) DO UPDATE SET
                   captured_at = excluded.captured_at,
                   items_json = excluded.items_json""",
            (artifact_id, now(), items_json),
        )
        await self.db.conn.commit()

    async def get_host_state_capture(self, artifact_id: str) -> list[dict[str, Any]] | None:
        """The pre-apply capture for an artifact, or None if there is not one.

        None means "nothing to roll back to" - which a revoke must report rather
        than treat as "nothing to do".
        """
        import json as _json

        row = await self.db.fetchone(
            "SELECT items_json FROM host_state_captures WHERE artifact_id = ?",
            (artifact_id,),
        )
        if row is None:
            return None
        try:
            items: list[dict[str, Any]] = _json.loads(row["items_json"])
        except (ValueError, TypeError):
            return None
        return items

    # Tables the retention reconciler may prune, and the column it prunes on.
    # A hardcoded allowlist because the table name is interpolated into SQL: it
    # must never be able to come from anywhere but this module.
    _PRUNABLE: ClassVar[dict[str, str]] = {
        "audit_log": "timestamp",
        "agent_audit": "ts",
        "webhook_deliveries": "created_at",
    }

    async def prune_before(self, table: str, column: str, cutoff: str) -> int:
        """Delete rows in `table` older than `cutoff`. Returns the row count.

        Nothing was ever pruned (#431): audit_log, agent_audit and
        webhook_deliveries gain a row per operation and per event, and a year of
        that is a multi-GB SQLite file and a backup too big to move.
        """
        if self._PRUNABLE.get(table) != column:
            raise ValueError(f"refusing to prune unknown table/column: {table}.{column}")
        cursor = await self.db.execute(
            f"DELETE FROM {table} WHERE {column} IS NOT NULL AND {column} < ?", (cutoff,)
        )
        await self.db.conn.commit()
        return int(cursor.rowcount or 0)

    async def prune_finished_tasks(self, cutoff: str, states: tuple[str, ...]) -> int:
        """Delete FINISHED tasks older than `cutoff`.

        Only finished ones: a pending or running task older than the horizon is a
        STUCK task, and deleting it would hide the problem and strand whatever is
        waiting on it.
        """
        placeholders = ", ".join("?" for _ in states)
        cursor = await self.db.execute(
            f"DELETE FROM tasks WHERE status IN ({placeholders}) AND created_at < ?",
            (*states, cutoff),
        )
        await self.db.conn.commit()
        return int(cursor.rowcount or 0)

    async def reclaim_free_pages(self) -> None:
        """Return freed pages to the filesystem.

        SQLite keeps them in the file otherwise, so a delete-only retention
        policy shrinks nothing an operator can see - which is the whole reason
        they asked for retention. `incremental_vacuum` is a no-op unless the
        database is in `auto_vacuum=INCREMENTAL`, so a plain `VACUUM` is the
        fallback; both are best-effort because neither is worth failing a
        reconciler cycle over.
        """
        try:
            await self.db.execute("PRAGMA incremental_vacuum")
            await self.db.conn.commit()
        except Exception as exc:
            logger.debug("incremental_vacuum unavailable: %s", exc)

    async def get_drift_checks(
        self,
        drifted: bool | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        if drifted is not None:
            rows = await self.db.fetchall(
                "SELECT * FROM drift_checks WHERE drifted = ? "
                "ORDER BY checked_at DESC LIMIT ? OFFSET ?",
                (int(drifted), limit, offset),
            )
        else:
            rows = await self.db.fetchall(
                "SELECT * FROM drift_checks ORDER BY checked_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            )
        return [dict(r) for r in rows]

    async def get_drift_check(self, artifact_id: str) -> dict[str, Any] | None:
        row = await self.db.fetchone(
            "SELECT * FROM drift_checks WHERE artifact_id = ?",
            (artifact_id,),
        )
        return dict(row) if row is not None else None

    async def create_webhook_config(
        self,
        url: str,
        event_types: list[str],
        secret: str | None = None,
        max_retries: int = 3,
    ) -> int:
        import json

        ts = now()
        await self.db.execute(
            """INSERT INTO webhook_configs
               (url, event_types, secret, enabled, max_retries, created_at)
               VALUES (?, ?, ?, 1, ?, ?)""",
            (url, json.dumps(sorted(event_types)), secret, max_retries, ts),
        )
        await self.db.conn.commit()
        row = await self.db.fetchone("SELECT last_insert_rowid() as id")
        return row["id"] if row else 0

    async def list_webhook_configs(self) -> list[dict[str, Any]]:
        rows = await self.db.fetchall("SELECT * FROM webhook_configs ORDER BY id")
        return [dict(r) for r in rows]

    async def get_webhook_config(self, config_id: int) -> dict[str, Any] | None:
        row = await self.db.fetchone("SELECT * FROM webhook_configs WHERE id = ?", (config_id,))
        return dict(row) if row is not None else None

    async def delete_webhook_config(self, config_id: int) -> bool:
        await self.db.execute("DELETE FROM webhook_deliveries WHERE webhook_id = ?", (config_id,))
        await self.db.execute("DELETE FROM webhook_configs WHERE id = ?", (config_id,))
        row = await self.db.fetchone("SELECT changes() as cnt")
        await self.db.conn.commit()
        return bool(row and row["cnt"] > 0)

    async def get_webhook_configs_for_event(self, event_type: str) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            "SELECT * FROM webhook_configs WHERE enabled = 1",
            (),
        )
        import json as _json

        result: list[dict[str, Any]] = []
        for row in rows:
            types = _json.loads(row["event_types"])
            if event_type in types or "*" in types:
                result.append(dict(row))
        return result

    async def create_webhook_delivery(
        self,
        webhook_id: int,
        event_type: str,
        payload: str,
        status: str = "pending",
        attempts: int = 0,
    ) -> int:
        ts = now()
        await self.db.execute(
            """INSERT INTO webhook_deliveries
               (webhook_id, event_type, payload, status, attempts, last_attempt_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (webhook_id, event_type, payload, status, attempts, ts, ts),
        )
        await self.db.conn.commit()
        row = await self.db.fetchone("SELECT last_insert_rowid() as id")
        return row["id"] if row else 0

    async def update_webhook_delivery(
        self,
        delivery_id: int,
        status: str,
        attempts: int,
    ) -> None:
        ts = now()
        await self.db.execute(
            """UPDATE webhook_deliveries
               SET status = ?, attempts = ?, last_attempt_at = ?
               WHERE id = ?""",
            (status, attempts, ts, delivery_id),
        )
        await self.db.conn.commit()
