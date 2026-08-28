# Changelog

## 3.6.8 - 2026-08-28

### The console's token ladder mints what it says (#614), and the superuser scope is spelled "all" (#579)

The Tokens panel offered read_only / full / admin and described full as "can
change things" - but posted scope=full, which normalizes to `*`: the SUPERUSER
scope, strictly above the admin rung beside it. An operator following the UI's
own advice minted a token-managing, secret-reading credential believing it was
a write token. The middle rung now mints `read,write` labeled **write**, and
superuser minting is not offered in the console at all.

Underneath, the superuser API scope is now advertised as **all** everywhere
(hp init, the claim, CLI, docs); `full` remains accepted forever as a silent
legacy alias - a unit gate pins the alias so removing it (which would brick
every pre-rename token) fails the suite. The only "full" an operator reads now
is the MCP write tier.

## 3.6.7 - 2026-08-28

### Provisioning can finally build its own template (#594)

`provision_guest` clones `template_vmid`, so a cloud-init template had to
already exist - and there was no product path to CREATE one: the manual route
needs root on the PVE node, which HomePilot's scoped token deliberately does
not have ("Only root can pass arbitrary filesystem paths"), so provisioning was
undeliverable on a cluster nobody had hand-prepared. The new
**`create_guest_template`** MCP tool (admin tier) builds the template over the
API alone - stage or download a cloud image, create a VM, import the image as
its disk, add the cloud-init drive, serial console and guest agent, convert -
tracked as a `create_guest_template` background task like a provision. It
refuses a `template_vmid` already in use rather than overwriting it, destroys
the half-made VM on any later failure, and adds the `import` content type to
the storage when it is missing, saying so on the task result.

### A guest budget can be removed, not only set (#607)

`DELETE /admin/guests/quota/{cn}` (404 when there is none, audited) plus the
`delete_guest_quota` MCP tool and a "Remove budget" console action. Removal
deletes the ROW - a row of nulls is an unlimited budget, not the absence of
one - so after it the guest's own `/guest/quota` answers `limits: null`.

### One name for "which machine" (#608)

Every host-addressed MCP tool now takes `host`; the four that said `hostname`
still accept it as a deprecated alias (declared in the schema, warned in the
result), so existing callers keep working. A registry-driven gate asserts the
whole surface stays uniform.

### The word "full" is two words, said out loud (#579)

API scope `full` (= `*`, everything - what `hp init` mints) and the MCP tier
`full` (= write) collide. The collision is now named in `normalize_scope`, the
ARCHITECTURE tier table, the `--scope` help, and a stderr note when
`hp token create --scope full` is about to mint a superuser token. The rename
stays an open owner decision on #579.

### The local gate runs the same security audits CI runs (#548)

`make gate` gained `gate-security`: the mirror integration job's exact bandit
and detect-secrets invocations, failing on any finding - so secret-scan and
bandit hits stop costing a 15-minute mirror round-trip to discover.


## v3.6.6 (2026-08-27)

The guest-network hardening round, all found by walking the real dev cluster.

### The fence lands ACCEPT-first, and applying it enforces itself safely (#599 #600)

Per-VM fence rules were compiled REVERSED (gateway DROP above the DNS/DHCP
ACCEPTs it shadows), so a fenced guest lost DNS and DHCP - kernel-confirmed.
They now land ACCEPT-first in the compiled chain (the test fakes model PVE's
rule-prepend, so the gate asserts the real order). Applying a guest-network
artifact now also enables the PVE datacenter firewall the safe way -
enable=1 with policy_in=ACCEPT, so the per-VM fence actually enforces while
the host INPUT chain stays open and management can never be locked out - and
the report states whether the fence is ENFORCED or merely CONFIGURED.

### Provision cleans up after itself, and the KB/monitoring rough edges (#595 #592 #593 #596)

A provision that fails after the clone now destroys the half-made guest
instead of orphaning it. record_fact's returned id works with
get/update/delete_kb_doc. Alert rules are really updatable (threshold/
comparison/duration), not just enable-toggled. Guest-network status reports
vnet-bridge realization honestly (confirmed at provision) rather than a bare
"in spec" the API cannot actually prove.


## v3.6.5 (2026-08-27)

### A vnet is a valid guest bridge

The bridge probe now accepts an SDN vnet by name (this PVE's node network
listing omits vnets even with type=any_bridge), so the provisioning default
can finally point at the guest vnet the guest-network artifact built.


## v3.6.4 (2026-08-27)

### The bridge probe sees SDN vnets, and drift stays honest after an apply

The guest network went live and two edges surfaced immediately, both as
honest refusals: the bridge probe listed /nodes/{node}/network without
type=any_bridge and so rejected the newly-real guest vnet as "no bridge";
and an applied subnet reports DHCP ranges as dicts where pending state uses
strings, keeping the drift check DRIFTED after a clean apply. Both fixed,
both gated.


## v3.6.3 (2026-08-27)

### Pending SDN objects are read for what they will be

