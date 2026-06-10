# HomePilot v2.3 — Docker Deployment Guide

Step-by-step production deployment using Docker Compose.

---

## Prerequisites

- Docker Engine ≥ 24 and Docker Compose v2
- A Linux host (amd64 or arm64)
- (Optional) A Proxmox VE node reachable from the Docker host

---

## 1. Clone and Configure

```bash
git clone https://github.com/mtclab/homepilot-core-public.git
cd homepilot-core-public
cp .env.example .env
```

HomePilot uses a **zero-secrets `.env`** — no HomePilot secrets in the environment file. The vault passphrase is auto-generated on first start.

### Minimum `.env` (zero secrets)

```env
HP_DAEMON_PORT=8000
HP_PROXMOX_HOST=pve.example.local
HP_PROXMOX_VERIFY_SSL=false
```

All secrets (API signing key, admin secret, PVE token, etc.) are stored in the encrypted vault and resolved at runtime. See [Vault](#vault) for details.

The `.env` examples use `10.0.0.1` as placeholder IPs — replace with your actual host IPs.

### Bootstrap with `hp init` (recommended)

```bash
# 1. Start the stack — passphrase auto-generates, persisted to {data_dir}/.vault_passphrase
docker compose up -d

# 2. Initialize vault secrets and create admin token
docker compose exec backend hp init

# 3. Verify
docker compose exec backend hp vault list
```

`hp init` sets all 5 vault secrets and creates an admin API token in one step.

### Manual bootstrap (advanced)

```bash
# Set each secret individually
docker compose exec -it backend hp vault set secret-key
docker compose exec -it backend hp vault set admin-secret
docker compose exec -it backend hp vault set pve-token        # {"token": "admin@pam!tokenid=uuid"}
# (optional) docker compose exec -it backend hp vault set pve-write-token
docker compose exec -it backend hp vault set webhook-secret

# Create API token
docker compose exec backend hp token create
```

For production, put the vault passphrase in a file and use `HP_VAULT_PASSPHRASE_FILE` instead (see [Vault](#vault)).

---

## 2. Start the Stack

### Default (backend + agent services, no LLM)

```bash
docker compose up -d
```

The backend starts at **http://localhost:8000** (UI at `/ui`). This starts n8n, SearXNG, Radicale, Whisper, and Piper alongside the backend.

### With LLM (GPU)

```bash
docker compose -f docker-compose.yml -f docker-compose.agent.yml --profile gpu up -d
```

### With LLM (CPU only, slower)

```bash
docker compose -f docker-compose.yml -f docker-compose.agent.yml --profile cpu up -d
```

The LLM overlay adds llama.cpp (chat) and BGE-M3 embedding services. BGE-M3 is required for KB vector search; without it, KB search falls back to keyword-only mode.

> **Using a remote LLM?** Just leave the LLM overlay off and configure your preferred backend (Ollama, OpenAI, etc.) via environment variables — no local GPU needed.

---

## 3. Create the First API Token

`hp token create` works while the backend is running — it uses the HTTP API internally and falls back to direct DB access only when the backend is stopped.

```bash
docker compose exec backend hp token create
```

Output:

```
hp_abc123...
```

Copy the token — you'll enter it in the web UI login screen. On first open, the UI redirects to `/ui/settings`; paste the token there and click **Save** to start a session. You'll be returned to the page you originally requested.

To create a read-only token:

```bash
docker compose exec backend hp token create --scope read_only
```

To get JSON output (useful in scripts):

```bash
docker compose exec backend hp token create --output json
```

```json
{"token": "hp_abc123...", "scope": "read,write"}
```

---

## 4. Proxmox Integration

### Create a least-privilege API token

On the Proxmox node, run:

```bash
# Create a dedicated user
pveum user add hpadmin@pve --comment "HomePilot API user"

# Create an API token (--privsep 0 keeps it within user privs; 1 is safer but limits inherit)
pveum user token add hpadmin@pve api --privsep 0

# Grant read access to the cluster
pveum aclmod / --users hpadmin@pve --roles PVEAuditor
```

The token output looks like `hpadmin@pve!api=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`.

### Store in the vault (recommended)

```bash
docker compose exec -it backend hp vault set pve-token
```

When prompted, enter JSON:

```json
{"token": "admin@pam!tokenid=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"}
```

### Or set via environment variable

Add to `.env`:

```env
HP_PROXMOX_HOST=pve.example.local          # or IP address
PVE_API_TOKEN=admin@pam!tokenid=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
```

### Self-signed certificates

If your Proxmox node uses a self-signed TLS certificate (common in homelabs), add:

```env
HP_PROXMOX_VERIFY_SSL=false
```

This disables certificate verification for Proxmox API calls. For production, prefer adding the CA cert to the container's trust store instead.

### Dual PVE tokens (read + write)

HomePilot v2.3 supports **separate read and write Proxmox API tokens**. This lets you use a low-privilege read-only token for inventory and a higher-privilege write token for VM/container operations:

1. **Create a read-only token** (recommended: `PVEAuditor` role):

```bash
pveum user add hpadmin-ro@pve --comment "HomePilot read-only"
pveum user token add hpadmin-ro@pve api --privsep 0
pveum aclmod / --users hpadmin-ro@pve --roles PVEAuditor
```

2. **Create a write token** (recommended: `PVEVMAdmin` + `PVEPoolAdmin`):

```bash
pveum user add hpadmin-rw@pve --comment "HomePilot read-write"
pveum user token add hpadmin-rw@pve api --privsep 0
pveum aclmod / --users hpadmin-rw@pve --roles PVEVMAdmin,PVEPoolAdmin
```

3. **Store both tokens in the vault**:

```bash
docker compose exec -it backend hp vault set pve-token
# Enter: {"token": "hpadmin-ro@pve!api=uuid-read"}

docker compose exec -it backend hp vault set pve-write-token
# Enter: {"token": "hpadmin-rw@pve!api=uuid-write"}
```

4. **Or configure in the web UI**: Go to Settings → Proxmox. Enter the **Read API Token** and optionally the **Write API Token**. Click Save.

If no separate write token is configured, the read token is used for all operations (backward compatible).

### Token scope display

After logging into the web UI, the Settings page displays the **scope** of your current API token (e.g., `read,write` or `read_only`). If the token has insufficient scope for write operations, a warning is shown with a link to create a new token.

Then restart:

```bash
docker compose restart backend
```

Verify with:

```bash
docker compose exec backend hp inventory refresh
```

---

## 5. Agent Hub

HomePilot v2.3 uses the **Agent Hub** (TCP relay on port 8443) for managed host connectivity. The jump server has been replaced by the `hp-agent` binary, which connects directly to the hub.

### Enable the Agent Hub

In `.env`:

```env
HP_AGENT_HUB_ENABLED=true
HP_AGENT_HUB_AUTH_TOKEN=<shared-secret>
```

### Enroll a managed host

Install the `hp-agent` binary on each managed host, then:

```bash
hp-agent enroll --hub https://<homepilot-host>:8443 --token <shared-secret>
hp-agent start
```

See the Agent Hub documentation for details.

---

## 6. Artifact Git Backup

Every artifact write is committed to a local git repo inside the container. To push those commits to a private GitHub (or other Git host) repository:

### Create a deploy key

```bash
ssh-keygen -t ed25519 -f artifacts_deploy -N ""
```

- Add `artifacts_deploy.pub` to the target repository with **write** access (GitHub: Settings → Deploy keys → Add deploy key → Allow write).
- Copy `artifacts_deploy` (private key) into your data volume:

```bash
# Assuming hp-data volume mounts to /home/homepilot/.hp inside container
docker compose cp artifacts_deploy backend:/home/homepilot/.hp/artifacts_deploy
docker compose exec backend chmod 600 /home/homepilot/.hp/artifacts_deploy
```

### Configure `.env`

```env
HP_ARTIFACTS_REMOTE=git@github.com:org/homepilot-artifacts.git
HP_ARTIFACTS_SSH_KEY=/home/homepilot/.hp/artifacts_deploy
```

If you need a custom SSH host alias (e.g. to use a per-key `IdentityFile`), create `~/.ssh/config` inside the container and use a host alias in `HP_ARTIFACTS_REMOTE`:

```
Host github.com-artifacts
  HostName github.com
  IdentityFile /home/homepilot/.hp/artifacts_deploy
  IdentitiesOnly yes
```

```env
HP_ARTIFACTS_REMOTE=git@github.com-artifacts:org/homepilot-artifacts.git
```

Push failures are non-fatal — HomePilot continues if the remote is unreachable.

---

## 7. Vault

The vault encrypts secrets (PVE token, service credentials) with [age](https://age-encryption.org/) using a passphrase-derived key.

### Auto-generated passphrase (default)

If neither `HP_VAULT_PASSPHRASE` nor `HP_VAULT_PASSPHRASE_FILE` is set, the system auto-generates a 256-bit passphrase and persists it to `{data_dir}/.vault_passphrase` (mode `0o600`). This is the **recommended** approach for Docker deployments — no manual passphrase management required.

```
HP_VAULT_PASSPHRASE not set → auto-generate → persist to .vault_passphrase (0o600)
```

On subsequent starts, the persisted passphrase is loaded automatically. See [docs/vault.md](vault.md) for full architecture details.

### Use a passphrase file (production, Docker Swarm)

Write the passphrase to a file with restricted permissions:

```bash
echo "my-strong-passphrase" > /etc/homepilot/vault_pass
chmod 600 /etc/homepilot/vault_pass
```

Mount it into the container and set:

```env
HP_VAULT_PASSPHRASE_FILE=/run/secrets/hp_vault_passphrase
```

With Docker secrets (Swarm):

```yaml
secrets:
  hp_vault_passphrase:
    file: /etc/homepilot/vault_pass

services:
  backend:
    secrets:
      - hp_vault_passphrase
    environment:
      HP_VAULT_PASSPHRASE_FILE: /run/secrets/hp_vault_passphrase
```

### Zero-secrets `.env`

The `.env` file contains **no HomePilot secrets**. All 5 secrets (`secret-key`, `admin-secret`, `pve-token`, `pve-write-token`, `webhook-secret`) are stored in the encrypted vault and resolved at runtime via `_try_vault_secret`.

### Manage secrets

```bash
# Store a secret interactively
docker compose exec -it backend hp vault set <name>

# Retrieve (JSON output)
docker compose exec backend hp vault get <name>

# List all secrets
docker compose exec backend hp vault list
```

---

## 8. MCP Server

### stdio (local — Claude Code on the same machine)

Install HomePilot locally and point Claude Code at it:

```bash
pip install homepilot
```

`.claude/mcp_servers.json`:

```json
{
  "mcpServers": {
    "homepilot": {
      "command": "hp",
      "args": ["mcp-serve"]
    }
  }
}
```

### HTTP (remote — AI client runs on a different host)

Start an HTTP MCP endpoint alongside the backend:

```bash
HP_MCP_TOKEN=<secret> hp mcp-serve --transport http --host 0.0.0.0 --port 9000
```

Or add it as a service in `docker-compose.override.yml`:

```yaml
services:
  mcp:
    image: ghcr.io/mtclab/homepilot-core-public:${HP_IMAGE_TAG:-latest}
    command: ["hp", "mcp-serve", "--transport", "http", "--host", "0.0.0.0", "--port", "9000"]
    ports:
      - "9000:9000"
    volumes:
      - hp-data:/home/homepilot/.hp
    env_file:
      - .env
    environment:
      HP_MCP_TOKEN: ${HP_MCP_TOKEN}
```

Client config (e.g. in a Kasm workspace):

```json
{
  "mcpServers": {
    "homepilot": {
      "url": "http://<homelab-ip>:9000/mcp",
      "headers": { "Authorization": "Bearer <HP_MCP_TOKEN>" }
    }
  }
}
```

---

## 9. Deploy & Rollback

### Deploy

```bash
HP_IMAGE_TAG=2.3.4 docker compose up -d backend
```

Migrations run automatically on startup.

### Rollback

```bash
# Revert to previous known-good tag
HP_IMAGE_TAG=2.3.4 docker compose up -d

# Or if the new image never started healthy:
docker compose down && HP_IMAGE_TAG=2.3.4 docker compose up -d
```

### Verify

```bash
scripts/smoke-test.sh http://localhost:8000
```

Or manually:

```bash
curl http://localhost:8000/health
curl -H "Authorization: Bearer <token>" http://localhost:8000/inventory
docker compose exec backend hp inventory refresh
```

CI runs on push to `dev` and `main` branches, and on PRs targeting `dev`.

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `HP_IMAGE_TAG` | `2.3.4` | Docker image tag for the backend container |
| `HP_DAEMON_PORT` | `8000` | Host port mapped to the backend |
| `HP_SECRET_KEY` | — | Signs API tokens (required in production; auto-generated in dev if not set) |
| `HP_VAULT_PASSPHRASE` | — | Vault encryption passphrase (auto-generated if not set) |
| `HP_VAULT_PASSPHRASE_FILE` | — | Path to file containing vault passphrase (overrides `HP_VAULT_PASSPHRASE`) |
| `HP_PROXMOX_HOST` | — | Proxmox VE hostname or IP |
| `PVE_API_TOKEN` | — | Proxmox read API token (use vault `pve-token` instead) |
| `HP_PROXMOX_VERIFY_SSL` | `true` | Set `false` for self-signed Proxmox certs |
| `HP_AGENT_HUB_ENABLED` | `false` | Enable Agent Hub for managed host connectivity |
| `HP_AGENT_HUB_PORT` | `8443` | Agent Hub TCP port |
| `HP_AGENT_HUB_AUTH_TOKEN` | — | Shared secret for agent authentication |
| `HP_ARTIFACTS_REMOTE` | — | Git remote URL for artifact backup |
| `HP_ARTIFACTS_SSH_KEY` | — | Path to SSH deploy key for artifact push |
| `HP_MCP_TOKEN` | — | Auth token for MCP HTTP endpoint |
| `HP_RATE_LIMIT` | `60` | Max requests per IP per minute |
| `HP_CORS_ORIGINS` | `*` | Comma-separated allowed CORS origins |
| `HP_INVENTORY_INTERVAL_SECONDS` | `300` | Inventory reconciler interval |
| `HP_DRIFT_INTERVAL_SECONDS` | `1800` | Drift reconciler interval |
| `HP_AUTO_APPLY_ENABLED` | `false` | Enable auto-apply reconciler |
| `HP_AUTO_APPLY_INTERVAL_SECONDS` | `300` | Auto-apply reconciler interval |
| `HP_EMBEDDING_SERVICE_URL` | `http://llm-embed:8081/v1/embeddings` | Primary embedding endpoint (OpenAI-compatible) |
| `HP_EMBEDDING_MODEL` | `bge-m3` | Model name for primary embedding service |
| `HP_EMBEDDING_FALLBACK_URL` | `http://localhost:11434/api/embeddings` | Fallback embedding endpoint (Ollama-compatible) |
| `HP_EMBEDDING_FALLBACK_MODEL` | `nomic-embed-text` | Model name for fallback embedding service |

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| 401 on all API calls | Token not set or expired | Run `hp token create` again; use the new token |
| 403 on write operations | Token scope is `read_only` | Create a token with `--scope read,write` or `--scope '*'` |
| Inventory shows 0 hosts | Proxmox not configured or refresh not run | Set `HP_PROXMOX_HOST` + token; run `hp inventory refresh` |
| Vault errors on start | Passphrase not set | Auto-generated in v2.2+; for manual setup, set `HP_VAULT_PASSPHRASE` or `HP_VAULT_PASSPHRASE_FILE` |
| Agent not connecting | Hub not enabled or token mismatch | Set `HP_AGENT_HUB_ENABLED=true` and ensure token matches on both sides |
| Artifact push fails | Deploy key permissions or network | Check key has write access; push failures are non-fatal |
