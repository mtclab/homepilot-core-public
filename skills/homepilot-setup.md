---
name: homepilot-setup
description: Full HomePilot v2 infrastructure setup — Docker control plane, Proxmox integration, agent deployment, vault secrets. Trigger on HomePilot setup, install HomePilot, deploy HomePilot, bootstrap HomePilot, setup proxmox, agent hub, homepilot-v2, homepilot installation.
---

# HomePilot v2 Full Setup

One-shot install of the complete HomePilot stack: control plane + agent services + Proxmox + managed host agents.

## When to Use

- User says "setup HomePilot", "install HomePilot", "deploy HomePilot", "bootstrap"
- User mentions `homepilot-v2`, `homepilot-core-public`, infrastructure setup
- User wants the full stack: backend, n8n, searxng, radicale, Proxmox, agents

## Prerequisites

| Requirement | Minimum | Recommended |
|---|---|---|
| Docker Engine | 24.x | Latest |
| Docker Compose | v2 | v2 |
| RAM | 4GB | 8GB+ |
| Disk | 20GB | 50GB+ |
| OS | Linux amd64/arm64 | Ubuntu 22.04/24.04 |
| (Optional) Proxmox | PVE 8.x | 3-node cluster |

## Quick Start

> **IMPORTANT: Follow the user's guidance.** If the user already has a `.env` file, configs, or existing infrastructure, do not overwrite or modify them. Ask before touching any existing files.

### 1. Clone

```bash
git clone https://github.com/mtclab/homepilot-core-public.git
cd homepilot-core-public
```

> If the user already has the repo cloned or a custom `.env`, skip `cp .env.example .env` and use their existing configuration.

### 2. Configure

Check if `.env` already exists. If yes, review it with the user. If not:

```bash
cp .env.example .env
```