Second live apply: PVE reports created-but-unapplied objects with their real
values under `pending` and the top level empty, so the plan diffed against
emptiness and scheduled a spurious full update right after its own create -
and the update then 501'd because a subnet is addressed by PVE's composed id,
not its CIDR. Row reads now overlay the pending values (they are what
apply-sdn makes true), real updates use the composed id, and the test fake
now reproduces the observed pending semantics so this class stays caught
without a cluster.


## v3.6.2 (2026-08-27)

### The vnet firewall waits for the cluster to catch up

The first live guest-network apply built the zone, vnet and subnet and then
failed writing the vnet firewall: PVE validates the vnet against the APPLIED
SDN config, and the plan wrote firewall options while everything was still
pending. Firewall steps now run after apply-sdn, an apply also commits
leftovers a failed earlier run parked, and a firewall-only repair no longer
runs a needless apply. The failed apply left nothing broken - the report
named every step, done and not attempted, which is how the bug was found.


## v3.6.1 (2026-08-27)

### A garbage write token no longer takes the guest network down

Found live driving 3.6.0 over MCP: the vault's write-token slot held an
error message a past failed settings save had stored, and the shared PVE
client refused to build around it - every guest-network read failed. The
client factory now falls back to the read token (warning names the repair),
and the Proxmox settings save refuses to store a value that is not shaped
like a PVE token.


## v3.6.0 (2026-08-26)

The estate's network becomes a recorded change, and credentials grow up.

### The guest network ships as an artifact (#553)

A new `guest-network` artifact kind carries the desired guest subnet (zone,
vnet, subnet, gateway, SNAT, DHCP, isolation CIDRs). Propose it - over MCP or
the UI - approve it with the human-relayed code, and apply runs an idempotent
plan against the live cluster; drift checking runs the same plan, so "in spec"
means the cluster still agrees. Provisioning onto the guest vnet fences every
guest at the tap device before first boot (DHCP/DNS to the gateway allowed,
the operator LAN dropped; an empty isolate list refuses to provision onto
the guest vnet at all - fail closed by refusal); a fence that
cannot be written destroys the half-made guest loudly. VNet-level forward
rules are written too - dormant on the legacy firewall stack, live the day the
node switches to nftables, and the report says which.

All PVE endpoint knowledge comes from the estate's own
`homepilot-proxmox-mcp` package (pinned to the public mirror by commit) - the
one shared Proxmox client. HomePilot adds no endpoint paths of its own; the
gaps found in the library (structured reads, vnet firewall, subnet update)
are written down in code as upstream debt, not re-implemented.

### Tokens are minted by admins, and MCP speaks API tokens

`hp token create` now authenticates through the API like every other client;
the unauthenticated direct-DB mint survives only on a fresh install with zero
live tokens, and says so. The MCP transports authenticate API tokens against
the same machinery as the HTTP API (expiry, live revocation, last-used),
mapped read->read_only, write->full, admin->admin by the same constants the
tier gate enforces - so an operator mints an assistant token in Settings ->
Tokens, revocable like any other. HP_MCP_TOKEN remains as a legacy fallback,
and /mcp now mounts unconditionally behind that always-on auth. A
claim-installed instance autocreates its own local-cli admin credential, so
the CLI works there at last; the human still holds exactly one login token.


## v3.5.1 (2026-08-26)

The feedback round on 3.5.0, same day.

### Settings explains every field it asks for (#549 F7)

Owner verdict on 3.5.0: the tab organization is right, the information is
missing. Every Guests input now says what it is for and what happens with it;
Monitoring, Tokens, Connection and Proxmox get lead-ins; an env-locked setting
now states HOW to hand control to the UI (remove the variable and restart).
Writing the explanations down surfaced two real gaps: the "empty = the
Provisioning default" invite path was unreachable from the product (the form
still required node and pre-filled the template), and the invite TTL was a
hidden constant - both fixed, and the mint response now shows which defaults
the invite actually resolved to. A standing gate walks every rendered Guests
control and fails on any input without an explanation.

### The artifact DB mirror records every artifact (#545)

Every mirror row was written with file_path="" against a NOT NULL UNIQUE
column, so the second-and-later artifact's row silently failed to insert.
Both write sites now record the store-relative path, and the upsert repairs
pre-fix rows on their next status sync.

### An auth failure no longer reads as a network fault

Found live: an unreadable deploy key surfaced as "git push failed (network)",
pointing at connectivity while the fix was key ownership. Auth patterns now
classify before network ones.


## v3.5.0 (2026-08-26)

The facelift: every page reorganised around attention before enumeration, and
the optional subsystems plus VPS provisioning become configurable from the
product - in the UI and, for the non-secret subset, over admin-tier MCP.

### The UI leads with what needs an operator (#549)

