# HomePilot — System Architecture

## Deployment Topology

```
╔══════════════════════════════════════════════════════════════════════╗
║  AI CLIENT  (Kasm workspace, laptop, any machine with Claude/opencode)║
║                                                                      ║
║   ┌─────────────┐   ┌─────────────┐                                  ║
║   │ Claude Code │   │  OpenCode   │                                  ║
║   └──────┬──────┘   └──────┬──────┘                                  ║
║          │  HTTP MCP  Authorization: Bearer <API token>              ║
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
║  │  MCP tools (73) - the READ surface is at parity with the     │   ║
║  │  management API's GET routes, gated by                       │   ║
║  │  tests/test_mcp_read_parity.py:                              │   ║
║  │   inventory   query_inventory  get_host  get_environment_doc │   ║
║  │               refresh_inventory                              │   ║
║  │   artifacts   query_artifacts  get_artifact  propose_artifact│   ║
║  │               get_artifact_status  check_artifact_drift      │   ║
║  │               get_fleet_drift  get_task_result  list_tasks   │   ║
║  │   agents      list_agents  get_agent  get_agent_audit        │   ║
║  │               get_enrolment_window  check_host_reachable     │   ║
║  │   monitoring  list_alert_rules  get_monitoring_alerts        │   ║
║  │               get_host_metrics  get_host_metrics_series      │   ║
║  │   kb          search_kb  list_kb  get_kb_doc  record_fact    │   ║
║  │               get_kb_embedding_status                        │   ║
║  │   ops         get_dashboard_summary  get_audit_log           │   ║
║  │               get_selfcheck  get_proxmox_settings            │   ║
║  │   guests      query_guests  set_guest_quota                  │   ║
║  │               delete_guest_quota  revoke_guest_invite        │   ║
║  │   settings    query_settings_overrides set_setting_override  │   ║
║  │               clear_setting_override probe_setting_override  │   ║
║  │   raw access  proxmox_api_read  http_call_read               │   ║
║  │               read_file_on_guest  exec_on_guest_readonly     │   ║
║  │  Never over MCP: the hub/enrolment token, the installer      │   ║
║  │  one-liner, agent binaries, and any secret setting (#553).   │   ║
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
hp mcp-serve --transport http --port 8000
```

The client authenticates with an API token from Settings -> Tokens; `HP_MCP_TOKEN=<secret>` still works as the legacy static fallback (see MCP authentication below).

Claude Code config:

