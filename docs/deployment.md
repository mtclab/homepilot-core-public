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

`hp init` sets all 4 vault secrets and creates an admin API token in one step.

### Manual bootstrap (advanced)

```bash
# Set each secret individually
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

### Default (backend only)

```bash
docker compose up -d
```

The backend starts at **http://localhost:8000** (UI at `/ui`). Only the HomePilot
backend runs — the optional agent services (n8n, SearXNG, Radicale, Whisper,
Piper) are gated behind the `agents` profile so a stale extras image can never
block a core backend update.

### With agent services (n8n, SearXNG, Radicale, Whisper, Piper)

```bash
docker compose --profile agents up -d
```

### With LLM (GPU)

```bash
docker compose -f docker-compose.yml -f docker-compose.agent.yml --profile gpu up -d
```

### With LLM (CPU only, slower)

```bash
docker compose -f docker-compose.yml -f docker-compose.agent.yml --profile cpu up -d
```

The LLM overlay adds llama.cpp (chat) and BGE-M3 embedding services, and points
`HP_EMBEDDING_SERVICE_URL` at the embedding container for you. Without the
overlay that variable is empty and KB search runs keyword-only — which works, and
which the self-check below states rather than leaving you to discover it.

> **Using a remote LLM?** Just leave the LLM overlay off and configure your preferred backend (Ollama, OpenAI, etc.) via environment variables — no local GPU needed.

### What is on, what is off, what is broken

Every optional subsystem is off by default, and nothing is configured to point at
a host a stock install does not run (ADR-004 corollary 3). To see where an
instance actually stands, read the **startup self-check**:

```bash
docker compose logs backend | grep -A10 'Startup self-check'
```

```
Startup self-check - optional subsystems:
  proxmox: ok - The Proxmox API at pve.example.com answered; inventory and provisioning are available.
  agent_hub: ok - The agent hub is listening on 0.0.0.0:8443; managed hosts can connect.
  vault: ok - The vault is unlocked; secrets are stored encrypted in the data directory.
  embeddings: off - KB search is keyword-only because no embedding service is configured. ...
  events_webhook: off - Artifact and task events are not forwarded anywhere ...