One shared tab pattern (URL-addressable, keyboard-reachable) across the Host
page, Changes, Records and Settings. Overview opens with a "Needs attention"
zone and a calm line when there is nothing. Hosts groups the fleet by state
with healthy groups collapsed on large fleets. Changes reviews proposals as a
card queue with the approval-code panel, and drift shows disagreeing artifacts
only - in-spec artifacts are one summary line, never enumerated. Records is a
grouped chronology: tasks by day with running and failed pinned first, the
journal as one-line entries with disclosure, the KB grouped by kind. Settings
becomes seven tabs, and every legacy link and fragment lands on the right one.

### Subsystems tell the truth, and can be configured in place (#553)

The Settings Subsystems tab shows one card per optional subsystem, sourced from
the server's own self-check: a failing subsystem names its reason and its
target, "off" is stated as a choice, never a grey mystery. Non-secret settings
(archive push remote and interval, KB embedding URL and model, both retention
horizons, the events webhook URL) are editable on their card with one binding
precedence: an explicitly set env var wins and records nothing; otherwise the
stored value; otherwise the code default. Changes take effect without a restart
- the consumers re-resolve at use time, and the scheduler re-asks its interval
while it waits. Secrets are structurally absent from the registry: no route or
tool built on it can list, echo or accept one.

### Provisioning defaults the cluster has confirmed (#553)

Default node, template VMID, pool, bridge, VLAN tag and ipconfig are
first-class settings consumed by guest provisioning and invite minting - an
invite can now carry a person and a size instead of raw infra details, and the
values it was minted with are frozen into it. Every save is checked against the
live cluster first and a refuted value is refused with the cluster's own answer
("no bridge vmbr7 on node pve1; node has: vmbr0, vmbr1"); an unreachable
cluster refuses the save too, rather than storing an unchecked value. A bridge
default is also what makes provisioning write net0 at all - with a VLAN tag
when set - so the guest network can finally be enforced per provision.

### The settings reach MCP at the admin tier (#553)

query/set/clear/probe tools over the same machinery the API uses, refusing in
the same words, held to the API's admin scope by the tier gate - with no new
exemptions.


## v3.4.1 (2026-08-25)

### The archive push can actually reach its remote (#550)

The image shipped `git` but no `openssh-client`, so every push to the SSH
artifacts remote has failed since the feature landed in 3.2.0 - with correct
configuration. The runtime image now carries an ssh client, and the image smoke
gate asserts it stays there.


## v3.4.0 (2026-08-25)

MCP reaches parity with the management API. The MCP tool surface went from 20
tools to ~70 across a read_only < full < admin scope ladder, so the assistant
can do over MCP what an operator can do through the portal - gated to the same
scope the API enforces for each route.

### Every MCP tool is tied to its API scope

`TestMcpTierMatchesApiScope` asserts each tool's MCP tier equals its route's
`require_scope` (read<->read_only, write<->full, admin<->admin), and two
route-walk gates fail the build if any management route has neither an MCP tool
nor a stated exclusion - so the surface cannot silently drift or a tool be more
permissive over MCP than over the API. `HP_MCP_TOKEN_SCOPE` gains `admin`.

### A human can approve through the assistant, but the assistant still cannot

`approve_artifact` is reachable over MCP again, gated by a per-artifact approval
code a human reads from an operator surface (the review screen, `hp artifacts
show`, or the proposed-artifact webhook) and relays. The code lives in its own
table, is never returned by any MCP read, and locks after five wrong tries - so
a valid code is proof a human decided, and the assistant calling the tool alone
cannot approve its own proposal.

### Deliberate exclusions

Secret-minting routes, the binary/installer endpoints, the SSE stream, the two
Proxmox-credential/secret-reload settings writes, and self-approval-without-a-
code stay off MCP by design.


## v3.3.0 (2026-08-24)

The open-mechanics sweep: the #381 hub-hardening remainder, an operator-gated
enrolment window for the shared fleet token, real provision cancel, mirror
provenance, and the config/docs parity gate.

### A provision cancel now cancels (#452)

Cancelling a provision task used to mark the row and let the clone finish -
which then overwrote the row with 'succeeded'. Cancel now reaches the running
job, unwinds what it put on Proxmox (stop the in-flight PVE task, destroy the
half-created guest, disks and all), and the record states what actually
happened - including "cleanup failed, guest vmid N may remain on node X" and
the post-restart "PVE state unknown" case.

### The route scope guard sees the whole API again (#472)

FastAPI 0.137+ stopped flattening `include_router`, which silently blinded the
startup scope guard (5 routes inspected instead of 98). The guard now walks the
include wrappers - accumulating prefixes and include-time dependencies - on
both old and new FastAPI, and the pin is lifted (0.141.1 in the lock). A floor
on the inspected-route count makes an empty finding mean "nothing unscoped",
never "nothing checked".

### The secret key that signed nothing is gone (#394)

`HP_SECRET_KEY` guarded a value nothing read (tokens never used JWT), and its
production fail-closed gate could refuse to boot over it. Deleted end-to-end -
field, vault entry, generation chain, docs claims. A stale value in a live
`.env` is ignored; production now starts in strictly more cases. A new parity
gate reflects over the real Settings fields and fails when `.env.example`, the
deployment env table, or the compose image tags drift from the code.

