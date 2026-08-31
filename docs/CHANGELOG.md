# Changelog

## 3.6.22 - 2026-08-31

Five fixes, all from one afternoon of a friend redeeming an invite on prod.

### A VMID is not an identity

PVE hands the same number out again the moment a guest is destroyed, so `hosts`
accumulates a row per occupant - and the lookup returned whichever the database
gave first, in practice the oldest. On prod, vmid 116 carried THREE rows: a
machine imported in June, a guest destroyed in August, and the one its owner was
logged into. The refresh kept updating the June row, so the live guest was never
in the seen set and the absent sweep marked it gone three minutes after it was
built.

The redeemer's portal showed both his machines "gone", offered a Start button
for one that does not exist, and reported his budget as **0 of 1 while he was
sitting inside a machine** - which would have let him take a second past his own
quota. That is #613 from the other direction: it fixed a destroyed machine
holding a slot; this was a live machine holding none.

### Guests get their own VMID range

`/cluster/nextid` returns the LOWEST free id, which is what made the reuse
possible at all. The new `provision_vmid_range` (e.g. `8000-8999`; empty keeps
the previous behaviour) allocates highest-first, so a guest's id is never reused
and cannot collide with the operator's own machines. A full range refuses rather
than falling back.

### A refused resize reported a finished provision

`resize_disk` answers with a UPID that nothing waited on - the fifth
"acceptance is not completion" site in this codebase. An invite promised 30 GB
from a 32 GB template, PVE declined the shrink, and the provision reported
success. Harmless in that direction; in the other, you would be told you got
40 GB of a 32 GB template and find out when the disk filled.

### An invite may not promise a disk PVE will refuse to make

The caps are frozen at mint - the contract the redeemer is promised - so that is
where the disk is now clamped to the template's own.

### A reason truncated before the reason is not a reason

Failure detail kept the FIRST 240 characters of guest output. Tailscale's
installer runs under `set -x`, so that kept the command trace and discarded the
error: all anyone could see of a real failure was `+ mkdir ... + curl ... +
tee ... + curl`, which sent the operator hunting a template that was fine. The
tail is kept now, and the install waits for the guest's own package lock with
its own exit code - apt exiting 100 because `unattended-upgrades` still held the
lock is not an install failure.

## 3.6.21 - 2026-08-30

**The self-check could only ever see a HEALTHY Proxmox token.** Found by
re-driving 3.6.20 on a real control plane: with a genuinely refused write token
the Proxmox probe timed out and reported `unknown` - "treat it as unproven" -
while `/health` on the same instance correctly said `write_token_refused`. The
check added in 3.6.19 to notice a refused write token could see a good token and
never a bad one, which is the entire thing it exists for.

Measured rather than guessed: PVE delays a refused credential by a steady ~3.0s,
warm and cold alike - deliberate anti-brute-force behaviour on their side, so
3.6.20's "one round trip" change could not have helped. The latency is in the
refusal, not in the number of calls.

A probe may now declare its own budget. Proxmox asks for 8s, with the reason
recorded beside the number. The report stays bounded, the larger budget is
opt-in, and a gate proves it does not loosen anyone else's.

*A check that cannot outlast the fault it hunts is not a check.*

## 3.6.20 - 2026-08-30

Four defects that DRIVING 3.6.19 found and the green suite did not. Three are
one root shape: **a rule that reads two different state objects, so two
surfaces give two answers** - which is #631's shape, hit three more times.

* **The self-check reported "No reconciler is registered, so nothing maintains
  the estate on a timer" about an instance running seven of them.** Over MCP,
  while `/admin/selfcheck` on the SAME process said they were all on time. The
  lifespan set the scheduler on `app.state`; the MCP tool holds the `AppState`.
* **A Proxmox settings reload left the `AppState` holding the client it had
  just closed**, so every MCP-side report answered `connection_status: error`
  with an empty token verdict while `/health` was correct.
* **3.6.19's own write-token check broke the Proxmox self-check**: three
  sequential round trips inside a 2-second probe budget turned `ok` into
  `unknown` against a real cluster. A check that times out establishes nothing.
  Both credentials are now probed concurrently, in one round trip.
* The two subsystems added in 3.6.19 had no UI labels.

`AppState` now declares `proxmox`, `mcp_app` and `reconciler_scheduler`, so a
caller holding it cannot silently miss one.

### Two of the tests written for 3.6.19 were vacuous

Recorded because it is the same failure this review keeps finding elsewhere: a
gate asserted the *string* `app.state.reconciler_scheduler = ...` appears in the
lifespan and that `"reconciler_scheduler"` appears in the probe. Both were true,
and neither established the two objects were the same one. **A source-string
gate proves names match, never that objects do.**

## 3.6.19 - 2026-08-30

