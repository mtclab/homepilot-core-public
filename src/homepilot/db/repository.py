from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from .connection import Database

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
                 disconnected_at=NULL""",
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

    async def mark_agent_disconnected(self, agent_id: str) -> None:
        await self.db.execute(
            "UPDATE agents SET connected = 0, disconnected_at = ? WHERE agent_id = ?",
            (now(), agent_id),
        )
        await self.db.conn.commit()

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
    ) -> list[dict[str, Any]]:
        limit = min(limit, 1000)
        offset = max(offset, 0)
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
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.extend([limit, offset])
        rows = await self.db.fetchall(f"SELECT * FROM hosts{where} LIMIT ? OFFSET ?", params)
        return [dict(r) for r in rows]

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

    async def delete_host(self, host_id: str) -> None:
        await self.db.execute("DELETE FROM hosts WHERE id = ?", (host_id,))
        await self.db.conn.commit()

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

    async def query_audit_log(
        self,
        action: str | None = None,
        artifact_id: str | None = None,
        target_host: str | None = None,
        source: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        limit = min(limit, 10000)
        offset = max(offset, 0)
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
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
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
    ) -> int:
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
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
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

    async def upsert_drift_check(
        self,
        artifact_id: str,
        drifted: bool,
        details_json: str | None = None,
    ) -> None:
        await self.db.execute(
            """INSERT INTO drift_checks (artifact_id, drifted, checked_at, details_json)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(artifact_id) DO UPDATE SET
                   drifted = excluded.drifted,
                   checked_at = excluded.checked_at,
                   details_json = excluded.details_json""",
            (artifact_id, int(drifted), now(), details_json),
        )
        await self.db.conn.commit()

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
