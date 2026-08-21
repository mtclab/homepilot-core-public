# HomePilot Artifact Spec

**Status:** v1.0 — implemented and stable.
**Author:** Claude (Opus 4.7), drafted with Olli
**Date:** 2026-05-06
**Companion to:** `ARCHITECTURE.md` (the architecture doc).

This document is the contract between every part of HomePilot v2 that touches an artifact: the agent that produces them, the user that reviews and approves them, the executor that applies them, the journal that indexes them, the web UI that renders them, and the export tool that packages them. If anything disagrees with this document, this document wins; that disagreement is a bug.

---

## 1. Goals and non-goals

### Goals
- Define exactly what a HomePilot artifact looks like on disk.
- Define the lifecycle state machine, exhaustively.
- Define, per `kind`, what the spec content looks like and what the executor does with it.
- Be implementable by one person in 4–6 weeks with no further design ambiguity.
- Be human-readable in any text editor, with or without HomePilot installed.
- Survive HomePilot itself: if you delete HomePilot tomorrow, the artifact directory is still understandable and the playbooks/scripts inside are still runnable manually.

### Non-goals
- Not an Ansible spec. Ansible playbooks are *embedded* in some artifacts; the artifact format itself is HomePilot's.
- Not a replacement for git. Artifacts live in a git repo; this spec describes the file format, not the git workflow.
- Not a permissioning spec. v1 is single-user; `approved_by` is always the daemon's owner.

---

## 2. File format and layout

### File path

```
<artifacts-root>/<YYYY>/<MM>/<id>.md
```

Default `artifacts-root` is `~/.hp/artifacts/`. Configurable via `HP_ARTIFACTS_DIR`.

### Filename / ID format

`id` is the filename basename (without `.md`). Format:

```
YYYY-MM-DD-<kebab-slug>[-<short-uuid>]
```

- `YYYY-MM-DD` reflects the date the artifact was *proposed*, not applied
- `kebab-slug` is `[a-z0-9-]+`, max 60 chars, agent-supplied
- `<short-uuid>` is a 6-char hex appended only if the slug+date already exists; the agent does not include it on first try

Examples:
- `2026-05-06-deploy-media-lxc.md`
- `2026-05-06-deploy-media-lxc-a3f201.md` (collision)
- `2026-05-06-pve-authentik-oidc.md`

### File structure

```
---
<frontmatter as YAML>
---

<body as Markdown, with embedded fenced code blocks for spec content>
```

Standard Markdown frontmatter. The body MAY contain fenced code blocks of language `yaml-spec` (or others, see per-kind sections) which the executor parses. Everything outside fenced spec blocks is human-readable narrative.

### Git

Each artifact write is a commit. Commit message format:

```
<event>: <id> — <intent first line>
```

Where `<event>` is one of `propose`, `edit`, `approve`, `reject`, `apply`, `fail`, `supersede`, `revoke`. The body of the commit is the full intent string. `git log <path>` shows the full lifecycle of one artifact.

---

## 3. Frontmatter schema

YAML. Keys are stable; do not rename without a version bump.

### Always required

| Field | Type | Notes |
|---|---|---|
| `id` | string | Matches the filename basename |
| `kind` | enum | One of: `ansible-playbook`, `proxmox-api-sequence`, `http-sequence`, `composite`, `shell-script`, `kb-note` |
| `intent` | string | One-line human description, ≤ 200 chars |
| `status` | enum | One of: `proposed`, `approved`, `rejected`, `applied`, `failed`, `superseded`, `revoked` |
| `mutating` | bool | `true` for all kinds except `kb-note` |
| `produced_by` | object | `{ session: str, agent: str, user: str, at: ISO8601 }` |
| `hash` | string | `sha256:<hex>` of the body (see §10) |

### Required for mutating kinds (everything except `kb-note`)

| Field | Type | Notes |
|---|---|---|
| `target` | object | See §3.1 |
| `idempotence` | enum | One of: `via-precheck`, `declared-natural`, `replay-only`. See §3.2 |

### Set by lifecycle transitions

| Field | Type | Set when |
|---|---|---|
| `approved_by` | object | `{ user: str, at: ISO8601, reason: str? }`. Set on approve transition. |
| `applied_at` | ISO8601 | Set when status moves to `applied`. |
| `failed_at` | ISO8601 | Set when status moves to `failed`. |
| `failure_reason` | string | Set when `failed`. |
| `supersedes` | list[str] | Optional; IDs of artifacts this one replaces. Set at propose time by the agent. |
| `superseded_by` | string | Set when status moves to `superseded`. |
| `rejected_by` | object | `{ user: str, at: ISO8601, reason: str? }`. Set on reject transition. |
| `revoked_by` | object | `{ user: str, at: ISO8601, reason: str? }`. Set on revoke transition. |

### Optional

| Field | Type | Notes |
|---|---|---|
| `tags` | list[str] | Free-form, e.g. `["security", "auth"]` |
| `rollback` | bool | `true` if a rollback section exists in the body |
| `replay_safe` | bool | Defaults to `true`. `false` per **D3** means the artifact cannot be replayed *at all*; to redo the operation, revoke and produce a fresh artifact. Agent must explain in the body why an artifact is not replay-safe. |
| `requires_snapshot` | bool | Defaults to `true` for `mutating: true` against VM/LXC targets, `false` otherwise. Override only when snapshotting is impossible or dangerous (e.g. snapshotting the running PVE node itself). |

### 3.1 `target` object

