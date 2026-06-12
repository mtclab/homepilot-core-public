# Changelog

## v2.4.0 (2026-06-12)

### Features

- **Agents survive backend restarts (#343)**: the agent registry is now
  persisted (migration v11 `agents` table). After a backend update agents show
  as known/reconnecting instead of vanishing, and coverage no longer flaps to
  "uncovered." `GET /agents/` overlays live connections on the persisted set;
  UI shows connected / stale / disconnected.
- **Overview dashboard (#344)**: the home page is now a current-state dashboard
  — coverage %, uncovered hosts, in-spec %, agent fleet, and status/role/artifact
  donuts. New `GET /dashboard/summary` (+ `/dashboard/config`). Hand-rolled SVG
  charts, no new dependency. HomePilot shows current state; history stays in
  Zabbix.
- **Zabbix deep-links (#345)**: `HP_ZABBIX_URL` (default `/zabbix`, the bundled
  reverse-proxy path) powers "Metrics ↗" links per host on Inventory and Agents.
- **Logo + favicon (#346)**: HomePilot mark; fixes the prior favicon 404.

## v2.3.10 (2026-06-12)

### Bug Fixes

- **Agents tab renders state/system_info correctly (#341)**: the agent `state`
  object (and nested `system_info` entries like disk/load/memory) showed as
  `[object Object]`, and the status badge compared `state === 'connected'`
  (never true). Now renders objects as compact JSON (empty as `—`) and derives
  the connected/stale badge from heartbeat age (`stale_seconds`).

## v2.3.9 (2026-06-12)

### Features

- **Inventory auto-enriches each cycle (#338)**: the inventory reconciler now
  runs an enrichment pass after each refresh, so IP addresses and derived
  online/offline status populate automatically — no manual Sync needed after a
  restart or for newly discovered guests. Best-effort: enrichment failures are
  logged and never fail the cycle.
- **Configurable hub advertise address (#339)**: `HP_AGENT_HUB_ADVERTISE_HOST`
  (accepts `host` or `host:port`) controls the address the enrollment endpoints
  and UI install command hand to agents. Set it to the HomePilot host's IP when
  HomePilot sits behind a reverse proxy so agents dial the raw hub port instead
  of the proxy. Resolution order: this setting → non-wildcard bind host →
  request hostname.

## v2.3.8 (2026-06-12)

### Bug Fixes

- **Inventory adoptions no longer vanish on restart (#335)**: host/service/audit
  writes were issued on the shared DB connection but never committed, so they
  lived only in the connection's implicit transaction and were rolled back when
  the connection closed on shutdown. An adopted guest reverted to
  `discovered`/`pending` on the next container restart/update. `create_host`,
  `update_host`, `delete_host`, `create_service`, `update_service`,
  `delete_service`, and `log_audit` now commit.
- **Agent enrollment works end-to-end (#336)**:
  - `GET /agents/bootstrap` and `/agents/token` returned 404 — the agent router
    was mounted under an extra `/api` prefix while the UI calls `/agents/*`.
  - `install-agent.sh` rewritten to match the env-configured Go agent: parses
    `--hub`/`--token`, writes `/etc/homepilot/agent.env`, installs a working
    systemd unit, and starts it (previously it expected env vars and called
    non-existent `hp-agent enroll`/`start` subcommands).
  - `install-agent.sh` is now published as a release asset (the UI one-liner
    fetched it from there).
  - Enrollment responses advertise the request host instead of the `0.0.0.0`
    bind address.
  - The UI offers a reboot-safe install one-liner using the durable shared hub
    token (the one-time bootstrap token cannot re-register after a restart).

## v2.3.7 (2026-06-12)

### Bug Fixes

- **Shared tokens survive multi-client logins (#323/#325)**: login no longer
  rotates-and-deletes a token when a second client (different IP/User-Agent)
  authenticates with it. The fingerprint is advisory: a mismatch is logged and
  the token stays valid.
- **Agent executor actually runs (#327)**: a latent gate on the removed SSH
  transport meant the agent-backed artifact executor was never constructed in
  production. Removed with the transport; agent execution now works as
  documented.
- **`hp token list` / `hp token revoke` work (#328)**: both accepted only an
  admin-scope bearer while the CLI sends the admin secret; they now accept
  either, matching token create. `list` shows all tokens, not just the
  caller's.
- **Root path redirects (#321)**: `GET /` returns 307 to `/ui/`.
- **Login errors are human-readable (#321)**: the web UI maps API errors to
  messages instead of dumping raw JSON.

### Changed

- **Jumpserver removed (#327)**: the SSH relay (code, image, compose service,
  `HP_JUMP_*` settings) is gone; the agent hub is the only host-management
  path. Stale `HP_JUMP_*` variables in an existing `.env` are ignored.
- **Rate limiter hardening (#321)**: anonymous requests no longer trigger a
  database token lookup, bounding flood amplification.
- **Metrics cardinality (#321)**: Prometheus labels use the route template
  (`/artifacts/{artifact_id}`) instead of the raw URL.
- **`GET /tasks` (#321)**: `artifact_id` is now optional — omitting it lists
  tasks system-wide.
- **Releases auto-tagged (#306/#329)**: a push to main with a new version in
  pyproject creates the `v<version>` tag automatically.
- **Dependencies**: aiohttp 3.14.0, starlette 1.0.1, transitive `cookie`
  override to ^0.7.0 (clears a low-severity advisory).

## v2.3.6 (2026-06-10)

### Features

- **Inventory import/sync of external PVE VMs**: discover and adopt VMs/LXC that
  were created outside HomePilot.

### Bug Fixes

- **Inventory status on refresh (#318)**: a guest that is shut down now surfaces
  as `offline` after an inventory refresh, instead of staying `unknown`. Derived
  `status` was previously computed only during enrichment; the refresh path now
  derives it (`stopped → offline`, `running + ip → online`) for nodes and guests,
  on both create and update. `Repository.create_host` gains a `status` argument
  (was hard-coded to `unknown`).
- **Proxmox settings endpoints + client close**: restored the admin Proxmox
  settings endpoints and fixed a client-close bug.

### UI

- **Drift page "uncovered" hosts (#318)**: the list previously labelled
  "unmanaged hosts" is renamed **"uncovered"**. It means *no applied artifact
  targets the host* — it is unrelated to the inventory `managed` flag. Adopting a
  host in inventory does not "cover" it; an artifact must target it. Label and
  help text updated to remove the ambiguity.

### Security

- **Scrub tooling no longer leaks into public mirrors (#316)**: `scrub-for-public.sh`
  and `validate-scrub.sh` are now deleted from the export, and the validator no
  longer excludes itself from the scan. Previously these scripts shipped to the
  public repos carrying the real PVE token and operator identifiers as pattern
  literals, and the self-exclusion hid them. The leaked PVE token was rotated and
  the public repo history was reset. Also fixed a sed BRE bug where the
  `10.x.x.[0-9]+` subnet replacement never matched.
- **Public nginx proxy template documented (#311)**: the scrubbed
  `deploy/control-plane/nginx-hp-proxy.conf` now carries a banner stating that its
  `proxy_pass` upstreams are placeholders the operator must set.

### Chores

- **Lint/type/security clean (#311)**: cleared ruff (`E501`, `SIM105`, `RUF059`),
  ruff-format, mypy (`no-any-return`), bandit (`B110`), and detect-secrets findings
  so the integration suite passes end-to-end. Upgraded CI pip before `pip-audit`
  to clear a pip self-advisory; guarded the `hp_agent` zabbix tests with
  `importorskip` so they skip where the host-agent package isn't installed.

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

- **E2E test rewrite**: `tests/test_e2e.py` completely rewritten for live server testing against the dev instance at `10.0.0.1:8000`.
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