The last three tranches of the platform review (#648), which finishes it:
the operator UI, inventory and the reconcilers, and the areas the review had
exempted by assumption. One belief, found nine more times: **the product states
conclusions it has not established.**

### The operator UI reported a calm it had not established (tranche 7)

Four screens, one cause - a read that did not come back, rendered as an answer.

* Overview composed its "needs attention" zone from four side calls made with
  `Promise.allSettled`. A rejection became an empty array and nothing else, so a
  backend that could not answer produced the exact screen of an estate with
  nothing wrong: **"Nothing needs you."** Each unread source is now its own line
  with its own door, and the calm sentence cannot appear beside one.
* The host page dropped every drift line when the drift read failed, so an
  unreadable check rendered as a host in spec.
* The change page polled the task it queued; a poll that threw stopped the timer
  and said nothing, leaving the spinner claiming "in progress" and every action
  disabled with nothing left running to correct either.
* Settings told an operator "the token was rejected" when the session re-read
  had simply not been answered - sending them to rotate a credential that was
  never the problem.

### Nothing could say whether the estate was being maintained (tranche 8)

* **Eight reconcilers run the estate and no surface reported any of them.**
  Every loop swallows its exceptions into a log line, so a loop crashing on
  every cycle was indistinguishable from a healthy one - and the drift loop is
  worse than indistinguishable: dead, it leaves the whole fleet on its last
  green verdict, so stopping it makes the estate look better. The self-check now
  reports them, naming per loop what stops happening.
* **A hypervisor sweep that did not finish was returned as one that did**, so
  the reconciler wrote "the whole fleet went absent" into the journal whenever
  the node list failed - or simply whenever no Proxmox is configured, every
  cycle, forever. The Sync button said "Synced: N hosts" either way.
* `enriched` counted hosts *looked at*, not hosts changed.

### The areas the review had exempted by assumption (tranche 9)

* **No one ever compared the fleet's agent versions to the control plane.**
  Enrolment serves the agent from the image, so a new agent matches the hub -
  and nothing upgrades it and nothing reported the gap. A fix living in the Go
  binary could ship, release and deploy without reaching a single managed host,
  with every surface green. Reported now, per host and fleet-wide.
* **The write token was never exercised** (#624). `connection_status: ok` only
  ever meant the READ token answered; on prod that held until a friend's first
  invite redemption 401ed in their face. Each credential is probed separately.
* **An operator-side failure burned the friend's only link** (#625). An invite
  now reopens when the build created nothing - and only when that is
  established, which required the failure path to record its unwind
  structurally (as the cancel path always had) and required distinguishing a
  clone PVE REFUSED from one whose fate is unknown.

### Tests that were defending defects

Three found and rewritten across these tranches: one asserting an unreadable
drift check must render "Nothing needs you"; one asserting a sweep that named no
hosts must report success and mark the whole database absent; and one that was
literally `assert hasattr(...) or True`.

## 3.6.18 - 2026-08-30

Tranche 6 of the platform review (#648): the knowledge base and search.

### A document was returned as the answer about a different, deleted document

Reproduced on the shipped image: a firewall policy came back, at full score,
tied with the genuine match for a query about a reboot-window policy the
operator had DELETED. Four shipped behaviours lined up to do it - `doc_metadata.id`
is a reusable rowid, deleting a document left its vector row behind, `reindex`
reassigned every id on every run, and the embedding store was a bare INSERT
whose `UNIQUE constraint failed` was swallowed at WARNING while the executor
still logged ", embedding stored". `get_kb_embedding_status` read 5/5 embedded
throughout.

### One write and a restart destroyed the vector index

Against a healthy embedding service, `indexed_with_embeddings` went 2 -> 0 while
search kept reporting `search_mode: "vector"`, and nothing rebuilt it.

### A note the product called `applied` was not in the knowledge base

`propose_artifact` returned `status: "applied"` for a note `get_kb_doc` then
answered "KB entry not found" for. That is the path `hp policy init` uses, which
prints "These policies are now in the KB" afterwards.

### Keyword search - the default mode of every install - looked for the whole query

It was one `LIKE '%<the entire query string>%'`, so `nginx business hours` found
nothing in a note containing exactly those words. Vector search had no relevance
floor, so a nonsense query returned every document. `search_mode` was read from
a key the service never set, so it was structurally always "unknown".

### The policy panel was wrong in both directions

Reviewing a change to one host it showed a policy whose own text said it applied
to a different host, and hid the global "no package installs without a snapshot"
rule against a plan that installs packages. All 19 notes `hp policy init` writes
are global, so none of them could ever have appeared.

### Two tests had enshrined the defects

`test_doc_insert_fails_still_succeeds` asserted that a KB note whose database
insert FAILED must report success; another asserted `search_mode == "vector"`
over an empty index. The suite was not blind to these - it was defending them.
Both flipped.

## 3.6.17 - 2026-08-30

Tranche 5 of the platform review (#648): metrics and alerting.

### An alert rule that could never fire looked exactly like one standing guard

`host_filter` is offered as a pattern - `*` is its default and its documented
"everything" - and it was compared with `==`. So a rule for `db-*` matched no
host, was skipped silently every evaluation cycle, and was indistinguishable in
`list_alert_rules` from a rule watching the fleet. Reproduced live in the
reconciler's own words: three enabled rules, `{'rules': 3, 'evaluated': 1}`.

The metric NAME was validated against nothing, and the MCP tool schema's own
example was `cpu.percent` - a metric no agent has ever emitted. The metrics test
suite is written on that same invented name, so the mismatch could not have been
caught by it. An operator, or an assistant reading the tool description, who set
up "disk low on all db-* hosts" was watching nothing at all.

Now: real glob matching, one metric vocabulary derived from the agent's own
source and gated against it, and `hosts_matched` / `last_eval_at` recorded per
rule so an inert rule says it is inert. The Overview reports
`rules_watching_nothing`.

### "Monitoring is part of the product" was true of collection, not of alerting

ADR-004 S5 says *"Nothing installs, imports or configures; monitoring is part of
the product."* Collection honours that. Alerting did not: a fresh install had
ZERO rules, so `firing_alerts: 0` sat green on the first screen an operator sees
while meaning "nothing is being watched". A fresh install now starts with two
default rules, guarded so an existing policy is never added to.

### Also

A latched alert on a deleted host could never resolve, because `delete_host` left
`alert_state` behind. A nameless agent was refused nowhere: its samples were
dropped while the hub acked them as accepted, so the agent freed its buffer over
data nobody stored. The self-check's events line named "artifact and task events"
only, though `alert_firing` rides that channel and, unlike an artifact, leaves no
durable record.

### S5's deferred decision, answered

S5 deliberately did not build rollups: *"measure a week of real data first"*. The
week has happened. Storage is a non-issue (128.9 bytes per row, ~6.3 MB per host
per week). The READ side is the problem: asked for the 7-day window it
advertises, dev returned 34.5 hours - 21% of it. Rollups are needed to make the
window readable, not to bound the table. Recorded in the ADR rather than
rewriting it.

Retention itself is sound, proved against real data: a 1-day horizon deleted
exactly the rows past the cutoff and nothing else, and no configuration can
delete a sample younger than 24 hours.

## 3.6.16 - 2026-08-30

Tranche 4 of the platform review (#648): backup, restore and data durability -
the capability that had already failed twice this week without anyone noticing.

### A full backup that could not restore the host, while saying it could

`hp export --include-secrets` printed, in red, that the archive was restorable.
On the deployment our own `docs/deployment.md` describes - compose, a data dir,
an `env_file` beside the compose file - the vault passphrase lives OUTSIDE the
data dir, and the secret paths were entirely data-dir relative. So the archive
carried the vault and not its key.

Restored onto a rebuilt host: `Failed to decrypt identity (wrong passphrase?)`,
the container exits, every secret unrecoverable - and no way back in, because
minting a token needs the admin secret, which is in the vault that will not
open. The trigger is following our own documentation.

### The remedy we printed destroyed the database

`run_migrations` refuses a database newer than the build and names
`backups/pre-migration-vN.db`. That file is WAL-mode, so copying it over
`homepilot.db` beside a stale `-wal` replays one database's journal into
another: `database disk image is malformed`, and now both copies are gone.

That is not hypothetical - it is the 2026-08-29 incident, reproduced by
following the sentence the product prints. There is now `hp db restore`, which
moves the sidecars aside and verifies the result, and `hp db check`.

### `database: ok` over a corrupt database

`/health` proved a connection opens (`SELECT 1`), not that the file is readable.
During the same incident it reported `ok` while `list_tasks` was 500ing on a
malformed image. It now reads a real row, and a reconciler runs `quick_check` on
its own connection - the app's page cache hides corruption from the connection
that has it open. A corrupt database now answers `{"status":"down",
"database":"corrupt"}` with a 503. `vault: ok` was likewise derived from a glob
of `*.age` files; it now opens the vault.

### Also

`.env.example` shipped in 3.6.14 and 3.6.15 with unresolved merge conflict
markers - the file step 1 of every install copies, and one `docker compose`
would refuse outright. Every parity gate passed over it: they assert that names
are documented and not inert, and not one asserted the file PARSES. Now gated.

The artifacts remote reported an off-box copy it had never made, and the README
promised a compaction that cannot happen at `auto_vacuum=0`.

## 3.6.15 - 2026-08-29

The first three tranches of the platform review (#648). Everything here was
found by DRIVING the product, not by reading it - the suite was green
throughout and found none of it.

### The read tier could take a managed host off the air, and lift the fleet's key

One allowlisted, **read-scope** command (`ls -laR /usr`) produced a reply larger
than the hub's frame budget. On a replay-protected connection - which is what
every shipped agent negotiates - the hub cannot MAC-verify a frame it will not
parse, so it closed the socket and unregistered the agent. A denial of service
against any managed host, from the weakest credential the product mints,
repeatable on demand, and silent: the agent reconnects moments later and nothing
tells an operator it happened.

The test guarding that path registered an agent **without** replay protection - a
shape no shipped agent uses. Green, and guarding a branch the product never
takes. The fix belongs in the agent, which must never produce a frame the hub
cannot accept: reads and command output are now bounded and say what they
dropped.

The same tier could **read any managed host's `/etc` as root** through
`read_file_on_guest` - including the unit file and env file holding the fleet's
shared enrolment token, the credential the MCP surface refuses to serve by name
elsewhere. The identical call over HTTP is admin-only; the tier gate never
compared them because those routes sat in an exclusion list. The agent now
refuses to hand back its own credentials, every tool must be mapped or declared,
and no tool may sit in zero tier sets.

### Revoking your last admin token reopened first-run claim

`is_claimed()` answered from "an admin token exists". On any instance that never
went through `POST /claim` - `hp init`, a console-minted token, anything
predating the claim - that token was the ONLY thing holding the claim shut.
Delete it, which is the ordinary first half of a rotation, and the instance
answered `unclaimed`: a codeless `POST /claim` from the LAN then minted a fresh
superuser token. The claim now latches at boot.

### The idempotence guarantee was decorative, and failed open

`idempotence: via-precheck` is what the spec REQUIRES for the mutating steps of
`proxmox-api-sequence`. It failed open three ways at once: the documented
binding (`response.status_code`, `response.json`) did not exist, because the
executor bound Proxmox's raw envelope instead of the documented proxy - so the
expression evaluated to `None`, `None == 200` is `False`, **and `False` is the
branch that mutates**. A precheck whose own call errored fell through to the
step. And every evaluator failure became `False` rather than raising.

Every precheck written the way the spec documents ran its step, every time.

### The sanctioned way to do multi-target work did not work

The spec says approving a composite approves its proposed steps, and names
`composite` as the answer to multi-target operations. Nothing implemented the
cascade: the composite went `approved`, its sub-artifacts stayed `proposed`, and
the apply died. The documented answer to "how do I change 50 hosts" was one the
product could not execute. The cascade is now atomic, recursive and audited.

### A failed read was recorded as absence, and the file was overwritten

`capture_pre_state`'s own docstring says a read that fails is recorded as
"unknown", because rolling back to a guess is how an undo deletes something. The
code wrote `existed: False`. A read fails for permission, for size, for a denied
path - none of which mean the file is not there. The apply then overwrote it as
a first write and the revoke reported it "created by this artifact". The prior
bytes were gone, with nothing recording that they had existed.

### Also

The audit trail recorded every REFUSED command as `success` and every
MCP-issued operation as `caller: unknown` - so "what was blocked on my hosts?"
returned nothing, and fleet-root operations from the primary interface were
unattributed. `exec_on_host`'s tool description claimed to be unrestricted when
the agent enforces its allowlist on everyone. #642's A5 and A10 were still live:
a failed agent count read as "new install" and PERSISTED a transport flip that
strands the plaintext fleet; an unreadable hub certificate was regenerated,
re-pinning every agent. #627 (a snapshot name over PVE's 40-char cap killing any
apply with a long artifact id) and #635 (a validation error reaching MCP callers
as "Internal server error") are both fixed.

`hp token create` hung for ever, minted nothing, and left an orphaned process.
Not the suspected sqlite lock: it opened an aiosqlite connection, raised on a
schema guard, and left it open, so CPython joined the worker at exit. **No test
in the suite had ever spawned the CLI** - fourteen files drive it in-process -
so a defect whose entire symptom is "the process does not exit" was invisible by
construction. Its gate now runs the real binary with a deadline.

## 3.6.14 - 2026-08-29

### A confined guest agent is not a broken network

Running the join against a real Fedora guest showed the last of it. On any
SELinux-enforcing distribution (Fedora, RHEL, Rocky, Alma, CentOS) the
qemu-guest-agent runs as the confined domain `virt_qemu_ga_t`, which may not
open http or https. From the installer's side that is indistinguishable from a
dead route: `curl` simply cannot connect.

So the guest was told to check the route out - and the route out was fine. From
that same guest, TCP to `1.1.1.1:53` connects and `:443` returns EPERM before a
packet leaves. Sending an operator to fix a working network is the most
expensive way to be wrong, so the installer now asks `id -Z` first and names the
confinement, with the remedy that actually applies: put tailscale in the image,
or install it in the guest yourself.


### The tailnet join, run against a real guest (#628)

3.6.12 shipped an install-then-join that had never been run against a guest. A
live run on dev proves it fails on the first one it meets, 28 seconds in,
logging one line: `Could not ask vmid 101 whether tailscale is installed`. The
task recorded `"tailnet": "failed"` and nothing else - no reason, for the
operator or for the friend holding the machine.

**`curl ... | sh` cannot fail.** A pipeline's exit status is its LAST command's,
so a download that 404s, is refused by DNS or is cut off feeds `sh` an empty
script and `sh` exits 0. The comment above it claimed `set -e` covered that; it
did not. The installer is now fetched to a file, checked non-empty, run, and
then the binary is looked for - because an installer exiting 0 is not evidence
that anything was installed either (#642, one layer down).

**The join fired before the guest could answer.** The machine had booted and an
IP had come back, but qemu-guest-agent need not have started accepting commands
yet. The join now waits for a ping, bounded - and when the agent never answers,
says so instead of blaming the key. (This was NOT what the live run turned out
to be failing on; see the `agent_exec` section below for that.)

**curl is not a given.** A cloud image ships the fetcher its distribution chose,
and the images that ship qemu-guest-agent are not the same set as the images
that ship curl. Whichever of curl / wget / python3 is present is used; a guest
with none of them is told so by name.

**A failure now says why**, in the requester's own words, with every auth key
scrubbed out of it - the one we sent, by value, and anything else shaped like a
`tskey-`. "Your key was already used" is actionable. "failed" cost a rebuilt
guest to diagnose.

**"I could not look" is not "it said no" (#642).** `tailnet` has a third value,
`unknown`, on the paths that established nothing: the agent never answered, PVE
refused the exec, the install or the join ran out of time. It is not pedantry -
`failed` tells the redeemer to mint a fresh key, and on these paths a fresh key
cannot help.

**The staged key is shredded through a call whose result is read.** The cleanup
fired `rm` through `agent_exec`, which answers with a pid: the same "acceptance
is not completion" mistake the join itself was built on, and what it leaves
behind is somebody's auth key on a disk.

Also: only one join runs against a guest at a time (two would overwrite each
other's staged key file, so the second is refused), and the whole join is
contained - nothing it raises can turn a built machine into a failed provision.

How far the live evidence actually goes is set out in "What the live run proved,
and what it did not" below. It stops short of the installer running.

### `agent_exec` sent PVE a parameter that does not exist (#628)

The live root cause, found by running the 3.6.12 code on dev and reading the
reason the new failure text carries:

```
"tailnet": "unknown",
"tailnet_detail": "The guest agent answered a ping but would not run a command:
 POST nodes/pve/qemu/102/agent/exec -> 400: {\"errors\":{\"capture-output\":
 \"property is not defined in schema and the schema does not allow additional
 properties\"}}"
```

3.6.12 added `agent_run` - exec, then poll exec-status until it exits - so that
a `tailscale up` which failed could not be reported as a join. It sent
`capture-output: 1` with the exec, reasoning that output had to be asked for.
PVE's exec endpoint declares `additionalProperties => 0` and has never taken
that parameter, so it refused **every** call. PVE sets `capture-output` on the
guest-agent command itself, so exec-status carries out-data/err-data anyway.

So the fix that was supposed to make the join honest could not run at all, and
the operator saw one line: *"Could not ask vmid 101 whether tailscale is
installed"*. Nothing had been run inside a guest by this code path, ever.

The fakes are why: every one of them answered exec with a pid whatever was
sent. They now enforce PVE's own parameter list and refuse an extra exactly as
PVE does, in the dedicated adapter gate and in the portal journey's transport.

### What the live run proved, and what it did not

Run on dev against a real Fedora 44 guest, through the product's own surfaces.

**Proved.** The guest agent is waited for and answers; `agent_exec` +
`exec-status` complete; the wait loop reads a real exit status; that status
reaches the caller; and the reason text names the stage that failed and quotes
the guest. A fresh provision, end to end, 39 seconds:

```
tailnet: failed
tailnet_detail: The guest could not download the tailscale installer. Name
  resolution was not shown to be the problem, so the route out is the thing to
  look at. The guest said: curl: (7) Failed to connect to tailscale.com port 443
  after 5087 ms: Could not connect to server
```

and the same through `POST /guests/{vmid}/tailnet-join` on an existing guest,
which is the re-join surface working end to end.

**NOT proved, and it matters.** The vendor installer never ran, `tailscale up`
never ran, and the Tailscale control plane was never reached - because on the
dev guest network a FENCED GUEST HAS NO ROUTE OUT. It gets an address and
resolves names through the subnet's own dnsmasq, and then cannot open a
connection. So on that network no tailnet join can succeed at all, installer or
no installer, and the remaining work for #628 is the guest network, not this
code path. The docs asserted the opposite ("a fenced guest still routes out, it
is only the LAN it may not touch"); that claim is removed.

**Also not proved: that a guest appears on a tailnet.** No tailscale auth key
exists in this estate, so every live run used a deliberately invalid one. The
registration itself is untested.

### Retrying a join with a fresh key (#628)

A failed join used to be terminal: the status page told the redeemer to run
`tailscale up` themselves on a machine they had only just been handed, and
nothing in the product would try again. The commonest cause - a key that has
expired or has already been used - is fixed by a FRESH key, which is exactly
what the original provision cannot be given.

The redeemer's own **guest portal** status page now shows the reason and offers
a form for a new key; it can only ever reach the machine that invite built,
because the vmid and node come from the invite's own provision result and never
from the posted body. Operators get `POST /guests/{vmid}/tailnet-join` and the
`rejoin_tailnet` MCP tool, both admin, both driven by the same resolution as
each other.

There is deliberately **no CLI command**: an `--auth-key tskey-...` flag would put
the key in an argv and in shell history, which is the one property this code
path exists to protect. A standing gate forbids one.

### The tool that reports outcomes could not report these (#628)

`get_task_result` declared `artifact_id: string` while every provision,
tailnet-join and template-build task carries NULL, so an MCP client validating
structured content refused the whole answer: *"Structured content does not match
the tool's output schema: data/artifact_id must be string"*. Reproduced live on
dev against a real provision task. The field is nullable now, and the task's own
`result` comes back with it - reading a provision used to answer with four empty
strings and nothing about the machine it built.

### A 422 no longer hands back what it rejected

FastAPI's default validation handler echoes the rejected value under `input`.
This API takes a `tailscale_auth_key` in a request body, so a key that failed
the shape check came straight back out to its caller and into whatever logs the
round trip touched. `loc`, `msg` and `type` are kept; the value is not.

### Two fakes that were why a green suite saw none of it

`tests/test_provision_service.py` stubbed `agent_run` outright, deleting the
exec-and-wait loop that is half of what was wrong, and left `agent_ping`
un-stubbed - which on an `AsyncMock(spec=...)` answers truthy, silently asserting
that every guest's agent was up. The guest in the new gates is a real `/bin/sh`
with a PATH of fake binaries the test composes, running the ACTUAL scripts: the
`curl ... | sh` defect fails a test in one line, which is the only way that class
of bug is ever caught.

## 3.6.13 - 2026-08-29

### An unread is not a fact (#642)

Five separate bugs were fixed in 3.6.10-3.6.12 before the pattern behind them was
named: a surface states a conclusion it has not established, drawing a verdict
from one signal while the evidence that would settle it sits unread. Where the
unknown case defaults to a confident answer that authorises a WRITE, HomePilot
changes the estate off evidence it never obtained.

The project already had the rule, in the one place that got it right (#425):
"I looked and it matches" and "I could not look" are different answers.

**The absent sweep ran off an incomplete picture.** The per-node guest listing is
wrapped in a handler that logs and carries on, so a node that failed to answer
left its guests out of the seen-set while the sweep ran anyway - stamping
`absent_since` and blanking `status`/`pve_status` on live machines. It fires on a
schedule, with no operator action, and it tells a guest their machine is gone. A
node rebooting is enough. A malformed node list was worse: it became an empty
list, so the sweep ran past an empty seen-set and would have marked the entire
inventory absent.

**A guest-network survey that could not read everything now plans nothing.**
Every read leaves its field empty on failure, and an empty field is
indistinguishable from "the cluster has none" - so a failed zone listing planned
`create-zone` over the operator's working one, and a failed firewall-rules read
re-planned every fence DROP on top of the rules already there. Ten writes, off
one failed read. The plan now carries blockers instead, and the executor already
refuses a plan that has one.

**An offline node could take a cluster-scoped apply.** `status == "online" or
node` made the status test dead code: every row carries a node key, so the first
row always won. Only a node that says it is online is picked now, and when none
does, nothing is guessed.

**And #631's bug in three more places** - `/health` and both `InventoryService`
constructions still read the environment half of the Proxmox address. `/health`
answered `not_configured`, an off-by-choice verdict counted as healthy, about a
vault-configured hypervisor it could not reach.

### Two gates that were not there

Every release commit updated this file until three in a row did not - which were
exactly the three no test covered. The version parity net now holds the changelog
beside the six files it already guarded.

And six CLI tests failed on one machine while passing on another: `rich` decides
at import time whether to emit ANSI escapes, so `FORCE_COLOR` in the environment
wrapped the output those tests assert on. A suite whose result depends on the
caller's terminal is not a gate; the colour environment is now cleared before
anything imports rich.

## 3.6.12 - 2026-08-29

### A destroyed machine stops counting against its owner (#613)

The inventory reconciler stamps `absent_since` the moment the hypervisor stops
reporting a guest, and three readers ignored it. A guest's VM was destroyed out
of band on prod; four minutes later HomePilot had recorded it as gone, and his
usage still read **1/1 machines**. His next invite would have been refused with
"Cannot build machines right now", and neither he nor a fresh invite could have
cleared it - only an operator editing the database.

`usage_for` now counts only machines that are still there. The guest page reads
`gone` with a `gone_since` rather than passing the last-seen `online` through -
that page is the guest's only window onto whether their machine exists. And
marking a host absent clears its `status`/`pve_status` to `unknown`, so a
destroyed guest stops answering "running" to every reader that does not also
check `absent_since`.

### A tailnet join can succeed at all (#628)

**Nothing installed tailscale.** The join ran `tailscale up` inside a stock
cloud image, so a guest handed a perfectly good auth key recorded
`tailnet: failed` and no retry would ever have helped. A guest without tailscale
now gets the vendor's installer first, behind **`HP_PROVISION_TAILSCALE_INSTALL`**
(default `1`, resolved at use time like the other provisioning defaults; `0` for
an image that ships its own or a guest with no route out).

The join is also waited for now. PVE's guest-agent exec is fire-and-forget - it
answers with a pid, not a result - so a `tailscale up` that exited non-zero on an
expired key was recorded as a successful join. The exec-and-wait loop moved onto
`ProxmoxClient.agent_run`, shared with the agent installer, which had the only
correct copy of it.

## 3.6.11 - 2026-08-29

### The pre-apply snapshot finishes before the change it protects runs (#636)

PVE answers a snapshot with a UPID and returns at once, leaving the guest LOCKED
while the task runs. The executor POSTed its snapshot and went straight to the
first step, which arrived at a locked guest:

```
[stop-101] POST /nodes/pve/qemu/101/status/stop -> ERROR 0: PVE task
           ...qmstop... finished with exitstatus 'VM is locked (snapshot)'
```

A raced step fails loudly. A raced snapshot is worse: the apply proceeds to
change a guest whose rollback point is not finished being taken, so the artifact
believes it has a safety net it does not have. The recorded `snapshot_id` was
also the task's UPID rather than the snapshot name an operator would roll back
to; it is now the name.

Third and last call site of the same mistake 3.6.10 fixed for sequence steps and
cancel cleanup.

## 3.6.10 - 2026-08-29

### The hypervisor is reported as configured when it is (#631)

`get_selfcheck` reported "No hypervisor is configured, so inventory stays empty
and guest provisioning is unavailable", and `hp status` printed
"PVE host | (not configured)" - on an install that was listing nine inventory
items and provisioning guests off 10.0.0.1.

`settings.proxmox_host` is only the ENVIRONMENT half. An install claimed through
the web UI keeps its hypervisor in the vault, and that secret wins - but the
resolved value was never written back, so the two surfaces that read settings
directly lied about a working install. There is one resolver now,
`homepilot/proxmox_config.py`; `app_state` and the admin router delegate to it,
`AppState` carries the resolved host, and both surfaces read that.

### A UPID is acceptance, not completion (#629, #626)

A PVE call that spawns a worker answers with a UPID the instant the work is
ACCEPTED. `proxmox-api-sequence` logged `-> OK` on that and ran the next step, so
a sequence outran its own cluster:

```
[stop-101]    POST /nodes/pve/qemu/101/status/stop -> OK
[destroy-101] DELETE /nodes/pve/qemu/101 -> ERROR 500: "VM 101 is running - destroy failed"
```

It also called an asynchronously-failed task a success. Each mutating step now
waits on its task and fails the step when that task ends non-OK; the node comes
from the UPID itself, because a step may address a node the target does not name.

The cancel path had the same mistake with a worse ending: it fired `delete_vm` at
a guest that may already be running, which PVE refuses - so a cancelled provision
recorded "cleanup failed" and left a live VM on the node. Cancel now shares the
stop-then-destroy the post-failure cleanup already had, and waits for the destroy
task, so "deleted" means gone.
### HomePilot gives a guest its address itself (#630)

Prod's SDN guest network has no DHCP server. A `simple` zone serves DHCP
through **dnsmasq**, the node does not have dnsmasq installed, and installing it
is a node mutation the operator refuses - rightly. Provisioning wrote
`ipconfig0=ip=dhcp` regardless, so the first real guest booted with a
link-local address while everything reported success: the clone finished, the
fence was written, the VM started, the task said "succeeded".

Provisioning no longer depends on a server this product does not run. A new
**`provision_ip_mode`** setting (Settings -> Provisioning defaults,
`HP_PROVISION_IP_MODE`) defaults to **`static`**: when the resolved `ipconfig0`
is `ip=dhcp` and the guest is going onto the guest network's own vnet,
HomePilot allocates a free address out of the guest subnet and writes
`ip=<addr>/<prefix>,gw=<gateway>` into cloud-init, with a resolver from the new
**`provision_default_nameserver`** (default `1.1.1.1`) - nothing hands one of
those out on a subnet with no DHCP either. `provision_ip_mode=dhcp` restores
the previous behaviour exactly.

Which address is free is decided by a **live scan of the cluster's own guest
configs** - every VM and container with a NIC on the vnet - never by a table.
So a destroyed guest's address is free the moment the guest is gone, and an
address an operator typed into the PVE UI is respected. The lowest free host
address at or above the tenth wins; `.1`-`.9` stay with infrastructure. A scan
that cannot complete refuses rather than risk issuing an address twice, and an
exhausted subnet fails the provision **before the clone**, so a refusal never
strands a half-made guest on the node.

An operator-written static address still wins, from the request, from the
`provision_guest` tool or from `provision_default_ipconfig`. An invite that
froze `ip=dhcp` in its caps is allocated at redemption rather than at mint, so
two outstanding invites cannot claim one address.

The allocated address goes onto the guest record and into the provision result
(which now also states the `ipconfig0` the guest was built with), so the friend
sees their address on the portal immediately - a bare cloud image may never run
the guest agent this used to wait for.

## 3.6.9 - 2026-08-28

### Provisioning can choose where a guest's disks land (#618)

Every clone inherited the template's storage, because the clone call never
carried one - so a cluster's guests piled onto whatever storage the template
happened to sit on, and no setting, request field or invite could move them.
A new **`provision_default_storage`** setting (Settings -> Provisioning
defaults, `HP_PROVISION_DEFAULT_STORAGE`) names the target storage; the
`POST /guests/provision` body, the `provision_guest` MCP tool, `hp invite
create --storage` and the mint route all take a `storage` of their own that
wins over it.

**Empty still means inherit**, and it means it by sending no `storage` key at
all - the pre-#618 behaviour, unchanged for every existing install. The clone
stays a FULL clone in every case: PVE only honours a target storage on one, and
a linked clone would bind the guest to its template forever.

Like its siblings the setting is probed against the live cluster before it is
saved, and the probe asks the question that actually bites: a storage the node
does not have is refused with the ones it does, and a storage that holds no
`images` content is refused too - PVE reports a backup-only storage happily,
and a clone aimed at it dies deep inside the clone task. An invite freezes its
storage AT MINT beside its node and template, so repointing the default never
re-targets an invite already in somebody's inbox.

## 3.6.8 - 2026-08-28

### A pre-apply snapshot goes to the guest's own collection (#617)

The executor's snapshot tried qemu, swallowed its error, retried lxc - so a
failing snapshot on prod showed the WRONG collection's 401 while the real
cause (a write token without VM.Snapshot) stayed hidden. The guest type is
now resolved and exactly one collection is hit, and a 401/403 names the path,
the status and the credential possibility out loud.


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
  apply ended with `Invalid transition: applied -> applied` and a `failed` task.
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
  the propose -> approve -> apply lifecycle, with read-only drift detection. Paired
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
  of the proxy. Resolution order: this setting -> non-wildcard bind host ->
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
  derives it (`stopped -> offline`, `running + ip -> online`) for nodes and guests,
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

- **Dual PVE tokens (read + write)**: Separate low-privilege read token and higher-privilege write token for Proxmox operations. Read operations use `pve-token` from vault; mutations (POST/PUT/DELETE) use `pve-write-token` if configured, otherwise fall back to read token. Configurable in web UI (Settings -> Proxmox) or via vault.
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

- **vitest 2.1.9->4.1.7**: Dev dependency bump in `/web/` (PR #287).
- **esbuild 0.25.12**: Fixed esbuild dev server CVE (medium, absorbed via transitive dep).
- **vite 6.4.2**: Fixed CVE-2026-39365 path traversal (medium, absorbed via transitive dep).
- **Deferred**: CVE-2024-47764 cookie (low, SvelteKit transitive dep, no safe fix).

## v2.2.2 (2026-05-16)

### Features

- **Auto-generate vault passphrase**: When neither `HP_VAULT_PASSPHRASE` nor `HP_VAULT_PASSPHRASE_FILE` is set, the system generates a 256-bit passphrase using `secrets.token_urlsafe(32)` and persists it to `{data_dir}/.vault_passphrase` (mode `0o600`). On subsequent starts, the persisted passphrase is loaded automatically. This enables zero-secrets deployment where `.env` contains no HomePilot secrets.
- **`_try_vault_secret` multi-key extraction**: The configuration resolver now attempts multiple keys when extracting secrets from the vault: `value` -> `secret` -> `key` -> `token` -> first value. This accommodates different vault secret formats (e.g., `pve-token` stored as `{"token": "..."}` vs `secret-key` stored as `{"value": "..."}`).
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