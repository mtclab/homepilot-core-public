# HomePilot v2 — Progress & Audit Report

**Date:** 2026-05-06
**Latest commit:** b878dac
**Status:** All security+correctness fixes applied, routers implemented, tests passing (103), CI/Docker infra ready, 3 review cycles complete

## Progress

### Completed

| Module | Files | Lines | Status |
|--------|-------|-------|--------|
| Foundation (config, db, vault, auth, common, main) | 9 | ~900 | Done |
| Artifact engine (models, store, lifecycle) | 4 | ~685 | Done |
| Executor (orchestrator + 5 kind drivers + kb_note) | 7 | ~1035 | Done |
| Adapters (httpx Proxmox, SFTP SSH, jump server) | 5 | ~560 | Done |
| MCP server (11 agent + 7 executor tools) | 3 | ~825 | Done |
| CLI (15 commands) | 2 | ~501 | Done |
| KB service | 2 | ~151 | Done |
| Inventory service | 3 | ~172 | Done |
| Jump server relay | 1 | ~340 | Done |
| **Security + correctness fixes (commit 40578dc)** | 16 | +285 | Done |
| **Router CRUD implementations** | 3 | +264 | Done |
| **Executor tool wiring** | 3 | +120 | Done |
| **Tests (103 passing)** | 5 | +500 | Done |
| **Dockerfile + compose + CI** | 5 | +200 | Done |
| **Total** | **62** | **~6,800+** | |

### Remaining

- Web UI (read-only SvelteKit: artifact browser, review queue, env doc, journal, drift)
- More integration tests (executor drivers with mocked adapters)
- Dockerfile + docker-compose for v2
- CI pipeline (GitHub Actions)

## Fixes Applied (commit 40578dc)

### Security Critical

| ID | Finding | Fix | Status |
|----|---------|-----|--------|
| C-1 | `eval()` on skip_if — RCE | AST-based `safe_eval_skip_if` in new `executor/skip_if.py` | FIXED |
| C-2 | Unsandboxed Jinja2 — RCE | `SandboxedEnvironment` in http_sequence + proxmox_api | FIXED |
| C-3 | Jump server empty token = allow all | Empty token now refuses all connections | FIXED |
| C-4 | Token compare not constant-time | `hmac.compare_digest` | FIXED |
| C-5 | No auth on FastAPI endpoints | `require_token` dependency on all routers | FIXED |
| C-6 | SSH readonly command regex bypass | Prefix + exact allowlist `_READONLY_PREFIXES` / `_READONLY_EXACT` | FIXED |

### Security High

| ID | Finding | Fix | Status |
|----|---------|-----|--------|
| H-1 | SSH `known_hosts=None` — MITM | `JUMPSERVER_KNOWN_HOSTS_FILE` env var + `asyncssh.load_known_hosts()` | FIXED |
| H-3 | TLS `verify=False` on HTTP outbound | Changed to `verify=True` in MCP server + executor_tools | FIXED |
| H-4 | Default secret key with random fallback | Raises `ValueError` on default, no random fallback | FIXED |
| H-5 | Vault passphrase in plaintext env var | Deferred (needs HP_VAULT_PASSPHRASE_FILE support) | DEFERRED |
| H-6 | `http_call_read` unrestricted SSRF | base_url validation + must exist in vault | FIXED |
| H-7 | `proxmox_api_read` no path restrictions | Prefix allowlist + blocked token filter | FIXED |
| H-8 | No rate limiting | Per-IP sliding window middleware (60 req/min) | FIXED |

### Code Correctness

| ID | Finding | Fix | Status |
|----|---------|-----|--------|
| CC-1 | orchestrator `_dispatch()` dict vs ExecutionResult | Dict → ExecutionResult conversion in `_dispatch` | FIXED |
| CC-2 | composite rollback empty `applied_sub_ids` | Query store for applied/approved sub-artifacts | FIXED |
| CC-3 | KB vector search missing MATCH clause | Added `WHERE v.embedding MATCH ?` + `ORDER BY v.distance` | FIXED |
| CC-4 | `hash()` non-deterministic KB IDs | `hashlib.sha256(content).hexdigest()[:6]` | FIXED |
| CC-5 | db/connection bypasses `conn` property | `self._connection` → `self.conn` | FIXED |
| CC-6 | db/migrations `BEGIN` conflicts with aiosqlite | `async with db.conn.transaction()` | FIXED |
| CC-8 | `_log_audit` is no-op | Fire-and-forget `create_task` with background task set | FIXED |
| CC-9 | host_type `pve-node` vs `node` mismatch | Standardized to `"node"` | FIXED |
| CC-10 | Sync MCP store calls block event loop | `asyncio.to_thread()` wrapping | FIXED |