### The shared fleet token needs an open window to add a NEW host

The shared hub token never expires, so a copy of it could add machines to the
fleet for ever - and a fleet member gets fleet-root exec and file access. A
shared-token enrolment of a hostname this install has **never seen** is now
accepted only while an operator has an **enrolment window** open (default 15
min, capped at 24h) - or when the install has no agents at all, so the
zero-touch first rollout still needs zero input.

Unchanged: per-agent reconnects, one-time bootstrap tokens (including the UI's
**Install agent**), and re-enrolling a hostname the fleet already contains. A
refused agent is told why and logs it, the hub audits the refusal with the
claimed hostname, and no agent row is created for the stranger.

`GET/POST/DELETE /api/agents/enrolment-window` (admin), `hp agent
enrolment-window open|close|status`, and an Open/Close control next to the hub
token in **Inventory**.

### Every mirror export names the private commit it came from (#363)

`scrub-for-public.sh` now writes a `PROVENANCE` file at the export root
(`source_commit`, `source_repo`, `scrub_version` - the sha1 of the scrub script
itself) and prints the exact mirror commit invocation, including the
`Source-Commit: <sha>` trailer. There is deliberately no timestamp in the file:
a re-export of the same private commit with the same scrub rules stays
byte-identical. `validate-scrub.sh` FAILS when `PROVENANCE` is missing, when
`source_commit` is not a full 40-char sha, or when a timestamp was added - so a
tree hand-copied into the mirror, bypassing the scrub, does not validate.

### Breaking: the bootstrap token is minted by POST

`GET /api/agents/bootstrap` is now `POST /api/agents/bootstrap`. It mints a
fleet-enrolment credential, and the CSRF gate deliberately skips safe methods -
so as a GET it was mintable cross-origin from any page a signed-in admin
visited. `hp agent bootstrap` and the web UI move with it; a script calling the
old GET must be updated.

### The agent installer verifies what it installs

