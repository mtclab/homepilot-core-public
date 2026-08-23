from __future__ import annotations

import datetime
import logging
import sqlite3
from pathlib import Path

from .connection import Database

logger = logging.getLogger(__name__)

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
    10: [
        "ALTER TABLE hosts ADD COLUMN pve_status TEXT",
        (
            (
                "ALTER TABLE hosts ADD COLUMN source TEXT NOT NULL "
                "DEFAULT 'discovered' CHECK(source IN ('hp_created','discovered','imported'))"
            ),
            "hosts",
            "source",
        ),
        ("ALTER TABLE hosts ADD COLUMN description TEXT", "hosts", "description"),
        ("ALTER TABLE hosts ADD COLUMN artifact_id TEXT", "hosts", "artifact_id"),
        (
            (
                "ALTER TABLE hosts ADD COLUMN import_state TEXT "
                "CHECK(import_state IN ('pending','adopted','ignored'))"
            ),
            "hosts",
            "import_state",
        ),
        (
            (
                "ALTER TABLE hosts ADD COLUMN role_source TEXT DEFAULT 'inferred' "
                "CHECK(role_source IN ('inferred','user','artifact'))"
            ),
            "hosts",
            "role_source",
        ),
        (
            (
                "ALTER TABLE hosts ADD COLUMN ip_source TEXT "
                "CHECK(ip_source IN ('pve','dhcp','user','dns'))"
            ),
            "hosts",
            "ip_source",
        ),
        "UPDATE hosts SET pve_status = status WHERE pve_status IS NULL",
        "UPDATE hosts SET source = 'hp_created' WHERE managed = 1",
        "UPDATE hosts SET role_source = 'user' WHERE role != 'guest'",
        "UPDATE hosts SET status = 'unknown'",
        "CREATE INDEX IF NOT EXISTS idx_hosts_source ON hosts(source)",
        "CREATE INDEX IF NOT EXISTS idx_hosts_artifact ON hosts(artifact_id)",
    ],
    11: [
        # Persist the agent registry so connected agents survive a backend
        # restart/update: the Agents view + coverage show last-known agents
        # (reconnecting) instead of flapping to empty until each agent redials.
        """CREATE TABLE IF NOT EXISTS agents (
            agent_id        TEXT PRIMARY KEY,
            hostname        TEXT NOT NULL,
            system_info     TEXT,
            state           TEXT,
            connected       INTEGER NOT NULL DEFAULT 0,
            first_seen      TEXT,
            connected_at    TEXT,
            last_heartbeat  TEXT,
            disconnected_at TEXT
        )""",
        "CREATE INDEX IF NOT EXISTS idx_agents_hostname ON agents(hostname)",
        "CREATE INDEX IF NOT EXISTS idx_agents_connected ON agents(connected)",
    ],
    12: [
        # Per-agent credentials (#362 slice 2): each agent is issued a unique
        # durable token whose sha256 hash is stored here (never the raw token),
        # bound to its agent_id. Replaces the shared fleet token as the
        # steady-state per-connection credential. `revoked_at` fails a
        # credential so a compromised/retired agent cannot reconnect until it is
        # re-enrolled with a bootstrap/shared token.
        ("ALTER TABLE agents ADD COLUMN credential_hash TEXT", "agents", "credential_hash"),
        ("ALTER TABLE agents ADD COLUMN credential_set_at TEXT", "agents", "credential_set_at"),
        ("ALTER TABLE agents ADD COLUMN revoked_at TEXT", "agents", "revoked_at"),
    ],
    13: [
        # Durable, attributable agent-hub audit trail (#381). The agent hub is a
        # fleet-root command channel; its audit must survive a backend restart and
        # record WHO issued each command/lifecycle event — the in-memory deque in
        # AuditLog evaporated on restart and carried no caller attribution.
        """CREATE TABLE IF NOT EXISTS agent_audit (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            ts         TEXT,
            agent_id   TEXT,
            hostname   TEXT,
            action     TEXT,
            target     TEXT,
            result     TEXT,
            exit_code  INTEGER,
            caller     TEXT
        )""",
        "CREATE INDEX IF NOT EXISTS idx_agent_audit_ts ON agent_audit(ts)",
    ],
    14: [
        # Task cancellation (#376): widen the tasks.status CHECK to admit
        # 'cancelled'. SQLite can't ALTER a CHECK constraint, so rebuild the
        # table (rename → recreate → copy → drop). Indexes ride the rename onto
        # the old table and are dropped with it, so recreate them AFTER the drop
        # (a CREATE INDEX IF NOT EXISTS while the old-named index still exists
        # would be a silent no-op and leave the new table unindexed).
        "ALTER TABLE tasks RENAME TO tasks_old",
        (
            "CREATE TABLE tasks ("
            "id TEXT PRIMARY KEY, "
            "artifact_id TEXT NOT NULL, "
            "action TEXT NOT NULL CHECK(action IN ('apply', 'revoke', 'replay')), "
            "status TEXT NOT NULL DEFAULT 'pending' "
            "CHECK(status IN ('pending', 'running', 'succeeded', 'failed', 'cancelled')), "
            "result_json TEXT, "
            "created_at TEXT NOT NULL, "
            "finished_at TEXT, "
            "error TEXT)"
        ),
        (
            "INSERT INTO tasks "
            "(id, artifact_id, action, status, result_json, created_at, finished_at, error) "
            "SELECT id, artifact_id, action, status, result_json, created_at, finished_at, error "
            "FROM tasks_old"
        ),
        "DROP TABLE tasks_old",
        "CREATE INDEX IF NOT EXISTS idx_tasks_artifact ON tasks(artifact_id)",
        "CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)",
    ],
    15: [
        # Provision-from-template (#442 stage 1). Two independent changes:
        #
        # 1. tasks: admit the 'provision' action and make artifact_id NULLABLE.
        #    A provision task has no artifact — it creates infrastructure rather
        #    than applying authored intent — and it must stay OUT of the
        #    artifact-scoped dedup/active-task queries, which a NULL artifact_id
        #    guarantees (`WHERE artifact_id = ?` never matches NULL). Same
        #    rebuild dance as migration 14 (CHECK constraints and column
        #    nullability are not ALTERable in SQLite): rename → recreate → copy
        #    → drop → recreate the indexes AFTER the drop, because the old
        #    indexes ride the rename and only vanish with the old table.
        "ALTER TABLE tasks RENAME TO tasks_old",
        (
            "CREATE TABLE tasks ("
            "id TEXT PRIMARY KEY, "
            "artifact_id TEXT, "
            "action TEXT NOT NULL "
            "CHECK(action IN ('apply', 'revoke', 'replay', 'provision')), "
            "status TEXT NOT NULL DEFAULT 'pending' "
            "CHECK(status IN ('pending', 'running', 'succeeded', 'failed', 'cancelled')), "
            "result_json TEXT, "
            "created_at TEXT NOT NULL, "
            "finished_at TEXT, "
            "error TEXT)"
        ),
        (
            "INSERT INTO tasks "
            "(id, artifact_id, action, status, result_json, created_at, finished_at, error) "
            "SELECT id, artifact_id, action, status, result_json, created_at, finished_at, error "
            "FROM tasks_old"
        ),
        "DROP TABLE tasks_old",
        "CREATE INDEX IF NOT EXISTS idx_tasks_artifact ON tasks(artifact_id)",
        "CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)",
        # 2. hosts.owner: who asked for this guest. Recorded at provision time
        #    and never derived from Proxmox, so an inventory refresh cannot
        #    overwrite it.
        ("ALTER TABLE hosts ADD COLUMN owner TEXT", "hosts", "owner"),
    ],
    16: [
        # Invite-based self-service provisioning (#442 stage 2).
        #
        # The token is NEVER stored: only its 16-char prefix (the lookup key) and
        # the sha256 of the whole token, exactly as api_tokens does — a database
        # copy must not let anyone redeem an invite.
        #
        # bound_cn is the client-certificate CN the invite is minted FOR. It is
        # the whole point of the binding: the portal refuses a redemption whose
        # proxy-asserted CN differs, so a leaked URL is useless without the cert.
        #
        # The caps columns (template_vmid .. ipconfig0) are chosen by the OPERATOR
        # at mint time and are the only values that ever reach ProvisionService.
        # Nothing the redeemer submits can widen them.
        #
        # redeemed_at is the single-use latch: redemption claims the row with
        # `UPDATE ... WHERE redeemed_at IS NULL` and trusts rowcount, so two
        # simultaneous posts can never both provision.
        """CREATE TABLE IF NOT EXISTS invites (
            id                TEXT PRIMARY KEY,
            token_prefix      TEXT NOT NULL,
            token_hash        TEXT NOT NULL,
            bound_cn          TEXT NOT NULL,
            template_vmid     INTEGER NOT NULL,
            node              TEXT NOT NULL,
            pool              TEXT,
            cores             INTEGER,
            memory_mb         INTEGER,
            disk_gb           INTEGER,
            disk              TEXT NOT NULL DEFAULT 'scsi0',
            ipconfig0         TEXT NOT NULL DEFAULT 'ip=dhcp',
            expires_at        TEXT NOT NULL,
            created_by        TEXT NOT NULL,
            created_at        TEXT NOT NULL,
            redeemed_at       TEXT,
            redeemed_cn       TEXT,
            resulting_host_id TEXT,
            resulting_task_id TEXT,
            revoked_at        TEXT
        )""",
        "CREATE INDEX IF NOT EXISTS idx_invites_prefix ON invites(token_prefix)",
        "CREATE INDEX IF NOT EXISTS idx_invites_expires ON invites(expires_at)",
    ],
    17: [
        # First-run claim (#458 S1). ONE row, ever: `CHECK (id = 1)` makes a
        # second instance-claim row impossible at the storage layer rather than
        # by convention, so no query has to pick "the right" row.
        #
        # A dedicated table rather than a settings key/value row because the
        # single-use latch is a conditional UPDATE whose rowcount is the whole
        # guarantee (`WHERE id = 1 AND claimed_at IS NULL`, the invites.claim
        # pattern). Packing four fields into settings.value as JSON would make
        # that latch a read-modify-write, which two simultaneous POSTs both win.
        #
        # code_hash is the sha256 of the WHOLE code (auth/tokens.py scheme); the
        # code itself is never stored here. code_prefix is for log correlation
        # only - with a single row there is nothing to look up by.
        """CREATE TABLE IF NOT EXISTS instance_claim (
            id                  INTEGER PRIMARY KEY CHECK (id = 1),
            code_prefix         TEXT NOT NULL,
            code_hash           TEXT NOT NULL,
            created_at          TEXT NOT NULL,
            claimed_at          TEXT,
            claimed_label       TEXT,
            minted_token_prefix TEXT
        )""",
    ],
    18: [
        # Zero-touch agent rollout (#458 S4): the tasks CHECK constraint must
        # admit the 'install_agent' action. Artifactless like 'provision' - it
        # enrols a host rather than applying authored intent - so it relies on
        # the same NULL artifact_id to stay out of the artifact-scoped
        # dedup/active-task queries.
        #
        # Same rebuild dance as migrations 14 and 15 (a CHECK constraint is not
        # ALTERable in SQLite): rename -> recreate -> copy -> drop -> recreate
        # the indexes AFTER the drop, because the old indexes ride the rename
        # and only vanish with the old table.
        "ALTER TABLE tasks RENAME TO tasks_old",
        (
            "CREATE TABLE tasks ("
            "id TEXT PRIMARY KEY, "
            "artifact_id TEXT, "
            "action TEXT NOT NULL "
            "CHECK(action IN ('apply', 'revoke', 'replay', 'provision', 'install_agent')), "
            "status TEXT NOT NULL DEFAULT 'pending' "
            "CHECK(status IN ('pending', 'running', 'succeeded', 'failed', 'cancelled')), "
            "result_json TEXT, "
            "created_at TEXT NOT NULL, "
            "finished_at TEXT, "
            "error TEXT)"
        ),
        (
            "INSERT INTO tasks "
            "(id, artifact_id, action, status, result_json, created_at, finished_at, error) "
            "SELECT id, artifact_id, action, status, result_json, created_at, finished_at, error "
            "FROM tasks_old"
        ),
        "DROP TABLE tasks_old",
        "CREATE INDEX IF NOT EXISTS idx_tasks_artifact ON tasks(artifact_id)",
        "CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)",
    ],
    19: [
        # Native metrics (#458 S5, ADR-004). Raw samples only - NO rollups: the
        # ADR says measure a week of real data before deciding whether they earn
        # their complexity.
        #
        # WITHOUT ROWID with PRIMARY KEY (hostname, metric, ts) is the whole
        # storage design. One b-tree holds the data in exactly the order the
        # only read pattern wants ("this host, this metric, this window"), so
        # that query is a range scan over a key prefix and never touches another
        # host's rows. A rowid table would need the same three columns duplicated
        # into a secondary index PLUS a rowid b-tree - roughly double the bytes
        # for the same answer. The PK doubles as the dedupe key: a re-sent batch
        # (the agent re-sends anything the hub did not ack) is an INSERT OR
        # REPLACE onto the same key, not a duplicate point.
        #
        # The series is keyed by HOSTNAME, not agent_id: a reinstalled agent gets
        # a new agent_id but is the same machine, and an operator asking "what did
        # this host do last night" means the machine. agent_id is recorded as a
        # plain column so the reporter is still known.
        """CREATE TABLE IF NOT EXISTS metrics (
            hostname  TEXT NOT NULL,
            metric    TEXT NOT NULL,
            ts        INTEGER NOT NULL,
            value     REAL NOT NULL,
            agent_id  TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (hostname, metric, ts)
        ) WITHOUT ROWID""",
        # Retention prunes by AGE across every series, which the PK cannot serve
        # (ts is its last column). Without this the hourly pruner is a full scan
        # of the whole table.
        "CREATE INDEX IF NOT EXISTS idx_metrics_ts ON metrics(ts)",
        # Alert rules. `for_seconds` is the point of the feature: a rule fires
        # only when its condition held for that whole span, so a single spike
        # cannot page anyone.
        """CREATE TABLE IF NOT EXISTS alert_rules (
            id           TEXT PRIMARY KEY,
            name         TEXT NOT NULL,
            host_filter  TEXT NOT NULL DEFAULT '*',
            metric       TEXT NOT NULL,
            comparison   TEXT NOT NULL,
            threshold    REAL NOT NULL,
            for_seconds  INTEGER NOT NULL DEFAULT 300,
            enabled      INTEGER NOT NULL DEFAULT 1,
            created_at   TEXT NOT NULL,
            updated_at   TEXT NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_alert_rules_enabled ON alert_rules(enabled)",
        # One row per (rule, host) the rule has ever evaluated. firing_since is
        # the latch: NULL means not firing, a timestamp means "firing since".
        # It is what makes a fire notify ONCE rather than every evaluation, and
        # what lets a recovery be detected at all.
        """CREATE TABLE IF NOT EXISTS alert_state (
            rule_id      TEXT NOT NULL,
            hostname     TEXT NOT NULL,
            firing_since TEXT,
            last_value   REAL,
            last_eval    TEXT NOT NULL,
            PRIMARY KEY (rule_id, hostname)
        )""",
    ],
    20: [
        # Why an agent is not here (#430). Every reason the hub refuses or drops
        # an agent - a revoked credential, a hostname-bound token presented from
        # elsewhere, a replayed register, an identity already claimed, a banned
        # peer, a plain socket drop - was `logger.warning` only. None of it
        # reached the database, the audit trail or the API, so a revoked agent,
        # a banned agent, a duplicate-identity clash and a powered-off box were
        # pixel-identical grey dots in the UI.
        #
        # Two columns rather than a table: this is the LAST reason, the one an
        # operator looking at a dark host needs, and it is overwritten on each
        # new one. The history of rejections belongs in - and now goes to - the
        # audit log, which already has a durable append-only shape.
        ("ALTER TABLE agents ADD COLUMN last_error TEXT", "agents", "last_error"),
        ("ALTER TABLE agents ADD COLUMN last_error_at TEXT", "agents", "last_error_at"),
    ],
    21: [
        # A guest that no longer exists in Proxmox (#445 A5). The refresh only
        # ever wrote what it FOUND, so a destroyed VM kept its last-known row and
        # its last-known status forever - it simply stopped being updated, which
        # from the inventory looks exactly like a machine that is merely powered
        # off. The reconciler already computed the absent set and spent it on an
        # audit counter; nothing reached the host or the UI.
        #
        # A timestamp rather than a flag: "gone since Tuesday" is what decides
        # whether this is a deletion or a hypervisor that was briefly
        # unreachable, and clearing it on the next sighting is one write either
        # way.
        ("ALTER TABLE hosts ADD COLUMN absent_since TEXT", "hosts", "absent_since"),
        # Widen hosts.source to admit 'manual' (#445 A5). A CHECK constraint is
        # not ALTERable in SQLite, so it is the same rebuild dance as migrations
        # 14/15/18: rename -> recreate -> copy -> drop -> recreate the indexes
        # AFTER the drop, because the old indexes ride the rename and only vanish
        # with the old table.
        #
        # 'manual' is not cosmetic: it is what tells the absence sweep that
        # Proxmox never looked for this machine, so a sync must never declare it
        # gone.
        # NOTE the shape of this rebuild: build-copy-drop-RENAME, not the
        # rename-first dance migrations 14/15/18 use for `tasks`. `services.host_id`
        # REFERENCES hosts(id), and modern SQLite REWRITES that reference to
        # follow a rename - so renaming `hosts` out of the way first leaves
        # `services` pointing at `hosts_old`, and dropping it breaks every later
        # write to services with "no such table: main.hosts_old". Renaming the
        # NEW table into place instead leaves the reference text untouched,
        # pointing at the name that exists again by the end.
        """CREATE TABLE hosts_new (
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
            updated_at      TEXT NOT NULL,
            pve_status      TEXT,
            source          TEXT NOT NULL DEFAULT 'discovered'
                            CHECK(source IN ('hp_created','discovered','imported','manual')),
            description     TEXT,
            artifact_id     TEXT,
            import_state    TEXT CHECK(import_state IN ('pending','adopted','ignored')),
            role_source     TEXT DEFAULT 'inferred'
                            CHECK(role_source IN ('inferred','user','artifact')),
            ip_source       TEXT CHECK(ip_source IN ('pve','dhcp','user','dns')),
            owner           TEXT,
            absent_since    TEXT
        )""",
        """INSERT INTO hosts_new (
            id, proxmox_id, hostname, node, host_type, role, ip_address, fqdn, status,
            tags, managed_by, managed, storage_pool, os_info, cpu_cores, memory_mb,
            disk_gb, network_bridge, vlan_id, created_at, updated_at, pve_status,
            source, description, artifact_id, import_state, role_source, ip_source,
            owner, absent_since
        ) SELECT
            id, proxmox_id, hostname, node, host_type, role, ip_address, fqdn, status,
            tags, managed_by, managed, storage_pool, os_info, cpu_cores, memory_mb,
            disk_gb, network_bridge, vlan_id, created_at, updated_at, pve_status,
            source, description, artifact_id, import_state, role_source, ip_source,
            owner, absent_since
        FROM hosts""",
        "DROP TABLE hosts",
        "ALTER TABLE hosts_new RENAME TO hosts",
        "CREATE INDEX IF NOT EXISTS idx_hosts_proxmox ON hosts(proxmox_id)",
        "CREATE INDEX IF NOT EXISTS idx_hosts_managed ON hosts(managed)",
        "CREATE INDEX IF NOT EXISTS idx_hosts_role ON hosts(role)",
        "CREATE INDEX IF NOT EXISTS idx_hosts_status ON hosts(status)",
        "CREATE INDEX IF NOT EXISTS idx_hosts_source ON hosts(source)",
        "CREATE INDEX IF NOT EXISTS idx_hosts_artifact ON hosts(artifact_id)",
    ],
    22: [
        # Drift is tri-state (#425). `drifted` was a boolean, so every path that
        # could not check - no spec, no host, no executor, a timeout, an error,
        # and the whole (dead) ansible verifier - stored `drifted = 0`, which the
        # UI rendered as a green "in spec" for something nobody had looked at.
        #
        # The column carries 'in_spec' | 'drifted' | 'unknown'. Existing rows are
        # backfilled to 'unknown' rather than 'in_spec': what a pre-migration
        # `drifted = 0` row actually means is unknowable, and guessing "fine"
        # would carry the exact defect across the upgrade. A real check overwrites
        # it within one reconciler cycle.
        ("ALTER TABLE drift_checks ADD COLUMN state TEXT", "drift_checks", "state"),
        "UPDATE drift_checks SET state = 'drifted' WHERE drifted = 1 AND state IS NULL",
        "UPDATE drift_checks SET state = 'unknown' WHERE state IS NULL",
    ],
    23: [
        # What a host looked like before a host-provision artifact touched it
        # (#426). Without this there is nothing to invert TO: after the apply the
        # prior file bytes are gone, and "was this package already installed"
        # cannot be reconstructed from anywhere.
        #
        # One row per artifact, replaced on re-apply: the useful capture is the
        # state before the LAST apply, which is what a revoke now undoes. Keeping
        # a history would invite restoring a host to a state two applies ago.
        """CREATE TABLE IF NOT EXISTS host_state_captures (
            artifact_id  TEXT PRIMARY KEY,
            captured_at  TEXT NOT NULL,
            items_json   TEXT NOT NULL
        )""",
    ],
    24: [
        # Fields an OPERATOR set, which automation must not overwrite (#424).
        #
        # `role_source` / `ip_source` were the only provenance guards and they
        # covered two fields out of the several an operator can write. Worse, the
        # node refresh FORGED `role_source = "user"` from automation, which
        # defeated the #416 fix and hid the UI's "inferred" badge - a lie about
        # who decided a value.
        #
        # One JSON list per host rather than a `*_source` column per field: the
        # question is always "did a person set this", the answer is the same
        # shape for every field, and a new operator-writable field then needs no
        # migration - only the PATCH that writes it.
        ("ALTER TABLE hosts ADD COLUMN pinned_fields TEXT", "hosts", "pinned_fields"),
    ],
    25: [
        # One noun: Host (#514 S1). A machine carrying a connected agent was not
        # a host in the product's own model - Inventory said "No hosts" and
        # Coverage said 0% while an enrolled agent sat one tab over. Enrolment
        # now creates-or-links a host row, which needs (a) 'agent' as a legal
        # `source`, and (b) an explicit `agent_id` link so the join is a fact,
        # not a hostname coincidence.
        #
        # Table rebuild because the source CHECK cannot be altered in place.
        # Same rename-into-place dance as migration 21, for the same
        # services.host_id foreign-key reason documented there.
        """CREATE TABLE hosts_new (
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
            updated_at      TEXT NOT NULL,
            pve_status      TEXT,
            source          TEXT NOT NULL DEFAULT 'discovered'
                            CHECK(source IN
                              ('hp_created','discovered','imported','manual','agent')),
            description     TEXT,
            artifact_id     TEXT,
            import_state    TEXT CHECK(import_state IN ('pending','adopted','ignored')),
            role_source     TEXT DEFAULT 'inferred'
                            CHECK(role_source IN ('inferred','user','artifact')),
            ip_source       TEXT CHECK(ip_source IN ('pve','dhcp','user','dns')),
            owner           TEXT,
            absent_since    TEXT,
            pinned_fields   TEXT,
            agent_id        TEXT
        )""",
        """INSERT INTO hosts_new (
            id, proxmox_id, hostname, node, host_type, role, ip_address, fqdn, status,
            tags, managed_by, managed, storage_pool, os_info, cpu_cores, memory_mb,
            disk_gb, network_bridge, vlan_id, created_at, updated_at, pve_status,
            source, description, artifact_id, import_state, role_source, ip_source,
            owner, absent_since, pinned_fields
        ) SELECT
            id, proxmox_id, hostname, node, host_type, role, ip_address, fqdn, status,
            tags, managed_by, managed, storage_pool, os_info, cpu_cores, memory_mb,
            disk_gb, network_bridge, vlan_id, created_at, updated_at, pve_status,
            source, description, artifact_id, import_state, role_source, ip_source,
            owner, absent_since, pinned_fields
        FROM hosts""",
        "DROP TABLE hosts",
        "ALTER TABLE hosts_new RENAME TO hosts",
        "CREATE INDEX IF NOT EXISTS idx_hosts_proxmox ON hosts(proxmox_id)",
        "CREATE INDEX IF NOT EXISTS idx_hosts_managed ON hosts(managed)",
        "CREATE INDEX IF NOT EXISTS idx_hosts_role ON hosts(role)",
        "CREATE INDEX IF NOT EXISTS idx_hosts_status ON hosts(status)",
        "CREATE INDEX IF NOT EXISTS idx_hosts_source ON hosts(source)",
        "CREATE INDEX IF NOT EXISTS idx_hosts_artifact ON hosts(artifact_id)",
        "CREATE INDEX IF NOT EXISTS idx_hosts_agent ON hosts(agent_id)",
        # Backfill: machines whose agent enrolled BEFORE this migration. Link by
        # exact hostname where that is unambiguous; where a hostname matches no
        # host, create the row the enrolment would have created.
        """UPDATE hosts SET agent_id = (
            SELECT a.agent_id FROM agents a WHERE a.hostname = hosts.hostname
        ) WHERE agent_id IS NULL
          AND (SELECT COUNT(*) FROM agents a WHERE a.hostname = hosts.hostname) = 1""",
        """INSERT INTO hosts (id, hostname, host_type, role, status, managed_by,
                              managed, source, role_source, agent_id,
                              created_at, updated_at)
            SELECT lower(hex(randomblob(16))), a.hostname, 'physical', 'guest',
                   'unknown', 'agent', 0, 'agent', 'inferred', a.agent_id,
                   COALESCE(a.first_seen, strftime('%Y-%m-%dT%H:%M:%SZ','now')),
                   COALESCE(a.first_seen, strftime('%Y-%m-%dT%H:%M:%SZ','now'))
            FROM agents a
            WHERE NOT EXISTS (SELECT 1 FROM hosts h WHERE h.hostname = a.hostname)""",
    ],
}


_SCHEMA_VERSION_UPSERT = (
    "INSERT INTO settings (key, value, updated_at) "
    "VALUES ('schema_version', ?, ?) "
    "ON CONFLICT(key) DO UPDATE SET "
    "value=excluded.value, updated_at=excluded.updated_at"
)


def _is_memory_db(db_path: str) -> bool:
    return db_path == ":memory:" or "mode=memory" in db_path


async def _backup_before_migration(db: Database, current_version: int) -> Path | None:
    """Back the database up before any DDL runs.

    Fail closed: without a restorable copy the migration must not start, because
    there is no way back from a partially applied schema (#420).

    Returns None when there is provably nothing to lose: an in-memory database,
    or a database with no user tables at all (a fresh install, and every test
    that builds its own). Note this is decided on the TABLES, not on the version
    being 0 - a legacy database predating the schema_version row also reports 0
    and very much has data to protect.
    """
    if _is_memory_db(db.db_path):
        return None
    row = await db.fetchone(
        "SELECT COUNT(*) AS cnt FROM sqlite_master WHERE type = 'table' "
        "AND name NOT LIKE 'sqlite_%'"
    )
    if row is not None and int(row["cnt"]) == 0:
        return None

    backup_path = Path(db.db_path).parent / "backups" / f"pre-migration-v{current_version}.db"
    try:
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        # sqlite backup API, never a file copy: copying a live WAL database
        # file-by-file yields a torn snapshot (#421).
        target = sqlite3.connect(str(backup_path), check_same_thread=False)
        try:
            await db.conn.backup(target)
        finally:
            target.close()
    except Exception as exc:
        raise RuntimeError(
            f"Pre-migration backup to {backup_path} failed: {exc}. "
            "Migrations aborted - the database is unchanged."
        ) from exc

    logger.info("Pre-migration backup written: %s", backup_path)
    return backup_path


async def _apply_statement(db: Database, entry: str | tuple[str, str, str]) -> None:
    if isinstance(entry, tuple):
        sql, table, column = entry
        cols = await db.fetchall(f"PRAGMA table_info({table})")
        col_names = [c["name"] for c in cols]
        if column in col_names:
            logger.info("Skipping ALTER TABLE — column %s.%s already exists", table, column)
            return
        await db.execute(sql)
        return

    try:
        await db.execute(entry)
    except Exception as exc:
        # Only a re-run of an idempotent ADD COLUMN is survivable. Every other
        # ALTER failure - a RENAME above all - must abort the version, or the
        # database is left half-migrated with the version not bumped (#420).
        if "duplicate column name" not in str(exc).lower():
            raise
        logger.warning("Skipping ALTER TABLE - column already exists: %s", entry.strip())


async def _apply_version(db: Database, version: int, backup_path: Path | None) -> None:
    """Apply one version's statements and its schema_version bump in one transaction."""
    # The driver may hold an implicit transaction from earlier DML; BEGIN inside
    # one is an error, so close it first.
    await db.conn.commit()
    await db.execute("BEGIN IMMEDIATE")
    try:
        for entry in MIGRATIONS[version]:
            await _apply_statement(db, entry)
        ts = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        await db.execute(_SCHEMA_VERSION_UPSERT, (str(version), ts))
    except Exception as exc:
        rolled_back = True
        try:
            await db.execute("ROLLBACK")
        except Exception as rollback_exc:
            rolled_back = False
            logger.error(
                "ROLLBACK after failed migration to version %d failed: %s",
                version,
                rollback_exc,
            )
        # Restoring the backup is only the right move when the rollback itself
        # failed - after a clean rollback the database is intact at the prior
        # version and a plain retry (with the migration fixed) is correct;
        # restoring would needlessly rewind versions committed earlier this run.
        if rolled_back:
            raise RuntimeError(
                f"Migration to schema version {version} failed and was rolled back "
                f"(database intact at version {version - 1}): {exc}. "
                "Fix the cause and restart - no restore needed."
            ) from exc
        recovery = (
            f" Restore the pre-migration backup {backup_path} before retrying."
            if backup_path is not None
            else ""
        )
        raise RuntimeError(
            f"Migration to schema version {version} failed AND could not be rolled "
            f"back - the database may be inconsistent.{recovery}"
        ) from exc
    await db.execute("COMMIT")


async def run_migrations(db: Database) -> None:
    try:
        row = await db.fetchone("SELECT value FROM settings WHERE key = 'schema_version'")
        current_version = int(row["value"]) if row else 0
    except sqlite3.OperationalError:
        current_version = 0

    target_version = max(MIGRATIONS.keys())
    if current_version > target_version:
        raise RuntimeError(
            f"Database schema version {current_version} is newer than this build supports "
            f"(version {target_version}). No down-migrations exist: to run this build, restore "
            f"the database backup matching schema version {target_version} "
            "(<data_dir>/backups/pre-migration-v<version>.db)."
        )
    if current_version == target_version:
        return

    backup_path = await _backup_before_migration(db, current_version)

    for version in range(current_version + 1, target_version + 1):
        await _apply_version(db, version, backup_path)