### Feature Completion

| ID | Area | Status |
|----|------|--------|
| FC-1 | Artifacts router CRUD | DONE — list, get, propose, approve, reject, apply, revoke |
| FC-2 | Inventory router CRUD | DONE — list, get, refresh, patch |
| FC-3 | KB router CRUD | DONE — list, search, record_fact, get |
| FC-4 | Executor tools NotImplementedError stubs | DONE — wired to real executor implementations |
| FC-7 | MCP server auth gate | DONE — HP_MCP_TOKEN env var validation |

## Review Cycle 1 Findings (from code review)

### Fixed in cycle 1 fix pass

| Finding | Severity | Fix |
|---------|----------|-----|
| Jump server unbounded message size — OOM risk | HIGH | `MAX_MESSAGE_SIZE=1MB` clamp in `_read_message` |
| Jump server path traversal in read_file/write_file | HIGH | `_is_safe_path()` — reject `..`, require absolute |
| Artifacts router DELETE body consumed twice | HIGH | Single `request.body()` read + `json.loads` |
| `_cascade_invalidate` infinite recursion | MEDIUM | `visited` set to prevent re-visits |
| `_log_audit` fire-and-forget tasks GC'd | MEDIUM | `_background_tasks` set + done-callback |
| MCP server broken jump client used for SSHAdapter | HIGH | Set `jump_client=None` on connect failure |
| MCP token comparison timing | LOW | `hmac.compare_digest` |

### Known remaining (lower priority)

| Finding | Severity | Status |
|---------|----------|--------|
| M-1 TOCTOU on artifact hash verification | MEDIUM | Deferred |
| M-2 No `is_relative_to` path check in artifact store | MEDIUM | Deferred |
| M-5 PVE API token in env var | MEDIUM | Deferred |
| M-6 CLI writes plaintext secrets to .env | MEDIUM | Deferred |
| M-13 LIKE wildcard injection in inventory filter | LOW | Deferred |
| H-5 Vault passphrase in plaintext env var | HIGH | Needs HP_VAULT_PASSPHRASE_FILE |
| search_kb uses `repo.search_docs_by_source` not vector | MEDIUM | Needs repo method update |
| Unbounded rate limiter memory | LOW | Pruning added, bounded LRU better |
| N-C-1 SSH readonly validator broken — FIXED | CRITICAL | Rewritten with strict regex allowlist |
| N-H-1 Vault secret name path traversal — FIXED | HIGH | Alphanumeric-only validation |
| N-H-2 Jump server client OOM — FIXED | HIGH | 1MB message size cap |
| N-H-3 MCP empty token bypass — FIXED | HIGH | Strip + empty check |
| N-M-4 Ansible inventory host injection — FIXED | MEDIUM | Hostname regex validation |
| N-M-1 Vault temp file TOCTOU | MEDIUM | Deferred |
| N-M-2 Rate limiter IP behind proxy | MEDIUM | Deferred |
| N-M-3 Jump server TLS CERT_NONE | MEDIUM | Deferred |

## v1 Findings Reconciliation

| v1 Finding | v2 Status |
|-----------|----------|
| C-1 Guardrails never invoked | **REMOVED** — no Guardrails class in v2 (agent is external) |
| C-2 ssh_write command injection | **FIXED** — SFTP write, no heredoc |
| C-3 Jump server no auth | **FIXED** — empty token refuses all, constant-time compare |
| C-4 SSH known_hosts=None | **FIXED** — JUMPSERVER_KNOWN_HOSTS_FILE |
| H-3 Vault XOR wrapping | **FIXED** — AES-256-GCM |
| H-5 Deploy pipeline no hash check | **FIXED** — orchestrator verifies hash |
| H-7 Secrets in plaintext env | **PARTIAL** — HP_VAULT_PASSPHRASE_FILE still needed |
| M-2 Token hash comparison | **FIXED** — hmac.compare_digest |
| M-3 Rate limiter | **FIXED** — per-IP sliding window middleware |