```

The same report, computed fresh, is served at `GET /admin/selfcheck` (admin
scope) and rendered as **Optional subsystems** on the Settings page. It is a
sibling of `/health`, not part of it: `/health` is public and is the container's
liveness probe, while this report names the addresses the instance is wired to
and runs outbound probes.

The states mean different things and need opposite actions:

| State | Meaning | Action |
|---|---|---|
| `off` | Not configured. A choice, not a fault | None, unless you wanted the capability |
| `ok` | Configured and answering | None |
| `unreachable` | Configured but it did not answer — the capability is silently degraded | Fix the address, credentials or the service |
| `unknown` | The probe did not finish inside its 2s budget | Re-check; treat as unproven, not as working |

Probes are bounded at 2 seconds each and run concurrently, so the report never
delays startup (it is scheduled, never awaited) and never holds the endpoint
open. No token, passphrase or webhook path appears in it — addresses are reduced
to scheme, host and port.

### 2.1 Changing a subsystem's settings from the product

The non-secret settings behind those subsystems are editable on the Settings →
Subsystems tab, and persist in the instance's database. Precedence is binding:

**an explicitly set environment variable wins and records nothing; otherwise the
saved value; otherwise the code default.**

So a variable you set in `.env` or the compose file stays authoritative - the UI
renders that field read-only, naming the variable, and the API refuses the write
with `409` rather than saving a value it would never read. Unset the variable
and restart to manage the setting from the product instead.

Editable today: `HP_ARTIFACTS_REMOTE`, `HP_ARTIFACTS_PUSH_INTERVAL_SECONDS`,
`HP_EMBEDDING_SERVICE_URL`, `HP_EMBEDDING_MODEL`, `HP_RETENTION_DAYS`,
`HP_METRICS_RETENTION_DAYS`, `HP_EVENTS_WEBHOOK_URL` and the provisioning
defaults of 2.2 below. Each is re-read at use time - the next push, the next
prune, the next event, the next provision - so a change takes effect
on the next cycle without a restart. **Secrets are not on this surface at all**:
the webhook signing secret, tokens and passphrases stay in the environment or
the vault, and cannot be read or written through it.

| Endpoint | Scope | What it does |
|---|---|---|
| `GET /admin/settings/overrides` | admin | every setting with its value, its source (`env`/`db`/`default`) and whether it hot-reloads |
| `PUT /admin/settings/overrides/{key}` | admin | saves a value; `409` when the environment decides that key, `400` when the type rejects it |
| `DELETE /admin/settings/overrides/{key}` | admin | drops the saved value, back to the code default |
| `POST /admin/settings/overrides/{key}/probe` | admin | asks the cluster about a value WITHOUT saving it - the "Test" button |

### 2.2 Provisioning defaults, checked against the cluster

`HP_PROVISION_DEFAULT_NODE`, `HP_PROVISION_DEFAULT_TEMPLATE_VMID`,
`HP_PROVISION_DEFAULT_POOL`, `HP_PROVISION_DEFAULT_BRIDGE`,
`HP_PROVISION_DEFAULT_VLAN_TAG` and `HP_PROVISION_DEFAULT_IPCONFIG` are the
same kind of setting, on their own **Provisioning defaults** card, and they are
what lets an invite stop carrying raw infra details: mint an invite without a
node or a template and it takes both from here, frozen into the invite at mint
time. `POST /guests/provision` and the `provision_guest` MCP tool fill the same
gaps. Name neither a value nor a default and the call is refused, saying which
setting would have filled it.

### 2.3 The guest network

The `HP_GUEST_NETWORK_*` settings describe the subnet a friend's machine lives
on: an SDN zone, a vnet, a subnet with a gateway and DHCP, and - the point of
the whole thing - the list of networks a guest must never reach. They have their
own **Guest network** card on Settings -> Subsystems, with local shape probes
(a gateway outside its subnet is refused before it is saved).

Describing the network does NOT build it. `GET /admin/guest-network` (and the
`query_guest_network` MCP tool) reports the survey, the desired state and the
plan between them; the change itself ships as a **`guest-network` artifact** -
propose, approve with the relayed code, apply - so the record of who decided to
rebuild the guest subnet lives in the artifact store. See
[docs/guest-portal.md](guest-portal.md) for the design and for why the per-VM
firewall rules are the fence that actually holds on the legacy firewall stack.

Each one is **checked against the live cluster before it is stored**. A value
the cluster refutes comes back `422` with the cluster's own answer - *"no bridge
vmbr7 on node pve1; node has: vmbr0, vmbr1"* - and nothing is saved. A cluster
that cannot be reached comes back `502`: nothing is saved then either, because
an unchecked provisioning default is exactly what the check exists to refuse.
A bridge is per-node, so setting one before the node is refused with *"set the
node first"*; a VLAN tag needs a VLAN-aware bridge, and where the node does not
report VLAN-awareness at all the value is saved with the uncertainty stated
rather than guessed either way.

Setting a bridge also **turns on a capability**: from then on provisioning
writes `net0` (`virtio,bridge=<bridge>[,tag=<vlan>]`) on the fresh clone, which
is what makes a guest VLAN enforceable. With no bridge configured, `net0` is
never touched and the template's own NIC is cloned exactly as before.

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

### The hub configures itself (ADR-004 S3)

**Nothing has to be set for host management to work.** On a default install the
hub is enabled, and on first boot it generates what it needs:

| What | Where it goes | Reused on restart |
| --- | --- | --- |
| Shared hub token | vault secret `agent-hub-token` if the vault is unlocked, else `<data_dir>/.agent_hub_token` (0600) | yes |
| Self-signed server certificate | `<data_dir>/hub/hub-cert.pem` | yes |
| Its private key | `<data_dir>/hub/hub-key.pem` (0600) | yes |

The certificate is regenerated **only** when it is absent or expired, so the
fingerprint an agent pinned at enrolment keeps working across restarts.

Every setting below is still an override, and an override always wins:

```env
HP_AGENT_HUB_ENABLED=false            # turn host management off entirely
HP_AGENT_HUB_AUTH_TOKEN=<shared-secret>
HP_AGENT_HUB_TLS_CERT=/path/server.crt   # supplied cert+key replace the generated pair
HP_AGENT_HUB_TLS_KEY=/path/server.key
```

**Upgrade path for existing installs.** This changes defaults, not
configuration. Nothing under an existing deployment is rewritten:

- `HP_AGENT_HUB_ENABLED=false` in an existing `.env` keeps the hub off.
- An existing `HP_AGENT_HUB_AUTH_TOKEN` is used as-is; no token file is written.
- Existing `HP_AGENT_HUB_TLS_CERT`/`_KEY` are used as-is; nothing is generated.
- An install that set `HP_HUB_ALLOW_INSECURE=1` and never set `HP_AGENT_HUB_TLS`
  **keeps running plaintext**, so its agents (which dial without TLS) are not
  broken by the new TLS default. Set `HP_AGENT_HUB_TLS=true` explicitly when you
  are ready to move that fleet onto TLS.
- `HP_AGENT_HUB_TLS=false` set explicitly still means no TLS - and therefore
  still hits the fail-closed error below on a routable bind.

### Enrolment window — who may join the fleet

The shared hub token authenticates *enrolment*, and it never expires. Until the
window existed, a copy of it (a stale `.env`, a shell history, a screenshot of
the token panel) could add machines to the fleet indefinitely, and a fleet member
gets fleet-root exec and file access on the hosts it claims to be.

The rule the hub enforces at authentication time:

> A shared-token register for a hostname this install has **never seen** is
> accepted only when the install has **no agents at all** (a first rollout, which
> must still need zero operator input) **or** an operator has an enrolment window
> **open**.

Unaffected: per-agent reconnects (they authenticate before this check), one-time
bootstrap tokens (`POST /agents/bootstrap` — the sanctioned way to add a host
later), and re-enrolment of a hostname the fleet already contains, which remains
the recovery path for a host whose credential was revoked or lost.

| Method | Path | Scope | Description |
|---|---|---|---|
| GET | `/agents/enrolment-window` | admin | `open`, `expires_at`, `seconds_remaining`, `fleet_empty` |
| POST | `/agents/enrolment-window` | admin | Open or extend, `{"minutes": 15}` (1–1440, default 15) |
| DELETE | `/agents/enrolment-window` | admin | Close it now |

```bash
hp agent enrolment-window status
hp agent enrolment-window open --minutes 30
hp agent enrolment-window close
```

The state is one row in the `settings` table (`agent_enrolment_window_expires_at`)
compared at authentication time — a restart neither reopens nor extends it, and
an unreadable value reads as **closed**. Opening and closing are audited with the
operator who did it (`enrolment_window_opened` / `enrolment_window_closed`), and
a refusal is audited as `register_rejected` naming the hostname that was claimed.
The refused agent is told *why* and logs it: `enrolment refused: this hub is not
accepting new hosts right now…`. No agent row is created for a refused stranger.

### Transport security (TLS) — fail closed

The Agent Hub carries authentication tokens and privileged command/file traffic.
As of the #362 hardening it **fails closed** on an exposed plaintext transport:

- **Loopback bind** (`127.0.0.1`, `localhost`, `::1`) with no TLS is allowed for
  local development.
- **Any routable bind** (e.g. `0.0.0.0`, a LAN IP) with **no TLS** is a **hard
  startup error** — the backend refuses to start.

A default install satisfies this check **by itself**: TLS is on and the hub
generates its own certificate. The check is not special-cased, weakened, or
bypassed - it passes because there really is a TLS context. You only need the
settings below to depart from the default:

```env
HP_AGENT_HUB_TLS=true
HP_AGENT_HUB_TLS_CERT=/path/server.crt
HP_AGENT_HUB_TLS_KEY=/path/server.key
# Optional: enable mutual TLS by verifying agent client certs against a CA
HP_AGENT_HUB_TLS_CA=/path/ca.crt
```

…or, **only on a trusted, isolated network**, explicitly opt into plaintext:

```env
HP_HUB_ALLOW_INSECURE=1   # logs a loud warning on every start
```

### What the agent verifies

On the agent (`hp-agent`) side, TLS **verifies the hub by default**. There is no
silent `CERT_NONE`/`InsecureSkipVerify` downgrade anywhere in the shipped path.
Which check applies depends on how the hub's certificate was obtained:

| Agent configuration | What is verified |
| --- | --- |
| `HP_AGENT_TLS_PIN=sha256:<hex>` (what the UI one-liner sets) | The hub's certificate must be **byte-for-byte** the pinned one. Chain and hostname checks are replaced by this exact-identity check, which is strictly stronger for a known certificate. A different certificate - including another well-formed self-signed one - is refused. |
| `HP_AGENT_TLS_CA=/path/ca.crt` | Standard chain + hostname verification against that CA. |
| Neither | Standard verification against the system trust store. A self-signed hub certificate **cannot** satisfy this, which is why the pin exists. |

The pin comes from the hub itself: `GET /agents/token` and `POST /agents/bootstrap`
return `hub_cert_sha256`, and the UI renders it into the install one-liner as
`--tls-pin`. Because that value is fetched over the authenticated admin API, the
trust decision is anchored in the operator's session rather than in
trust-on-first-use.

**Not verified in this slice:** the agent does not present a client certificate
unless `HP_AGENT_HUB_TLS_CA` + agent cert/key are configured by hand, so agent
identity to the hub still rests on the per-agent token, not on mTLS (#362).
Certificate rotation is also manual: replacing the hub certificate invalidates
every pinned agent, which must be re-enrolled with the new fingerprint.

`HP_AGENT_TLS_INSECURE=1` remains **off** by default and exists for testing only
- it exposes the connection to man-in-the-middle interception, logs a loud
warning, and **cannot** override a pin (a pinned agent still refuses a
mismatched certificate).

> **Migration note (#362).** Deployments that ran the hub on plain TCP over a
> routable interface fail to start. Since ADR-004 S3 an install that configures
> nothing gets TLS with a generated certificate instead, so this only affects an
> install that explicitly set `HP_AGENT_HUB_TLS=false`; the remaining fix is to
> drop that setting, or set `HP_HUB_ALLOW_INSECURE=1` as a temporary,
> trusted-network-only measure.
> Per the P0 finding, also plan to rotate the shared `HP_AGENT_HUB_AUTH_TOKEN`;
> per-agent credentials, replay resistance, and protocol negotiation are tracked
> as follow-ups to #362.

### Enroll a managed host

Open **Agents** in the UI and copy the generated one-liner - it already carries
the hub address, the token, and the certificate pin. Run it as root on the host:

```bash
curl -fsSL https://github.com/mtclab/homepilot-core-public/releases/latest/download/install-agent.sh \
  | bash -s -- --hub <homepilot-host>:8443 --token <token> --tls --tls-pin sha256:<fingerprint>