The GitHub fallback used to `curl` the binary and run it as root with no digest
check at all (the control-plane path has verified since #464). It now checks the
release's `SHA256SUMS` asset and REFUSES when there is none, naming the manifest
it looked for; `--allow-unverified` is the only override. Both paths now
download into a private `mktemp -d` and `install` the file they verified -
no more predictable `/tmp/hp-agent` for a local user to pre-create as a symlink,
and no window between the checksum and the move.

### Agent

`docker run` can no longer be handed the host: `--volume/-v`, `--mount`,
`--privileged`, `--user/-u`, `--pid`, `--ipc`, `--net`/`--network`, `--cap-add`,
`--security-opt`, `--device` and `--userns` are refused in both the `--flag=x`
and `--flag x` forms. The durable hub credential is written atomically
(temp + fsync + rename) instead of truncate-in-place, which could leave a
half-written token and lock a host out for good. A command result must now
answer the action it was issued for, and the reconnect path no longer races on
the live socket (`make gate-go-race`).

## v3.2.0 (2026-08-24)

The archive gets real, and the assistant can manage guests.

### Artifact archive: configured now means synced

`HP_ARTIFACTS_REMOTE` used to be a setting whose self-check promised "the
next sync" while nothing synced. Now the artifact store is pushed to the
remote every `HP_ARTIFACTS_PUSH_INTERVAL_SECONDS` (default 3600) and shortly
after boot, and the Settings panel reports the LAST push's real outcome -
green while pushes land, an honest fault naming the staleness when one fails.
Point it at a private repo with a write deploy key:

```env
HP_ARTIFACTS_REMOTE=git@github.com:you/homepilot-archive.git
HP_ARTIFACTS_SSH_KEY=/home/homepilot/.hp/archive_key
```

### Guest management over MCP

`query_guests` (usage vs budget + invites), `set_guest_quota`,
`revoke_guest_invite` - the mutators write-scoped. Deliberately absent:
minting. An invite token is a machine-provisioning secret and an MCP
transcript is not a safe channel; mint in Settings -> Guests or the CLI,
where it is shown once to a human.

### Agent

`docker run nginx` no longer needs two spaces to pass the allowlist; the
injection shapes the pattern blocks are still blocked, gated both directions.

## v3.1.0 (2026-08-24)

The guest portal, as envisioned: same backend, a different client. A friend
with a client certificate gets one page - their machines, power buttons, and
their budget - and nothing else HomePilot has.

### To turn it on

1. Add the vhost block from `docs/guest-portal.md` to your public front
   nginx - one proxy location for `/guest/*` and `/invite/*`, nothing else.
   The portal page ships inside the backend and is served at `/guest/`;
   nothing is copied anywhere.
2. Set the four `HP_PORTAL_*` variables on the backend (unset = every guest
   route answers 503, fail-closed).
3. Mint invites and set budgets from **Settings -> Guests** in the console,
   or `hp invite create` / `hp quota set`.

### What a guest gets

Their machines (state, address, size), start / stop / reboot behind a
confirm, budget meters, invite redemption. Ownership is watertight by
construction: every query loads by owner, another guest's machine answers
exactly like a typo, no topology or hypervisor error text ever reaches a
guest page.

### Per-guest budgets

`guest_quotas` (one additive migration): totals across ALL a guest's machines
- count, cores, memory, disk. Redemption stops at the line and leaves the
invite open; the portal shows the meters so the line is never a surprise.

### Console

Settings -> Guests: every guest's usage next to their limits, invite mint
(the token is shown exactly once), revoke, budget editing. Overview gains a
fleet-health strip - one chip per host, straight to its page.

### Also

Agent-carried rows on the Hosts list link to their host page instead of an
adopt-era dash; the live-browser e2e now walks every route, proves all nine
S4 redirects on the shipped artifact, and runs a phone-viewport journey.

## v3.0.1 (2026-08-23)

Hotfix for the first real 3.0.0 install. **If you ever re-ran
`install-agent.sh` on the box that also runs the backend, check your data
directory's ownership** - the installer used to `chown -R` its write prefixes,
and on a control-plane box that handed the backend's database, vault and
`.env` to `hp-agent`. Recovery: `chown -R 999:999` the directory mounted at
`/home/homepilot/.hp`, restart the backend.

* `install-agent.sh` takes ownership only of directories it CREATES. A write
  prefix grants the agent permission to write inside it - it is not a claim on
  the ownership of what others put there.
* An unreadable `.env` in the data directory no longer crash-loops the backend
  with a raw traceback: it is skipped with a loud error naming the file, the
  uid, and the exact chown to run. Environment-variable configuration keeps
  working throughout.

## v3.0.0 (2026-08-23)

The operator console. The UI's nouns now match the operator's world - my
machines, and what HomePilot does to them - instead of the implementation's.
A major version because addresses and names changed; **nothing else did**:
no schema surprises beyond one additive migration, no API removals, no agent
changes. Upgrading is pulling the image.

### REQUIRED READING BEFORE UPGRADING

**Old bookmarks keep working.** Eleven tabs became five - Overview, Hosts,
Changes, Records, Settings - and every pre-move URL redirects: `/ui/inventory`
and `/ui/agents` land on Hosts, `/ui/artifacts` / `/ui/review` / `/ui/drift`
on Changes, `/ui/tasks` / `/ui/journal` / `/ui/kb` on Records, `/ui/tokens`
on Settings.

**Enrolled agents' machines appear in inventory on their own.** Migration 25
links every agent to a host row (creating one where none exists, source
`agent`). After the upgrade your Hosts page may show MORE machines than
Inventory did - those are the agent-carrying hosts that were invisible before.
Coverage counts them as covered, so that number can jump. Neither is a defect;
both are the point.

### One noun: Host
An enrolled agent's machine is a host - enrolment creates-or-links the row,
the agent's report fills the gaps (never overwriting what Proxmox or an
operator set), and the host's status follows the agent's channel unless an
operator pinned it.

