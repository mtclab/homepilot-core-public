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
║  ┌───────────▼────────────┐  ┌────────────▼───────────────────────┐ ║
║  │  Core Services         │  │  Data Layer                        │ ║
║  │                        │  │                                    │ ║
║  │  ArtifactLifecycle     │  │  SQLite  — hosts, services,        │ ║
║  │  ArtifactExecutor      │  │    artifacts, KB entries,          │ ║
║  │  KBService             │  │    audit log, vector index         │ ║
║  │  InventoryService      │  │                                    │ ║
║  │  VaultManager          │  │  Artifact Store — markdown files   │ ║
║  │  EventBus / Webhooks   │  │    (one file per artifact)         │ ║
║  └───────────┬────────────┘  │    (one file per artifact)         │ ║
║              │               │                                    │ ║
║   ┌──────────┼───────────┐   │  Vault — age-encrypted secrets     │ ║
║   │          │           │   │    (Proxmox token, service creds)  │ ║
║   ▼          ▼           ▼   └────────────────────────────────────┘ ║
║  Proxmox   SSH Jump   Adopted                                        ║
║  REST API  Server     Services                                       ║
║  :8006     :50051    (Authentik,                                     ║
║           (TCP relay  Traefik, …)                                    ║
║            length-prefixed                                           ║
║            JSON protocol)                                            ║
║          ┌───▼───────────────────┐                                   ║
║          │  Guest VMs / LXC      │                                   ║
║          │  (read-only SSH exec  │                                   ║
║          │   + SFTP file reads)  │                                   ║
║          └───────────────────────┘                                   ║
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
              │    ansible-playbook    → playbook via SSH jump server        │
              │    proxmox-api-seq     → ordered Proxmox REST calls          │
              │    http-sequence       → ordered calls to adopted services   │
              │    shell-script        → exec on guest via SSH               │
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
| `refresh_inventory` | no | read | Re-pull inventory from Proxmox API + SSH guests |
| `get_environment_doc` | no | read | Fused doc for a target: inventory + KB + artifact history |
| `query_artifacts` | no | read | Find artifacts by status, kind, target, or date |
| `get_artifact_status` | no | read | Detailed status of one artifact: id, kind, status, intent, target, last_updated |
| `search_kb` | no | read | Vector + keyword search over KB notes/policies/decisions |
| `proxmox_api_read` | no | read | GET-only Proxmox REST call; allowlisted paths only |
| `http_call_read` | no | read | GET-only call to an adopted service; vault-resolved creds |
| `read_file_on_guest` | no | read | SFTP file read from guest via jump server |
| `exec_on_guest_readonly` | no | read | Whitelisted read-only SSH exec (cat, ls, ps, systemctl status…) |
| `check_artifact_drift` | no | read | Check whether an applied artifact has drifted from desired state |
| `record_fact` | KB only | write | Write note/policy/decision to KB (auto-applied, no approval) |
| `propose_artifact` | triggers flow | write | Creates artifact with status: proposed; requires human approval |
| `approve_artifact` | yes | write | Approve a proposed artifact; rate-limited 10/min per caller |

`propose_artifact` initiates a change (requires write scope). `approve_artifact` advances it (also write scope, rate-limited). All other tools are read-only and require only read scope. Agents with read-only tokens cannot call write-scoped tools.

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

GPUs are owned by **homepilot-agent**, not by homepilot-v2:

```
GPU 0  →  Qwen3-14B (llama.cpp, port 8080)          — LLM inference (agent)
GPU 1  →  BGE-M3 embeddings (llama.cpp, port 8081)  — KB semantic search (agent)
GPU 2  →  (reserved for future use)
```

The embedding service (`llm-embed`) runs in the homepilot-agent stack pinned to GPU 1 via `NVIDIA_VISIBLE_DEVICES=1`. HomePilot v2 calls the embedding endpoint over the network; it falls back to a remote/Ollama endpoint if the service is unavailable.