```

`--tls-pin` is what lets the agent verify a self-signed hub; without it the
agent falls back to the system trust store and the connection fails. The
installer refuses anything that is not a sha256 fingerprint. See the Agent Hub
documentation for the grant flags (`--privileged`, `--write-prefix`, …).

The installed binary is always verified before it is put in place. Fetched from
the control plane (`--hp-api`), the digest travels in the `x-hp-sha256` header
served with the bytes; fetched from GitHub, it is checked against the release's
`SHA256SUMS` asset. If no digest can be obtained the install **refuses** and
names the manifest it looked for - `--allow-unverified` is the only override, and
it means exactly what it says: a binary that will run as root, unchecked.

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

The `.env` file contains **no HomePilot secrets**. All 4 secrets (`admin-secret`, `pve-token`, `pve-write-token`, `webhook-secret`) are stored in the encrypted vault and resolved at runtime via `_try_vault_secret`.

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
hp mcp-serve --transport http --host 0.0.0.0 --port 9000
```

The client authenticates with an API token minted in Settings -> Tokens; the
token's scope picks its tool tier. `HP_MCP_TOKEN=<secret>` still works as the
legacy static credential.

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
HP_IMAGE_TAG=2.8.0 docker compose up -d backend
```

Migrations run automatically on startup. Before applying any pending version the backend writes a
pre-migration snapshot to `$HP_DATA_DIR/backups/pre-migration-v<current_version>.db` (sqlite backup
API, so it is a consistent copy even with WAL active). If that backup cannot be written, the backend
refuses to migrate and starts nothing. Each version is applied in its own transaction and bumps
`schema_version` inside it, so a failed migration rolls back whole and leaves the database at the
last version that fully applied.

### Rollback

**Rolling back to an older image requires restoring the matching DB backup.** There are no
down-migrations: a newer image migrates the schema forward, and an older image refuses to start
against a schema newer than it supports (`RuntimeError: Database schema version N is newer than this
build supports`). Downgrading the image alone leaves the backend permanently down.

```bash
docker compose down