```json
{
  "mcpServers": {
    "homepilot": {
      "url": "http://<homelab-ip>:8000/mcp",
      "headers": { "Authorization": "Bearer <api token>" }
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
| `delete_guest_quota` | quota table | write | Remove a guest's budget entirely, so invites alone gate their provisions (#607). NOT the same as setting every axis to null - that keeps a budget which is unlimited |
| `rejoin_tailnet` | one command inside an existing guest | admin | Retry the tailnet join on a guest that ALREADY EXISTS, with a FRESH auth key - nothing is re-provisioned (#628). Starts a `tailnet_join` task; `get_task_result` carries `tailnet` (joined / failed / unknown) and `tailnet_detail`, the reason in plain words. Mirrors `POST /guests/{vmid}/tailnet-join`. The key is used once and never stored, audited or logged; there is deliberately no CLI equivalent, because a `--auth-key` flag would put it in an argv and in shell history |
| `create_guest_template` | a VM + possibly a storage content type | admin | Build the cloud-init TEMPLATE `provision_guest` clones, over the Proxmox API alone (no node root). Stages a cloud image (`source_volid` or `download_url`), creates a VM, imports the disk, adds the cloud-init drive/serial console/guest agent, converts. Refuses a `template_vmid` already in use; destroys the half-made VM on any later failure (#594) |
| `revoke_guest_invite` | invite state | write | Revoke an open invite by prefix. Minting is deliberately NOT over MCP: the token is a machine-provisioning secret and a transcript is not a safe channel - mint in Settings -> Guests or `hp invite create` |
| `query_settings_overrides` | no | admin | Every operator setting with its value, source (env/db/default), hot-reloadability, probeability and env var. Only NON-SECRET settings exist in the registry, so no token, passphrase or signing secret can be listed (#553 C4) |
| `set_setting_override` | settings table | admin | Persist one operator setting through the same `checked_set` the admin route uses. Refused - storing nothing - for an unknown key (which is every secret), a key the environment already decides, a value of the wrong shape, or a value the live cluster refutes or cannot be asked about |
| `clear_setting_override` | settings table | admin | Drop a stored setting so the install falls back to the environment or the code default. Same refusals |
| `probe_setting_override` | no | admin | Ask the cluster about a candidate value WITHOUT saving it (is this node real, is this bridge on it, is this vmid a template). Admin-tier despite writing nothing: its route is |
| `record_fact` | KB only | write | Write note/policy/decision to KB (auto-applied, no approval) |
| `propose_artifact` | triggers flow | write | Creates artifact with status: proposed; requires human approval |
| `approve_artifact` | triggers flow | write | Exposed, but gated by a per-artifact approval code a human relays: the code is generated at propose time and returned by no MCP read, so the assistant cannot approve its own proposal (#385 follow-up) |

**Naming.** Every tool that addresses a machine by name takes `host` (#608). `hostname` is accepted as a deprecated alias so older callers keep working, and tools that answer with a dict say so in a `warning` field; new tools must use `host` alone. `tests/test_mcp_host_param.py` walks the registry and enforces it.

`propose_artifact` initiates a change (requires write scope). Approval used to be refused over MCP outright, because the MCP credential is a single shared token and an LLM able to approve would both propose and approve its own mutations (#385); the relayed approval code replaced the blanket ban with a gate the assistant cannot pass on its own.

### MCP authentication

An MCP client authenticates with an ordinary **API token** - the same credential the CLI, the console and any script use. Mint one in Settings -> Tokens (or `hp token create`), give it to the client, and revoke it there when the assistant is done: revocation is live, expiry is honoured, and `last_used_at` is stamped on every call, because the transport verifies the token through the same machinery the HTTP API does.

The token's API scope selects its tool tier through one shared map (`homepilot.auth.scopes.API_SCOPE_TO_MCP_TIER`, also read by the tier<->scope parity gate, so the two cannot drift):

| API scope | MCP tier |
| --- | --- |
| `read` | `read_only` |
| `write` | `full` |
| `admin` | `admin` |

> **Vocabulary (#579):** the superuser API scope (= `*`, everything - what
> `hp init` mints) is spelled **`all`**. It resolves to the `admin` tier and is
> deliberately NOT in this table. `full` is a legacy alias for `all`, accepted
> forever but advertised nowhere - so the only "full" an operator reads is the
> MCP write tier above, which corresponds to API scope `write`.

`HP_MCP_TOKEN` remains as a **legacy static fallback**: a value equal to it authenticates at `HP_MCP_TOKEN_SCOPE`. Precedence is exact - a token that verifies as an API token wins its own scope's tier; a token carrying the `hp_` API prefix that does NOT verify is refused outright and never falls through to the static compare (a revoked assistant token must not be able to resurrect itself); anything else is refused. Both transports follow this one rule: the HTTP `/mcp` mount, and the stdio server through the `HP_MCP_TOKEN` entry in its client's `env` block.

The `/mcp` mount is unconditional and always authenticated. A backend with no credential configured at all refuses every MCP request rather than serving an open control plane.

The tier ladder mirrors the API scope ladder read < write < admin:

- `read_only` — read tools only.
- `full` (default) — reads plus the standard mutators (`add_host`, `apply_artifact`, `revoke_artifact`, …), but NOT admin tools.
- `admin` — everything above plus the admin tools that mirror API `require_scope("admin")` routes (`open_enrolment_window`, `revoke_agent`, `forget_agent`, `migrate_agents_tls`, `exec_on_host`, `write_file_on_host`, `delete_kb_doc`, `create_alert_rule`, guest management incl. `provision_guest`, `rejoin_tailnet` and `create_guest_template`, `delete_auth_token`, the operator-settings tools, …).

Each tool that mirrors a management route sits at exactly that route's API scope; `tests/test_mcp_read_parity.py::TestMcpTierMatchesApiScope` enforces the equality, so a lesser MCP token can never do what the API reserves for a greater one.

Seven tools mirror no route and therefore have no scope to be compared against; their tiers are placed by hand. Each is declared, with its tier and its reason, in `ROUTELESS_TOOLS` in the same test file, and `TestEveryToolIsTierGoverned` fails on any tool that is neither mapped nor declared — so a new tool cannot slip past the tier gate by simply not appearing in a map. `create_guest_template` (admin, MCP-only) is the deliberate one; `get_artifact_status`, `check_artifact_drift`, `proxmox_api_read` and `http_call_read` are narrow reads with no management twin.

**The two that are not settled**: `read_file_on_guest` and `exec_on_guest_readonly` run at the `read_only` tier, while every API route that reads a file from — or executes on — a managed host (`POST /agents/host/read-file`, `POST /agents/host/exec`, `GET /agents/test/adapter`) is `require_scope("admin")`. These tools are guarded in their own way (a read prefix allowlist plus a secret denylist; an allowlist of read-only commands), but they are the only place in the product where an MCP tier is deliberately **weaker** than the API scope for the same capability. A `read`-scope token therefore reads `/etc`, `/home`, `/var/log` and `/opt` on any managed host as root — which is enough to lift whatever credentials that host keeps in a config file. Under review (#648, tranche 2); until it is decided, treat a `read`-scope token as trusted with every managed host's on-disk configuration.

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
| Safe | `ls`, `cat`, `ps`, `hostname`, `uname`, `df`, `free`, `uptime`, `ip addr`, `ss`, `systemctl status/is-active/is-enabled/daemon-reload`, `journalctl`, `dpkg -l`, `docker ps/images/inspect/logs/stats/version/info` | Always allowed |
| Privileged | `docker pull/compose/run/stop/rm/restart`, `systemctl start/stop/restart/enable/disable`, `mkdir`, `chmod`, `cp`, `mv`, `bash /opt/homepilot/*.sh` | Requires `HP_AGENT_PRIVILEGED=true` **and** a root systemd unit |
| Package management | `apt`/`apt-get install/update/upgrade` | Additionally requires `HP_AGENT_ALLOW_PACKAGE_INSTALL=true` (the unit must drop `ProtectSystem`) |

`sudo` is not allowlisted in any tier: a privileged agent is already root, and an
unprivileged one runs under `NoNewPrivileges=yes`, where sudo cannot escalate.

Blocked commands return `exit_code=-1` with stderr `"command blocked: ..."`. The
allowlist is enforced by the AGENT, on every exec, whichever surface asked — the
admin-tier `exec_on_host` skips the hub-side read-only filter, not the agent's
allowlist. There is no unrestricted host exec.

### Reply size

One protocol frame carries at most 1 MiB, so a reply has a payload budget
(`maxPayloadBytes`, 512 KiB; stdout and stderr get half each). The agent refuses
a file read above the budget on the `stat`, and truncates command output with a
notice that states how much there was. This is a hard requirement rather than a
nicety: the hub will not parse a frame it cannot MAC-verify, so on a
replay-protected connection an oversize reply CLOSES the connection — the agent
must never produce one.

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

All agent operations (exec, read_file, write_file), plus enrolment attempts,
rejections and revocations, are logged with the caller that asked for them. The
in-memory rotating deque (max 1000 entries) is the fast view; each entry is also
mirrored to the `agent_audit` table, so the trail survives a restart (#381).
Queryable via `GET /agents/audit` (admin scope).

### Agent binary packaging

`hp-agent` is a Go program built as a single **static** binary
(`CGO_ENABLED=0`, stdlib only, ~4.5 MB, amd64 + arm64). There is no Python on a
managed host and no PyInstaller step; the original `agent/hp_agent/` package was
removed.

```bash
cd agent/go
CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -ldflags="-s -w" -o hp-agent-linux-amd64 .
```

The image carries the built binaries and `install-agent.sh` under
`/app/agent-dist`, and the control plane serves them at
`GET /agents/dist/install-agent.sh` and `GET /agents/dist/hp-agent-linux-<arch>`
with an `x-hp-sha256` header the installer verifies (#464). A guest therefore
enrols without reaching GitHub, and the agent it installs matches the hub that
will manage it. See `agent/go/README.md` for the environment variables and the
privileged-unit rules.

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
~/.hp/                        (HP_DATA_DIR; /data in the compose deployment)
├── homepilot.db              SQLite: hosts, services, artifacts index,
│   ├── homepilot.db-wal      KB entries (+ sqlite-vec for embeddings),
│   └── homepilot.db-shm      audit log, tasks, metrics, settings
├── homepilot.lock            advisory lock — one backend per data dir
├── artifacts/                a git repository, history included
│   └── YYYY/MM/<id>.md       one file per artifact: YAML frontmatter + body
├── backups/
│   ├── pre-migration-v<N>.db taken before each schema migration
│   ├── pre-import-<ts>/      what `hp import` replaced
│   └── pre-restore-<ts>/     what `hp db restore` replaced
├── vault/
│   ├── identities/
│   │   └── master.protected  age master identity, AES-256-GCM wrapped
│   └── secrets/              per-secret .age files
│       ├── pve-token.age
│       └── <service>.age
├── .vault_passphrase         ONLY on the auto-generated shape (0600)
├── .agent_hub_token          fleet enrolment token, if auto-generated
├── api-token                 first-run claim credential
├── ssh/                      managed-host SSH keys
└── hub/                      agent-hub TLS material
```

**What has to be backed up, and what cannot be rebuilt.** `homepilot.db` and
`artifacts/` are the state; `vault/` is worthless without the passphrase, which
is only in this directory on the auto-generated shape (see
[`vault.md`](vault.md#durability-losing-the-passphrase-is-losing-the-secrets)).
`hp export --include-secrets` collects all of it, resolving the passphrase from
the environment when it is not in the data dir. KB embeddings are NOT exported —
`hp kb reindex` rebuilds them. `homepilot.db-wal`/`-shm` must never be copied
into a backup: SQLite replays a WAL onto whatever database file is beside it, so
a stale journal corrupts a restored database.

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
