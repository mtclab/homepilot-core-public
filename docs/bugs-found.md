# Bugs Found During Testing — 2026-05-20

## Bug 1: X-Admin-Secret vs X-Hp-Admin-Secret

**Severity**: High  
**Status**: Found, not yet fixed  
**Location**: `src/homepilot/auth/router.py` — `admin_create_token` endpoint  
**Description**: The API endpoint for creating admin tokens checks the header `X-Hp-Admin-Secret`, but:
- The OpenAPI schema / auto-generated docs may advertise `X-Admin-Secret`
- Any client or script using `X-Admin-Secret` gets a 403 "Invalid admin secret" with no helpful error message
- This wasted 30+ minutes of debugging during screenshot work  

**Fix**: Either rename the header to `X-Admin-Secret` (standard convention) OR document `X-Hp-Admin-Secret` clearly in the OpenAPI schema and error messages. Better: add a clear error message like "Missing X-Hp-Admin-Secret header" instead of the generic "Invalid admin secret".

---

## Bug 2: `hp token create` CLI fails with vault coroutine error

**Severity**: Medium  
**Status**: Found, not yet fixed  
**Location**: `src/homepilot/cli/token.py` and `src/homepilot/config.py`  
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
**Status**: Found, not yet fixed  
**Location**: SvelteKit routing  
**Description**: Navigating to `/ui/auth/login` redirects to `/ui/login?returnTo=%2Fui%2Fauth%2Flogin`. The SvelteKit routes use `/ui/login` as the canonical path, but the API and some docs reference `/ui/auth/login`. Neither path is clearly documented as canonical.

**Fix**: Choose one canonical path and redirect the other. Document it in the API docs.

---

## Bug 4: Secure cookies over HTTP

**Severity**: Medium  
**Status**: Found, not yet fixed  
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