```yaml
target:
  kind: vm | lxc | node | cluster | service | network | global
  host: <hostname>            # inventory hostname; required for vm/lxc, optional otherwise
  vmid: <int>                 # required for kind=vm or kind=lxc
  node: <pve-node-name>       # required for kind=node, kind=vm, kind=lxc; forbidden for kind=cluster
  service: <service-name>     # required for kind=service
  network: <bridge-or-vlan>   # required for kind=network
```

Exactly one `kind`. Sub-field requirements per kind:

| `kind` | required sub-fields | optional |
|---|---|---|
| `vm` | `vmid`, `node` | `host` |
| `lxc` | `vmid`, `node` | `host` |
| `node` | `node` | — |
| `cluster` | (none — `node` MUST NOT be set) | — |
| `service` | `service` | `host` (if service is bound to one) |
| `network` | `network` | `node` (if network is node-local) |
| `global` | (none) | — |

Per **D8**: `kind: cluster` is for datacenter-level Proxmox config that lives in `/etc/pve/` and replicates via pmxcfs (realms, ACLs, datacenter firewall, storage). Executor for a `cluster`-targeted `proxmox-api-sequence` sends the API call to any healthy node and trusts pmxcfs to replicate. `kind: global` is for HomePilot-level operations with no homelab target (e.g. "rotate master vault passphrase").

**Single target only (per D1).** No multi-host artifacts. Multi-host operations are expressed as a `composite` whose sub-artifacts are single-target.

### 3.2 `idempotence` values

| Value | Meaning | Required for kinds |
|---|---|---|
| `via-precheck` | Spec includes prechecks the executor runs before each mutating step; if precheck says "already in desired state," step is skipped | `proxmox-api-sequence`, `http-sequence` |
| `declared-natural` | Agent claims the spec is naturally idempotent (e.g. an Ansible playbook with idempotent modules; a `PUT` with full state). Executor does best-effort verification (Ansible: `--check` first) but trusts the claim. | `ansible-playbook`, `shell-script` (with explicit preamble) |
| `replay-only` | Not idempotent; safe to apply once but not safely re-applicable. Executor refuses replay without `--force`. | Any kind, rare. |

---

## 4. Lifecycle state machine

```
                  propose_artifact()
                        │
                        ▼
                  ┌──────────┐
                  │ proposed │
                  └─┬─────┬──┘
              edit  │     │  reject
        (back to    │     ▼
         proposed)  │  ┌──────────┐
                    │  │ rejected │ (terminal)
                    │  └──────────┘
                  approve
                    │
                    ▼
                  ┌──────────┐
                  │ approved │
                  └─┬─────┬──┘
                    │     │
              executor    executor
              succeeds    fails
                    │     │
                    ▼     ▼
              ┌─────────┐  ┌────────┐
              │ applied │  │ failed │
              └─┬─────┬─┘  └───┬────┘
                │     │        │
        another │     │        │ user re-approves
        artifact│     │ revoke │ (only if idempotent)
        super-  │     │        │ OR revoke
        sedes   │     │        ▼
                │     │   (back to approved or revoked)
                ▼     ▼
        ┌────────────┐  ┌─────────┐
        │ superseded │  │ revoked │
        └────────────┘  └─────────┘

For kind=kb-note: skips approval gate.
                  propose_artifact()
                        │
                        ▼ (auto)
                  ┌─────────┐
                  │ applied │
                  └─────────┘
```

### Transitions

| From | To | Trigger | Notes |
|---|---|---|---|
| (none) | `proposed` | `propose_artifact(spec)` | Validation runs; on failure, no artifact written |
| `proposed` | `approved` | `hp artifacts approve <id>` (CLI) or web UI | Records `approved_by`. Hash re-verified at this step. |
| `proposed` | `rejected` | `hp artifacts reject <id> [--reason ...]` | Terminal. `rejected_by` recorded. |
| `proposed` | `proposed` | `hp artifacts edit <id>` | Body change recomputes hash; if `approved_by` was set (race: e.g. edit after approve), it's cleared. |
| `approved` | `applied` | Executor success on `hp artifacts apply <id>` | Records `applied_at`, appends `## Execution log` to body |
| `approved` | `failed` | Executor unrecoverable error during apply | Records `failed_at`, `failure_reason`, partial log appended |
| `failed` | `approved` | `hp artifacts approve <id>` again (must be idempotent) | New `approved_by` overwrites old. Refused if `idempotence: replay-only`. |
| `failed` | `revoked` | `hp artifacts revoke <id>` | Optionally executes rollback if hint present |
| `applied` | `superseded` | Auto: when another artifact with same `target` + same `intent semantic class` reaches `applied` | `superseded_by` set to the newer artifact ID |
| `applied` | `revoked` | `hp artifacts revoke <id>` | Optionally executes rollback if hint present |

### kb-note shortcut

For `kind: kb-note`, `propose_artifact` writes status `applied` directly. No `approved_by`, no executor run, no apply log. KB notes are non-mutating and exempt from review.

### Rejection is terminal

A rejected artifact stays in the repo for history but cannot be revived. To re-do the work, the agent produces a new artifact (which can `supersedes:` reference the rejected one for context).

### "Same intent semantic class"

Two artifacts auto-supersede when:
- Same `target.kind` and primary identifier (e.g. same `vmid`)
- Either: the new artifact lists the old in `supersedes:`, OR the same `tags` set within an active intent class

For v1, simplify: auto-supersede only when the new artifact explicitly lists the old in `supersedes:`. Anything more clever is a v1.x addition.

---

## 5. Per-kind contracts

Each subsection defines: where the spec body goes, what the executor does, the idempotence requirement, and the rollback format.

### 5.1 `kind: ansible-playbook`

**Body structure:**

