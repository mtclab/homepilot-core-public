# Changelog

## v2.3.4 (2026-06-09)

### Features

- **Dual PVE tokens (read + write)**: Separate low-privilege read token and higher-privilege write token for Proxmox operations. Read operations use `pve-token` from vault; mutations (POST/PUT/DELETE) use `pve-write-token` if configured, otherwise fall back to read token. Configurable in web UI (Settings → Proxmox) or via vault.
- **Token scope display**: Settings UI shows the scope of the current API token (e.g., `read,write` or `read_only`) after login, with warnings for insufficient scope and link to create a new token.
- **Proxmox Settings UI**: Configure Proxmox host, port, and both API tokens from the web UI (`/ui/settings`). Connection status and Test Connection button.
- **Agents page**: Web UI at `/ui/agents` showing connected agent status.
- **System Health section**: Health checks rendered from nested `checks` object with proper string filtering. Proxmox connectivity indicator.
- **Admin-scoped API**: Backend Proxmox settings endpoints mounted at `/admin/settings/proxmox/*`. Admin token (scope `full` or `admin`) required.
- **Agent Hub (replaces jump server)**: `hp-agent` binary enrolls with the hub over TCP (port 8443) using a shared secret. No more jump server relay, no more `~/.ssh/known_hosts` management.
- **Merged agent services**: `homepilot-agent` repo merged into `homepilot-v2`. Single repo, single compose, single deploy. n8n, SearXNG, Radicale, Whisper, and Piper are first-class services in the main compose.
- **LLM overlay (optional)**: Local llama.cpp + BGE-M3 embeddings moved to `docker-compose.agent.yml` (opt-in via `--profile gpu` or `--profile cpu`). Use a remote LLM (Ollama, OpenAI, etc.) by default.

### Bug Fixes

- **API prefix fix**: Frontend Proxmox settings calls corrected from `/settings/proxmox/*` to `/admin/settings/proxmox/*` — `getProxmoxSettings`, `saveProxmoxSettings`, `testProxmoxConnection`, `reloadSecrets`.
- **Health checks rendering**: Settings page now iterates `healthData.checks` instead of `healthData` directly. Non-string values filtered. Proxmox status reads `healthData.checks?.proxmox`.
- **CSRF protection**: Added `X-Requested-With: XMLHttpRequest` header on cookie-auth mutation requests alongside `X-CSRF-Token`. Updated `HealthInfo` TypeScript interface to include `checks?: { [key: string]: string }`.
- **Image tag**: Default `HP_IMAGE_TAG` in docker-compose.yml updated from `2.2.2` to `2.3.4`.

## v2.2.5+ (2026-06-02)

### Testing

- **E2E test rewrite**: `tests/test_e2e.py` completely rewritten for live server testing against the dev instance.
- **Rate limit resilience**: Session-scoped `session_auth` fixture pre-creates all tokens (session, revoke target, scope target, read-only, roundtrip) with 429 retry logic. Individual tests reuse pre-created tokens instead of creating new ones on the fly, eliminating rate-limit skips.
- **Browser cookie auth**: `auth_page` fixture authenticates via browser UI login so cookies (`hp_token`, `hp_csrf`) are set correctly in the Playwright context.
- **CSRF headers**: All cookie-authenticated mutations include `x-csrf-token` + `x-requested-with: XMLHttpRequest` headers.
- **Token prefix fix**: `test_revoke_token` uses `token[:16]` matching `PREFIX_LENGTH = 16`.
- **30 passed / 0 skipped / 0 failed** on live e2e run.

### Fixes

- **AGENTS.md model assignments**: Updated to match current `~/.config/opencode/agents/roles.md` (deepseek-v4-pro removed, kimi-k2.6 now primary for CoreSquad/ToolingSquad/QATester).
- **matrix_server.py default URL**: Changed from `example.com` to `matrix.example.com` and regex from `@hp-([a-z]+):example\.com` to `@hp-([a-z]+):`.

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