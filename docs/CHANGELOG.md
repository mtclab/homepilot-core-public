# Changelog

## v2.2.3 (2026-05-25)

### Security

- **vitest 2.1.9→4.1.7**: Dev dependency bump in `/web/` (PR #287).
- **esbuild 0.25.12**: Fixed esbuild dev server CVE (medium, absorbed via transitive dep).
- **vite 6.4.2**: Fixed CVE-2026-39365 path traversal (medium, absorbed via transitive dep).
- **Deferred**: CVE-2024-47764 cookie (low, SvelteKit transitive dep, no safe fix).

## v2.2.2 (2026-05-16)

### Features

- **Auto-generate vault passphrase**: When neither `HP_VAULT_PASSPHRASE` nor `HP_VAULT_PASSPHRASE_FILE` is set, the system generates a 256-bit passphrase using `secrets.token_urlsafe(32)` and persists it to `{data_dir}/.vault_passphrase` (mode `0o600`). On subsequent starts, the persisted passphrase is loaded automatically. This enables zero-secrets deployment where `.env` contains no HomePilot secrets.
- **`_try_vault_secret` multi-key extraction**: The configuration resolver now attempts multiple keys when extracting secrets from the vault: `value` → `secret` → `key` → `token` → first value. This accommodates different vault secret formats (e.g., `pve-token` stored as `{"token": "..."}` vs `secret-key` stored as `{"value": "..."}`).
- **Zero-secrets deployment verified**: Production dev server (homepilot.example.com:8000) now runs with zero HomePilot secrets in `.env`. All 5 secrets are stored in the encrypted vault and resolved at runtime.

### Bug Fixes

- **Lint fix**: Removed unused `stat` import in vault passphrase auto-generation code.

## v2.2.1 (2026-05-15)

- Initial deployment with zero-secrets architecture
- Vault passphrase auto-generation
- `_try_vault_secret` progressive key fallback

## v2.2.0 (2026-05-14)

- Vault encryption with age + AES-GCM identity protection
- SSH jump server relay
- MCP HTTP transport
- Artifact lifecycle (propose, approve, apply, revoke)