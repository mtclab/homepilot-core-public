# HomePilot v2

AI-first, artifact-backed, MCP-native homelab management platform.

Manage your homelab through structured artifacts — proposed by AI, reviewed by you, applied via SSH or Ansible. Every change is tracked, reversible, and auditable.

## Quick Start (Docker)

```bash
git clone https://github.com/mtclab/homepilot-core-public.git
cd homepilot-v2
cp .env.example .env
```

Edit `.env` — at minimum set:

```env
HP_SECRET_KEY=        # required: python -c "import secrets; print(secrets.token_hex(32))"
JUMPSERVER_AUTH_TOKEN= # required: shared secret between backend and jumpserver relay
```

Then initialise the database and first API token:

```bash
pip install homepilot  # or: pip install -e .
hp init
```

`hp init` writes `~/.hp/.env`, creates the database, and prints your API token. Copy it — you'll need it for the web UI.

Start the stack:

```bash
docker compose up -d
```

Web UI: **http://localhost:8000/ui**

## Pull from ghcr (no build required)

```bash
HP_IMAGE_TAG=2.2.5 docker compose pull
docker compose up -d
```

Available tags: `latest`, `2.2.0`, `2.2`, `2` — see [Releases](https://github.com/mtclab/homepilot-core-public/releases).

## Services

| Service | Default port | Description |
|---|---|---|
| backend | 8000 | FastAPI + web UI at `/ui` |
| jumpserver | 50051 | TCP SSH relay for managed hosts (length-prefixed JSON protocol) |
| agent hub | 8443 | TCP agent relay (outbound from managed hosts, length-prefixed JSON) |

## Configuration

All settings via environment variables (prefix `HP_`) or `.env` file. See [`.env.example`](.env.example) for the full reference.

Key settings:

| Variable | Default | Description |
|---|---|---|
| `HP_SECRET_KEY` | — | **Required.** Random hex string |
| `HP_DATA_DIR` | `~/.hp` | Data directory (DB, vault, SSH keys) |
| `HP_VAULT_PASSPHRASE_FILE` | — | Path to vault passphrase file (Docker secrets recommended) |
| `HP_PROXMOX_HOST` | — | Proxmox VE hostname |
| `HP_JUMP_ENABLED` | `false` | Enable SSH via jumpserver relay |
| `HP_ARTIFACTS_REMOTE` | — | Git remote for artifact backup (e.g. `git@github.com:org/homepilot-artifacts.git`) |
| `HP_ARTIFACTS_SSH_KEY` | — | Absolute path to SSH private key for artifacts remote (inside container) |
| `HP_AGENT_HUB_ENABLED` | `false` | Enable agent hub TCP server |
| `HP_AGENT_HUB_HOST` | `0.0.0.0` | Hub bind address |
| `HP_AGENT_HUB_PORT` | `8443` | Hub listen port |
| `HP_AGENT_HUB_AUTH_TOKEN` | — | Shared secret for agent authentication |
| `HP_AGENT_HUB_TLS` | `false` | Enable TLS on hub (`HP_AGENT_HUB_CERT`/`HP_AGENT_HUB_KEY` required) |

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

HomePilot can manage remote hosts through a lightweight agent daemon instead of SSH, reducing SSH dependency by ~90%. The agent connects outbound to the hub — no inbound ports needed on managed hosts.

### How it works

1. **Hub server** runs inside the HomePilot Docker container (TCP port 8443)
2. **Agent daemon** (`agent-host`) runs on each managed host, connects to hub via persistent TCP
3. **AgentAdapter** routes commands through the hub when an agent is connected, falls back to SSH via jumpserver when no agent is available
4. Commands are restricted by an allowlist (safe commands always, privileged commands require `HP_AGENT_PRIVILEGED=true`)
5. File reads/writes are restricted to allowed path prefixes

### Agent deployment

```bash
# Option 1: Standalone binary (recommended, no Python needed on target)
cd agent/
pip install pyinstaller
pyinstaller agent-host.spec
scp dist/agent-host target:/usr/local/bin/
ssh target 'chmod +x /usr/local/bin/agent-host'

# Option 2: Pip install (for development)
pip install "git+https://github.com/mtclab/homepilot-core-public.git#subdirectory=agent"
# or, from a checkout: pip install ./agent

# Configure via environment variables
export HP_AGENT_HUB_HOST=homelab.local
export HP_AGENT_HUB_PORT=8443
export HP_AGENT_AUTH_TOKEN=<token-from-agent-host-token>

# Or use a one-time bootstrap token (24h expiry, consumed on first connect)
export HP_AGENT_AUTH_TOKEN=hpbat_<bootstrap-token>

agent-host
```

Systemd unit file is included at `agent/agent-host.service` (calls `/usr/local/bin/agent-host`).

### Zabbix trapper integration

When `HP_ZABBIX_ENABLED=true`, the agent pushes system metrics (CPU, memory, disk, load) directly to your Zabbix server via the sender protocol (TCP port 10051) — no zabbix-agent2 needed on managed hosts. Metrics are sent on a configurable interval and on-demand via the API.

### Agent environment variables

| Variable | Default | Description |
|---|---|---|
| `HP_AGENT_HUB_HOST` | `localhost` | Hub server hostname |
| `HP_AGENT_HUB_PORT` | `8443` | Hub server port |
| `HP_AGENT_AUTH_TOKEN` | — | Persistent auth token or one-time bootstrap token |
| `HP_AGENT_PRIVILEGED` | `false` | Enable docker/systemctl/apt commands |
| `HP_AGENT_TLS` | `false` | Enable TLS for hub connection |
| `HP_AGENT_TLS_CA` | — | CA certificate path for TLS |
| `HP_AGENT_TLS_CERT` | — | Client certificate path for mTLS |
| `HP_AGENT_TLS_KEY` | — | Client key path for mTLS |
| `HP_ZABBIX_ENABLED` | `false` | Enable Zabbix trapper push |
| `HP_ZABBIX_SERVER` | `localhost` | Zabbix server IP |
| `HP_ZABBIX_PORT` | `10051` | Zabbix trapper port |
| `HP_ZABBIX_HOSTNAME` | system hostname | Hostname as registered in Zabbix |
| `HP_ZABBIX_SEND_INTERVAL` | `60` | Push interval in seconds |

### Agent API endpoints

All require API authentication. Endpoints marked **(admin)** require admin scope.

| Method | Path | Scope | Description |
|---|---|---|---|
| GET | `/api/agents/` | read | List connected agents |
| GET | `/api/agents/token` | admin | Get hub auth token for agent config |
| GET | `/api/agents/bootstrap` | admin | Generate one-time bootstrap token |
| GET | `/api/agents/audit` | admin | Query audit log |
| GET | `/api/agents/hostname/{hostname}/connected` | read | Check if agent is connected |
| POST | `/api/agents/host/exec` | admin | Execute command on host (via agent or SSH fallback) |
| POST | `/api/agents/host/read-file` | admin | Read file from host |
| POST | `/api/agents/host/write-file` | admin | Write file to host (allowed prefixes only) |
| POST | `/api/agents/{agent_id}/zabbix-push` | admin | Trigger on-demand Zabbix metrics push |
| POST | `/api/agents/{agent_id}/exec` | admin | Execute command on specific agent |
| POST | `/api/agents/{agent_id}/read-file` | admin | Read file from specific agent |
| POST | `/api/agents/{agent_id}/write-file` | admin | Write file to specific agent |

### Agent CLI commands

```bash
hp agent token          # Show hub auth token
hp agent bootstrap      # Generate one-time bootstrap token
hp agent list           # List connected agents
```

## CLI

```bash
hp init                       # first-run setup: .env + database + API token
hp mcp-serve                  # start the MCP server (stdio or HTTP)
hp status                     # show daemon info, vault state, artifact counts
hp artifacts list             # list artifacts
hp artifacts show <id>        # show artifact detail
hp artifacts approve <id>    # approve a proposed artifact
hp artifacts reject <id>     # reject a proposed artifact
hp artifacts apply <id>      # apply an approved artifact
hp artifacts edit <id>       # open artifact in editor
hp artifacts revoke <id>     # revoke an applied artifact
hp artifacts drift <id>      # check drift for an artifact
hp artifacts push             # push artifact git repo to remote
hp artifacts pull             # pull artifact git repo from remote
hp artifacts sync-status      # show git sync status of artifacts
hp inventory refresh          # sync inventory from Proxmox
hp inventory list             # list hosts and services
hp inventory show <host>      # show host detail
hp drift                      # detect drift across all applied artifacts
hp doc <topic>                # look up environment documentation
hp export                     # export data directory as tarball
hp import <file>              # restore from tarball
hp policy init                # initialize policy document for a target
hp kb reindex                 # rebuild KB search index from artifacts
hp token create               # create an API token
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

Tags follow semver (`v2.0.0`). Pushing a tag triggers the release workflow:

1. Tests + lint + type check
2. Multi-arch Docker build (`linux/amd64`, `linux/arm64`)
3. Push to `ghcr.io/mtclab/homepilot-core-public`
4. GitHub Release with auto-generated notes

```bash
git tag v2.2.0 && git push origin v2.2.0
```

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

## Events & Webhooks

HomePilot emits SSE events for artifact lifecycle changes (proposed, approved, applied, etc.) and drift detection. Events stream to connected clients at `/events` and can trigger outbound webhooks. See [`docs/EVENTS.md`](docs/EVENTS.md) for the event schema and [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the webhook delivery system.

All state transitions write an append-only audit log stored in SQLite. Use `hp status` or the API to query it.

## Architecture

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — deployment topology, artifact lifecycle, MCP tools reference
- [`docs/ARTIFACT_SPEC.md`](docs/ARTIFACT_SPEC.md) — artifact file format and executor spec
- [`docs/EVENTS.md`](docs/EVENTS.md) — event and webhook payload schema
- [`docs/testing.md`](docs/testing.md) — unit, e2e, and manual smoke tests
- [`docs/deployment.md`](docs/deployment.md) — step-by-step Docker deployment

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