```markdown
# <intent> (rendered by agent)

## Plan
<human-readable narrative the user reads during review>

## Inventory
<host(s) the playbook will run against; pulled from frontmatter target>

## Variables
<extra-vars passed to ansible-playbook, optional>

## Spec

​```yaml ansible-spec
- hosts: "{{ target_host }}"
  become: true
  tasks:
    - name: Install jellyfin
      apt:
        name: jellyfin
        state: present
    ...
​```

## Rollback   (optional)

​```yaml ansible-rollback
- hosts: "{{ target_host }}"
  become: true
  tasks:
    - name: Remove jellyfin
      apt:
        name: jellyfin
        state: absent
​```
```

**Required:** the `## Spec` section with a fenced ```` ```yaml ansible-spec ```` block containing a valid Ansible playbook list.

**Executor algorithm:**

1. Resolve `target.host` from frontmatter; look up its IP / SSH user from inventory cache.
2. Build a temporary inventory file pointing at that host. SSH uses direct connections (no jump server required) — managed hosts run `hp-agent` which connects back to the Agent Hub.
3. Resolve any extra-vars from the `## Variables` block.
4. Run `ansible-playbook --check --diff` first. Capture stdout/stderr.
5. If `--check` fails (parse error, unreachable host, missing module), abort with `failed` and the captured error.
6. Run `ansible-playbook` for real. Capture stdout/stderr/rc.
7. Append a `## Execution log` section to the artifact body containing: command line, dry-run diff, real-run output, return code, duration, timestamp.
8. If rc == 0, `applied`. Else, `failed`.

**Idempotence:** `declared-natural`. Ansible's promise. The `--check` dry-run is verification.

**Rollback:** another fenced block ```` ```yaml ansible-rollback ```` under `## Rollback`. Optional but encouraged. When `revoke_artifact` runs, executor runs the rollback playbook the same way.

### 5.2 `kind: proxmox-api-sequence`

**Body structure:**

```markdown
# <intent>

## Plan
<narrative>

## Spec

​```yaml proxmox-api-spec
steps:
  - id: create-lxc
    method: POST
    path: /nodes/{{ target.node }}/lxc
    body:
      vmid: "{{ target.vmid }}"
      ostemplate: "local:vztmpl/debian-13-standard_13.0-1_amd64.tar.zst"
      hostname: "{{ target.host }}"
      memory: 4096
      cores: 4
      net0: "name=eth0,bridge=vmbr1,ip=dhcp"
    precheck:
      method: GET
      path: /nodes/{{ target.node }}/lxc/{{ target.vmid }}/status/current
      skip_if: "response.status_code == 200"
    on_error: halt

  - id: start-lxc
    method: POST
    path: /nodes/{{ target.node }}/lxc/{{ target.vmid }}/status/start
    precheck:
      method: GET
      path: /nodes/{{ target.node }}/lxc/{{ target.vmid }}/status/current
      skip_if: "response.json['data']['status'] == 'running'"
    on_error: halt
​```

## Rollback   (optional)

​```yaml proxmox-api-rollback
steps:
  - id: stop-lxc
    method: POST
    path: /nodes/{{ target.node }}/lxc/{{ target.vmid }}/status/stop
    on_error: continue
  - id: delete-lxc
    method: DELETE
    path: /nodes/{{ target.node }}/lxc/{{ target.vmid }}
    on_error: continue
​```
```

**Step shape:**

```yaml
- id: <unique-step-id>           # required
  method: GET | POST | PUT | DELETE
  path: <relative-path>          # Jinja2-interpolated; see D2 for variables
  body: <object>                 # optional, for POST/PUT; values may be Jinja2-interpolated
  precheck:                      # required for mutating steps unless idempotence: declared-natural
    method: GET
    path: <relative-path>
    skip_if: <python expression evaluated in {response, target} context>
  on_error: halt | continue      # default halt
```

For `target.kind: cluster` artifacts (datacenter-level config), the executor picks any one healthy PVE node from inventory and sends the call there; pmxcfs replicates. Paths in this case use `/access/...`, `/cluster/...`, etc., which don't have a `{{ target.node }}` segment.

**Executor algorithm:**

