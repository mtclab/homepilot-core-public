# Facelift v2 - density, hierarchy, and configure-from-the-product (#549 + #553)

Owner mandate 2026-08-25: the 3.4 UI is "too cluttered and unintuitive...
long lists of things"; sectors need clean differentiation (owner suggestion:
tabs per page); optional subsystems and the VPS-hosting networking must be
configurable from the UI (and, non-secret parts, over MCP). Built first,
reviewed by the owner after (explicit owner sequencing). This doc is the
binding design the slices are built against. It does NOT reopen #514's
decisions (5-noun nav, one-noun Host, monitoring-is-a-home) - it is the
layer above: how each page organizes and breathes, and where configuration
lives.

## Design principles (from repot/branding IDENTITY_SYSTEM + the owner's words)

1. **Attention before enumeration.** Every page leads with what needs an
   operator's eyes NOW (drift, refusals, failures, proposals awaiting review,
   firing alerts), rolled up healthy state second, and the full inventory
   third - collapsed or paged, never an unbounded scroll by default.
2. **Tabs differentiate sectors within a page** (owner suggestion, ADOPTED).
   Changes and Records already carry the #514 tab-bar sub-layout; the same
   pattern extends to the Host page and Settings. One shared TabBar component,
   URL-addressable (?tab= or subroutes), keyboard-reachable. Overview is the
   exception: it is the at-a-glance home - zones, not tabs.
3. **White-space discipline is the fingerprint.** Looser card padding, a
   consistent vertical rhythm scale, reading measure on prose, calm
   line-height (~1.6). Cramped = off-brand (the suit does not squint).
4. **Tables only where rows are genuinely comparable**, and then with a
   primary column (name + state chip), secondary data in muted sans, row
   actions in one overflow menu (P7 vocabulary). Everything else becomes
   grouped sections, definition lists, or summary chips with disclosure.
5. **Civic/data house rules hold**: near-black field, ONE ember accent
   (semantic colour separate), serif reading register, no gradients, no
   uniform auto-fill card grids, no uppercase eyebrows, "Built by MTC Lab"
   mark stays.

## Per-surface design

### F2 Overview - the operator's morning glance
Three zones, in priority order, no tabs:
1. **Needs attention** (only renders when non-empty): proposed artifacts
   awaiting review, drifting artifacts, failed/stuck tasks, disconnected
   agents, firing alerts - each a one-line item linking straight to the fix
   surface. Empty = one calm line "Nothing needs you."
2. **Fleet at a glance**: the host chips strip (state + headline metric),
   coverage, and the honest counts (P6 rules hold).
3. **Recent movement**: last N tasks/journal entries as one merged, compact
   feed with a "Records" door. StatCard grid shrinks to the numbers that
   drive action; "Hosts by role" moves into Hosts.

### F3 Hosts - the fleet page + the host page
List: grouped by state (needs-attention first: offline/agent-refused/drifted,
then healthy managed, then discovered/unadopted), group headers with counts,
collapsed-by-default for healthy groups when the fleet is large; the P4 bulk
mechanics stay. Search stays server-side.
Host page: **tabs** - Overview (identity dl + headline metrics + attention
items for this host) / Metrics (chart grid) / Changes / Activity / Agent.
Deep links keep working; the legacy anchor sections 301 into tabs.

### F4 Changes - one lifecycle, attention-first
Queue tab leads with proposed-awaiting-review as CARDS (kind, target,
plan summary, approval code panel) - a review queue, not a table. Applied
history is the table, paged. Drift tab: disagreeing artifacts only, each with
its plan diff summary; healthy checked artifacts are ONE summary line
("41 in spec, checked 12m ago"), never enumerated. 0-checked honesty holds.

### F5 Records - grouped chronology, not endless lists
Tasks: group by day; each row = action + target + state chip; running/failed
pinned to top regardless of day; log behind the existing toggle.
Journal: same day-grouping, entry kind chips, compact single-line entries
expanding to detail.
KB: grouped by kind (note/policy/doc), search stays primary; each entry
one line + disclosure.

### F6 Settings - tabs + configure-from-the-product (the #553 seam)
Tabs: **Connection** (API + health) / **Proxmox** / **Subsystems** /
**Guests** / **Monitoring** / **Tokens** / **About**.
The Subsystems tab is the #553 centerpiece - per subsystem (archive push,
KB embedding, webhooks/events, metrics retention, portal/guest hosting):
- status chip driven by selfcheck, truthful ("failing: <reason>"), never grey mystery;
- non-secret settings editable in place (persisted server-side, see C2);
- secret values (keys, tokens) settable UI-only into the vault, shown as
  configured/not-configured + source, never echoed back;
- a probe button where wiring can be verified without secrets
  (pattern: test_proxmox_connection).

## The configuration backend (#553 slices)

### C2 - persisted settings with explicit precedence
Settings move to the DB settings table; env stays as bootstrap/override.
Precedence, binding: **explicit env var wins and records nothing; otherwise
the DB value; otherwise the code default** (the hub_tls_mode precedent).
Each setting declares hot-reloadable or restart-required; the UI labels
restart-required honestly. First movers: archive push (remote, interval),
KB embedding (URL, model), metrics retention, events webhook (URL; secret
stays vault).
### C3 - VPS-hosting / provisioning defaults
First-class persisted settings: default node, template vmid, pool, bridge,
VLAN tag, ipconfig pattern for guest provisioning. Consumed by
provision_guest + invite redemption so an invite stops carrying raw infra
details. Each has a **live validation probe** against the cluster (template
exists; bridge/VLAN present on the node; token can see the pool) - refuse to
save a value the cluster refutes, with the cluster's answer shown.
### C4 - MCP admin-tier setters for the NON-SECRET subset
get/set tools for the C2+C3 settings at admin tier (tier gate enforces).
Secret values stay UI-only (standing owner rule). Probes exposed read-only.

## Slices and gates

| Slice | Scope | Gate (assert the GOAL) |
|---|---|---|
| F1 | TabBar unification + density/rhythm tokens + table primary/secondary pattern | tab pattern URL-addressable + keyboard; svelte-check/vitest green; no visual-regression of Changes/Records tabs |
| F2 | Overview zones | attention zone lists a seeded drifting artifact + failed task with working links; empty state calm line |
| F3 | Hosts grouping + host-page tabs | grouped fleet renders + healthy group collapsed at >N; every legacy host-page deep link lands on the right tab |
| F4 | Changes queue cards + drift rollup | a seeded proposed artifact renders as a card w/ approval code; healthy drift is ONE line |
| F5 | Records grouping | seeded multi-day tasks group by day with failed pinned first |
| F6 | Settings tabs + subsystem status cards | every subsystem shows a truthful status sourced from selfcheck; failing one names its reason |
| C2 | settings persistence + archive/embedding/retention/webhook editable | set archive interval in UI -> reconciler actually reschedules (journey); env override wins (gated) |
| C3 | provisioning defaults + probes | save a bad bridge -> refused with cluster answer; invite consumes defaults |
| C4 | MCP setters | tier gate green; set_archive_interval over admin MCP -> same journey as C2; secret settings NOT reachable (gated) |

Order: F1 first (foundation). F2-F5 parallel (disjoint routes, consume F1
only). F6 next (touches Settings). C2 -> C3 -> C4 sequential (backend
stack). Everything ships behind the standing gates (svelte-check
--fail-on-warnings, vitest, gate-py, tier gate, description-truth), each
slice walked on the real build before merge.
