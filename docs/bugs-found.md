# Bugs Found During Testing — 2026-05-20

## Bug 1: X-Admin-Secret vs X-Hp-Admin-Secret

**Severity**: High  
**Status**: Fixed — the endpoint accepts both header spellings:
`request.headers.get("x-hp-admin-secret") or request.headers.get("x-admin-secret")`.
**Location**: `src/homepilot/auth/router.py` — `admin_create_token` endpoint  
**Description**: The API endpoint for creating admin tokens checks the header `X-Hp-Admin-Secret`, but:
- The OpenAPI schema / auto-generated docs may advertise `X-Admin-Secret`
- Any client or script using `X-Admin-Secret` gets a 403 "Invalid admin secret" with no helpful error message
- This wasted 30+ minutes of debugging during screenshot work  

**Fix**: Either rename the header to `X-Admin-Secret` (standard convention) OR document `X-Hp-Admin-Secret` clearly in the OpenAPI schema and error messages. Better: add a clear error message like "Missing X-Hp-Admin-Secret header" instead of the generic "Invalid admin secret".

---

## Bug 2: `hp token create` CLI fails with vault coroutine error

**Severity**: Medium
**Status**: Fixed — `_mint_token_via_api()` now resolves the admin secret exactly
like the backend (`settings.admin_secret` → vault `admin-secret`, no passphrase
fallback) and, on refusal, surfaces the backend's real `detail` (e.g. "admin
secret must be configured") with a hint to run `hp init --non-interactive`,
instead of the misleading "passphrase may not match". Verified: with the
admin-secret bootstrapped in the vault, `hp token create` against a running
backend returns a token (was 403).
**Location**: `src/homepilot/cli/main.py` (`_mint_token_via_api`, `token create`)
**Description**: Running `hp token create` inside the container:
```
RuntimeWarning: coroutine 'VaultManager.get_secret' was never awaited
HP_SECRET_KEY not set — loaded persisted key from /home/homepilot/.hp/.secret_key
Backend rejected admin secret (403). HP_VAULT_PASSPHRASE may not match the running backend.
```
The `VaultManager.get_secret` is an async method being called synchronously from the Pydantic Settings `model_validator`. The CLI cannot authenticate to the running backend to create tokens, making `hp token create` completely broken.

**Fix**: Either:
1. Make `VaultManager.get_secret` work in sync context (use `asyncio.run()` or `run_until_complete()`)
2. Or have `hp token create` write directly to the database instead of going through the HTTP API
3. Or document that `hp token create` only works when the backend is down (offline mode)

---

## Bug 3: `/ui/auth/login` redirects to `/ui/login`

**Severity**: Low  
**Status**: Fixed — `web/src/routes/auth/login/+page.ts` 308-redirects
`/ui/auth/login` → the canonical `/ui/login` (preserving `returnTo`).
**Location**: SvelteKit routing  
**Description**: Navigating to `/ui/auth/login` redirects to `/ui/login?returnTo=%2Fui%2Fauth%2Flogin`. The SvelteKit routes use `/ui/login` as the canonical path, but the API and some docs reference `/ui/auth/login`. Neither path is clearly documented as canonical.

**Fix**: Choose one canonical path and redirect the other. Document it in the API docs.

---

## Bug 4: Secure cookies over HTTP

**Severity**: Medium  
**Status**: Fixed — cookies use `secure=_cookie_secure(request)`, which is `False`
over HTTP and `True` over HTTPS, so login works on the plain-HTTP dev server.
**Location**: `src/homepilot/auth/router.py` — `login()` function  
**Description**: The login endpoint sets cookies with `Secure` flag, but the dev server runs on HTTP (`http://10.0.0.100:8000`). Browsers correctly refuse to send `Secure` cookies over plain HTTP, causing authentication to fail in real browser contexts. The Playwright Python `add_cookies` workaround (removing the Secure flag) works but shouldn't be necessary.

**Fix**: Make the `Secure` flag conditional on the request scheme — set `Secure=True` only when `request.url.scheme == 'https'`.

---

## Bug 5: Scrub script `eval`/`find -exec` pattern was broken

**Severity**: High (was)  
**Status**: Fixed  
**Location**: `scripts/scrub-for-public.sh`  
**Description**: The original scrub script used `eval $SCRUB_FIND -exec sed ... {} +` which broke under `set -euo pipefail` due to special characters in sed expressions and shell expansion issues.

**Fix**: Rewrote with `mapfile` + loop pattern instead of `eval`/`find -exec`.

---

## Bug 6: `media-lxc` global replace broke test assertion

**Severity**: Low  
**Status**: Fixed  
**Location**: `scripts/scrub-for-public.sh` + test file  
**Description**: The scrub script replaced `media-lxc` with `media-lxc` globally, including inside test assertions in `test_inventory_service.py`. This caused the test to fail because the test fixture expected `media-lxc` but the scrubbed version had `media-lxc`.

**Fix**: Added a post-scrub fix to restore `media-lxc` in the test file.

---

# Clean-install / onboarding findings — 2026-06-04

Found while doing a full teardown + from-source reinstall of the control plane
(backend + agent-hub, jumpserver-free) onto the dev hosts (app-server, monitoring-host).

## Bug 7: Agent file-op errors are swallowed → fake write "success" / empty read

**Severity**: High (silent data-integrity failure)
**Status**: Fixed
**Location**: `src/homepilot/agent_hub/server.py` (`send_command`/`send_read_file`/`send_write_file`), `src/homepilot/adapters/agent.py` (`write_file`)
**Description**: When an agent rejected a request (e.g. `write_file` to a path not
in `_ALLOWED_WRITE_PREFIXES`, or `read_file` of a missing file) it replied with an
`{"error": ...}` message. The hub's `send_*` methods returned that result raw
**without checking for `error`**, so:
- `adapter.write_file` then computed `after_hash` from the *intended* content and
  returned `{"changed": true, ...}` — reporting success for a write that never
  landed on disk. Verified live: API said `changed:true`, `cat` on the host found
  no file.
