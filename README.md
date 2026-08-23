# HomePilot v2

AI-first, artifact-backed, MCP-native homelab management platform.

Manage your homelab through structured artifacts — proposed by AI, reviewed by you, applied through the hp-agent, the Proxmox REST API, or HTTP. Every change is tracked and auditable.

## Quick Start (Docker)

```bash
git clone https://github.com/mtclab/homepilot-core-public.git
cd homepilot-v2
cp .env.example .env
docker compose up -d
```

Then open **http://localhost:8000/ui** (or `http://<host>:8000/ui` from another
machine on the same network) and **claim the instance**: the first-run screen
creates the admin credential and takes your Proxmox address and API token in the
same step. That is the whole install — no shell, no token to copy out of
container output, and nothing to edit in `.env`.

Claim it as soon as you start it: until it is claimed, anyone on the same
network can claim it.

<details>
<summary>Reaching it from outside its own network</summary>

An instance claimed from a public address (a port-forward, a reverse proxy on a
public host) refuses the codeless path and asks for a **claim code** instead.
It is generated on first boot and printed once in the backend log; read it again
at any time with:

```bash
docker compose exec backend hp claim-code
```

Behind a reverse proxy, set `HP_TRUSTED_PROXIES` to the proxy's address so
HomePilot judges the forwarded client rather than the proxy. A forwarding header
from an address that is not listed there is never trusted.
</details>

Everything else — the vault passphrase, the secret key, the database — generates
itself on first boot. `hp init` still exists for scripted installs; it is no
longer the way in.

## Pull from ghcr (no build required)

```bash
HP_IMAGE_TAG=3.0.0 docker compose pull
docker compose up -d
```

