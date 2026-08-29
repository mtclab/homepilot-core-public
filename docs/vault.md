# HomePilot Vault — Zero-Secrets Architecture

## Overview

HomePilot v2 uses a **zero-secrets deployment model**: the `.env` file contains **no HomePilot secrets**. All sensitive values (API tokens, encryption keys, shared secrets) are stored in an age-encrypted vault and resolved at runtime.

## Architecture

```
Secret resolution order:
  env var → vault → file → auto-generate

  HP_ADMIN_SECRET=         → vault("admin-secret") → ConfigError (production)
  PVE_API_TOKEN=           → vault("pve-token") → unavailable
  HP_EVENTS_WEBHOOK_SECRET=→ vault("webhook-secret") → unavailable
```

The `.env` file only contains **non-secret configuration**: ports, URLs, feature flags, and service discovery addresses.

## Auto-Generated Vault Passphrase

When neither `HP_VAULT_PASSPHRASE` nor `HP_VAULT_PASSPHRASE_FILE` is set, the system automatically generates a strong passphrase and persists it to:

```
{data_dir}/.vault_passphrase   (mode 0o600)
```

- Generated using `secrets.token_urlsafe(32)` (256 bits of entropy)
- File permissions are set to `0o600` (owner read/write only)
- On subsequent starts, the persisted passphrase is loaded automatically
- If the file cannot be written (e.g., read-only filesystem), the passphrase is ephemeral and will change on restart

### Code Reference

```python
# src/homepilot/config.py — Settings._auto_generate_passphrase()


def _auto_generate_passphrase(self, secrets_mod, logger):
    passphrase_path = Path(self.data_dir) / ".vault_passphrase"
    if passphrase_path.exists():
        passphrase = passphrase_path.read_text().strip()
        if passphrase:
            logger.info("Vault passphrase loaded from %s", passphrase_path)
            return passphrase
    passphrase = secrets_mod.token_urlsafe(32)
    passphrase_path.parent.mkdir(parents=True, exist_ok=True)
    passphrase_path.write_text(passphrase)
    passphrase_path.chmod(0o600)
    logger.info("No HP_VAULT_PASSPHRASE — auto-generated and saved to %s", passphrase_path)
    return passphrase
```

## _try_vault_secret — Multi-Key Extraction

The `_try_vault_secret` method attempts to resolve a secret from the vault with **progressive key fallback**:

```python
# src/homepilot/config.py — Settings._try_vault_secret()

def _try_vault_secret(self, name: str) -> str:
    # 1. Open vault with resolved passphrase
    # 2. Get secret dict (e.g., {"token": "pve_token_value"})
    # 3. Try keys in order: "value" → "secret" → "key" → "token"
    # 4. Fall back to first value in dict if none of the above match
    # 5. Return "" if vault unavailable or secret not found
```

This accommodates different vault secret formats:

| Vault Key | Common Value Keys | Example |
|-----------|------------------|---------|
| `admin-secret` | `value`, `secret` | `{"secret": "xyz789..."}` |
| `pve-token` | `token`, `key` | `{"token": "admin@pam!tokenid=uuid"}` |
| `pve-write-token` | `token`, `key` | `{"token": "admin@pam!tokenid=uuid"}` (optional, falls back to pve-token) |
| `webhook-secret` | `value`, `secret` | `{"value": "webhook_hex..."}` |

## Secret Lifecycle

### 1. Environment Variable (highest priority)

If the env var is set, it's used directly. No vault lookup.

```bash
HP_ADMIN_SECRET=abc123  # Used directly
```

### 2. Vault (recommended for production)

If the env var is empty, `_try_vault_secret` attempts to load from the encrypted vault:

```bash
# Set a secret in the vault
docker compose exec -it backend hp vault set pve-token
# Enter JSON: {"token": "admin@pam!tokenid=uuid"}
```

### 3. Auto-Generate (fallback, development only)

If neither env var nor vault has the secret, some keys are auto-generated:

