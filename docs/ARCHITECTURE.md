# HomePilot — System Architecture

## Deployment Topology

```
╔══════════════════════════════════════════════════════════════════════╗
║  AI CLIENT  (Kasm workspace, laptop, any machine with Claude/opencode)║
║                                                                      ║
║   ┌─────────────┐   ┌─────────────┐                                  ║
║   │ Claude Code │   │  OpenCode   │                                  ║
║   └──────┬──────┘   └──────┬──────┘                                  ║
║          │  HTTP MCP  Authorization: Bearer <HP_MCP_TOKEN>           ║
╚══════════╪══════════════════╪═══════════════════════════════════════╝
           └────────┬─────────┘
                    │ HP_MCP_URL = http://<homelab>:8000/mcp
                    │
╔═══════════════════▼══════════════════════════════════════════════════╗
║  HOMELAB SERVER  (always-on; runs alongside or on a Proxmox node)   ║
║                                                                      ║
║  ┌──────────────────────────────────────────────────────────────┐   ║
║  │  hp mcp-serve --transport http --port 8000                   │   ║
║  │  MCP endpoint: /mcp  (StreamableHTTP, stateful sessions)     │   ║
║  │                                                              │   ║
║  │  MCP tools:                                                  │   ║
║  │   query_inventory      refresh_inventory   get_environment_doc│  ║
║  │   query_artifacts      propose_artifact    approve_artifact  │  ║
║  │   get_artifact_status  search_kb           proxmox_api_read  │  ║
║  │   record_fact          http_call_read      read_file_on_guest│  ║
║  │   exec_on_guest_readonly check_artifact_drift                │  ║
║  └───────────┬────────────────────────────┬──────────────────────┘  ║
║              │                            │                          ║
║   ┌───────────▼────────────┐  ┌────────────▼───────────────────────┐ ║
║   │  Core Services         │  │  Data Layer                        │ ║
║   │                        │  │                                    │ ║
║   │  ArtifactLifecycle     │  │  SQLite  — hosts, services,        │ ║
║   │  ArtifactExecutor      │  │    artifacts, KB entries,          │ ║
║   │  KBService             │  │    audit log, vector index,        │ ║
║   │  InventoryService      │  │    bootstrap tokens                │ ║
║   │  VaultManager          │  │                                    │ ║
║   │  EventBus / Webhooks   │  │  Artifact Store — markdown files   │ ║
║   │  AgentAdapter          │  │    (one file per artifact)         │ ║
║   │  AgentHubServer        │  │                                    │ ║
║   └───────────┬────────────┘  │    (one file per artifact)         │ ║
║              │               │                                    │  ║
║   ┌──────────┴──────────┐    │  Vault — age-encrypted secrets     │  ║
║   │                     │    │    (Proxmox token, service creds)  │  ║
║   ▼                     ▼    └────────────────────────────────────┘  ║
║  Proxmox            Agent Hub                                        ║
║  REST API           :8443                                            ║
║  :8006              (TCP relay, length-prefixed JSON)                ║
║                        ┌──────────────────────────┐                  ║
║                        │  Managed Hosts           │                  ║
║                        │                          │                  ║
║                        │  hp-agent daemon ────────┼──► outbound TCP  ║
║                        │  (exec, read/write file, │    to hub :8443  ║
║                        │   heartbeat, metrics)    │                  ║
║                        └──────────────────────────┘                  ║
║   ┌───────────────────────┐                                          ║
║   │  Guest VMs / LXC      │                                          ║
║   │  (hp-agent required;  │                                          ║
║   │   no SSH fallback)    │                                          ║
║   └───────────────────────┘                                          ║
╚══════════════════════════════════════════════════════════════════════╝
```

For local / stdio use (no server):

```bash
hp mcp-serve               # default: stdio transport
```

Claude Code config (`~/.claude/settings.json`):

```json
{
  "mcpServers": {
    "homepilot": { "command": "hp", "args": ["mcp-serve"] }
  }
}
```

For remote / HTTP use (recommended for shared homelab):

```bash
HP_MCP_TOKEN=<secret> hp mcp-serve --transport http --port 8000
```