Edit `.env` — minimum viable (adjust per user's existing setup):

```env
HP_DAEMON_PORT=8000
HP_PROXMOX_HOST=pve.example.local
HP_PROXMOX_VERIFY_SSL=false
HP_IMAGE_TAG=2.3.0
```

> **Do not modify the user's existing `.env` without explicit permission.** If they have a ready-made one, ask what needs changing.

### 3. Start

```bash
docker compose up -d
```

### 4. Bootstrap secrets

```bash
docker compose exec backend hp init
```

Copies admin token from output.

### 5. Open UI

http://localhost:8000/ui → Settings → paste token → Save.

## Platform Architecture

```
┌─────────────────────────────────────────┐
│  HomePilot Control Plane (Docker)       │
│  ├─ backend (FastAPI + SvelteKit)      │
│  ├─ n8n (workflows)                    │
│  ├─ searxng (search)                   │
│  ├─ radicale (calendar)                │
│  ├─ whisper (STT)                      │
│  └─ piper (TTS)                        │
├─────────────────────────────────────────┤
│  Agent Hub (TCP 8443)                   │
│  ├─ hp-agent enroll → managed hosts    │
│  └─ TLS-encrypted JSON protocol          │
├─────────────────────────────────────────┤
│  Proxmox VE (via API)                   │
│  ├─ Read token: inventory + monitoring   │
│  └─ Write token: VM/LXC operations       │
└─────────────────────────────────────────┘
```

## Configuration Reference

### Core

| Variable | Default | What |
|---|---|---|
| `HP_IMAGE_TAG` | `2.3.0` | Backend image tag |
| `HP_DAEMON_PORT` | `8000` | Web UI + API port |
| `HP_DATA_DIR` | `/home/homepilot/.hp` | DB + vault + artifacts |
| `HP_LOG_LEVEL` | `info` | debug / info / warning / error |
| `HP_RATE_LIMIT` | `60` | Requests/min per IP |

### Vault (zero-secrets)

```bash
# Auto-generated on first boot:
HP_VAULT_PASSPHRASE=...

# Stored in vault by `hp init`:
#   secret-key      — JWT signing
#   admin-secret    — Admin auth
#   pve-token       — Proxmox read API
#   pve-write-token — Proxmox write API (optional)
#   webhook-secret  — Event verification
```

### Proxmox

```env
HP_PROXMOX_HOST=pve.example.local      # PVE hostname or IP
HP_PROXMOX_PORT=8006                   # API port
HP_PROXMOX_VERIFY_SSL=false            # true for valid certs
```

Store tokens in vault:
```bash
docker compose exec -it backend hp vault set pve-token
# {"token": "admin@pam!tokenid=uuid"}

docker compose exec -it backend hp vault set pve-write-token
# {"token": "admin@pam!tokenid=uuid"}  # optional
```

### Agent Hub

```env
HP_AGENT_HUB_ENABLED=true
HP_AGENT_HUB_PORT=8443
HP_AGENT_HUB_AUTH_TOKEN=$(openssl rand -hex 32)
```

### Agent Services (n8n, searxng, radicale)

```env
N8N_ENCRYPTION_KEY=$(openssl rand -hex 32)
SEARXNG_SECRET_KEY=$(openssl rand -hex 32)
RADICALE_PASSWORD=...
```

## Managed Host Deployment

### Install agent binary

```bash
curl -fsSL https://raw.githubusercontent.com/mtclab/homepilot-core-public/main/scripts/install-agent.sh | bash
```

### Enroll with hub

```bash
hp-agent enroll --hub https://homepilot-host:8443 --token <HUB_TOKEN>
hp-agent start
```

Or systemd:
```bash
hp-agent service install
sudo systemctl enable --now hp-agent
```

## Operations

### Health

```bash
curl http://localhost:8000/health
docker compose ps
docker compose logs -f backend
```

### Inventory refresh

```bash
docker compose exec backend hp inventory refresh
```

### Token management

```bash
# Create admin token
docker compose exec backend hp token create

# Create read-only
docker compose exec backend hp token create --scope read_only

# List tokens
docker compose exec backend hp token list

# Revoke
docker compose exec backend hp token revoke <prefix>
```

### Vault operations

```bash
# List secrets
docker compose exec backend hp vault list

# Get a secret
docker compose exec backend hp vault get pve-token

# Set a secret
docker compose exec -it backend hp vault set webhook-secret
```

### Upgrade

```bash
# Update image tag in .env: HP_IMAGE_TAG=2.3.1
docker compose pull backend
docker compose up -d backend
```

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| 401 all API | Missing token | `hp token create`, paste in UI |
| 403 write | Read-only scope | `hp token create --scope read,write` |
| Vault fail | Passphrase missing | Run `hp init` inside container |
| 0 inventory hosts | PVE not configured | Set `HP_PROXMOX_HOST` + store `pve-token` |
| Agent offline | Hub not enabled / token mismatch | Check `HP_AGENT_HUB_ENABLED=true` |
| n8n blank | Workflows not imported | UI: Workflows → Import from File |
| KB search weak | No embedding service | Start LLM overlay: `docker compose -f docker-compose.yml -f docker-compose.agent.yml --profile cpu up -d` |

## Directory Structure

```
homepilot-core-public/
├── docker-compose.yml          # Core + agent services
├── docker-compose.agent.yml     # LLM overlay (optional GPU/CPU)
├── .env                         # Config (no secrets)
├── data/
│   └── hp/                      # DB, vault, artifacts, git backup
├── agent/
│   ├── go/                      # hp-agent binary source (Go)
│   ├── n8n/workflows/           # 7 bundled workflows
│   ├── searxng/settings.yml    # Search engine config
│   └── radicale/config          # CalDAV config
└── scripts/
    ├── install-agent.sh         # One-line agent installer
    ├── setup-credentials.sh     # .env helper
    └── smoke-test.sh           # Health verification
```

## GitHub Releases

- Docker: `ghcr.io/mtclab/homepilot-core-public:2.3.0`
- Agent binary: `https://github.com/mtclab/homepilot-core-public/releases/download/v2.3.0/hp-agent-linux-amd64`
- Install script: `https://raw.githubusercontent.com/mtclab/homepilot-core-public/main/scripts/install-agent.sh`