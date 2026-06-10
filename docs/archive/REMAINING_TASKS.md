# HomePilot v2 — Remaining Tasks

**Updated:** 2026-05-08
**Status:** Core v2 complete. 266 tests passing. mypy strict: 0 errors. CI green.
**Commits:** 4b40347 → ac8051d (PRs #33–#36)

## Done Since Last Update

PR #33 (feat/kb-reindex-and-tests):
- `hp kb reindex` — deletes artifact-sourced KB index, re-indexes all applied kb-note artifacts
- Fix `run_migrations` — chicken-and-egg settings table query + wrong transaction context manager
- Fix `KBService.record_fact` duplicate-ID bug — was comparing stored hash vs stored body, not new content
- Add `tests/test_kb_service.py` — 16 tests for KBService.search, record_fact, KB router
- Closes #24

PR #34 (feat/inventory-stubs-and-tests):
- `_guess_ip(hostname)` — async DNS resolution, None on failure
- `verify_connectivity(host, port, timeout)` — async TCP reachability check
- Integrated `_guess_ip` into `refresh_inventory` as IP fallback when Proxmox API returns no IP
- Add `tests/test_inventory_service.py` — 18 tests
- Closes #25

PR #35 (feat/hp-import):
- `hp import <path>` — restore artifacts repo + DB from tarball
- Pre-import DB backup to `{data_dir}/backups/` before any overwrite
- Path traversal guard — rejects archive members with absolute paths or `..`
- Closes #29

Issue #26: Closed — `get_environment_doc` was already implemented in inventory/service.py

PR #36 (chore/mypy-strict-clean):
- mypy strict: 253 → 0 errors across all 45 source files
- Added `dict[str, Any]`, `list[Any]`, proper return types throughout
- No behavior changes

## Remaining

### Web UI (v1.x, not blocking v2 release) — GitHub #31

- SvelteKit scaffold (package.json, svelte.config.js, vite.config.ts)
- Artifact browser — list/filter artifacts by status/kind/date
- Review queue — proposed artifacts with approve/reject/edit buttons
- Environment doc renderer — per-host/service page fusing inventory+KB+artifacts
- Drift view — artifact pile vs live inventory diff
- Journal viewer — chronological artifact history
- No chat UI — opencode/Claude Code is the chat

### Code Quality (minor, no blocker)

- `from __future__ import annotations` cleanup (mixed usage across some files)
- Empty `common/` directory shadows `common.py` module — rename or merge

### Future Features (v1.x) — GitHub #33 (renamed)

- `hp policy init` onboarding wizard — seeding ~20 policy KB entries (done in CLI but not tested)
- Catalog seed: starter artifact templates for Jellyfin, Vaultwarden, AdGuard, Gitea
- context7 caching in KB (proxy + ingest with TTL)
- Scheduled tasks via cron (`hp schedule add ...`)
- RBAC: enforce token scope column (read-only tokens, automation tokens)
- `known_hosts` auto-population during SSH bootstrap step