Claude Code config:

```json
{
  "mcpServers": {
    "homepilot": {
      "url": "http://<homelab-ip>:8000/mcp",
      "headers": { "Authorization": "Bearer <secret>" }
    }
  }
}
```

---

## Artifact Lifecycle

Every change HomePilot can make follows one path: **propose → review → approve → apply**.
The agent never mutates directly. It drafts a fully-specified plan; you decide when it runs.

```
 AGENT (Claude / opencode)
 ──────────────────────────────────────────────────────────────────────

 1. READ      query_inventory / proxmox_api_read / search_kb
              → Agent builds context about the current state


 2. PROPOSE   propose_artifact(spec)
              → Artifact written to store with status: proposed

              ┌──────────────────────────────────────────────────────┐
              │  id:          2026-05-08-install-nginx-web1-a3f9c2   │
              │  kind:        ansible-playbook                       │
              │  status:      proposed                               │
              │  target:      { kind: lxc, host: web1 }             │
              │  intent:      "Install nginx on web1"                │
              │  idempotence: via-precheck                           │
              │  mutating:    true                                   │
              │  body:        [Ansible playbook YAML]                │
              └──────────────────────────────────────────────────────┘


 YOU
 ──────────────────────────────────────────────────────────────────────

 3. REVIEW    hp artifacts show <id>   ← inspect the spec
              (or web UI once #31 ships)

              Edit the artifact file directly if needed, then:

              hp artifacts approve <id>    → status: approved
              hp artifacts reject <id>     → status: rejected (terminal)


 4. APPLY     hp artifacts apply <id>
              → ArtifactExecutor._dispatch() runs the spec

              ┌─────────────────────────────────────────────────────────────┐
              │  Before mutating: Proxmox snapshot (if proxmox target)      │
              │  Dispatch by kind:                                           │
              │    ansible-playbook    → playbook via hp-agent (Agent Hub)    │
              │    proxmox-api-seq     → ordered Proxmox REST calls          │
              │    http-sequence       → ordered calls to adopted services   │
              │    shell-script        → exec on guest via hp-agent           │
              │    composite           → ordered list of sub-artifacts       │
              │    kb-note             → write to KB  (auto-applied, no exec)│
              └─────────────────────────────────────────────────────────────┘

                         │                         │
                         ▼                         ▼
                   status: applied           status: failed
                         │                         │
                         │                    hp artifacts approve <id>
                         │                    (re-queue after fix)
                         │
                         ▼
                   Snapshot pruned
                   Audit entry written


 5. REVOKE    hp artifacts revoke <id>  (applied → revoked)
              → If Proxmox snapshot exists: VM rolled back automatically
              → Artifact status: revoked, snapshot deleted
```

### Status transitions

```
                    ┌─────────┐
              ┌────►│ proposed│
              │     └────┬────┘
              │          │ approve / reject
              │     ┌────┴──────┐
              │     ▼           ▼
              │  approved    rejected  ──── (terminal)
              │     │
              │     │ apply
              │  ┌──┴───┐
              │  ▼       ▼
              │ applied  failed ──► approve (retry)
              │  │
              │  │ supersede
              │  ▼
              │ superseded ──── (terminal)
              │
              └── revoke ──► revoked ──── (terminal, triggers rollback)
```

---

## MCP Tools Reference

