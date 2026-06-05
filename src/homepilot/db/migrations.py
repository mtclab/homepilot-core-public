from __future__ import annotations

import logging
import sqlite3

from .connection import Database

MIGRATIONS: dict[int, list[str | tuple[str, str, str]]] = {
    1: [
        """CREATE TABLE IF NOT EXISTS hosts (
            id              TEXT PRIMARY KEY,
            proxmox_id      INTEGER,
            hostname        TEXT NOT NULL,
            node            TEXT,
            host_type       TEXT NOT NULL,
            role            TEXT NOT NULL DEFAULT 'guest',
            ip_address      TEXT,
            fqdn            TEXT,
            status          TEXT NOT NULL DEFAULT 'unknown',
            tags            TEXT,
            managed_by      TEXT NOT NULL DEFAULT 'user',
            managed         INTEGER NOT NULL DEFAULT 0,
            storage_pool    TEXT,
            os_info         TEXT,
            cpu_cores       INTEGER,
            memory_mb       INTEGER,
            disk_gb         INTEGER,
            network_bridge  TEXT,
            vlan_id         INTEGER,
            created_at      TEXT NOT NULL,
            updated_at      TEXT NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_hosts_proxmox ON hosts(proxmox_id)",
        "CREATE INDEX IF NOT EXISTS idx_hosts_managed ON hosts(managed)",
        "CREATE INDEX IF NOT EXISTS idx_hosts_role ON hosts(role)",
        "CREATE INDEX IF NOT EXISTS idx_hosts_status ON hosts(status)",
        """CREATE TABLE IF NOT EXISTS services (
            id              TEXT PRIMARY KEY,
            host_id         TEXT NOT NULL REFERENCES hosts(id),
            name            TEXT NOT NULL,
            runtime         TEXT NOT NULL,
            version         TEXT,
            status          TEXT NOT NULL DEFAULT 'unknown',
            managed_by      TEXT NOT NULL DEFAULT 'user',
            config          TEXT,
            deployed_at     TEXT,
            updated_at      TEXT,
            UNIQUE(host_id, name)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_services_host ON services(host_id)",
        """CREATE TABLE IF NOT EXISTS users (
            id              TEXT PRIMARY KEY,
            display_name    TEXT,
            auth_source     TEXT NOT NULL DEFAULT 'api_token',
            created_at      TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS api_tokens (
            id              TEXT PRIMARY KEY,
            user_id         TEXT NOT NULL REFERENCES users(id),
            token_type      TEXT NOT NULL,
            prefix          TEXT NOT NULL,
            hash            TEXT NOT NULL,
            scope           TEXT,
            expires_at      TEXT,
            last_used_at    TEXT,
            role            TEXT,
            created_at      TEXT NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_tokens_user ON api_tokens(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_tokens_prefix ON api_tokens(prefix)",
        """CREATE TABLE IF NOT EXISTS audit_log (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ')),
            user_id         TEXT NOT NULL,
            source          TEXT NOT NULL,
            action          TEXT NOT NULL,
            artifact_id     TEXT,
            target_host     TEXT,
            target_service  TEXT,
            command         TEXT,
            exit_code       INTEGER,
            snapshot_id     TEXT,
            duration_ms     INTEGER,
            details_json    TEXT
        )""",
        "CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_log(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_audit_host ON audit_log(target_host)",
        "CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action)",
        "CREATE INDEX IF NOT EXISTS idx_audit_time ON audit_log(timestamp)",
        "CREATE INDEX IF NOT EXISTS idx_audit_artifact ON audit_log(artifact_id)",
        """CREATE TABLE IF NOT EXISTS artifacts (
            id              TEXT PRIMARY KEY,
            kind            TEXT NOT NULL,
            intent          TEXT NOT NULL,
            status          TEXT NOT NULL DEFAULT 'proposed',
            mutating        INTEGER NOT NULL DEFAULT 1,
            target_json     TEXT,
            idempotence     TEXT,
            hash            TEXT NOT NULL,
            produced_by_json TEXT NOT NULL,
            approved_by_json TEXT,
            applied_at      TEXT,
            failed_at       TEXT,
            failure_reason  TEXT,
            supersedes_json TEXT,
            superseded_by   TEXT,
            rejected_by_json TEXT,
            revoked_by_json TEXT,
            tags_json       TEXT,
            rollback        INTEGER NOT NULL DEFAULT 0,
            replay_safe     INTEGER NOT NULL DEFAULT 1,
            requires_snapshot INTEGER NOT NULL DEFAULT 1,
            note_kind       TEXT,
            file_path       TEXT NOT NULL UNIQUE,
            created_at      TEXT NOT NULL,
            updated_at      TEXT NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_artifacts_status ON artifacts(status)",
        "CREATE INDEX IF NOT EXISTS idx_artifacts_kind ON artifacts(kind)",
        "CREATE INDEX IF NOT EXISTS idx_artifacts_target ON artifacts(target_json)",
        """CREATE TABLE IF NOT EXISTS artifact_dependencies (
            composite_id    TEXT NOT NULL REFERENCES artifacts(id),
            sub_artifact_id TEXT NOT NULL REFERENCES artifacts(id),
            depends_on      TEXT,
            on_error        TEXT NOT NULL DEFAULT 'halt',
            step_order      INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (composite_id, sub_artifact_id)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_deps_sub ON artifact_dependencies(sub_artifact_id)",
        """CREATE TABLE IF NOT EXISTS doc_metadata (
            id              INTEGER PRIMARY KEY,
            source          TEXT NOT NULL,
            kind            TEXT NOT NULL DEFAULT 'note',
            target          TEXT,
            title           TEXT NOT NULL,
            content         TEXT NOT NULL,
            url             TEXT,
            version         TEXT,
            embedded_at     TEXT NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_docs_kind ON doc_metadata(kind)",
        "CREATE INDEX IF NOT EXISTS idx_docs_target ON doc_metadata(target)",
        (
            "CREATE VIRTUAL TABLE IF NOT EXISTS vec_docs "
            "USING vec0(id INTEGER PRIMARY KEY, embedding float[768])"
        ),
        """CREATE TABLE IF NOT EXISTS settings (
            key         TEXT PRIMARY KEY,
            value       TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        )""",
    ],
    2: [
        """CREATE TABLE IF NOT EXISTS drift_checks (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            artifact_id     TEXT NOT NULL UNIQUE,
            drifted         INTEGER NOT NULL DEFAULT 0,
            checked_at      TEXT NOT NULL,
            details_json    TEXT
        )""",
        "CREATE INDEX IF NOT EXISTS idx_drift_checks_artifact ON drift_checks(artifact_id)",
        "CREATE INDEX IF NOT EXISTS idx_drift_checks_drifted ON drift_checks(drifted)",
    ],
    3: [
        (
            "CREATE TABLE IF NOT EXISTS tasks ("
            "id TEXT PRIMARY KEY, "
            "artifact_id TEXT NOT NULL, "
            "action TEXT NOT NULL CHECK(action IN ('apply', 'revoke', 'replay')), "
            "status TEXT NOT NULL DEFAULT 'pending' "
            "CHECK(status IN ('pending', 'running', 'succeeded', 'failed')), "
            "result_json TEXT, "
            "created_at TEXT NOT NULL, "
            "finished_at TEXT, "
            "error TEXT)"
        ),
        "CREATE INDEX IF NOT EXISTS idx_tasks_artifact ON tasks(artifact_id)",
        "CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)",
    ],
    4: [
        "ALTER TABLE api_tokens ADD COLUMN role TEXT",
    ],
    5: [
        """CREATE TABLE IF NOT EXISTS webhook_configs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT NOT NULL,
            event_types TEXT NOT NULL,
            secret TEXT,
            enabled INTEGER NOT NULL DEFAULT 1,
            max_retries INTEGER NOT NULL DEFAULT 3,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE TABLE IF NOT EXISTS webhook_deliveries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            webhook_id INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            payload TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            last_attempt_at TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (webhook_id) REFERENCES webhook_configs(id)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_wh_cfg_enabled ON webhook_configs(enabled)",
        "CREATE INDEX IF NOT EXISTS idx_wh_del_status ON webhook_deliveries(status)",
        "CREATE INDEX IF NOT EXISTS idx_wh_del_wh_id ON webhook_deliveries(webhook_id)",
    ],
    6: [
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_doc_metadata_source ON doc_metadata(source)",
    ],
    7: [
        ("ALTER TABLE api_tokens ADD COLUMN label TEXT", "api_tokens", "label"),
    ],
    8: [
        ("ALTER TABLE api_tokens ADD COLUMN fingerprint TEXT", "api_tokens", "fingerprint"),
    ],
    9: [
        """CREATE TABLE IF NOT EXISTS bootstrap_tokens (
            token_hash     TEXT PRIMARY KEY,
            expires_at     REAL NOT NULL
        )""",
    ],
}


async def run_migrations(db: Database) -> None:
    try:
        row = await db.fetchone("SELECT value FROM settings WHERE key = 'schema_version'")
        current_version = int(row["value"]) if row else 0
    except sqlite3.OperationalError:
        current_version = 0

    target_version = max(MIGRATIONS.keys())
    if current_version >= target_version:
        return

    for version in range(current_version + 1, target_version + 1):
        for entry in MIGRATIONS[version]:
            if isinstance(entry, tuple):
                sql, table, column = entry
                cols = await db.fetchall(f"PRAGMA table_info({table})")
                col_names = [c["name"] for c in cols]
                if column in col_names:
                    logging.getLogger(__name__).info(
                        "Skipping ALTER TABLE — column %s.%s already exists",
                        table,
                        column,
                    )
                    continue
                await db.execute(sql)
            else:
                sql = entry
                try:
                    await db.execute(sql)
                except Exception:  # re-raised if not ALTER TABLE
                    sql_stripped = sql.strip().upper()
                    if sql_stripped.startswith("ALTER TABLE"):
                        logging.getLogger(__name__).warning(
                            "Skipping ALTER TABLE (column may exist): %s",
                            sql.strip(),
                        )
                    else:
                        raise

    import datetime

    ts = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    await db.execute(
        (
            "INSERT INTO settings (key, value, updated_at) "
            "VALUES ('schema_version', ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET "
            "value=excluded.value, updated_at=excluded.updated_at"
        ),
        (str(target_version), ts),
    )
    await db.conn.commit()
