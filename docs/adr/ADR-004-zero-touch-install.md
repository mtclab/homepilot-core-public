# ADR-004: Zero-Touch Install

**Status:** Accepted
**Date:** 2026-08-20
**Deciders:** Owner, Architect
**Epic:** #458

## Context

HomePilot's install is a sequence of manual steps that only an operator holding
this repository's history can perform correctly. Audited against the code, a
fresh install today requires: editing `.env`, `docker compose up`, a
`docker compose exec backend hp init` plus copy-pasting the admin token out of
container output, pasting that token into the UI, pasting the Proxmox token into
Settings, deciding how to satisfy the agent hub's fail-closed TLS check before
host management works at all, and running an installer one-liner on every
managed host by hand.

Monitoring is the extreme case: nothing installs or configures Zabbix. There is
no Zabbix service in `docker-compose.yml`, `scripts/install-agent.sh` installs no
Zabbix agent, `deploy/control-plane/nginx-hp-proxy.conf` proxies `/zabbix/` to a
hardcoded `10.0.0.1:8084` that HomePilot never provisions, and
`monitoring/zabbix/template_hp_agent.json` must be imported by hand. A fresh
install therefore ships "Metrics" deep-links that 502 and collects no metrics.

Two defaults point at services absent from compose (`HP_EMBEDDING_SERVICE_URL`,
and the events webhook now that n8n sits behind an optional profile).

The failure mode is consistent: **capability that exists in the code but is
unreachable without knowledge that lives outside it.** Documentation does not
fix this - it is the shape of the problem.

## Decision

**The only thing an operator supplies is the Proxmox API address and token.**
Everything else configures itself, or is not required.

Corollaries, binding on every future feature:

1. Secrets self-generate on first boot. The vault passphrase and secret key
   already do this (`config.py:_auto_generate_passphrase`,
   `_auto_generate_secret_key`); that is the pattern, not the exception.
2. A capability that needs an operator decision must pick a safe default and
   proceed, not refuse to start. Fail-closed checks stay, but the install must
   satisfy them by itself (a generated certificate, a loopback bind), never by
   asking a human to choose.
3. An optional service either works out of the box or is off and says so. A
   default must never point at a host that does not exist.
4. Automating a step beats documenting it. A README step is the failure, not
   the fix.
5. Monitoring is native. HomePilot collects, stores and alerts on its own
   metrics over the agent channel it already owns.

### Monitoring, specifically

Native metrics over the existing hub channel, with a **7-day retention window**,
stored in SQLite, retention configurable. The agent is already a collector:
`agent/go/sysinfo.go` gathers CPU count, disk, memory and load, and
`agent/go/zabbix.go` reshapes exactly those into metric items - it simply posts
them to a Zabbix trapper instead of to us.

This **supersedes** the earlier "HomePilot = current state, Zabbix = history"
boundary, which was decided when HomePilot had no metric pipeline at all. Longer
retention and richer history are a separate, later question; they are not a
reason to keep an out-of-repo dependency for the recent window.

What is knowingly given up: SNMP and IPMI collection for devices that cannot run
the agent, and Zabbix's template library and mature escalation. Acceptable
because everything HomePilot manages runs the agent. If network-gear monitoring
is wanted later, that is an argument for adding a collector, not for keeping
Zabbix wired into the install.

## Design

### S1 - First-run claim

A fresh instance is reachable and claimable from a browser. No shell, no
`hp init`, no copy-paste from container logs.