| Tool | Mutates? | Scope | Description |
|------|----------|-------|-------------|
| `query_inventory` | no | read | List hosts/services/networks from DB; optional JSON filter |
| `refresh_inventory` | no | read | Re-pull inventory from the Proxmox REST API + connected agents |
| `get_environment_doc` | no | read | Fused doc for a target: inventory + KB + artifact history |
| `query_artifacts` | no | read | Find artifacts by status, kind, target, or date |
| `get_artifact_status` | no | read | Detailed status of one artifact: id, kind, status, intent, target, last_updated |
| `search_kb` | no | read | Vector + keyword search over KB notes/policies/decisions |
| `proxmox_api_read` | no | read | GET-only Proxmox REST call; allowlisted paths only |
| `http_call_read` | no | read | GET-only call to an adopted service; vault-resolved creds |
| `read_file_on_guest` | no | read | File read from a guest via the agent hub |
| `exec_on_guest_readonly` | no | read | Read-only exec on guest via agent hub or SSH (cat, ls, ps…) |
| `check_artifact_drift` | no | read | Check whether an applied artifact has drifted from desired state |
| `query_guests` | no | read | Every portal guest's usage vs budget, plus invites (prefixes only - never tokens) |
| `set_guest_quota` | quota table | write | Set a guest's resource budget (totals across all their machines) |
| `revoke_guest_invite` | invite state | write | Revoke an open invite by prefix. Minting is deliberately NOT over MCP: the token is a machine-provisioning secret and a transcript is not a safe channel - mint in Settings -> Guests or `hp invite create` |
| `record_fact` | KB only | write | Write note/policy/decision to KB (auto-applied, no approval) |
| `propose_artifact` | triggers flow | write | Creates artifact with status: proposed; requires human approval |
| `approve_artifact` | n/a | n/a | **Not exposed over MCP.** Delisted from `list_tools()` and hard-refused in dispatch (#385) - approval must come from an operator via CLI or web UI |

`propose_artifact` initiates a change (requires write scope). Approval is deliberately NOT available over MCP: the MCP credential is a single shared token, so an LLM able to approve would both propose and approve its own mutations (#385). All other tools are read-only and require only read scope. Agents with read-only tokens cannot call write-scoped tools.

---

## Agent Hub

The agent hub reduces SSH dependency by ~90%. Instead of SSH-ing into every host, a lightweight agent daemon on each managed host maintains a persistent outbound TCP connection to the hub. Commands flow through this connection; SSH is only used as a fallback when no agent is connected.

### Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  HomePilot Server (Docker)                                      │
│                                                                 │
│  ┌────────────────┐    ┌──────────────┐    ┌──────────────────┐│
│  │  FastAPI /api/  │    │  AgentHub   │    │  AgentAdapter    ││
│  │  agents/*      ├───►│  Server      │◄───┤  (host_adapter)  ││
│  │  (REST API)    │    │  :8443      │    │                  ││
│  └────────────────┘    └──────┬───────┘    └──────────────────┘│
│                               │                                 │
│                        ┌──────┴───────┐                         │
│                        │  Registry    │                         │
│                        │  + AuditLog  │                         │
│                        │  + Tokens    │                         │
│                        └──────────────┘                         │
└──────────────────────────────┬──────────────────────────────────┘
                               │ TCP (outbound from agents)
                    ┌──────────┼──────────┐
                    ▼          ▼          ▼
              ┌─────────┐ ┌─────────┐ ┌─────────┐
              │hp-agent │ │hp-agent │ │hp-agent │
              │ hp-core │ │hp-monitor│ │  host3  │
              │(metrics)│ │(metrics)│ │(metrics)│
              └─────────┘ └─────────┘ └─────────┘
```

### Protocol

The agent hub uses **length-prefixed JSON** over TCP:

```
[4 bytes: big-endian length N][N bytes: UTF-8 JSON]
```

### Authentication

1. **Persistent token** (`HP_AGENT_AUTH_TOKEN`): Shared secret between hub and agent. Set once, works forever.
2. **Bootstrap token** (`hpbat_*`): One-time-use, 24h expiry. Generated via `hp agent bootstrap` or `POST /agents/bootstrap`. Consumed on first connection. Stored as hash in SQLite (migration 9).

### Enrolment without touching the host (ADR-004 S4)

`POST /agents/install` installs and enrols the agent inside a Proxmox guest over
qemu-guest-agent — the same channel provisioning uses to join a tailnet — and
tracks it as an artifactless `install_agent` task (migration 18).

- Preconditions are checked before any task exists, each with its own reason
  (`GET /agents/install/{host_id}` returns the same answer for the UI): hub up
  and advertising a dialable address, Proxmox configured, the host a QEMU guest
  with a known node+VMID, no agent already live on it, the guest **running**, and
  qemu-guest-agent **answering**.
- The bootstrap token and the hub's certificate pin are written to a tmpfs file
  the guest's shell sources and deletes; they never appear in an argv.
- The task succeeds only when that agent is **connected in the registry** — the
  installer's exit code is a step, not the outcome.
- `scripts/install-agent.sh` remains the path for everything else (bare metal,
  containers, privileged installs).

### Authorization

Agent API endpoints use scope-based access control:
- **read scope**: List agents, check connectivity
- **admin scope**: Hub auth token, bootstrap tokens, audit log, exec/read/write on agents, agent install

### Agent allowlist

Three tiers:

| Tier | Commands | Requirement |
|------|----------|-------------|
| Safe | `ls`, `cat`, `ps`, `hostname`, `uname`, `df`, `free`, `uptime`, `ip addr`, `ss`, `systemctl status`, `journalctl`, `dpkg -l` | Always allowed |
| Privileged | `docker pull/compose/run/stop/rm/restart`, `systemctl start/stop/restart/enable/disable/daemon-reload`, `mkdir`, `chmod`, `cp`, `mv`, `bash /opt/homepilot/*.sh` | Requires `HP_AGENT_PRIVILEGED=true` **and** a root systemd unit |
| Package management | `apt`/`apt-get install/update/upgrade` | Additionally requires `HP_AGENT_ALLOW_PACKAGE_INSTALL=true` (the unit must drop `ProtectSystem`) |

`sudo` is not allowlisted in any tier: a privileged agent is already root, and an
unprivileged one runs under `NoNewPrivileges=yes`, where sudo cannot escalate.

Blocked commands return `exit_code=-1` with stderr `"command blocked: ..."`.

### File access

**Read prefixes**: `/var/log/`, `/etc/`, `/opt/homepilot/`, `/proc/`, `/sys/`, `/tmp/homepilot/`, `/home/`, `/usr/local/bin/`

**Write prefixes** (`HP_AGENT_WRITE_PREFIXES`, privileged default): `/etc/homepilot/`, `/opt/homepilot/`, `/tmp/homepilot/`, `/etc/systemd/system/`, `/etc/docker/`, `/etc/nginx/`. An unprivileged install grants only HomePilot's own three directories.

Write attempts outside allowed prefixes return an error.

### Grant ↔ runtime coherence (#422)

The allowlist says what the agent may be asked to do; the systemd unit decides
what it can actually do. `scripts/install-agent.sh` derives `User=`,
`ProtectSystem=` and `ReadWritePaths=` from the same grant it writes into
`/etc/homepilot/agent.env`, so the two cannot drift, and the agent runs a startup
self-check that refuses privileged mode when it is not root or a configured write
prefix is not writable. `agent/go/unit_matrix_test.go` gates every entry of
`privilegedCommands` and every write prefix against the generated unit.

### Native metrics (ADR-004 S5)

Every agent sends a `metrics` frame over the hub connection every
`HP_AGENT_METRICS_INTERVAL` seconds (default 60). Nothing installs, imports or
configures; monitoring is part of the product.

| Metric | Description |
|--------|-------------|
| `cpu.count` | CPU core count |
| `disk.total_gb` / `disk.free_gb` | Root filesystem size and free space |
| `memory.total_gb` / `memory.free_gb` | Total and available memory |
| `load.1m` / `load.5m` / `load.15m` | Load averages |

**Delivery.** Samples are buffered on the agent while the hub is unreachable
(`HP_AGENT_METRICS_BUFFER`, default 1440 samples) and flushed on reconnect. A
batch leaves the buffer only when the hub has acked it, so a connection that
dies mid-write is re-sent instead of lost. Past the bound the OLDEST samples are
dropped first and each drop is logged with a running total.

**State.** The `metrics` frame is also the agent's only state channel: the hub
folds the freshest values into the agent record and persists `last_heartbeat`
with them. The former `report_state` action was removed — no agent ever sent it,
which is why the Agents view used to show an empty state and a frozen heartbeat.

**Storage.** One table, `metrics(hostname, metric, ts, value, agent_id)`,
`WITHOUT ROWID` with primary key `(hostname, metric, ts)` — the read pattern
("this host, this metric, this window") is a range scan over the key prefix, and
that key doubles as the dedupe identity for a re-sent batch. `idx_metrics_ts`
serves the retention pruner, which deletes raw samples older than
`HP_METRICS_RETENTION_DAYS` (default 7). There are deliberately no rollups.

**Alerting.** A rule is `(host filter, metric, comparison, threshold,
for_seconds)` and fires only when the condition held for that whole span, so a
single spike cannot page anyone. Firing and recovery both go out as
`alert_firing` / `alert_resolved` through the existing SSE + webhook event
machinery. See `/monitoring/*` in the README for the API.

### Audit logging

All agent operations (exec, read_file, write_file) are logged to an in-memory rotating deque (max 1000 entries) and Python logging. Queryable via `GET /agents/audit` (admin scope).

### Agent binary packaging

The `hp-agent` daemon is distributed as a **standalone binary**, built with PyInstaller. This eliminates the need for Python on managed hosts — deployment is just `scp` + `chmod +x`:

```bash
# Build the binary
cd agent/
pip install pyinstaller
pyinstaller --onefile --name hp-agent hp_agent/main.py
# Output: dist/hp-agent

# Deploy to managed host
scp dist/hp-agent target:/usr/local/bin/
ssh target 'chmod +x /usr/local/bin/hp-agent'
```

The binary bundles Python 3.10+ stdlib and all dependencies (asyncio, ssl, json, subprocess). No virtualenv or pip install needed on the target host. Systemd unit file (`hp-agent.service`) calls `/usr/local/bin/hp-agent` directly.

Source code also available for `pip install` into a venv during development: `pip install -e agent/`.

### AgentAdapter (the only host transport)

The `AgentAdapter` class is the sole implementation of the `HostAdapter` protocol - the SSH/jump-server transport was removed in #327. When the orchestrator needs to run a command on a host:

1. Check if an agent is connected for that hostname → route through hub
2. No agent → raise `AgentAdapterError`

The agent is a **drop-in** replacement — existing artifact executors work unchanged.

---

## Events, SSE, and Webhooks

Every artifact lifecycle transition (proposed, approved, applied, etc.) and drift detection emits an SSE event via the `EventBus`. Connected clients stream these at `GET /events`.

Registered webhooks receive the same events as outbound HTTP POST deliveries with HMAC-SHA256 signatures. The webhook system supports configurable per-endpoint event type filters, retry with exponential backoff, and delivery status tracking in the database.

See [`docs/EVENTS.md`](EVENTS.md) for the full event payload schema.

All state transitions also write an append-only `audit_log` table in SQLite (see §7 of the Artifact Spec).

---

## Data Layer

```
~/.hp/
├── homepilot.db          SQLite: hosts, services, artifacts index,
│                         KB entries (+ sqlite-vec for embeddings),
│                         audit log
├── artifacts/
│   └── YYYY-MM-DD-<slug>-<hash>.md   one file per artifact
│                         frontmatter (YAML) + body (playbook/spec)
└── vault/
    ├── master.protected  age-encrypted master identity
    └── secrets/          per-secret .age files
        ├── pve-token.age
        └── <service>.age
```

Vault encryption: `pyrage` (pure Python age implementation). No system `age` binary required. Master identity protected with AES-256-GCM derived from `HP_VAULT_PASSPHRASE`.

---

## GPU Allocation

GPUs are owned by the **LLM overlay** (`docker-compose.agent.yml`), opt-in via `--profile gpu`:

```
GPU 0  →  Qwen3-14B (llama.cpp, port 8080)          — LLM inference (overlay)
GPU 1  →  BGE-M3 embeddings (llama.cpp, port 8081)  — KB semantic search (overlay)
GPU 2  →  (reserved for future use)
```

The embedding service runs in the LLM overlay pinned to GPU 1, and the overlay points `HP_EMBEDDING_SERVICE_URL` at it. **By default neither URL is set, so KB search is keyword-only** — it works, and the startup self-check says so rather than letting it degrade quietly (ADR-004 S6). Point `HP_EMBEDDING_SERVICE_URL` (OpenAI-compatible) or `HP_EMBEDDING_FALLBACK_URL` (Ollama-compatible) at any embedding endpoint you already run to get vector search without a local GPU.
