# HomePilot v2 — Testing Guide

Three layers: unit tests (always), integration (no server needed), and e2e (requires a live instance).

---

## 1. Unit + Integration Tests

Run everything (no live server required):

```bash
pip install -e ".[dev]"
pytest tests/ --ignore=tests/test_e2e.py
```

Full check (what CI runs):

```bash
ruff check src/
ruff format --check src/
mypy src/
pytest tests/ --ignore=tests/test_e2e.py -v
```

What's covered:

| Test file | What it tests |
|---|---|
| `test_auth_cookie.py` | HttpOnly cookie auth, CSRF protection, admin token create endpoint |
| `test_cli_token_create.py` | `hp token create` — direct DB path + HTTP API fallback |
| `test_security_fixes.py` | Rate limiting middleware, vault passphrase handling, token scopes |
| `test_artifacts_*.py` | Artifact lifecycle, store, executor |
| `test_kb_*.py` | KB indexing, vector search |
| `test_inventory_*.py` | Inventory service, host discovery |
| `test_migrations.py` | Schema migrations idempotency |
| `test_mcp_*.py` | MCP tool wiring |

---

## 2. End-to-End Tests (live instance)

Tests in `tests/test_e2e.py` run Playwright against a real running HomePilot.

### Prerequisites

```bash
pip install playwright
playwright install chromium
```

### Run against dev server

```bash
export HP_TEST_URL=http://homepilot:8000
export HP_TEST_TOKEN=hp_60709341b3d8c1b602b5d82eb5429344375debdcc3c5f83a14dfaed645c89c66
export HP_TEST_ADMIN_SECRET=your-admin-secret  # required for token CRUD tests

pytest tests/test_e2e.py -v
```

> **Note:** E2e tests should be run **in isolation** (not after the full unit test suite) because unit tests hit auth endpoints heavily and can exhaust the server's per-IP rate limit window. If running after unit tests, wait ~70s for the rate limit window to reset.

On the Kasm workspace (no display needed — runs headless):

```bash
DISPLAY=:1.0 HP_TEST_TOKEN=hp_... pytest tests/test_e2e.py -v
```

### What's tested

| Test class | What it verifies |
|---|---|
| `TestAuthRedirect` | Fresh browser → redirected to `/login`; login → redirect back |
| `TestAPIHealth` | `/health` OK; authenticated requests succeed; unauthenticated → 401 |
| `TestUIPages` | All 7 UI routes render without inline 401 errors |
| `TestAdminTokenCreate` | `/auth/tokens` without admin secret → 403 |
| `TestTokenCRUD` | Create, list, and revoke tokens via admin secret |
| `TestKBCRUD` | Create and search KB notes |
| `TestCSRFProtection` | Mutation without CSRF headers → 403; with CSRF → accepted |
| `TestTokenScopeEnforcement` | Read-only token cannot delete or access admin endpoints |
| `TestPathTraversal` | Path traversal in ingest paths rejected |
| `TestSessionIndicator` | `auth/me` returns authenticated user info |
| `TestAuthRoundTrip` | Login → me → logout → me=401 flow |
| `TestAuthBypass` | Unauthenticated requests to protected paths → 401/403/429 |
| `TestRateLimiting` | Authenticated requests within limit; unauthenticated flood → 429 |

### Auth model

E2e tests use **two auth patterns**:

1. **Bearer auth** — API calls that don't need cookies (health, artifacts, KB search, etc.)
2. **Cookie auth + CSRF** — Browser-authenticated mutations (token create/revoke, KB create, UI navigation)

The `session_auth` fixture pre-creates all needed tokens at session start (with 429 retry logic) so individual tests don't hit the token creation rate limit.

### Skip in CI

The e2e tests skip automatically when `HP_TEST_TOKEN` is not set:

```bash
pytest tests/  # safe — e2e tests self-skip without the env var
```

CI explicitly ignores them too (see `.github/workflows/ci.yml`).

---

## 3. Manual Smoke Test Checklist

Run after deploying a new image to verify nothing regressed.

### Auth flow

- [ ] Open `http://homepilot:8000/ui` in a fresh incognito window
- [ ] Redirected to `/ui/settings` automatically (no 401 errors shown)
- [ ] Paste token in the **API Token** field, click **Save**
- [ ] Redirected back to `/ui/artifacts`
- [ ] Status bar shows "Session active"

### Core pages

- [ ] **Artifacts** — page loads, no error
- [ ] **Inventory** — hosts visible (or "no hosts" message, not 401)
- [ ] **KB** — search box present
- [ ] **Drift** — loads without error
- [ ] **Settings** — shows "Session active" with the current token

### Token management

```bash
# Create token while backend is running (HTTP-first path)
docker compose exec backend hp token create --label smoketest --output json
```

- [ ] Returns `{"token": "hp_...", "scope": "read,write"}`
- [ ] No "database is locked" error

### Vault

```bash
docker compose exec backend hp vault list
docker compose exec backend hp vault set smoketest-secret --value '{"key":"value"}'
docker compose exec backend hp vault get smoketest-secret
docker compose exec backend hp vault delete smoketest-secret --yes
```

- [ ] All four commands succeed

### Startup log check

```bash
docker compose logs backend --tail=50
```

- [ ] No `WARNING` for `HP_VAULT_PASSPHRASE via env var` (downgraded to INFO)
- [ ] No `WARNING` for `Vault secret 'pve-token' unavailable` when `PVE_API_TOKEN` is set (logged at DEBUG)
- [ ] `Vault unlocked` present (if `HP_VAULT_PASSPHRASE` configured)
- [ ] `HomePilot v2 started` present

### API directly

```bash
TOKEN=hp_...
curl -s http://homepilot:8000/health | jq .
curl -s -H "Authorization: Bearer $TOKEN" http://homepilot:8000/artifacts | jq .total
curl -s -H "Authorization: Bearer $TOKEN" http://homepilot:8000/inventory | jq .total
```

---

## 4. Dev Server Details

| Thing | Value |
|---|---|
| Web UI | `http://homepilot:8000/ui` |
| API | `http://homepilot:8000` |
| MCP | `http://homepilot:8000/mcp` |
| Health | `http://homepilot:8000/health` |
| SSH | `ssh deploy-user@homepilot` |
| Docker stack | `/opt/homepilot/repo` on `homepilot` |

Create a fresh token when needed:

```bash
docker exec repo-backend-1 hp token create --label dev --output json
```

Tail live logs:

```bash
ssh deploy-user@homepilot "docker compose -f /opt/homepilot/repo/docker-compose.yml logs -f backend"
```

Deploy a new image:

```bash
ssh deploy-user@homepilot "cd /opt/homepilot/repo && HP_IMAGE_TAG=2.0.4 docker compose pull && HP_IMAGE_TAG=2.0.4 docker compose up -d"
```

---

## 5. Frontend Unit Tests

```bash
cd web
npm test -- --run          # vitest, headless
```

Covers `api.ts` helper functions and URL validation. No browser needed.