1. Resolve PVE token from vault. Key lookup: prefer `proxmox-{node}`, fall back to `proxmox-cluster`.
2. Open httpx client to `https://{pve-host}:8006/api2/json/`, auth via header `Authorization: PVEAPIToken={token}`.
3. For each step in order:
   a. Interpolate `path` using `target` fields.
   b. If `precheck` present:
      - GET the precheck path.
      - Evaluate `skip_if` expression with a sanitised `response` proxy and `target` in scope. Allowed attributes: `response.status_code` (int), `response.headers` (dict, auth/cookie keys excluded), `response.json` (pre-parsed body dict/list/None — use subscript access). Function calls and private/dunder attributes are blocked.
      - If true: log "skipped" and continue to next step.
   c. Else (or precheck didn't skip): send `method path` with `body` JSON-encoded.
   d. Capture status, headers (filtered), body.
   e. If status >= 400 and `on_error: halt`: stop. Mark `failed`.
4. Append `## Execution log` with each step's outcome (skipped / ok / error) and timing.

**Idempotence:** `via-precheck` is the default and required for mutating steps. `declared-natural` allowed only for steps that are demonstrably state-replacing (e.g. `PUT /access/domains/authentik` with full state).

**Rollback:** another ```` ```yaml proxmox-api-rollback ```` block. Same step shape. `on_error: continue` is typical for rollback (best-effort cleanup).

### 5.3 `kind: http-sequence`

Same shape as `proxmox-api-sequence`, but each step has `name:` for credential resolution instead of relying on a global node/cluster token.

**Body structure:**

```yaml http-spec
steps:
  - id: create-authentik-provider
    name: authentik-admin           # vault key: { base_url, headers: { Authorization: "Bearer ..." } }
    method: POST
    path: /api/v3/providers/oauth2/
    body:
      name: homepilot-pve
      client_type: confidential
      ...
    precheck:
      name: authentik-admin
      method: GET
      path: /api/v3/providers/oauth2/?name=homepilot-pve
      skip_if: "response.json['count'] > 0"
    on_error: halt
```

**Vault entry shape:**

When the executor resolves `name: authentik-admin`, the vault returns:

```json
{
  "base_url": "https://auth.example.lan",
  "headers": { "Authorization": "Bearer <token>" },
  "verify_tls": true
}
```

**Executor algorithm:** identical to `proxmox-api-sequence` except per-step credential resolution. Each step may use a different `name` if the artifact crosses multiple services (rare; usually a `composite` is cleaner).

**Idempotence:** `via-precheck` required for mutating steps.

**Rollback:** ```` ```yaml http-rollback ```` block.

### 5.4 `kind: composite`

A composite artifact references other artifacts and runs them in order. Used when a single user intent spans multiple kinds (create VM via proxmox-api, then configure it via ansible, then register it in Authentik via http).

**Body structure:**

```markdown
# <intent>

## Plan
<narrative; describes the whole flow at a high level>

## Spec

​```yaml composite-spec
steps:
  - id: provision
    artifact: 2026-05-06-jellyfin-create-lxc      # kind: proxmox-api-sequence
    on_error: halt

  - id: configure
    artifact: 2026-05-06-jellyfin-ansible-config  # kind: ansible-playbook
    depends_on: [provision]
    on_error: halt

  - id: register-in-authentik
    artifact: 2026-05-06-jellyfin-authentik       # kind: http-sequence
    depends_on: [configure]
    on_error: halt

  - id: kb-update
    artifact: 2026-05-06-jellyfin-deployed-note   # kind: kb-note
    depends_on: [register-in-authentik]
    on_error: continue
​```
```

**Constraints at propose time:**
- All referenced sub-artifacts must exist with status `proposed` or `approved`.
- A composite cannot reference an `applied`, `revoked`, `superseded`, `rejected`, or `failed` sub-artifact.
- A composite cannot reference itself (no recursion).
- Cycles in `depends_on` are rejected.

**Executor algorithm:**

1. Topologically sort steps by `depends_on`.
2. For each step in topological order:
   a. Look up sub-artifact by ID.
   b. If sub-artifact status is `applied` already (e.g. user applied it manually before), log "already-applied" and continue.
   c. Else: apply the sub-artifact (recursively run §5.1 / 5.2 / 5.3 / 5.5 / 5.6 algorithm).
   d. On sub-artifact failure: if `on_error: halt`, abort the composite and mark composite `failed`.
3. Composite status flips to `applied` when all `halt`-on-error steps have succeeded.
4. Append `## Execution log` at the composite level summarizing each step's outcome.

**Idempotence:** inherited from sub-artifacts. The composite itself doesn't introduce new mutations.

**Rollback:** walk steps in reverse topological order; for each sub-artifact whose final status is `applied`, run its rollback hint if present. Continue past failures (best-effort).

**Approval semantics for composites:** approving a composite implicitly approves all referenced `proposed` sub-artifacts that aren't already `approved`. Records this in audit log.

**Composite invalidation on sub-artifact edit (per D4):** if any sub-artifact's body changes (via `hp artifacts edit`) while the composite is `approved`, the composite is automatically flipped back to `proposed` and `approved_by` is cleared. The user must re-review. Cascade is propagated through composites-of-composites. Each invalidation produces an audit log entry: `{ action: "invalidate", artifact_id: <composite>, reason: "sub-artifact <id> edited" }`.

### 5.5 `kind: shell-script`

Last resort for ad-hoc operations the structured kinds don't fit. Discouraged; agent system prompt steers it toward the structured kinds.

**Body structure:**

```markdown
# <intent>

## Plan
<narrative>

## Idempotence preamble
<MANDATORY. Plain English explanation of how the script is idempotent
or what state it expects on entry. Example:
"This script checks for the presence of /etc/foo before writing it.
If /etc/foo exists, no changes are made. The script can safely be
run multiple times.">

## Spec

​```bash shell-spec
#!/bin/bash
set -euo pipefail

if [ ! -f /etc/foo ]; then
  echo "writing /etc/foo"
  cat > /etc/foo <<EOF
some config
EOF
fi
​```

## Rollback   (optional, encouraged)

​```bash shell-rollback
#!/bin/bash
set -euo pipefail
rm -f /etc/foo
​```
```

**Required:** `## Idempotence preamble` MUST be present and non-empty. The agent is required by tool-wrapper validation to fill this in.

**Executor algorithm:**

1. Resolve `target.host`.
2. SSH to target (direct, no jump server — managed hosts use `hp-agent` which connects via the Agent Hub).
3. Stream the shell-spec block to `bash -s`. The script is fed via stdin so no temp file is created on the target.
4. Capture stdout/stderr/exit code.
5. Append `## Execution log` with command, output, exit, duration, timestamp.
6. exit == 0 → `applied`. Else → `failed`.

**Idempotence:** `declared-natural` — agent's responsibility, documented in the preamble.

**Rollback:** ```` ```bash shell-rollback ```` block. Optional but strongly encouraged.

**Restriction:** `target.host` must NOT be the PVE node itself (no SSH to PVE — see PLAN_V2 §"What v1 is *not*"). Tool wrapper enforces this at propose time.

### 5.6 `kind: kb-note`

Knowledge base entry. Non-mutating. No executor side effects on the homelab.

**Body structure:**

```markdown
# <intent>

<free-form Markdown body>

## Tags
- target: media-lxc
- topic: hardware-transcode
- kind-of-note: note | policy | decision
```

**Frontmatter additions:**

```yaml
note_kind: note | policy | decision
```

- `note` — durable fact ("Jellyfin uses /dev/dri")
- `policy` — preference / default ("Prefer LXC over VM for stateless services")
- `decision` — recorded choice with reasoning ("Chose Caddy over Traefik in March 2026 because…")

**Executor algorithm:**

1. Validate body is non-empty Markdown.
2. Index in SQLite: insert into `kb_entries (id, target, note_kind, content, embedding, applied_at)`.
3. Compute embedding via local model (sqlite-vec) if available; fall back to keyword-only if not.
4. No external side effects.

**Idempotence:** N/A (`idempotence: not-applicable` allowed for kb-note only).

**Auto-applies on propose.** No approval gate. The user can still edit (`hp artifacts edit`) or revoke later, which sets the index entry to inactive.

**Embeddings are not stored in the artifact file (per D5).** They live only in the SQLite index. To rebuild after a backup restore or model migration, run `hp kb reindex`. This walks every `applied` `kb-note` artifact, recomputes embeddings, and rewrites the SQLite index. `hp export` does NOT include embeddings; the export README points at `hp kb reindex` as the post-restore step.

---

## 6. Composite semantics

Already covered in §5.4. Recapping for findability:

- Steps reference sub-artifact IDs.
- Sub-artifacts must be `proposed` or `approved` at composite propose time.
- Cycles rejected.
- Topological ordering by `depends_on`; sequential as listed when no deps.
- Approving a composite cascades approval to its sub-artifacts.
- Composite executor walks the DAG; sub-artifact failures halt or continue per `on_error`.
- Rollback walks the DAG in reverse, best-effort.

Edge cases:

- **Sub-artifact applied between composite propose and composite apply:** treat as already-done, skip; no error.
- **Sub-artifact rejected after composite proposed:** composite cannot proceed; user is prompted to edit or reject the composite.
- **Sub-artifact superseded:** composite cannot proceed; user is prompted to edit (point at the new sub-artifact) or reject.

---

## 7. Approval semantics

### Approval is a recorded human action

Every transition `proposed → approved` records:

```yaml
approved_by:
  user: <user-id from API token or web session>
  at: <ISO8601 timestamp>
  reason: <optional one-line explanation>
```

### Audit log entry

Every approval / rejection / apply / revoke writes a row to `audit_log`:

```
{ user, action, artifact_id, at, source: "cli" | "ui", request_id }
```

The audit log is independent of the artifact's frontmatter (which can be edited). It is append-only.

### Rendering during review

`hp artifacts show <id>` and the web UI present the full artifact in this order:
1. Frontmatter as a small table (intent, kind, target, status, mutating, hash)
2. The Plan section (narrative)
3. The Spec section (with syntax highlighting)
4. The Rollback section if present
5. Idempotence preamble (for shell-script)
6. A **diff preview** if possible:
   - For `ansible-playbook`: `--check --diff` output
   - For `proxmox-api-sequence`: each precheck's current response
   - For `http-sequence`: same
7. Hash and produced_by metadata at the bottom

The diff preview is best-effort: it queries current state via the read-only equivalents of the executor's tools and shows what would change. If the preview itself errors (target unreachable, vault locked), that's reported but does not block approval.

### Re-approval after `failed`

If status is `failed`, `hp artifacts approve <id>` is allowed *only if* `idempotence` is `via-precheck` or `declared-natural`. For `replay-only`, refuse with an explanation; user must explicitly `hp artifacts revoke` and the agent should produce a fresh artifact.

### Combining approve + apply

`hp artifacts apply <id>` accepts `--approve` to do both transitions atomically (still writes both audit log entries, still records `approved_by`). Convenience for trusted, frequent operations.

`hp artifacts apply <id>` without `--approve` requires the artifact to already be `approved`.

### Composite approval

Approving a composite cascades: every referenced sub-artifact in `proposed` status flips to `approved`, with the composite's `approved_by` reason copied. Cascade is recorded as separate audit log entries (one per sub-artifact). Cascade is atomic — if any sub-artifact fails to approve (e.g. it was edited and the hash is now stale), the whole cascade is rolled back and the composite stays `proposed`.

---

## 8. Editing artifacts

`hp artifacts edit <id>`:

1. Open the file in `$EDITOR` (or write the file's path to stdout for piping).
2. On save, recompute hash from the body.
3. If body changed:
   - Status drops from `approved` back to `proposed` (if it was `approved`).
   - Status drops from `failed` back to `proposed`.
   - `approved_by` cleared if previously set.
   - **Composite invalidation cascade (per D4):** every `approved` composite that references this artifact in its `steps` is also flipped back to `proposed`, with `approved_by` cleared. Cascade propagates through composites-of-composites. Each invalidation produces an `audit: invalidate` log entry.
4. Frontmatter-only edits (e.g. adding a `tags:` entry) leave the hash and status unchanged. (Lifecycle-managed frontmatter fields like `status`, `approved_by`, `applied_at` are written by HomePilot, not by the user.)

Editing `applied` / `superseded` / `revoked` / `rejected` artifacts is rejected. Those are historical; create a new artifact instead.

**Manual git edits outside `hp artifacts edit` (per D7):** HomePilot reads the working tree on every operation, no locking. If you `vim` an artifact file directly:
- For a `proposed` artifact: HomePilot recomputes the hash on next read and logs `audit: hash-recomputed`. No status change.
- For an `approved` artifact: the hash mismatch is caught at apply time (§10) and apply is refused. The user must `hp artifacts approve` again (which re-verifies hash) or revert the manual edit.
- For an `applied` artifact: undefined behavior. Don't.
- For structural changes (renaming the file, changing the `id` field, changing the `kind`): undefined behavior. Don't.

Documented in `~/.hp/artifacts/README.md` written by `hp init`.

---

## 9. File layout details

```
~/.hp/artifacts/
├── .git/                              # the git repo
├── 2026/
│   ├── 04/
│   │   ├── 2026-04-12-jellyfin-deploy.md
│   │   └── 2026-04-15-bump-jellyfin-ram.md
│   └── 05/
│       ├── 2026-05-06-deploy-media-lxc.md
│       ├── 2026-05-06-jellyfin-create-lxc.md   # sub-artifact of composite
│       ├── 2026-05-06-jellyfin-ansible-config.md
│       └── 2026-05-06-pve-authentik-oidc.md
└── README.md                          # generated by hp init; explains layout to git-clone-only readers
```

The auto-generated `README.md` describes the spec in human terms so a future you (or anyone with `git clone` and no HomePilot) can understand the directory.

---

## 10. Hash and tamper-detection

### What is hashed

The hash covers the artifact body — *everything after the closing `---` of frontmatter*, including trailing whitespace, with one normalization: trailing whitespace on each line is stripped, and the file is treated as ending with exactly one newline.

`hash` value: `sha256:<64-hex-chars>`.

### When hash is computed

- At `propose_artifact`: hash of the body the agent submitted.
- At `hp artifacts edit`: recomputed; if changed, status drops to `proposed`.
- At `hp artifacts approve`: recomputed and verified against stored. Mismatch = the body was edited outside HomePilot's flow; refuse approval, prompt user to reload or edit.
- At `hp artifacts apply`: recomputed and verified. Mismatch = abort with `tampered` error; this is *not* a `failed` status (which implies executor ran), it's a refusal-to-run.

### Why

Without hash verification, a user (or a malicious editor) could approve artifact A, then edit the body to do something else, then apply. Hash binds the approval to the exact body the user reviewed.

The hash is in the frontmatter, not a separate file, because the frontmatter is what's loaded everywhere. Storing the hash in the frontmatter means lifecycle transitions (which only edit frontmatter) don't invalidate the hash; but body edits do.

### Edge case: line-ending normalization

The git repo enforces LF line endings via `.gitattributes`:

```
*.md text eol=lf
```

This avoids hash mismatches across editors / OSes that introduce CRLF.

---

## 11. Validation at propose time

When the agent calls `propose_artifact(spec)`, HomePilot validates before writing:

1. **Frontmatter schema:** every required field present, types match, enums valid (incl. `kind`, `status` initial = `proposed` only, `target.kind` ∈ `{vm, lxc, node, cluster, service, network, global}` per D8).
2. **`target` sub-field requirements** match the per-kind table in §3.1 (e.g. `kind: cluster` MUST NOT have a `node:` set; `kind: vm` MUST have both `vmid:` and `node:`).
3. **ID format:** matches `YYYY-MM-DD-[a-z0-9-]{1,60}(-[a-f0-9]{6})?`.
4. **ID uniqueness:** doesn't already exist in repo.
5. **Body parses:** Markdown body parses; for kinds with structured spec blocks, the spec block parses as YAML/bash.
6. **Jinja2 interpolation safety (per D2):** all `{{ ... }}` expressions in the spec body resolve against the allowed context (`target.*`, `vault.<name>` non-secret fields, `now`, `artifact.id`, `artifact.intent`); references to other names are rejected. No `{% %}` control blocks.
7. **Per-kind requirements:**
   - `ansible-playbook`: `## Spec` block present and parses as a YAML list of plays.
   - `proxmox-api-sequence`: `steps:` present, every step has required fields, every mutating step has a `precheck` unless `idempotence: declared-natural`. For `target.kind: cluster` artifacts: paths must NOT contain `{{ target.node }}` (the executor picks the node).
   - `http-sequence`: same; every `name:` resolves to an existing vault key.
   - `composite`: every `artifact:` reference resolves to an existing `proposed` or `approved` artifact; no cycles; single-target rule of D1 is satisfied (each sub-artifact has its own `target`).
   - `shell-script`: `## Idempotence preamble` non-empty; target host is not a PVE node (`target.kind` ∈ `{vm, lxc}` only — never `node` or `cluster`).
   - `kb-note`: body non-empty; `note_kind` set.
8. **`replay_safe: false` requires explanation (per D3):** if `replay_safe: false` is set, the body MUST contain a `## Why not replay-safe` section. Validator rejects otherwise.
9. **Hash computed and added to frontmatter.**
10. **File written to `<root>/<YYYY>/<MM>/<id>.md` and committed.**

Validation failure = `propose_artifact` returns an error to the agent with the specific failure; no file is written, no commit made. The agent is expected to fix and re-propose.

---

## 12. Decisions

These were the open questions in v0.1 of this spec. Resolved as follows. They are now binding.

### D1. One target per artifact
**Decision:** an artifact has exactly one `target`. Multi-target operations ("patch all production VMs") are expressed as a `composite` artifact whose sub-artifacts are single-target.
**Why:** keeps the executor's per-kind algorithm simple, makes per-artifact rollback meaningful, and forces the agent to reason about each target individually rather than batch-and-pray.
**Implication:** the executor never has to handle "step succeeded on host A, failed on host B"; each host is its own artifact with its own success/failure state. The composite captures the fan-out.
**Reconsider:** v1.x if 50-host fleet patches become routine and the composite-of-50 noise is real. Not a v1 problem.

### D2. Jinja2-style variable interpolation, consistently
**Decision:** all path / value interpolation in spec bodies uses Jinja2 syntax: `{{ target.node }}`, `{{ target.vmid }}`, `{{ target.host }}`. No `{node}` / `${node}` / other forms.
**Why:** matches Ansible's mental model (which uses Jinja2 natively); single template engine across all kinds; well-documented; safe against shell-style misinterpretation.
**Available variables in interpolation context:**
- `target.*` — every field of the artifact's `target` object
- `vault.<name>` — read-only access to vault entries' `base_url` / non-secret metadata (NEVER the auth token directly; the executor injects auth as a header, not as a substituted value)
- `now` — ISO8601 timestamp at apply time
- `artifact.id`, `artifact.intent` — for log lines
**No conditionals, no loops** in the interpolation context. If logic is needed, the agent produces multiple steps or chooses a different `kind` (e.g. `ansible-playbook` for branching).

**`skip_if` expression context** (§5.2 `proxmox-api-sequence`, §5.3 `http-sequence`): separate from Jinja2 interpolation, evaluated by a restricted AST sandbox with these bindings only:
- `response.status_code` — int HTTP status of the precheck response
- `response.headers` — dict of response headers; auth/cookie headers (`authorization`, `cookie`, `set-cookie`) are excluded
- `response.json` — pre-parsed JSON body (dict, list, or None); navigate with subscript: `response.json["key"]`
- `target` — artifact's target dict; use subscript: `target["host"]`

Function calls (including `.get()`, `.json()`) and private/dunder attributes are blocked by the evaluator. Use subscript notation for all data access: `response.json["status"] == "running"` not `response.json().get("status") == "running"`.

### D3. `replay_safe: false` artifacts cannot be replayed, period
**Decision:** if an artifact has `replay_safe: false`, `hp artifacts replay <id>` refuses with an explanation. To re-do the operation, the user revokes the artifact (running rollback if present) and the agent produces a fresh artifact.
**Why:** sharper than "requires `--force`." Removes the temptation to bypass; makes the audit trail clean (the new artifact records the new intent).
**Used for:** one-time-only operations like generating a unique identity, registering a one-time secret, performing a destructive migration.
**Default:** `replay_safe: true`. Agent must explicitly set `false` and explain in the artifact body why it's not replay-safe.

### D4. Editing a sub-artifact invalidates composite approval
**Decision:** when an `approved` sub-artifact's body changes (via `hp artifacts edit`), every `approved` composite that references it is also flipped back to `proposed`. The user must re-review the composite.
**Why:** the user approved a specific bundle of work; changing any piece of it changes what they approved. Better to force a quick re-review than to silently apply a different plan.
**Cascade:** invalidation propagates: composite-of-composite is also invalidated. Cycles are already rejected at propose time so this terminates.
**Audit:** each invalidation is logged with `{ action: "invalidate", artifact_id, reason: "sub-artifact <id> edited" }`.

### D5. KB embeddings are derived, not artifact content
**Decision:** embeddings live in SQLite (sqlite-vec) only. The artifact file contains the human-readable Markdown body; embeddings are computed on `apply` and on `hp kb reindex`.
**Why:** keeps artifact files portable (someone with `git clone` and no embedding model can still read them); embeddings are model-dependent and regenerating from text is fast.
**Reindex path:** `hp kb reindex` re-embeds every applied `kb-note` artifact. Run after restoring from a backup, after changing the embedding model, or after migrating to a different host.
**Consequence:** `hp export` does NOT include embeddings. The export README points at `hp kb reindex` as the post-restore step.

### D6. Audit log is SQLite-only
**Decision:** the audit log lives in a SQLite table, not in a git-tracked file.
**Why:** cheaper writes (no commit per audit entry), still backed up via `hp export` (which dumps the DB), still queryable. Git history of artifact files already provides a separate immutable record of the artifact lifecycle.
**Schema:** `audit_log (id, user, action, artifact_id, at, source, request_id, details_json)`.
**Export:** the audit log travels inside the `homepilot.db` snapshot in the `hp export` tarball; there is no separate `audit_log.jsonl`.

### D7. Working tree is truth; manual git edits are unsupported
**Decision:** HomePilot reads the working tree on every operation. It does not fight git. If the user manually edits an artifact file in the repo, the next HomePilot read sees the edit. There is no locking, no mtime check, no merge resolution.
**Why:** the artifacts repo is a normal git repo; users should be able to `git log` / `git diff` / `git revert` it without HomePilot's permission. Reinventing locking would lose that.
**Risk:** a manual edit to an `approved` artifact between approval and apply will be caught by the hash check (§10) and refused. A manual edit to a `proposed` artifact will silently succeed but produce a hash mismatch on next read; HomePilot updates the hash and logs `audit: hash-recomputed`. Bigger structural edits (renaming a file, changing the `id` field) are undefined behavior; document this.
**Documented in:** the auto-generated `~/.hp/artifacts/README.md` written at `hp init`.

### D8. New `target.kind: cluster` for datacenter-level Proxmox config
**Decision:** target kinds are `vm | lxc | node | cluster | service | network | global`.
- `vm` / `lxc` — a specific guest, identified by `vmid` (+ `node` for Proxmox addressing)
- `node` — a Proxmox node, identified by `node` name; for node-level config (network bridges, node firewall, node apt updates via API)
- `cluster` — Proxmox datacenter / cluster as a whole; for shared config (realms, ACLs, datacenter firewall, storage pools)
- `service` — a logical service, identified by `service` name; for service-level docs and notes
- `network` — a bridge / VLAN / SDN zone, identified by `network` name
- `global` — affects the platform itself or has no single target (e.g. "rotate master vault passphrase," "reindex KB")
**Why:** `cluster`-vs-`node` distinction matters because Proxmox config like OIDC realms live at the datacenter level (in `/etc/pve/`, replicated across nodes via pmxcfs) and only need to be applied once. A cluster-targeted artifact's executor sends the API call to any one healthy node and lets pmxcfs replicate.
**Removed:** `host` from the kind enum (was ambiguous with the `host:` sub-field). `host:` remains as a sub-field of `target` for "the inventory hostname" when relevant (e.g. for `vm` / `lxc` targets, `host: media-lxc` is the friendly name).

---

## 13. Examples

### Example 1: simple proxmox-api-sequence

```markdown
---
id: 2026-05-06-snapshot-jellyfin
kind: proxmox-api-sequence
intent: "Take a pre-upgrade snapshot of media-lxc"
status: proposed
mutating: true
target: { kind: lxc, vmid: 142, node: pve1, host: media-lxc }
idempotence: via-precheck
produced_by: { session: chat-2026-05-06-abc, agent: claude-opus-4-7, user: olli, at: "2026-05-06T14:32:00Z" }
hash: sha256:a3f201...
---

# Take pre-upgrade snapshot of media-lxc

## Plan
Snapshot the LXC before next week's upgrade so we can roll back if needed.

## Spec

​```yaml proxmox-api-spec
steps:
  - id: snapshot
    method: POST
    path: /nodes/{{ target.node }}/lxc/{{ target.vmid }}/snapshot
    body:
      snapname: "pre-upgrade-2026-05-06"
      description: "Pre-upgrade snapshot, can prune after 2026-05-20"
    precheck:
      method: GET
      path: /nodes/{{ target.node }}/lxc/{{ target.vmid }}/snapshot
      skip_if: "response.status_code == 200"
    on_error: halt
​```
```

### Example 2: composite for full deploy

(Shown in §5.4 above.)

### Example 3: cluster-targeted Proxmox config (datacenter-level OIDC realm)

```markdown
---
id: 2026-05-06-pve-authentik-oidc
kind: proxmox-api-sequence
intent: "Add Authentik as OIDC realm at the Proxmox cluster level"
status: proposed
mutating: true
target: { kind: cluster }
idempotence: via-precheck
produced_by: { session: chat-2026-05-06-abc, agent: claude-opus-4-7, user: olli, at: "2026-05-06T14:32:00Z" }
hash: sha256:c4d189...
---

# Add Authentik OIDC realm to Proxmox

## Plan
Configure PVE to accept logins from Authentik via OIDC. This is a datacenter-level
config (lives in /etc/pve/domains.cfg, replicated via pmxcfs); only needs one API call.

## Spec

​```yaml proxmox-api-spec
steps:
  - id: create-realm
    method: POST
    path: /access/domains
    body:
      realm: authentik
      type: openid
      issuer-url: https://auth.example.lan/application/o/homepilot-pve/
      client-id: "{{ vault.authentik_pve_client_id }}"
      client-key: "{{ vault.authentik_pve_client_secret }}"
      username-claim: email
      autocreate: 1
      default: 0
    precheck:
      method: GET
      path: /access/domains/authentik
      skip_if: "response.status_code == 200"
    on_error: halt
​```

## Rollback

​```yaml proxmox-api-rollback
steps:
  - id: delete-realm
    method: DELETE
    path: /access/domains/authentik
    on_error: continue
​```
```

Note `target: { kind: cluster }` — no `node:` set, per D8. Executor picks any healthy node from inventory and sends the call there; pmxcfs replicates `/etc/pve/domains.cfg` to all nodes.

### Example 4: kb-note (policy)

```markdown
---
id: 2026-05-06-policy-default-storage
kind: kb-note
note_kind: policy
intent: "Default storage pool is nvme-pool for new VMs/LXCs"
status: applied
mutating: false
produced_by: { session: chat-2026-05-06-abc, agent: claude-opus-4-7, user: olli, at: "2026-05-06T14:35:00Z" }
hash: sha256:b9c842...
applied_at: "2026-05-06T14:35:00Z"
tags: [policy, storage]
---

# Default storage pool: nvme-pool

When creating new VMs or LXCs without an explicit storage pool, use `nvme-pool`.

## Reasoning
nvme-pool is the SSD pool; rotational pool is for backups/cold data.
Putting active workloads on rotational kills IOPS.

## Exceptions
- Backup volumes go to `hdd-pool`
- Media library data is on the NAS (not Proxmox storage)
```

---

## 14. What this enables

Once this spec is locked, the v1 build is mostly mechanical:

- **Agent prompt:** the system prompt for opencode/Claude Code references this spec by section. "When proposing an `ansible-playbook` artifact, follow §5.1: include `## Spec`, declare `idempotence: declared-natural`, optional `## Rollback`."
- **Executor:** one Python module per kind. Each implements the algorithm in its §5.x section. ~200–400 lines per kind.
- **Validator:** one function per kind, implementing §11 checks. Pure, testable.
- **Web UI rendering:** one component per kind, rendering the body sections in §7 review order.
- **Tests:** every example in §13 should round-trip through validate → write → read → render → simulate-apply without errors.

The previous build's failure mode was "many features, blurry contracts." This spec is the contract. Implement against it, not around it.