### A real host page
`/ui/hosts/{id}`: identity and facts, metrics with range switching (this is
where a machine's stats live now), the changes and journal scoped to the
machine, and the agent's version, channel state and last refusal reason -
with adopt, zero-touch install, edits, revoke and forget on the same page.
The old details dump (raw JSON, values floating at the table edge) is gone.

### Fleet operations
Select the disconnected half of a fleet in one click, forget or revoke the
whole batch behind ONE dialog that names every machine, and watch rows update
in place - no reloads, no buttons swapping under the cursor.

### Honest numbers
"In spec" shows a dash until something has actually been checked. Coverage
counts machines HomePilot has a live channel onto. Empty states name a door
that is actually open on this install.

### Relocations
Alert rules live in Settings -> Monitoring (firing alerts still show on the
Overview and the affected host); API tokens live in Settings.

### Rate limit
The authenticated per-IP request limit default rises 120 -> 300/min
(`HP_AUTH_RATE_LIMIT`): the richer console legitimately makes more API calls
per page, and at 120 a busy operator could rate-limit themselves. The
anonymous limit is unchanged at 60.

## v2.9.0 (2026-08-23)

The release that makes the web UI enough to run HomePilot with, and closes the
2026-08-16 five-lens review: the features that could not work on a real install
now work, the ones that reported success without doing anything now tell the
truth, and the fleet can explain itself.

### REQUIRED READING BEFORE UPGRADING

**One backend per data directory, enforced.** A second process touching a live
install used to mark the first one's in-flight tasks failed, and a CLI command
could migrate the schema under a running server. An advisory lock now refuses
the second process instead (#431). If you run `hp` commands against the same
data directory as a running backend, they will now say so rather than corrupt
it - stop the backend first. The lock is released by the kernel, so a crashed
backend never leaves a stale one behind.

**History is pruned from now on.** `audit_log`, `agent_audit`, finished tasks and
webhook deliveries were never pruned; a year of them is a multi-GB SQLite file.
They are now swept on a schedule, default 90 days:

```env
HP_RETENTION_DAYS=90                 # audit trail, finished tasks, deliveries
HP_RETENTION_INTERVAL_SECONDS=21600  # how often the sweep runs
```

Artifacts are **never** pruned - they are the record of intent, not history. Set
`HP_RETENTION_DAYS` higher before first start if you need a longer trail; the
first sweep runs shortly after boot.

**Schema migrations include a `hosts` table rebuild.** Migrations have been
per-version transactional with a pre-migration backup since 2.8.0 (#420), so a
failure rolls back rather than half-applying - but take your own backup anyway,
as with any release that touches table shape.

### The UI can run the product (#445)

* Approval shows what will actually change on the host, not a diff of the file.
* Propose an artifact from the UI; read a task's execution log.
* Search across artifacts, hosts and the journal.
* Add a host by hand, notice one that is gone, forget it - a non-Proxmox
  homelab is representable now.
* A first-run path from an empty install to a managed change.
* The shell works on a phone and with a keyboard, and a failed knowledge-base
  search no longer looks like an empty knowledge base.

### Agents you can operate (#415, #430, #464)

* Forget a decommissioned agent, credential and all - a scrapped box's token no
  longer authenticates.
* The fleet explains itself: agent version, the REASON a connection was refused
  (revoked credential, wrong host, banned peer) instead of an identical grey
  dot, and a revoke that closes the live channel now.
* HomePilot serves the agent payload itself, so enrolment needs no external
  download.

### The review epic's dangerous defects (#423-#433)

* **One** engine applies, replays and revokes - the shadow CLI path that skipped
  snapshots, hash checks and rollback is gone.
* Drift never reports "in spec" for something it did not check; the Ansible
  verifier that had been silently dead is fixed.
* Rollback is derived rather than claimed, and reported honestly when it is not
  possible.
* Automation stops overwriting what an operator set.
* The AI can read back what it did and check before it proposes.
* Semantic knowledge-base search actually runs, and stops hiding what you put in.
* A credential in an artifact is a reference, never the value.

### Shutdown and the gate (#496)

Background work can no longer outlive the database it writes to. An in-flight
write whose event loop went away killed aiosqlite's worker thread, after which
every later operation - the close included - queued to a thread that would never
pick it up, and the timeouts meant to catch that deadlocked inside it. The agent
hub also waited on live connections that had not hung up, so a shutdown could
ignore SIGTERM until Docker killed it. Both fixed; provision, enrolment,
artifact runs and the audit trail are now drained before the database closes,
and a close that has to be abandoned is abandoned somewhere that cannot hold up
the process exiting.

## v2.8.0 (2026-08-20)

The zero-touch install (ADR-004): a fresh HomePilot is claimed from a browser
and finished with a Proxmox address and token, and nothing else. Monitoring
moves in-product, the agent hub configures its own TLS, and agents are enrolled
from the UI instead of by hand on each host.

### REQUIRED READING BEFORE UPGRADING

**Your existing fleet keeps working, and that is deliberate.** 2.8.0 turns the
agent hub's TLS on by default, but only for installs that have never had an
agent. An install that already has enrolled agents keeps serving the transport
those agents speak, records that decision once, and says so in the log. Flipping
it under a live fleet would strand every agent: they read `HP_AGENT_TLS` from
`/etc/homepilot/agent.env`, written once at enrolment, so even upgrading the
agent binary would not have saved them (#468).

To move an existing fleet onto TLS, use the new migration rather than editing
any host:

```
curl -sS -H "authorization: Bearer $HP_ADMIN_TOKEN" http://<hp>:8000/agents/migrate-tls   # preview
curl -sS -X POST -H "authorization: Bearer $HP_ADMIN_TOKEN" \
     -H 'content-type: application/json' -d '{}' http://<hp>:8000/agents/migrate-tls
```

It refuses, naming names, if an enrolled agent is offline and would be stranded;
`{"force": true}` overrides. Restart the backend afterwards to serve TLS.

**If your `.env` sets `HP_AGENT_HUB_TLS=false`**, the hub still refuses to serve
plaintext on a routable bind — but the control plane now starts anyway, with the
hub disabled and the reason reported at `GET /admin/selfcheck`. Before 2.8.0 that
configuration exited the process outright.

### Added

- **First-run claim (#458 S1+S2).** A fresh instance is claimable from a browser
  with nothing to type on a local network, and a printed claim code when it is
  reached from outside. No `hp init`, no copying a token out of container logs.
  The same screen takes the Proxmox address and token and verifies them live.
  The vault passphrase and secret key generate and persist themselves.
- **The agent hub configures itself (#458 S3).** On by default, with a
  self-signed certificate generated on first boot and pinned by the agent at
  enrolment. Closes #454.
- **Agent rollout from the UI (#458 S4).** Enrolling an agent into a guest is a
  host action driven through qemu-guest-agent, verified by the agent appearing in
  the hub registry rather than by an exit code.
- **Native metrics, and Zabbix retires (#458 S5).** CPU, memory, disk and load
  over the existing hub channel with a 7-day retention window, duration-based
  alert rules with recovery, and sparklines in the UI. Storage is measured, not
  guessed: 6.26 MB per agent per week. The hardcoded `/zabbix` proxy block,
  `HP_ZABBIX_URL` and the deep-links are gone.
- **Fleet TLS migration (#468).** The hub pushes its certificate to every
  connected agent over the channel it already has, so "enable TLS" never means
  visiting a host. `GET/POST /agents/migrate-tls`.
- **A startup self-check (#458 S6).** `GET /admin/selfcheck` reports every
  optional subsystem as `off` / `ok` / `unreachable` / `unknown` with the
  consequence in plain words. "Off" and "configured but unreachable" are separate
  states because they call for opposite actions.
- **Self-service guest provisioning (#442).** A Proxmox template is cloned,
  cloud-init configured, resized and started as a HomePilot task, with an
  invite-based portal behind a client-certificate vhost.
- **Design tokens and the estate type voice in the web UI (#445 lane B1).**

### Fixed

- **P0: a failed migration bricked the backend permanently (#420).** Migrations
  are now per-version transactional with a pre-migration backup taken through the
  sqlite backup API.
- **P0: the backup was not a backup (#421).** It omitted the vault, so a restore
  left every secret undecryptable, and a live-WAL copy could be torn; import left
  the old `-wal` in place and could corrupt the restored database.
- **P0: drift verification issued real mutating HTTP requests (#419).** An
  http-sequence step without a precheck had the verifier executing its own
  DELETE/POST, unattended, every 1800 seconds.
- **P0: `--privileged` could not work on the shipped systemd unit (#422).**
  Phase B provisioning was undeliverable on a stock install.
- **P0: every successful apply reported failure to the operator (#467).** The
  executor and the task runner each performed the same transition, so a correct
  apply ended with `Invalid transition: applied → applied` and a `failed` task.
- **P0: an upgrade either stranded the fleet or refused to boot (#468).**
- **The dashboard overstated connected agents (#469).** It counted a persisted
  column that survives an unclean disconnect, while `/agents/` read the live
  registry — so it was most wrong right after a restart, when an operator checks
  the fleet came back.
- **The liveness probe failed the whole instance over one unhappy subsystem
  (#470).** A locked vault, an unreachable Proxmox or a disabled hub made a
  serving HomePilot answer 503, marking the container unhealthy over something no
  restart repairs. Only the database failing means `down` now; everything else is
  `degraded` with HTTP 200 and still named in the check map.
- **Web: an inert API-base setting, an endless cancelled-task poller, ungated
  write actions and SSE refetch storms (#434).** Every KB document is reachable
  and the count is true (#447).
- **Host roles no longer revert to `guest` after an inventory refresh (#416).**
- A secret leak in the embedding client, which logged a configured URL complete
  with any `user:pass@` or `?key=` it carried.

### Changed

- **Honest defaults (#458 S6).** `HP_EMBEDDING_SERVICE_URL` and
  `HP_EMBEDDING_FALLBACK_URL` no longer point at hosts a stock install does not
  run. An optional service either works out of the box or is off and says so.
- **fastapi is pinned below 0.137 (#472).** That release stopped flattening
  `include_router`, which took the startup route scope guard from inspecting 81
  routes to 5 while still reporting no problems. Dependencies are otherwise
  current, including cryptography 50.

## v2.7.1 (2026-08-16)

### Fixed

- **P0 regression: agents could not reconnect after 2.6.0 (#417).** The agent
  generated a NEW random `agent_id` on every restart (`HP_AGENT_ID` unset, and the
  installer never set it). Harmless before, but per-agent credentials (2.6.0) bind
  a credential to the agent_id it was issued to — so after a restart the agent
  presented its persisted per-agent token under a brand-new id, the hub found no
  matching credential, auth was rejected, and the retry loop tripped the
  auth-failure ban. Fleet-wide lockout on any agent restart or backend update.
  Fixed in three layers: the agent now persists its id (`HP_AGENT_ID_FILE`,
  default `/etc/homepilot/agent.id`) so it is stable across restarts; the agent
  self-heals by falling back to the enrollment token if the stored credential is
  rejected; and the hub accepts a credential presented under a changed id when it
  matches a non-revoked credential issued to the SAME hostname, rebinding it (a
  token presented for a different hostname, or a revoked one, is still rejected).
  `install-agent.sh` now pins a stable id for new installs (idempotent).

  **Upgrading the backend alone recovers bricked agents** — they rebind on the
  next dial (the auth ban clears itself within a minute). Agents on the old binary
  that had already stored a credential recover the same way; if a host's *hostname*
  also changed, revoke its credential so it re-enrolls.


## v2.7.0 (2026-08-16)

Manage imported hosts, end to end — plus deployment robustness.

### Features

- **Provision managed hosts (epic #397 Phase B).** Native, idempotent agent
  actions — `install_package`, `manage_service`, `write_config` — that stay
  inside the command allowlist (no target shell, no ansible; the spike showed
  ansible needs a full shell that breaks containment). A new **`host-provision`
  artifact kind** describes a host's desired state declaratively (packages
  installed, services in a state, config files written) and applies it through
  the propose → approve → apply lifecycle, with read-only drift detection. Paired
  with Phase A (introspect-on-adopt, in 2.6.0), HomePilot can now observe and
  provision an imported host without reverse-engineering it.

### Deployment

- **The optional agent stack (n8n, SearXNG, Radicale, Whisper, Piper) is gated
  behind the `agents` compose profile.** `docker compose up -d` now starts only
  the backend; use `docker compose --profile agents up -d` for the extras. A
  stale optional-image tag can no longer abort a core backend update. Corrected
  the Piper image tag (`2.4.2`, no `v` prefix) and refreshed the optional-service
  tags.

## v2.6.0 (2026-08-16)

Large hardening + upgrade batch from the 2026-08-15 code audit.

### ⚠️ Upgrade notes (read before updating an existing deployment)

- **The Agent Hub now FAILS CLOSED without TLS on a non-loopback bind.** If you
  run the hub (`HP_AGENT_HUB_ENABLED=true`) on `0.0.0.0`/a routable address and
  it previously started plaintext with only a warning, 2.6.0 will **refuse to
  start** (`RuntimeError: Agent Hub refusing to start: … TLS is not configured`).
  To upgrade, either configure TLS (`HP_AGENT_HUB_TLS=1` + `HP_AGENT_HUB_CERT`/
  `HP_AGENT_HUB_KEY`), or, on a trusted isolated network (LAN/VPN), set
  `HP_HUB_ALLOW_INSECURE=1` to keep plaintext explicitly. The wire risk on a
  trusted network is further mitigated by the new per-agent credentials + replay
  protection.
- Agents already enrolled with the shared token keep working; on first reconnect
  they are handed a per-agent credential automatically (no manual migration).

### Security (Agent Hub)

- **TLS fail-closed** by default in the shipped Go agent; verification only
  disabled via an explicit `HP_AGENT_TLS_INSECURE` (#377).
- **Per-agent credentials** (#362): each agent gets a minted, hashed-at-rest
  credential bound to `(agent_id, hostname)`; the shared fleet token is now
  enrollment-only and never handed back. Revoke with `hp agent revoke <id>`.
- **Replay protection** (#362): per-frame HMAC + sequence keyed by the per-agent
  credential, plus register nonce/timestamp freshness (defense-in-depth for
  insecure-transport deployments), feature-gated so old agents still work.
- **Identity/robustness**: reject hostname/agent_id hijack from a live
  connection, compare-and-delete unregister, oversize-frame survival, auth-flood
  ban, sudo arg-regex bypass closed, unrestricted `read_file` given an allowlist
  + secret denylist, symlink-safe atomic writes.
- **Durable, attributable audit**: the fleet-root command audit now persists to
  the DB with caller attribution and lifecycle events.

### Backend

- **HTTP MCP transport fixed** (#382): it was dead (`Task group is not
  initialized`); the mounted app's session manager now runs. Migrated to the
  **mcp 2.x SDK** and lifted the version pin.
- **Data integrity** (#383): every repository write now commits (a whole class of
  silently-rolled-back writes, incl. token creation, is closed) + a static gate.
- `hp init` no longer destroys the vault on re-run; task lifecycle strandings
  fixed + **task cancellation** (`POST /tasks/{id}/cancel`) and a global Tasks
  view; SSRF IPv6 gaps closed; vault key derivation off the event loop.
- **Scopes enforced to the edges**: a startup guard requires a scope dependency
  on every non-public route.
- **Observability**: real PVE node state (no longer hardcoded), drift now emits
  `artifact_drifted` events, SSE-driven live UI.
- **Reproducible builds**: the image installs from `uv.lock`; `make gate` /
  `make gate-image` local gate runner added.
- **Manage imported hosts, Phase A** (#397): adopting a host runs a read-only
  introspection and records observed state (services + an as-found KB note) —
  never as artifacts.

### Web

- Logout no longer deletes the shared API token; live session state; global 401
  handling; honest save/health states; the Tasks view; clickable artifact links.

### Removed

- The orphaned `mscp` module and the removed Python agent's remnants.

## v2.5.0 (2026-06-12)

### Features

- **Agents reconnect after a backend restart even when bootstrap-enrolled
  (#348)**: an agent enrolled with a one-time bootstrap token used to loop on
  "registration failed: invalid auth_token" after the hub restarted (the token
  is consumed). The hub now hands the durable shared token back in
  `register_ack`; the agent persists it to `HP_AGENT_TOKEN_FILE`
  (`/etc/homepilot/agent.token`) and prefers it over the env token on subsequent
  starts. Enroll once with a bootstrap, reconnect forever. `install-agent.sh`
  wires the token file + `ReadWritePaths`.

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