- `vault_passphrase` → persisted to `{data_dir}/.vault_passphrase` (0o600)
- `agent_hub_auth_token` → persisted to `{data_dir}/.agent_hub_token` (0o600)

**Production safeguard**: under `HP_ENV=production` the vault passphrase is never auto-generated — supply `HP_VAULT_PASSPHRASE` or `HP_VAULT_PASSPHRASE_FILE`, or the vault stays disabled.

## Deployment: Zero-Secrets .env

A production `.env` holds **none of the secrets the vault protects** — no admin
secret, no Proxmox token, no webhook key. What it may hold is the ONE key that
opens the vault, and whether it does depends on how the instance was set up:

| Install path | Where the vault passphrase lives |
|---|---|
| First-run claim / plain `docker compose up` | `{data_dir}/.vault_passphrase`, auto-generated at 0600. `.env` holds no secret at all. |
| `hp init` | **`.env`, as `HP_VAULT_PASSPHRASE=…`**, written at 0600. This is what the command does today; the file below is the other shape. |
| `HP_VAULT_PASSPHRASE_FILE` | Wherever the operator points it. Preferred for production. |

Two other values are secrets in the environment rather than in the vault, because
they are needed before the vault is open or by a process that has no vault:
`HP_AGENT_HUB_AUTH_TOKEN` (the shared fleet enrolment token, auto-generated or
read from the vault at startup) and `HP_PORTAL_PROXY_SECRET`. "Zero-secrets"
describes the four vault-held credentials below, not the whole environment.

The example is the auto-generated shape:

```env
# Non-secret configuration only
HP_DAEMON_PORT=8000
HP_PROXMOX_HOST=pve.example.local
HP_PROXMOX_VERIFY_SSL=false
HP_AGENT_HUB_ENABLED=true
HP_AUTO_APPLY_ENABLED=true
HP_INVENTORY_INTERVAL_SECONDS=300
HP_RATE_LIMIT=60
HP_CORS_ORIGINS=*
# Empty by default (KB search is then keyword-only). Set it only when you
# actually run an embedding service — here, the LLM overlay's llm-embed.
HP_EMBEDDING_SERVICE_URL=http://llm-embed:8081/v1/embeddings
HP_EMBEDDING_MODEL=bge-m3

# All secrets are in the vault — no HP_ADMIN_SECRET, PVE_API_TOKEN, etc.
```

The vault holds 4 secrets:

| Vault Key | Purpose | Value Format |
|-----------|---------|-------------|
| `admin-secret` | Admin authentication | `{"secret": "random_hex_32"}` |
| `webhook-secret` | Event webhook verification | `{"value": "random_hex_32"}` |
| `pve-token` | Proxmox VE read API access | `{"token": "admin@pam!tokenid=uuid"}` |
| `pve-write-token` | Proxmox VE write API access (optional) | `{"token": "admin@pam!tokenid=uuid"}` |

## Bootstrap (First Deployment)

```bash
# 1. Start the stack — passphrase auto-generates
docker compose up -d

# 2. Set vault secrets
docker compose exec -it backend hp vault set admin-secret
docker compose exec -it backend hp vault set webhook-secret
docker compose exec -it backend hp vault set pve-token
docker compose exec -it backend hp vault set pve-write-token   # optional

# 3. Create API token
docker compose exec backend hp token create

# 4. Restart to load vault secrets into config
docker compose restart backend
```

## Security Considerations

- Passphrase file (`{data_dir}/.vault_passphrase`) is mode `0o600` — only the HomePilot process can read it
- Vault data is encrypted with age (X25519 + ChaCha20-Poly1305)
- Auto-generated secrets are ephemeral if the passphrase file cannot be persisted (read-only filesystem)
- In production (`HP_ENV=production`), the vault passphrase must be supplied explicitly (`HP_VAULT_PASSPHRASE` / `HP_VAULT_PASSPHRASE_FILE`) — auto-generation is forbidden