Available tags: `latest`, `3.0.0`, `3.0`, `3` — see [Releases](https://github.com/mtclab/homepilot-core-public/releases).

## Services

| Service | Default port | Description |
|---|---|---|
| backend | 8000 | FastAPI + web UI at `/ui` |
| agent hub | 8443 | TCP agent relay (outbound from managed hosts, length-prefixed JSON) |
| n8n | 5678 | Workflow automation (optional, for AI-driven orchestration) |
| searxng | 8080 | Meta-search engine (private, for agent web search) |
| radicale | 5232 | CalDAV/CardDAV server (calendar + contacts) |
| whisper | 8000 | Speech-to-text (CPU, for voice commands) |
| piper | 5000 | Text-to-speech (CPU, for voice replies) |

## Configuration

All settings via environment variables (prefix `HP_`) or `.env` file. See [`.env.example`](.env.example) for the full reference.

Key settings:

| Variable | Default | Description |
|---|---|---|
| `HP_SECRET_KEY` | — | **Required.** Random hex string |
| `HP_DATA_DIR` | `~/.hp` | Data directory (DB, vault, artifacts) |
| `HP_VAULT_PASSPHRASE_FILE` | — | Path to vault passphrase file (Docker secrets recommended) |
| `HP_PROXMOX_HOST` | — | Proxmox VE hostname |
| `HP_ARTIFACTS_REMOTE` | — | Git remote for artifact backup (e.g. `git@github.com:org/homepilot-artifacts.git`) |
| `HP_ARTIFACTS_SSH_KEY` | — | Absolute path to SSH private key for artifacts remote (inside container) |
| `HP_AGENT_HUB_ENABLED` | `true` | Enable agent hub TCP server |
| `HP_AGENT_HUB_HOST` | `0.0.0.0` | Hub bind address |
| `HP_AGENT_HUB_PORT` | `8443` | Hub listen port |
| `HP_AGENT_HUB_AUTH_TOKEN` | auto-generated | Shared secret for agent authentication (generated and persisted on first boot) |
| `HP_AGENT_HUB_TLS` | `true` | TLS on the hub. With no `HP_AGENT_HUB_TLS_CERT`/`_KEY`, a self-signed pair is generated into `<data_dir>/hub/` on first boot and reused |

## Artifact Git Backup

Every artifact write is committed to a local git repo under `HP_ARTIFACTS_DIR`. To push those commits to a remote (e.g. a private GitHub repo) after each write, set:

```env
HP_ARTIFACTS_REMOTE=git@github.com-artifacts:org/homepilot-artifacts.git
HP_ARTIFACTS_SSH_KEY=/home/homepilot/.hp/artifacts_deploy_key
```

Setup steps:
1. Create a private repo (e.g. `org/homepilot-artifacts`)
2. Generate a deploy key: `ssh-keygen -t ed25519 -f artifacts_deploy -N ""`
3. Add the **public** key to the repo with write access
4. Copy the **private** key into your data dir (mounted volume in Docker)
5. Add an SSH alias in `~/.ssh/config` if using a custom host alias:
   ```
   Host github.com-artifacts
     HostName github.com
     IdentityFile /path/to/artifacts_deploy
     IdentitiesOnly yes
   ```
6. Set `HP_ARTIFACTS_REMOTE` and `HP_ARTIFACTS_SSH_KEY` in `.env` and restart

Push failures are non-fatal — HomePilot continues normally if the remote is unreachable.

## Agent Hub

HomePilot manages remote hosts through a lightweight agent daemon. The SSH/jump-server transport was removed - host operations require a connected agent. The agent connects outbound to the hub — no inbound ports needed on managed hosts.

### How it works

0. **Nothing to configure.** The hub is on by default; its shared token and its
   TLS certificate generate themselves on first boot and persist under
   `HP_DATA_DIR` (ADR-004 S3). Explicit settings still override them.
1. **Hub server** runs inside the HomePilot Docker container (TCP port 8443)
2. **Agent daemon** (`hp-agent`) runs on each managed host, connects to hub via persistent TCP
3. **AgentAdapter** routes commands through the hub when an agent is connected
4. Commands are restricted by an allowlist (safe commands always, privileged commands require `HP_AGENT_PRIVILEGED=true` **and** a root systemd unit — see [Privileged installs](#privileged-installs-provisioning))
5. File reads/writes are restricted to allowed path prefixes

### Agent deployment

#### Zero-touch install from the UI (Proxmox guests)

A guest HomePilot already manages, that is **running** and answers on
**qemu-guest-agent**, is enrolled without touching it: open the host in
**Inventory** and press **Install agent**. HomePilot mints a single-use bootstrap
token, stages it (with the hub address and the certificate pin) into the guest's
tmpfs — never onto a command line — and runs the same
`scripts/install-agent.sh` the one-liner below runs. Progress is an
`install_agent` task, and it succeeds only once the agent has actually connected
to the hub; the installer's exit code alone is not treated as enrolment.

The host page states the reason when this path does not apply — the guest is
stopped, it has no qemu-guest-agent, it is not a Proxmox QEMU guest, or it
already has a live agent. Those hosts (and privileged installs, see below) use
the manual installer, which remains fully supported.

#### Manual install

The host agent is a single static Go binary (`hp-agent`) — no Python runtime on
the target. Use a release binary or build from source:

```bash
# Option 1: download the release binary (recommended)
curl -LO https://github.com/mtclab/homepilot-core-public/releases/latest/download/hp-agent-linux-amd64
chmod +x hp-agent-linux-amd64
sudo mv hp-agent-linux-amd64 /usr/local/bin/hp-agent

# Option 2: build from source (Go 1.23+)
cd agent/go && CGO_ENABLED=0 go build -o hp-agent .
scp hp-agent target:/usr/local/bin/

# Configure via environment variables
export HP_AGENT_HUB_HOST=homelab.local
export HP_AGENT_HUB_PORT=8443
export HP_AGENT_AUTH_TOKEN=<token-from-hp-agent-token>

# Or use a one-time bootstrap token (24h expiry, consumed on first connect)
export HP_AGENT_AUTH_TOKEN=hpbat_<bootstrap-token>

# The hub's certificate is self-signed, so tell the agent WHICH certificate is
# the hub's. The fingerprint comes from the UI / GET /agents/token.
export HP_AGENT_TLS=true
export HP_AGENT_TLS_PIN=sha256:<hub_cert_sha256>

hp-agent
```

`hp-agent --version` prints the binary's build stamp, and the agent reports it
on every register, so the **Agents** page and `hp agent list` show which binary
each host runs. A binary built by hand reports `dev`; release builds carry the
release tag. (Before #430 the release passed `-X main.version` against a symbol
that did not exist, so every released binary was silently unversioned.)

`scripts/install-agent.sh` is the supported installer: it writes
`/etc/homepilot/agent.env` **and** the matching systemd unit, so the two cannot
disagree. `agent/hp-agent.service` is a reference copy of the unprivileged unit.

### Privileged installs (provisioning)

Privileged mode is what enables the provisioning actions (`install_package`,
`manage_service`, `write_config`) and the privileged command allowlist. It is a
**root grant**, and it must be installed as one — a non-root unit with
`ProtectSystem=strict` cannot carry out a single privileged action (issue #422).

| Install | Runs as | `ProtectSystem` | Writable paths | Grants |
|---|---|---|---|---|
| default | `hp-agent` | `strict` | `/etc/homepilot`, `/opt/homepilot`, `/tmp/homepilot` | read-only allowlist |
| `--privileged` | `root` | `strict` | the write prefixes, exactly | systemctl/docker/file commands, `manage_service`, `write_config` |
| `--allow-package-install` | `root` | `no` | unrestricted | the above **plus** apt/`install_package` |

The **Agents** page renders the exact one-liner, already carrying `--tls` and
`--tls-pin`. The forms below omit them for brevity; a real install needs the pin
whenever the hub uses its generated certificate.

```bash
# unprivileged (default)
curl -fsSL .../install-agent.sh | bash -s -- --hub HOST:PORT --token TOKEN --tls --tls-pin sha256:FP

# privileged: root unit, writes confined to the granted prefixes
... | bash -s -- --hub HOST:PORT --token TOKEN --privileged

# privileged + package management (removes the filesystem write boundary)
... | bash -s -- --hub HOST:PORT --token TOKEN --allow-package-install

# narrow the grant to specific paths (repeatable; /etc/homepilot always included)
... --privileged --write-prefix /etc/nginx --write-prefix /opt/homepilot
```

The installer prints the exact paths and commands it is granting. Security
tradeoffs, in plain terms:

- **A privileged agent is root on that host.** The command allowlist is the
  containment boundary; `ReadWritePaths` confines only the agent's *own* writes,
  not what systemd, dockerd or a package's maintainer scripts do once the agent
  asks them to act.
- **Package management removes the write boundary.** dpkg unpacks into `/usr`,
  `/etc` and `/var`, so `ProtectSystem` has to be off for it. That is why it is a
  separate flag rather than part of `--privileged`.
- **`sudo` is not an allowlisted command anywhere.** A privileged agent is
  already root; an unprivileged one runs under `NoNewPrivileges=yes` where sudo
  could not escalate anyway.
- **The agent fails closed at startup.** If `HP_AGENT_PRIVILEGED` is set but the
  agent is not root, or any configured write prefix is not actually writable, it
  logs which path is wrong and exits non-zero instead of accepting work it
  cannot do.

**Upgrading an agent installed with the broken combination** (2.x installs made
with `--privileged`: `HP_AGENT_PRIVILEGED=true` in `agent.env` while the unit ran
as `User=hp-agent` with `ReadWritePaths=/etc/homepilot`): such an agent will now
**refuse to start** after a binary upgrade — that is intentional, it never worked.
Re-run the installer with the same flags to regenerate the unit:

```bash
curl -fsSL .../install-agent.sh | sudo bash -s -- --hub HOST:PORT --token TOKEN --privileged
```

The agent id and durable token in `/etc/homepilot` are preserved, so the host does
not re-enroll. To keep it unprivileged instead, drop `HP_AGENT_PRIVILEGED` from
`/etc/homepilot/agent.env` and restart.

### Native metrics

Every agent reports CPU count, memory, disk and load over the hub connection it
already holds — no monitoring server, no template import, nothing to configure.
Samples are stored for `HP_METRICS_RETENTION_DAYS` (default 7) and served back
under `/monitoring`; the UI draws them as sparklines on the Agents page.

While the hub is unreachable the agent buffers up to `HP_AGENT_METRICS_BUFFER`
samples (default 1440, roughly three hours at the default cadence) and flushes
them on reconnect, so a backend restart delays the series rather than holing it.
Past that bound the OLDEST samples are dropped first and every drop is logged
with a running total.

### Agent environment variables

| Variable | Default | Description |
|---|---|---|
| `HP_AGENT_HUB_HOST` | `localhost` | Hub server hostname |
| `HP_AGENT_HUB_PORT` | `8443` | Hub server port |
| `HP_AGENT_AUTH_TOKEN` | — | Persistent auth token or one-time bootstrap token |
| `HP_AGENT_PRIVILEGED` | `false` | Enable docker/systemctl/file-management commands + provisioning. Requires a root unit — the agent refuses to start otherwise |
| `HP_AGENT_ALLOW_PACKAGE_INSTALL` | `false` | Additionally enable apt/apt-get and `install_package` |
| `HP_AGENT_WRITE_PREFIXES` | see agent/README | Colon-separated write allowlist. Must match the unit's `ReadWritePaths` |
| `HP_AGENT_TLS` | `false` | Enable TLS for hub connection |
| `HP_AGENT_TLS_PIN` | — | sha256 of the hub certificate (`sha256:<hex>`). Required to verify a self-signed hub; a mismatching certificate is refused, and `HP_AGENT_TLS_INSECURE` cannot override it |
| `HP_AGENT_TLS_CA` | — | CA certificate path for TLS |
| `HP_AGENT_TLS_CERT` | — | Client certificate path for mTLS |
| `HP_AGENT_TLS_KEY` | — | Client key path for mTLS |
| `HP_AGENT_METRICS_ENABLED` | `true` | Report system metrics to the hub. Only an explicit false turns it off |
| `HP_AGENT_METRICS_INTERVAL` | `60` | Seconds between metric samples |
| `HP_AGENT_METRICS_BUFFER` | `1440` | Samples buffered while the hub is unreachable; past it the oldest are dropped (logged) |

### Agent API endpoints

All require API authentication. Endpoints marked **(admin)** require admin scope.

| Method | Path | Scope | Description |
|---|---|---|---|
| GET | `/api/agents/` | read | List connected agents |
| GET | `/api/agents/token` | admin | Get hub auth token for agent config |
| GET | `/api/agents/bootstrap` | admin | Generate one-time bootstrap token |
| GET | `/api/agents/audit` | admin | Query audit log (`agent_id`, `action`, `limit`) |
| GET | `/api/agents/hostname/{hostname}/connected` | read | Check if agent is connected |
| POST | `/api/agents/host/exec` | admin | Execute an allowlisted command on a host via its connected agent |
| POST | `/api/agents/host/read-file` | admin | Read file from host |
| POST | `/api/agents/host/write-file` | admin | Write file to host (allowed prefixes only) |
| POST | `/api/agents/{agent_id}/exec` | admin | Execute command on specific agent |
| POST | `/api/agents/{agent_id}/read-file` | admin | Read file from specific agent |
| POST | `/api/agents/{agent_id}/write-file` | admin | Write file to specific agent |
| POST | `/api/agents/{agent_id}/revoke` | admin | Revoke an agent's credential **and close its live channel** |
| DELETE | `/api/agents/{agent_id}` | admin | Forget a decommissioned agent: revoke its credential, then delete the row (409 while connected) |

### Metrics API endpoints

Mounted at `/monitoring`, not `/metrics`: the latter is the public Prometheus
exposition endpoint and these routes are authenticated.

| Method | Path | Scope | Description |
|---|---|---|---|
| GET | `/monitoring/hosts/{hostname}/series?metric=&hours=` | read | One series over a window, oldest point first (at most 2000 points; `truncated` says when older points were left out) |
| GET | `/monitoring/hosts/{hostname}/latest` | read | Newest value of every metric this host reports |
| GET | `/monitoring/rules` | read | List alert rules |
| POST | `/monitoring/rules` | admin | Create an alert rule (`metric`, `comparison`, `threshold`, `for_seconds`, `host_filter`) |
| PATCH | `/monitoring/rules/{rule_id}` | admin | Silence (`{"enabled": false}`) or re-enable a rule without losing it |
| DELETE | `/monitoring/rules/{rule_id}` | admin | Delete an alert rule and its state |
| GET | `/monitoring/alerts` | read | Alerts currently firing |

A rule fires only when its condition holds for the whole `for_seconds` span, so a
single spike never pages anyone, and it emits `alert_firing` / `alert_resolved`
through the existing SSE + webhook event machinery. Rules are managed from the
**Agents** page (Alert Rules), which is also where each host's sparklines and
recent-metrics panel live.

### Agent CLI commands

```bash
hp agent token          # Show hub auth token
hp agent bootstrap      # Generate one-time bootstrap token
hp agent list           # List connected agents
hp agent revoke <id>    # Revoke an agent's per-agent credential
hp agent remove <id>    # Forget a decommissioned agent (revoke, then delete)
```

## CLI

```bash
hp init                       # scripted setup: .env, database, vault secrets, API token
                              #   (the browser claim replaces this for a normal install)
hp claim-code                 # print the claim code of an unclaimed instance
                              #   (only needed when it is reached from outside its own network)
hp mcp-serve                  # start the MCP server (stdio or HTTP)
hp status                     # show daemon info, vault state, artifact counts
hp artifacts list             # list artifacts
hp artifacts show <id>        # show artifact detail
hp artifacts approve <id>     # approve a proposed artifact
hp artifacts reject <id>      # reject a proposed artifact
hp artifacts apply <id>       # apply an approved artifact (via the backend's executor)
hp artifacts edit <id>        # open artifact in editor
hp artifacts revoke <id>      # revoke an applied artifact
hp artifacts replay <id>      # re-apply an already-applied artifact
hp artifacts push             # push artifact git repo to remote
hp artifacts pull             # pull artifact git repo from remote
hp artifacts sync-status      # show git sync status of artifacts
hp inventory refresh          # sync inventory from Proxmox
hp inventory list             # list hosts and services
hp inventory show <host>      # show host detail
hp drift                      # detect drift across all applied artifacts
hp doc <topic>                # look up environment documentation
hp export                     # export DB + artifacts as tarball (NO secrets - cannot restore a host)
                              #   --include-secrets makes it restorable (holds the vault: treat as a credential)
hp import <file>              # restore from tarball (stop the backend first)
hp policy init                # global interactive onboarding to seed the policy KB
hp kb reindex                 # rebuild KB search index from artifacts
hp token create               # create an API token
hp token list                 # list API tokens (prefix, label, scope, last-used)
hp token revoke               # revoke an API token by its prefix
hp invite create              # mint a one-time, CN-bound self-service provisioning invite
hp invite list                # list invites (prefix, CN, caps, expiry, state - never tokens)
hp invite revoke <prefix>     # revoke an unredeemed invite by its prefix
hp vault set <name>           # store a JSON secret in the vault
hp vault get <name>           # retrieve a secret from the vault
hp vault list                 # list secret names
hp vault delete <name>        # delete a secret
hp webhook add                # register a webhook endpoint
hp webhook list               # list webhook configurations
hp webhook delete             # remove a webhook endpoint
hp webhook test               # send a test event to a webhook
hp agent token                # show hub auth token for agent config
hp agent bootstrap            # generate one-time bootstrap token
hp agent list                 # list connected agents
hp agent revoke               # revoke an agent's per-agent credential
hp agent remove <id>          # forget a decommissioned agent (revokes its credential, then deletes the row)
```

## MCP Server

**stdio** (local, Claude Code / opencode on the same machine):

```bash
hp mcp-serve
```

```json
{
  "mcpServers": {
    "homepilot": { "command": "hp", "args": ["mcp-serve"] }
  }
}
```

**HTTP** (remote server — recommended when AI client runs elsewhere, e.g. Kasm workspace):

```bash
HP_MCP_TOKEN=<secret> hp mcp-serve --transport http --host 0.0.0.0 --port 8000
```

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

## Vault

Secrets are encrypted with [age](https://age-encryption.org/) via the `pyrage` Python library — no system binary required. The master identity is protected with AES-256-GCM derived from your vault passphrase.

Use Docker secrets for the passphrase:

```env
HP_VAULT_PASSPHRASE_FILE=/run/secrets/hp_vault_passphrase
```

## Releases

Releases are cut by **manual workflow dispatch** on the public mirror — there is
no tag-push trigger, and the release pipeline runs **no test/lint/type gate**
(verification is the local `make gate`, run before promotion).

The chain: sync the reviewed change to the public mirror's `main`, then dispatch
`auto-tag.yml`. If `pyproject.toml`'s version has no matching tag, it creates and
pushes `v<version>` and dispatches `release.yml`, which:

1. Builds and pushes the Docker image (**`linux/amd64` only**) to `ghcr.io/mtclab/homepilot-core-public`
2. Cross-compiles the `hp-agent` Go binary for `linux/amd64` **and** `linux/arm64`
3. Publishes a GitHub Release with auto-generated notes and the agent binaries

Do not `git tag` by hand — `auto-tag.yml` owns tagging.

## Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# backend (uvicorn)
HP_SECRET_KEY=dev uvicorn homepilot.main:app --reload

# web UI (separate terminal)
cd web && npm install && npm run dev
```

Tests:

```bash
pytest tests/ --ignore=tests/test_e2e.py -v   # unit + integration (no server)
mypy src/
ruff check src/

# e2e against live instance
HP_TEST_TOKEN=hp_... pytest tests/test_e2e.py -v
```

See [`docs/testing.md`](docs/testing.md) for the full testing guide including manual smoke test checklist and dev server details.

## Day-2 operations

**Retention.** `audit_log`, `agent_audit`, finished `tasks` and
`webhook_deliveries` are pruned past `HP_RETENTION_DAYS` (default 90) by a
reconciler, and freed pages are returned to the filesystem afterwards - a
delete-only policy shrinks nothing an operator can see. Nothing was pruned at all
before: each of those gains a row per operation, and a year on a homelab VM is a
multi-GB SQLite file and a backup too big to move.

Not pruned, deliberately: **artifacts** (the record of intent), **hosts /
services / agents** (the estate, not its history), **drift_checks** (one upserted
row per artifact, already bounded), and **metrics** (its own pruner, its own
horizon - the right retention for a time series is not the right retention for an
audit log). A **stuck** task - pending or running past the horizon - is never
pruned either: deleting it would hide the problem.

**Logging.** `HP_LOG_LEVEL` is applied at startup. It was defined, documented and
read by nothing, so every `logger.debug` diagnostic was invisible in production
and could not be turned on.

**One backend per data directory.** The backend takes an advisory lock on
`<data_dir>/homepilot.lock` for its lifetime. A second backend used to run the
orphan sweep and mark the first one's pending/running tasks failed while they
carried on running. The kernel releases the lock if the process dies, so a crash
never leaves a stale one. CLI commands that migrate the schema refuse while that
lock is held, rather than migrating under a running server.

## Secrets in artifacts

`host-provision` config content and `shell-script` bodies resolve
`{{ vault.<name>.<field> }}` at execute time, in memory, immediately before the
value reaches the host. The stored body holds only the reference.

This matters because the artifact store is a **git repository designed to be
pushed**: before this, vault was wired into `http_sequence` and `proxmox_api`
only, so a password in a config file or a token in a script had to be committed,
and `git push` is a one-way door.

- The resolved value never reaches an execution log, a task result or a failure
  reason - all three are read back by an operator, and the execution log is
  persisted on purpose.
- A missing credential is a REFUSAL, not an empty substitution: a config written
  with an empty password is a working-looking file that fails at 3am.
- Propose refuses a body that appears to carry a literal credential, because that
  is the last moment before it is in history.

## Knowledge base

Semantic search works. It never had: sqlite-vec requires the `k` constraint on
the vec0 table's own query - a `LIMIT` on an outer join does not count - so every
search raised `A LIMIT or 'k = ?' constraint is required on vec0 knn queries`,
which the handler turned into a keyword fallback and a debug-level warning.

A doc with no embedding is never hidden. `vec_docs` is written by the kb-note
artifact executor, so ingested documentation and observed-state notes had none -
and the vector query joins that table, which meant those docs were returned ONLY
by the fallback that runs when the embedding service is DOWN. Vector hits keep
their order and their scores; keyword hits fill in behind them. Ingest embeds as
it writes, and `reindex` sweeps everything still missing an embedding rather than
re-walking artifact notes alone.

## Reviewing

The approval screen shows the plan (what changes on the host), the artifact body,
and the **policies** the operator recorded for that host - `kind: "policy"` KB
entries for the target - so approving is an informed decision rather than reading
YAML the AI wrote. A KB that is down never blocks the plan.

The queue supports **bulk approve/reject** with an inline two-step confirm, the
same pattern as every other destructive action here (Approve used to raise a
native `confirm()` in one place and nothing in another). One refusal does not
abandon the batch, and the toast names which artifacts did not go through.

The sidebar shows **"Live updates offline"** while the SSE stream is
disconnected. It reconnects silently with backoff, so the UI used to just stop
updating - and "nothing is happening" and "I am not being told what is happening"
are opposite conclusions on an ops console. The Tokens page shows each token's
expiry, which the API had always returned and nothing rendered.

## What the AI can see over MCP

The agent half of "AI-first" has to be able to close its loop:

- **`get_artifact`** returns the whole artifact - frontmatter AND body. The
  execution log is appended to the body on apply, so this is how an agent sees
  what happened when its own artifact ran, and how it uses a prior artifact as a
  pattern. `get_artifact_status` returns a status string and nothing else.
- **`get_task_result`** returns the outcome of an apply/replay/revoke by task id
  or by artifact id, with the execution log the runner kept.
- **`check_host_reachable`** answers, from the hub's live registry, whether a host
  has a connected agent - so an agent can find out BEFORE proposing work that an
  artifact targeting that host would fail at apply.
- **`propose_artifact`** names `host-provision` in its schema. It is the native
  provisioning kind with a real pre-apply plan and a captured rollback, and an
  agent reading the schema could not previously know it existed.
- **`get_environment_doc`** now calls the service implementation, so it includes
  the knowledge base its own description has always advertised.

## Operator intent is pinned

A `PATCH /inventory/{host_id}` is an operator deciding, so the fields it writes
are recorded in `hosts.pinned_fields` and every sync and enrich pass goes through
one `update_host_from_automation()` door that leaves them alone. Before this,
editing a description or a status in the UI lasted until the next reconciler
cycle, and `PATCH status` was simply a lie.

Automation never writes `role_source: "user"` or `ip_source: "user"`. A sync that
stamps an operator's provenance over its own guess defeats the guard AND hides
the UI badge that says a value was inferred - it is a lie about who decided.

## Drift is tri-state

A drift check answers `in_spec`, `drifted` or **`unknown`**, with a reason.
`drifted` used to be a boolean, so every unverifiable path - no spec, no host, no
executor, a timeout, a raised exception, a sequence whose every step was skipped
- returned `false`, and the UI painted it green. The dashboard's headline was
`(total - drifted) / total`, inflated by exactly the artifacts nobody had looked
at; it is now a percentage of what was actually CHECKED, with the unknown count
beside it.

`unknown` is the default, on purpose: a path that forgets to say reads as "not
established" rather than as a clean bill of health.

**Ansible drift checking is not implemented and now says so.** The verifier
called `executor.ssh.exec(...)` on an attribute that went with the jump server,
so it raised on every run into a handler that returned "no drift" - every applied
ansible artifact reported in-spec forever, having checked nothing. Reviving it
needs a real playbook transport (#388), not a rename.

## Rollback truth

`rollback` in an artifact's frontmatter is a FACT about the body, not a switch:
ARTIFACT_SPEC defines it as "true if a rollback section exists in the body", and
it is now derived at propose from the fenced rollback block the artifact's own
executor would run. A `## Rollback` heading with prose under it is not a rollback
- nothing can execute it.

A `rollback: true` that cannot be honoured is refused at propose, rather than
discovered on revoke: the operator has already decided to undo something by
then.

`host-provision` has no rollback section to write - its inverse comes from a
capture taken at apply time (prior package presence, prior service state, prior
file bytes and mode), stored per artifact. Revoking restores what it can and
NAMES what it cannot: the agent has no package-removal or file-deletion verb, so
a package that was absent stays installed and a config file that did not exist
stays on disk. Guessing at those - `apt-get remove`, `rm` - is how an undo takes
out a dependency or a file somebody else wrote.

Revoking returns whether the host was actually put back. **`revoked` describes
the ARTIFACT and says nothing about the machine** - a rollback that was skipped,
that no-op'd, or that failed used to be indistinguishable from one that worked.
The journal now records `outcome: reversed` or `outcome: relabelled` with the
reason, and the task result carries the same. A composite whose sub-artifacts
were only relabelled reports a failed rollback rather than a clean success.

## One apply engine

`apply`, `replay` and `revoke` all run through `ArtifactExecutor` in the backend
process - from the UI, from MCP, and from the CLI. There used to be a second,
weaker engine behind `hp artifacts apply|replay|revoke` with no pre-apply
snapshot, no approved-body tamper check, no task row, no host-provision support
and no rollback (on revoke it printed "Rollback spec exists in artifact body" and
executed nothing). `hp artifacts replay` therefore also bypassed the
`replay_safe: false` and replay-only guards the spec promises.

The CLI now calls `POST /artifacts/{id}/apply`, `POST /artifacts/{id}/replay` and
`DELETE /artifacts/{id}` with `sync=true` and reports the task's real outcome, so
it cannot return before the host has been touched. The executor, its agent
transport, its snapshots and its task rows live in the backend; a separate
process cannot honestly have any of them.

## The management UI

One token layer (`web/src/app.css`) decides colour, type and spacing; nothing in
the markup carries a raw palette value. HomePilot belongs to the estate's
civic/data house: a deep near-black field, ONE restrained accent (muted copper),
a serif reading register for text and sans for chrome, data and numbers. Red,
amber, green and teal mean status and are never decoration. Every text/background
pair is asserted against WCAG AA by `web/src/lib/tokens.test.ts`.

The shell is responsive: below `md` the sidebar collapses behind a nav bar (it is
a fixed 176px column otherwise, which on a phone ate the width the tables need),
and it closes on Escape, on navigation, and on picking a link. Table row density
lives in `.data-table` rather than on individual cells, so it is tunable in one
place. `make gate-web` runs `svelte-check --fail-on-warnings`, so an
unassociated label or a click handler on a non-interactive element fails the
build rather than accumulating as a note.

## Inventory paging

`GET /inventory` returns a real `COUNT(*)` as `total`, not the page size, and the
Inventory page has a pager. It used to return `len(hosts)`, which capped the UI at
100 rows with no way to reach page 2 and told the operator their estate was
smaller than it is. The filter clauses live in one builder shared by the list and
the count, so the two cannot disagree about which rows they describe.

The inventory reconciler pages through every host rather than reading the default
first 100: its absent/changed sets were computed from an arbitrary page on any
estate larger than that.

## Inventory lifecycle

Inventory is not only Proxmox guests:

- **Add a host by hand** (`POST /inventory`, "+ Add host" on the Inventory page)
  for the NAS, the router, the Pi - anything the hypervisor has never heard of.
  It is recorded as `source: "manual"` and adopted immediately, and a Proxmox
  sync will never mark it absent, because Proxmox never looked for it.
- **A guest that disappears from Proxmox is stamped `absent_since`** and shown as
  **gone**, instead of keeping its last-known status forever and looking exactly
  like a machine that is merely powered off. The stamp is set once (so "gone
  since Tuesday" survives) and cleared the moment the host is seen again. Only a
  FULL sync marks absence - a scoped, single-node sync never sees the other
  nodes' guests.
- **Forget a host** (`DELETE /inventory/{host_id}`) removes it with its services
  and its as-found observation note. Refused with 409 while the hypervisor still
  reports the host: the next sync would bring it straight back, and the operator
  would believe it was gone. Destroy the guest in Proxmox, or set its import
  state to `ignored`, instead.

## Search

`GET /artifacts`, `GET /inventory` and `GET /audit` all take a `q` free-text
parameter, and the Artifacts, Inventory and Journal pages each have a search box
that uses it. The query is evaluated on the SERVER on purpose: those lists are
paginated (and the artifact list is capped), so a filter applied in the browser
could only ever search the rows already fetched and would confidently report "no
match" for something on the next page.

| Endpoint | `q` matches |
|---|---|
| `/artifacts` | id, intent, kind, status, target, tags, produced_by |
| `/inventory` | hostname, fqdn, ip address, node, role, tags, description, owner, os |
| `/audit` | artifact id, target host/service, command, actor, action, details |

`q` composes with the existing filters (they narrow together), and on `/audit`
the reported `total` counts the search, so the pager never offers empty pages.

## Events & Webhooks

HomePilot emits SSE events for artifact lifecycle changes (proposed, approved, applied, etc.) and drift detection. Events stream to connected clients at `/events` and can trigger outbound webhooks. See [`docs/EVENTS.md`](docs/EVENTS.md) for the event schema and [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the webhook delivery system.

All state transitions write an append-only audit log stored in SQLite. Use `hp status` or the API to query it.

## Architecture

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — deployment topology, artifact lifecycle, MCP tools reference
- [`docs/ARTIFACT_SPEC.md`](docs/ARTIFACT_SPEC.md) — artifact file format and executor spec
- [`docs/EVENTS.md`](docs/EVENTS.md) — event and webhook payload schema
- [`docs/testing.md`](docs/testing.md) — unit, e2e, and manual smoke tests
- [`docs/deployment.md`](docs/deployment.md) — step-by-step Docker deployment
- [`docs/portal.md`](docs/portal.md) — the `/invite/*` self-service provisioning portal: nginx block, mint→redeem flow, trust model

> Historical planning and design docs are in [`docs/archive/`](docs/archive/) — do not use for reference.

## Screenshots

| Login | Artifacts | Inventory |
|------|-----------|-----------|
| ![Login](docs/images/hp-v2-login.png) | ![Artifacts](docs/images/hp-v2-artifacts.png) | ![Inventory](docs/images/hp-v2-inventory.png) |

| Drift Detection | Knowledge Base | Journal |
|----------------|----------------|---------|
| ![Drift](docs/images/hp-v2-drift.png) | ![KB](docs/images/hp-v2-kb.png) | ![Journal](docs/images/hp-v2-journal.png) |

| Settings | Tokens | Health | Review |
|----------|--------|--------|--------|
| ![Settings](docs/images/hp-v2-settings.png) | ![Tokens](docs/images/hp-v2-tokens.png) | ![Health](docs/images/hp-v2-health.png) | ![Review](docs/images/hp-v2-review.png) |

## License

AGPL-3.0
