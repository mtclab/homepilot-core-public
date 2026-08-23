# Operator console replan (DECIDED 2026-08-23)

Owner decisions, recorded from the discussion of the 2026-08-23 UI walk
(all four went with the recommendation):

- **D1: the Agents tab dies.** Agents fold into Hosts; agent plumbing
  (credential, transport, refusal history) lives on the host page's Agent section.
- **D2: one Changes tab.** Artifacts + Review + Drift are one lifecycle —
  the queue is artifacts filtered to `proposed`, drift is artifacts whose
  reality disagrees.
- **D3: monitoring gets a home, not a product.** Overview fleet strip +
  host-page charts. No fleet-compare dashboards, no retention UI, ever,
  without a new owner decision.
- **D4: one release.** S1-S4 build on this branch and ship as **3.0.0**
  (the noun change is the major). S5 follows.

## The diagnosis (from the walk, 2.9.0 live instance, all 11 tabs captured)

Not a styling problem - a noun problem. The console mirrors the
implementation (agent daemon, Proxmox sync, drift checker) instead of the
operator's world (my machines, and what HomePilot does to them). Proof: a
connected agent while Inventory says "No hosts" and Coverage says 0% - the
machine the agent runs on is not a host in the product's own model.

Findings F1-F7 and the full plan text live in the session artifact; this doc
is the binding summary the slices are built against.

## The plan

- **P1 - one noun: Host.** Enrolment creates-or-links a host row
  (source: `agent`, alongside `proxmox`/`manual`). Hosts is the one fleet
  page; state chips managed / discovered / agent-only / offline / gone.
- **P2 - a real host page** at `/ui/hosts/{id}`: identity header + actions;
  sections Metrics (chart grid, one aligned column system, raw JSON never
  rendered), Changes (applied artifacts + drift state), Activity (host-scoped
  tasks + journal), Agent (credential, transport, refusals).
- **P3 - monitoring home**: Overview fleet-health strip (chip per host:
  state + headline metric + firing alerts); alert rules move to
  Settings -> Monitoring.
- **P4 - fleet mechanics**: row checkboxes, "select all disconnected",
  ONE batch confirm naming what dies, optimistic in-place updates, no
  full-table reloads, nothing moves under the cursor.
- **P5 - nav 11 -> 5**: Overview / Hosts / Changes / Records (Tasks, Journal,
  KB) / Settings (incl. Tokens, Monitoring rules). Every pre-move URL
  redirects.
- **P6 - honest numbers**: 0 checked -> "nothing checked yet", never a green
  100%; coverage counts agent-managed hosts; every empty state names a door
  that is actually open on this install.
- **P7 - visual pass, last**: one definition-list pattern (label col, value
  col, left-anchored, tabular numerals), one chip vocabulary, semantic colour
  separate from the ember accent, row actions collapse to one overflow menu.

## Slices and journey gates

| Slice | Scope | Journey gate (assert the GOAL, not the call) |
|---|---|---|
| S1 | P1 host<->agent merge (backend + Hosts list) | enroll agent on empty install -> host row exists, coverage > 0, no Proxmox involved |
| S2 | P2 host page; metrics move; detail dump deleted | from Overview, reach a host's load chart in <=2 clicks; no raw JSON in any DOM |
| S3 | P4 bulk ops | enroll 3, disconnect 2, forget both in one confirm; table never fully reloads |
| S4 | P5 nav + P6 honesty + Tokens/alert-rules relocation | every pre-move URL redirects; 0-checked shows no percentage |
| S5 | P7 polish + play-every-function e2e on the shipped image | walk every route on the real build, phone viewport included |

S1 first on purpose: it is the only slice with backend/migration risk, and
every later slice stands on the merged noun.

## S2 build note (2026-08-23)

`/ui/inventory/{id}` (doc view + adopt + zero-touch install + role/ip edit)
predates the plan. S2 makes `/ui/hosts/{id}` the host page and the hostname
link target; the legacy page stays reachable via its "Manage" button ONLY
until S4, which ports adopt/install/edit onto the host page and deletes it
with a redirect. Two permanent detail pages would be the two-noun mistake
at the detail level.

## Scope boundaries

- Absorbs the remainder of #435. Does NOT absorb #506 (artifact
  library/templating) - separate epic.
- The 2.9.0 agent-operability work (version, refusal reasons, revoke that
  bites) is reused on the host page, not redone.