**The normal path is codeless.** A request that reaches an unclaimed instance
from a private/local source - loopback, RFC1918, CGNAT 100.64/10, link-local,
IPv6 ULA - claims it directly: open the page, the admin credential is created,
Proxmox is optional in the same step. This is the appliance model (Home
Assistant's), and it is what "normal install" means here.

**The hardened path applies when the instance is exposed.** A request from any
other source is refused the codeless path and must present a **claim code**
instead: generated on first boot with no admin credential, stored hashed exactly
as API tokens are (prefix + sha256), stable across restarts.

- `GET /claim/status` reports `unclaimed` or `claimed`, plus - while unclaimed
  only - whether THIS caller needs the code. That second field describes the
  caller's own source address, which the caller already knows; it discloses
  nothing about the instance.
- `POST /claim` mints the admin token, marks the instance claimed, and **closes
  the claim path permanently** (subsequent calls 410, whatever is presented). A
  code that IS presented must be correct, local or not.
- Source trust fails closed: the forwarded client address counts only when the
  peer is listed in `HP_TRUSTED_PROXIES`; a forwarding header from anywhere else
  makes the source untrusted outright, and a trusted proxy forwarding no client
  address is untrusted too. A client-supplied header can never promote itself.
- The claim window is bounded: attempts are rate-limited per effective client
  address, and the code is constant-time compared.
- The UI, when it reaches an unclaimed instance, shows the claim screen instead
  of the login form, and says which of the two modes it is in.
- `hp claim-code` prints the pending code, so the exposed case has an answer
  that is not "scroll the container log".

**Decision: local-trust by default, a code when exposed.** Requiring the code
everywhere was the earlier decision; it was rejected because reading a code out
of container output is not how a normal install feels, and it re-introduces the
shell step this ADR exists to remove. Requiring it *nowhere* would make a
briefly-exposed port a compromise rather than a nuisance. Splitting on the source
address gives the appliance experience on the network the operator already
controls, and keeps the strong gate exactly where the exposure is.

Residual risk, stated plainly: on a LAN, anyone who reaches the port before the
operator does can claim a fresh instance. That is the same exposure every
self-hosted appliance accepts, it lasts only until the first claim, and the UI
says so on the claim screen rather than leaving it implicit.

### S2 - Proxmox credentials as the one input

The claim screen asks for the Proxmox address and token in the same step.
Both are verified against the live API **before** they are stored (the existing
`POST /admin/settings/proxmox/test` path), so a typo fails immediately rather
than becoming a broken instance - and a verification failure does not consume the
claim, so the operator simply retries. On success they are stored through the
existing `PUT /admin/settings/proxmox` path, whose reload rebinds the live
Proxmox client onto the inventory service, so the reconciler picks them up
without a restart. Both fields are optional: claiming without them leaves a
usable instance and Proxmox can be added later in Settings.

### S3 - The agent hub configures itself

- Enabled by default.
- When no certificate is supplied, generate a self-signed one on first boot and
  use it, so the fail-closed transport check passes without an operator
  decision. `HP_AGENT_HUB_TLS_CERT`/`_KEY` still win when set.
- Advertise host auto-detected, shared token auto-generated.
- Existing installs keep their configuration; this changes defaults, not
  overrides. Subsumes #454.

### S4 - Agent rollout without touching each host

HomePilot already executes inside guests through qemu-guest-agent - that is how
provisioning joins a tailnet. The same channel installs and starts `hp-agent` on
any guest whose agent answers, making enrolment a UI action. The manual
one-liner remains for hosts without a guest agent.

### S5 - Native metrics

- The agent sends metric frames over the existing hub protocol on an interval.
- Storage: one table keyed by host, metric and timestamp; raw samples pruned at
  the retention horizon (default 7 days, configurable). Rollups are deliberately
  NOT built up front - measure a week of real data first, then decide.
- Alert rules evaluate over the stored window with a duration condition, so a
  single spike does not page anyone; notifications reuse the existing webhook
  and event machinery.
- The UI gains sparklines and a per-host recent view. This is the point at which
  the "no time-series in HomePilot" rule is retired.
- Retirement, in the same slice that replaces it: the `/zabbix` nginx block,
  `HP_ZABBIX_URL`, the dashboard deep-links, the agent's Zabbix sender, and the
  hand-imported template.

### S6 - Honest defaults

Optional services work or are off. A startup self-check reports what is
configured, what degraded and why, so a partially-configured instance says so
rather than failing quietly at first use.

### S7 - The install gate

An end-to-end test that starts from a clean checkout and a clean data directory,
supplies ONLY a Proxmox address and token, and asserts a **working instance**:
inventory populated, hub up, an agent enrolled, metrics arriving. It asserts the
outcome, never that each step returned success. Without this gate the principle
decays back into README steps within two releases.

## Consequences

- The `.env` file becomes optional for a default install, and every remaining
  variable is an override rather than a requirement.
- HomePilot takes on responsibility for metric storage, retention and alert
  quality - the real cost of this decision, and the reason the window starts at
  7 days rather than "forever".
- Zabbix leaves the shipped configuration entirely. Operators running it keep
  it; HomePilot simply stops assuming it.
- Every new feature is now judged by "what must an operator do by hand for this
  to work?", where the acceptable answer is "nothing".