- `adapter.read_file` returned `result.get("content", "")` → empty string for a
  read error, indistinguishable from an empty file.
- The audit log pre-logged `result="success"` *before* the agent even responded.

**Fix**: Added `AgentCommandError` + `_finalize_result()` in `server.py`: audit by
the real outcome and `raise AgentCommandError` when the agent returns `error`.
`adapter.write_file`'s best-effort `before_hash` probe now catches broadly. Router
already maps adapter exceptions → HTTP 502, so rejected writes now surface cleanly.

## Bug 8: Backend won't start on a fresh bind-mount — `unable to open database file`

**Severity**: High (blocks first start)
**Status**: Fixed — `deploy/control-plane/docker-compose.yml` now uses a **named
volume** (`hp-data`) which inherits the image's uid-999 ownership, so no host
chown is needed. The bind-mount + `chown -R 999:999` path is documented for
host-accessible-data setups.
**Location**: deployment (`docker-compose` data bind mount) / `Dockerfile`
**Description**: The container runs as user `homepilot` (uid **999**). A freshly
created host `./data/hp` dir is `root:root`, so sqlite can't create
`homepilot.db` → `sqlite3.OperationalError: unable to open database file`,
crash-loop. **Fix applied at deploy time**: `chown -R 999:999 ./data/hp`. Should be
baked into the deploy procedure (an init step or an entrypoint chown), and
documented — first-time deployers will hit this.

## Bug 9: `hp init` is interactive-only and hangs headless

**Severity**: Medium (blocks scripted/Docker bootstrap)
**Status**: Fixed — added `hp init --non-interactive` (`-y`): reads
`PVE_API_TOKEN`/`HP_ADMIN_SECRET`/`HP_VAULT_PASSPHRASE` from the env (the same
`.env` the backend reads) and auto-generates blanks, no prompts. Verified headless
via `docker compose run --rm --entrypoint hp backend init --non-interactive`.
**Location**: `src/homepilot/cli/main.py` `init()`
**Description**: `init()` uses `typer.prompt(... hide_input=True, confirmation_prompt=True)`.
Under `docker compose exec -T` (no TTY) the hidden/confirmation prompt blocks
forever (getpass on a closed TTY). deployment.md advertises
`docker compose exec backend hp init` as a one-step bootstrap, but it cannot run
non-interactively. **Workaround used**: skip `hp init`; pin `HP_VAULT_PASSPHRASE`
in `.env`, then create the token via the offline DB path (see Bug 2).
**Fix**: add a non-interactive mode (`hp init --yes`/env-driven, autogen blanks).

## Bug 10: `hp inventory refresh` CLI conflicts with the running backend

**Severity**: Low
**Status**: Fixed — `hp inventory refresh` now mints a write-scoped token via the
admin API and calls `POST /inventory/refresh` (the backend owns the sqlite write
lock) when the backend is up, falling back to the direct-DB path only when it is
down. Verified: refresh against a running backend reports "7 hosts" instead of
"database is locked".
**Location**: `src/homepilot/cli/main.py` `inventory refresh`
**Description**: Run via `docker compose exec` while the backend holds the sqlite
DB, the CLI opens a second writer → `Failed to refresh proxmox inventory: database
is locked` / `Sync complete: 0 hosts`. The backend's own reconciler already
populates inventory. **Fix**: route CLI refresh through the HTTP API when the
backend is up (as `hp token create` does), or document "use the API endpoint".

## Bug 11: Root `docker-compose.yml` couples control plane + agent stack + jumpserver

**Severity**: Medium (no clean control-plane-only deploy)
**Status**: Fixed (new file)
**Location**: `docker-compose.yml`
**Description**: The root compose mixes backend, **jumpserver** (now replaced by the
agent-hub), and the homepilot-agent stack (llm, n8n, searxng, radicale, whisper,
piper). `backend` has `depends_on: jumpserver: service_healthy`, so a
jumpserver-free start blocks. **Fix**: added `deploy/control-plane/docker-compose.yml`
(+ `.env.example`) — backend only, no jumpserver, no coupling. Build context `./repo`.

## Bug 12: Agent binary crashes on connect/auth failure instead of retrying

**Severity**: Medium (log spam; relies on systemd restart)
**Status**: Fixed — `_connect_with_retry()` (capped exponential backoff, retries
until connected or stopped) now backs both the initial connect and `_reconnect()`;
failures log a one-line WARNING instead of a traceback. Verified: with an
unreachable hub the binary logs `connect attempt N … retrying in Ns` and does not
crash.
**Location**: `agent/hp_agent/main.py` `run()` / `connect()`
**Description**: With an unreachable hub the agent logs `connecting to hub …` then
`ERROR: fatal error` + full traceback and exits (caught only by systemd
`Restart=always`). A post-connect auth/protocol failure likewise tracebacks.
**Fix**: wrap connect in a retry loop with backoff; on auth rejection log a clean
"auth rejected" and exit non-zero without a stack trace.

## Bug 13: PEP 668 blocks pip for the legacy agent

**Severity**: Low (moot once the agent is a binary)
**Status**: N/A (binary supersedes)
**Location**: agent install/uninstall
**Description**: On Ubuntu 24.04 `pip install/uninstall agent-host` needs
`--break-system-packages` (externally-managed env). The agent now ships as a
PyInstaller binary, so pip is no longer in the install path.