# Restore the snapshot taken before the schema version this image expects.
# Backups live in $HP_DATA_DIR/backups/ (default ~/.hp/backups/ on the host volume).
cp ~/.hp/backups/pre-migration-v<version>.db ~/.hp/homepilot.db
rm -f ~/.hp/homepilot.db-wal ~/.hp/homepilot.db-shm

HP_IMAGE_TAG=2.3.4 docker compose up -d
```

Data written after the backup was taken is lost by the restore - export anything needed first.
If the new image never started healthy (migrations aborted before any change), the database is
still at the old version and the image tag can be reverted on its own.

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

There is no push-triggered CI (this is a private repo — Actions minutes are billed). Verification runs locally via `make gate` before any push; see [`docs/testing.md`](testing.md).

---

## 10. Backup & Restore

`hp export` writes a tarball; `hp import` restores one. Read this section before
you rely on either — the default tarball deliberately holds no secrets, and a
tarball without secrets cannot bring a host back on its own.

### What is in a tarball

| Path in the tarball | Default | With `--include-secrets` |
|---|---|---|
| `manifest.json` (archive schema, hp version, DB schema version, `includes_secrets`) | yes | yes |
| `README.md` | yes | yes |
| `homepilot.db` (a `VACUUM INTO` snapshot — consistent, compacted, no `-wal`/`-shm`) | yes | yes |
| `artifacts/` (the artifact Git repo, history included) | yes | yes |
| `secrets/vault/identities/master.protected` (the vault identity) | **no** | yes |
| `secrets/vault/secrets/*.age` (pve-token, admin-secret, webhook secrets, …) | **no** | yes |
| `secrets/.env`, `secrets/.vault_passphrase` (the passphrase that unwraps the identity) | **no** | yes |
| `secrets/api-token` | **no** | yes |
| `secrets/ssh/` (managed-host SSH keys) | **no** | yes |

Never in a tarball: `homepilot.db-wal` / `homepilot.db-shm` (a stale journal
replayed onto a restored database corrupts it), and KB embeddings — run
`hp kb reindex` after a restore to rebuild them.

### Default export — data only, NOT a host backup

```bash
docker compose exec backend hp export -o /backups
```

This prints a loud warning, and it means it: the tarball **cannot restore a
working host**. The vault identity and the passphrase that unwraps it stay
behind, so on a rebuilt host every vault secret — `pve-token`, `admin-secret`,
webhook secrets — is permanently undecryptable. Use the default
form only when you already hold the vault material somewhere else (a secrets
manager, Docker secrets, a passphrase file you back up separately).

### Full export — restorable, and a credential in its own right

```bash
docker compose exec backend hp export --include-secrets -o /backups
```

The resulting file is written `0600` and is worth exactly as much as the host:
anyone who reads it can decrypt everything HomePilot holds. Encrypt it at rest,
keep it out of Git and off shared storage, and delete the working copy once the
restore is done.

### Restore recipe

1. **Stop the backend.** `hp import` refuses to run while any process holds the
   database open (it takes an exclusive SQLite lock to check, so a pidfile-free
   stale container is detected too):

   ```bash
   docker compose stop backend
   ```

2. **Restore.** Add `--restore-secrets` to put the vault back; without it the
   vault on the target host is left untouched even when the tarball has one.

   ```bash
   docker compose run --rm --entrypoint hp backend \
     import /backups/homepilot-export-20260819-101500.tar.gz --restore-secrets
   ```

   What import does, in order: validate every archive path (no absolute or `..`
   members), read and version-check `manifest.json`, verify no one holds the DB,
   back the current DB + artifacts tree up to
   `<data_dir>/backups/pre-import-<timestamp>/` (fail closed — nothing is
   overwritten if the backup fails), extract, delete any stale
   `homepilot.db-wal` / `-shm`, restore secrets if asked (stashing the ones it
   replaces into the same backup dir), and run migrations.

3. **Start and verify.**

   ```bash
   docker compose start backend
   curl http://localhost:8000/health
   docker compose exec backend hp status
   docker compose exec backend hp kb reindex   # embeddings are not exported
   ```

If the restore was wrong, everything it replaced is under
`<data_dir>/backups/pre-import-<timestamp>/`.

### Version rules

- **Restoring an older DB requires no special step** — `hp import` runs
  migrations after extraction and leaves the schema at the version this image
  expects. That upgrade is one-way.
- **Restoring a NEWER DB is refused.** No down-migrations exist, so an archive
  whose `db_schema_version` (or `manifest_schema_version`) is ahead of the
  running build is rejected outright. Run the image that produced the archive —
  the tarball's `manifest.json` records `homepilot_version` for exactly this.
- **Tarballs without a `manifest.json` are refused.** Archives produced before
  the backup fix contain a raw copy of a live WAL database and can be torn;
  re-export from the source host instead.

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `HP_IMAGE_TAG` | `3.6.1` | Docker image tag for the backend container |
| `HP_ENV` | — | Set to `production` to refuse an auto-generated vault passphrase (the vault stays disabled unless one is supplied) |
| `HP_DATA_DIR` | `~/.hp` | Data directory (DB, vault, artifacts) inside the container |
| `HP_DAEMON_PORT` | `8000` | Docker host port mapped to the container's fixed `:8000` |
| `HP_ADMIN_TOKEN` | — | An admin-scope API token for the CLI on the box (`hp token create`, `hp token list/revoke`, `hp agent …`). Falls back to `<data dir>/api-token`, which `hp init` and the browser claim write automatically |
| `HP_ADMIN_SECRET` | — | Admin API auth secret (vault `admin-secret` preferred). One of the two ways to authorise minting; an admin-scope API token is the other, and a claim-installed instance has only the token |
| `HP_VAULT_PASSPHRASE` | — | Vault encryption passphrase (auto-generated if not set) |
| `HP_VAULT_PASSPHRASE_FILE` | — | Path to file containing vault passphrase (overrides `HP_VAULT_PASSPHRASE`) |
| `HP_VAULT_AUTO_INIT` | `1` | A vault passphrase is generated and persisted when none is set. Set `0` to refuse, leaving the vault disabled. Never generated when `HP_ENV=production` |
| `HP_PROXMOX_HOST` | — | Proxmox VE hostname or IP |
| `PVE_API_TOKEN` | — | Proxmox read API token (use vault `pve-token` instead) |
| `HP_PROXMOX_VERIFY_SSL` | `true` | Set `false` for self-signed Proxmox certs |
| `HP_PROVISION_DEFAULT_NODE` | — | Node guests are cloned on when the request does not name one |
| `HP_PROVISION_DEFAULT_TEMPLATE_VMID` | `0` | Template cloned when the request does not name one; `0` means no default |
| `HP_PROVISION_DEFAULT_POOL` | — | PVE resource pool provisioned guests join |
| `HP_PROVISION_DEFAULT_BRIDGE` | — | Bridge the guest NIC is put on. Setting it is what makes provisioning write `net0` at all; empty leaves the template's NIC untouched |
| `HP_PROVISION_DEFAULT_VLAN_TAG` | `0` | VLAN tag for the guest NIC, applied only together with the bridge above. `0` is untagged |
| `HP_PROVISION_DEFAULT_IPCONFIG` | `ip=dhcp` | cloud-init `ipconfig0` used when the request does not give one |
| `HP_GUEST_NETWORK_ZONE` | `guest` | SDN zone the guest network lives in (1-8 characters, PVE's own limit) |
| `HP_GUEST_NETWORK_VNET` | `innkeep` | Vnet guests attach to. Point `HP_PROVISION_DEFAULT_BRIDGE` at this name to put provisioned guests on it |
| `HP_GUEST_NETWORK_SUBNET` | — | The guest subnet in CIDR form, e.g. `198.51.100.0/24`. Empty means this instance describes no guest network |
| `HP_GUEST_NETWORK_GATEWAY` | — | The guest subnet's gateway; must be inside the subnet |
| `HP_GUEST_NETWORK_SNAT` | `1` | `1` source-NATs guest traffic out of the node |
| `HP_GUEST_NETWORK_DHCP` | `1` | `1` runs dnsmasq on the zone; needs the `dnsmasq` package on the node |
| `HP_GUEST_NETWORK_DHCP_RANGE` | — | Addresses DHCP may hand out, as `<start>-<end>` |
| `HP_GUEST_NETWORK_DHCP_DNS_SERVER` | — | Resolver handed to guests; empty means the gateway resolves for them |
| `HP_GUEST_NETWORK_ISOLATE_CIDRS` | `10.0.0.1/24` | The networks a guest must never reach. This is the fence; empty means no fence and no per-VM firewall rules |
| `HP_AGENT_HUB_ENABLED` | `true` | Enable Agent Hub for managed host connectivity |
| `HP_AGENT_HUB_PORT` | `8443` | Agent Hub TCP port |
| `HP_AGENT_HUB_ADVERTISE_HOST` | — | Host (optionally `host:port`) agents dial to reach the hub behind a proxy |
| `HP_AGENT_DIST_DIR` | `/app/agent-dist` | Where the agent payload HomePilot serves to guests lives (installer + per-arch binaries, baked into the image). Override only when running from a source checkout, which has none until they are built |
| `HP_AGENT_HUB_AUTH_TOKEN` | auto-generated | Shared secret for agent authentication (vault `agent-hub-token`, else `<data_dir>/.agent_hub_token`) |
| `HP_AGENT_HUB_TLS` | `true` | Enable TLS on the hub (required for non-loopback binds) |
| `HP_AGENT_HUB_TLS_CERT` | auto-generated | Hub server certificate; a self-signed pair is written to `<data_dir>/hub/` when unset |
| `HP_AGENT_HUB_TLS_KEY` | auto-generated | Hub server private key; a self-signed pair is written to `<data_dir>/hub/` when unset |
| `HP_AGENT_HUB_TLS_CA` | — | CA to verify agent client certs (enables mutual TLS) |
| `HP_HUB_ALLOW_INSECURE` | `false` | Override fail-closed check: allow plaintext on a non-loopback bind (trusted networks only; logs a loud warning) |
| `HP_AGENT_TLS_INSECURE` | `false` | **Agent side, test-only:** skip hub cert/hostname verification (`CERT_NONE`); exposes MITM. Never in production |
| `HP_ARTIFACTS_REMOTE` | — | Git remote URL for artifact backup |
| `HP_ARTIFACTS_SSH_KEY` | — | Path to SSH deploy key for artifact push |
| `HP_MCP_TOKEN` | — | Legacy static credential for the MCP transports. An MCP client normally authenticates with an API token from Settings -> Tokens; this value still works, at `HP_MCP_TOKEN_SCOPE` |
| `HP_RATE_LIMIT` | `60` | Max requests per IP per minute |
| `HP_CORS_ORIGINS` | `http://localhost:5173,http://localhost:4173,http://127.0.0.1:5173,http://127.0.0.1:4173` | Comma-separated allowed CORS origins (the four localhost dev origins by default) |
| `HP_COOKIE_SECURE` | `true` | Set `false` only for plain-HTTP local dev |
| `HP_TRUSTED_PROXIES` | — | Comma-separated proxy IPs trusted for `X-Forwarded-For` |
| `HP_METRICS_RETENTION_DAYS` | `7` | How long raw metric samples are kept before the pruner deletes them |
| `HP_RETENTION_DAYS` | `90` | How long operational history is kept: audit log, agent audit, finished tasks, webhook deliveries. Artifacts are never pruned |
| `HP_RETENTION_INTERVAL_SECONDS` | `21600` | How often the retention sweep runs |
| `HP_ARTIFACTS_PUSH_INTERVAL_SECONDS` | `3600` | How often the artifact store is pushed to `HP_ARTIFACTS_REMOTE` (only when a remote is set) |
| `HP_METRICS_PRUNE_INTERVAL_SECONDS` | `3600` | How often the retention pruner runs |
| `HP_METRICS_ALERT_INTERVAL_SECONDS` | `60` | How often alert rules are evaluated against the stored window |
| `HP_PORTAL_CN_HEADER` | `ssl-client-subject-dn` | Header the mTLS proxy sets with the client-certificate subject DN (see [portal.md](portal.md)) |
| `HP_PORTAL_VERIFY_HEADER` | `ssl-client-verify` | Header that must equal `SUCCESS` for the certificate to be trusted |
| `HP_PORTAL_TRUSTED_PROXY` | — | Address/CIDR(s) `/invite/*` requests must arrive from; unset = portal 503s |
| `HP_PORTAL_PROXY_SECRET` | — | Shared secret the proxy sets in `X-Hp-Portal-Secret`; unset = portal 503s |
| `HP_PORTAL_BASE_URL` | — | Public origin of the mTLS vhost, used only to print complete invite URLs |
| `HP_INVENTORY_INTERVAL_SECONDS` | `300` | Inventory reconciler interval |
| `HP_DRIFT_INTERVAL_SECONDS` | `1800` | Drift reconciler interval |
| `HP_AUTO_APPLY_ENABLED` | `false` | Enable auto-apply reconciler |
| `HP_AUTO_APPLY_INTERVAL_SECONDS` | `300` | Auto-apply reconciler interval |
| `HP_EMBEDDING_SERVICE_URL` | — (off) | Primary embedding endpoint (OpenAI-compatible). Empty = KB search is keyword-only. The LLM overlay sets it to `http://llm-embed:8081/v1/embeddings` |
| `HP_EMBEDDING_MODEL` | `bge-m3` | Model name for primary embedding service |
| `HP_EMBEDDING_FALLBACK_URL` | — (off) | Fallback embedding endpoint (Ollama-compatible). `localhost` here is the backend container; reach a host Ollama as `http://host.docker.internal:11434/api/embeddings` |
| `HP_EMBEDDING_FALLBACK_MODEL` | `nomic-embed-text` | Model name for fallback embedding service |
| `HP_PORT` | `8000` | Port the CLI dials the local daemon on (`Settings.daemon_port`; distinct from `HP_DAEMON_PORT`, which is the Docker host mapping) |
| `HP_LOG_LEVEL` | `info` | Log verbosity: `debug` \| `info` \| `warning` \| `error` |
| `HP_VAULT_DIR` | `<HP_DATA_DIR>/vault` | Vault directory |
| `HP_ARTIFACTS_DIR` | `<HP_DATA_DIR>/artifacts` | Artifact working directory |
| `HP_ALLOWED_HTTP_DOMAINS` | — | Comma-separated allowlist for `http_call_read` (SSRF guard) |
| `HP_AUTH_RATE_LIMIT` | `120` | Max authentication requests per IP per minute |
| `HP_RATE_LIMIT_BACKEND` | `memory` | Rate-limit store. Only `memory` is implemented; other values log a warning and fall back |
| `HP_BUSY_TIMEOUT` | `10000` | SQLite busy timeout in milliseconds |
| `HP_DB_CONNECT_RETRIES` | `3` | Database connection attempts on startup |
| `HP_DB_CONNECT_RETRY_DELAY` | `1.0` | Seconds between database connection retries |
| `HP_GIT_TIMEOUT` | `30` | Timeout in seconds for git operations during artifact backup |
| `HP_AUTO_APPLY_MUTATING` | — | Allow the auto-apply reconciler to run mutating actions |
| `HP_PROXMOX_PORT` | `8006` | Proxmox API port |
| `HP_AGENT_HUB_HOST` | `0.0.0.0` | Agent Hub bind address |
| `HP_AGENT_HUB_ALLOW_INSECURE` | `false` | Accepted alias of `HP_HUB_ALLOW_INSECURE` (same setting) |
| `HP_MCP_TOKEN_SCOPE` | `full` | Tool tier granted to the LEGACY `HP_MCP_TOKEN` value only - an API token brings its own tier, mapped from its scope (read -> read_only, write -> full, admin -> admin). Mirrors the API scope ladder read < write < admin. `read_only` = read tools only; `full` = reads + mutators (the default), but NOT admin tools; `admin` = every tool except the permanently-forbidden approval one |
| `HP_EVENTS_WEBHOOK_URL` | — (off) | Webhook posted when an artifact is proposed. Empty = events are recorded and shown in the UI but forwarded nowhere. n8n is behind the `agents` profile and mints the path per workflow, so there is no default that works |
| `HP_EVENTS_WEBHOOK_SECRET` | — | Webhook signing secret (vault `webhook-secret` preferred) |
| `HP_N8N_API_KEY` | — | Optional API key sent to the n8n instance |

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| 401 on all API calls | Token not set or expired | Run `hp token create` again; use the new token |
| 403 on write operations | Token scope is `read_only` | Create a token with `--scope read,write` or `--scope '*'` |
| Inventory shows 0 hosts | Proxmox not configured or refresh not run | Set `HP_PROXMOX_HOST` + token; run `hp inventory refresh` |
| Vault errors on start | Passphrase not set | Auto-generated in v2.2+; for manual setup, set `HP_VAULT_PASSPHRASE` or `HP_VAULT_PASSPHRASE_FILE` |
| Agent not connecting | Hub disabled or token mismatch | Check `HP_AGENT_HUB_ENABLED` is not set to `false`, and re-copy the one-liner from **Agents** (the token regenerates only if the data dir was wiped) |
| Agent logs "hub certificate does not match the pinned fingerprint" | The hub certificate was replaced (data dir wiped, or an explicit cert configured) | Re-enroll the host with the current `--tls-pin` from **Agents** |
| Agent logs a certificate-verification failure with no pin | `--tls` without `--tls-pin` against a self-signed hub | Re-run the installer with the `--tls-pin` the UI shows |
| Backend won't start: "Agent Hub refusing to start" | `HP_AGENT_HUB_TLS=false` set explicitly on a non-loopback bind | Drop the setting (TLS self-configures) or, on a trusted isolated network only, set `HP_HUB_ALLOW_INSECURE=1` |
| Artifact push fails | Deploy key permissions or network | Check key has write access; push failures are non-fatal |
