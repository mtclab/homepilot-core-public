# Changelog

## 3.10.0 - 2026-11-09

Changes you can follow, power from the Hosts list, and a console you can search.
An approved change no longer seems to vanish: Changes > Inbox keeps it as
"Ready to run" until it runs, every change can be approved and applied in one
press, and an approved change can be withdrawn. The Hosts list can start, shut
down, reboot and stop a guest from its row menu and says in the row what
happened, and a friend's guest portal now runs the same safety checks as the
operator's button. `Ctrl+K` searches pages, hosts, changes and knowledge;
Monitoring > Alerts is a full page; the Overview was relaid out.

Two fixes you may notice. `docker stop` now shuts HomePilot down cleanly: since
May 2026 the app ignored SIGTERM, so every stop waited for Docker's kill and
skipped the shutdown that stops background jobs and closes the database. And
the first request after a start no longer holds every other request for about
a third of a second while the app builds its route table; it is built before
the server starts listening.

### Operator actions

- Migration 40 runs at the first start, after the usual backup (`backups/pre-migration-v39.db`); it rebuilds `tasks`, keeping every row. To go back, restore that backup with `hp db restore`.
- Copy `proxy_read_timeout 360s;` from the reference vhosts into your deployed nginx vhosts (`location /` and the portal's guest/invite block), then reload nginx.
- MCP clients with a tool allow-list: add `withdraw_artifact`.

### `docker stop` shuts HomePilot down cleanly

The app installed its own SIGTERM handler at startup. That handler replaced
uvicorn's, logged "initiating graceful shutdown" and then did nothing, so the
server kept serving until Docker killed it. The handler is gone. uvicorn now
handles SIGTERM itself and runs the shutdown: it stops the reconcilers, closes
the agent hub, and checkpoints and closes the database.
`tests/test_sigterm_stops_the_server.py` starts the real entry point, sends
SIGTERM and requires a clean exit that completes the shutdown. With the old
handler restored, the server is still running when the test's 30 s wait ends.

### The browser tests pass again, and `make gate` runs them

3.9.0's `e2e` job on the public mirror failed twice:

- **Stale redirect expectations.** The test still expected `/tokens` to land on
  Settings, but 3.9.0 moved tokens to Assistants on purpose. The redirect test
  now lists every 3.9.0 move: `/tokens`, `/agents` and `/monitoring`, the
  Settings tabs that left (`?tab=` and the old `#` links for monitoring,
  alerts, guests and tokens), and query and fragment travelling with the
  redirect. Each case must match the exact landing address. The old check
  matched loosely, so a wrong target could pass by sharing a prefix (`/agents`
  "passed" on `/ui/hosts`).
- **A false alarm from the loop-block detector.** Playwright's sync API runs its
  own event loop, and the test's code runs inside one of that loop's callbacks.
  The detector timed a fixture's wait as a 62 s loop block. Playwright's loop
  belongs to the test driver, not to HomePilot, so the detector now marks it
  when Playwright creates it and does not time it. Every other loop is still
  timed. `tests/test_loop_block_detector_driver.py` proves both: a block inside
  Playwright's loop is not reported, and a real block on an ordinary loop in the
  same session still fails its test.

The fixture was waiting because the suite exceeded its own rate limit. Token
creation allows 5 attempts per IP per minute, refused attempts included. The
CI harness used one, and the suite then tried eight more: a fixture minted
spare tokens from before logout stopped deleting tokens (#389). The suite now
makes four attempts. The table at the top of `tests/test_e2e.py` lists each
one, and an attempt over the limit fails at once, naming the table, instead of
sleeping for a minute. The product's limits are unchanged. The redirect test
loads only the console shell and the session check, because loading every
destination page in full used most of the per-IP request limit, and the tests
after it then got 429s. The whole e2e run went from 80 s to about 30 s.

`make gate` now includes `gate-e2e` (`scripts/gate-e2e.sh`), which runs the
mirror workflow's steps locally:

- build the web UI with `npm ci`;
- start the server from this tree, with an empty environment and a throwaway
  HOME and data directory, on free localhost ports;
- create the run's token over HTTP;
- run `tests/test_e2e.py` in headless chromium.

It fails if any e2e test is skipped, or if the server does not stop cleanly
on SIGTERM. The first run needs network for `playwright install chromium`,
which goes into the user's Playwright cache, not the system.

### Changes you can follow: the Inbox, approve and apply, withdraw (humane Changes queue)

An approved change used to leave the review queue with nothing saying it still
had to be applied, so the queue looked empty and the change looked lost. Now:

- **Changes > Inbox** is the first tab (it replaces the review queue; old
  `/changes/review` and `/review` links land there). It holds proposed changes
  AND approved ones that have not run. An approved change stays as **Ready to
  run** with **Apply** and **Withdraw** on its card; a running one says
  "Running: <intent>, started 18:28" with the task's live state; a finished one
  moves to **Finished today** with the outcome in words. The Inbox is empty only
  when nothing waits. Keyboard: `j`/`k` move between cards, `a` approve and
  apply (asks first), `r` reject, `Enter` opens.
- **Approve and apply** is the primary action for every kind, in one press
  (admin session). A composite or a `replay_safe: false` change keeps two
  explicit steps and says why. A destroy or a forced stop asks for the guest's
  name typed back first.
- Every change shows a **plain-words summary** at the top (what it does, to
  which guest); the raw spec is behind a disclosure. The same summary is in
  `GET /api/artifacts/{id}` (`summary`) and the MCP `get_artifact`.
- **Withdraw** takes back an approved change that has not run (#797): new
  status `withdrawn`, terminal, runs nothing, audited, recorded as a task.
  `POST /api/artifacts/{id}/withdraw`, MCP `withdraw_artifact`,
  `hp artifacts withdraw <id>`. Refused while a run is in flight.
- **Revoking an approved change that never ran no longer runs its rollback**:
  nothing was applied, so there is nothing to undo.
- **PVE task warnings** are read from the task log and shown verbatim with a
  one-line explanation and a verdict (the UEFI `ms-cert=2023k` note is marked
  harmless), never just "WARNINGS: 1".
- The sidebar **Changes count** and the Overview include approved changes
  ready to run (`changes: {proposed, ready, running}` in the dashboard
  summary), and update when a change moves.

Migration 40 rebuilds the `tasks` table so a task can record a `withdraw`;
every task row is kept. Webhooks: a new event, `artifact_withdrawn`; re-import
`n8n/artifact-notification.json` if you route events through n8n.

### Fixes

- **#797:** a DELETE step's options (`body: {purge: 1}`) went to PVE as a JSON
  body and PVE answered `501 Unexpected content for method 'DELETE'`. A DELETE's
  parameters are now always sent as URL query parameters, so existing
  artifacts written with `body:` on a DELETE work as written. Options in the
  path (`?purge=1`) keep working too, also next to a `query`.
- **#796:** a run that changed part of what it was asked to is reported as
  `partly applied: steps a, b done; step c failed: <reason>` instead of a bare
  `failed`, on the artifact (`partial_apply`) and on its task. A failed config
  write is read back, so a refused `delete=scsi2,hostpci0` that removed scsi2
  says so.

### Hosts list: power from the row menu (UI slice 5)

- **Row menu:** Open host · Metrics · Open in Proxmox · **Console** (Proxmox's
  noVNC for a VM, xterm.js for a container) · **Start / Shut down / Reboot /
  Stop** · Adopt / Ignore · Forget. The verbs are judged by the guest's live
  state, read from Proxmox when the menu opens; a verb that does not apply now
  stays in the menu, disabled, and says why ("Already running", "Not running",
  "Needs an admin session", "Could not read its state from Proxmox").
- **Confirmation by tier:** Start, Shut down and Reboot of an ordinary guest
  confirm in place ("Confirm start"). A forced Stop, and any verb on the
  machine HomePilot runs on or a protected guest, open a dialog that names the
  VMID and node, says what happens, and waits for the host name to be typed.
- **Honest progress:** the row says "Starting... following Proxmox task" with
  the old state chip kept and marked `pending`. When Proxmox has settled it,
  the row shows the outcome and the chip the state the server read back. A
  refusal names the reason and the holder in the row, for example "Start
  refused: Address 192.0.2.20 is used by relay (VMID 112). Nothing was sent."
  The row stays in its group until you reload, so nothing moves under the
  cursor.
- **The row is updated at once:** after a power action the server writes the
  state Proxmox reported onto the inventory row, so the list, the guest portal
  and the overview agree before the next sync. A state Proxmox did not confirm
  is not written.
- **State chip for guests** is Proxmox's word (`running`, `stopped`), not
  `online`/`unknown` (a running guest without a known address used to read
  `unknown`).
- **`+ Add` menu:** Add a LAN device (the existing form), Provision a VM from
  template (shown disabled: there is no operator provision form yet), Enrol an
  agent (Hosts > Agents).
- **Table:** group headings stay table sections; the header sticks on a long
  fleet. The row menu opens above or below its row and is no longer cut off
  by the table's edge. A hand-added host's source reads "Manual" (it was an
  empty pill).
- **Phones:** under 768 px the fleet is a card list (name and chips, then
  addresses, VMID and env, the same row menu); the header buttons wrap.
- Fixed: a menu item that changes into its own confirm on click closed the
  whole menu in a real browser, before the confirm could be pressed.

### Guest portal: the operator's power guards (#781 follow-up)

The portal's `POST /guest/vms/{id}/power` had its own short copy of the
checks (gone, and the name at the VMID). It now runs the operator's
`PowerService`, the same guard function the operator's button and an approved
`guest-power` change use: address clash before Start and before a Reboot that
applies a pending address, a passthrough disk a running guest on the same node
holds, a locked guest, the wrong state, a reused VMID, a gone or template
guest, and a protected guest (off actions are refused on the portal; only the
operator can switch a protected machine off). The friend reads a plain
sentence; the operator's audit row keeps the full reason with the holder
named. The portal page says "Starting... waiting for it to finish" and then
the result.

For portal clients: a refused power action now answers `409` with a `code`
(for example `address_clash`, `disk_shared`, `locked`, `state`,
`confirmation_required`) and a sentence that names nothing outside the
friend's own machine. A power action that went through answers once it has
settled, with `outcome` (`ok`, `timed_out` or `unknown`) and `status`. Audit
rows for portal power actions are now `guest_start` / `guest_stop` /
`guest_reboot` (and `guest_power_refused`) with `source = guest-portal`, the
names the operator's actions use; they were `vm_start` and so on.

Why the nginx timeout (Operator actions): a power action now answers once
Proxmox has finished it, and a reboot can take five minutes at the default
shutdown timeout. With nginx's 60 s default you would see an error while the
action goes on. `deploy/control-plane/nginx-hp-proxy.conf` (`location /`) and
`deploy/portal/nginx-guest-portal.conf` (`location ~ ^/(guest|invite)`) carry
the line; `tests/test_power_proxy_timeouts.py` derives the bound from the
power code.

### Search from anywhere, alerts get their page, the Overview acts (UI slices 2-4)

- **Command palette.** Press `Ctrl+K` (`Cmd+K` on a Mac), or `/` when you are
  not typing in a field, or the Search box at the top of the sidebar (the
  magnifier on a phone). It finds pages (every sidebar noun and every tab),
  hosts, changes and knowledge-base entries; type a host's name and press
  Enter to open its page. Each source answers on its own: when one cannot be
  searched it says "could not search" in its own group instead of looking
  empty. Arrow keys move, Enter opens, Tab switches what you search in (the
  choice is remembered in this browser), Escape closes and puts you back where
  you were. A knowledge result opens the knowledge base with that search run
  and that entry expanded (`/ui/records/kb?q=...&open=...` now works as a
  link).
- **Monitoring > Alerts is a full page.** It starts with what is watched at
  all: how many hosts have a connected agent, how many do not (with the way to
  enrol them), and how many alert rules are on and how many match no host.
  Each firing alert links to its rule, and a new Deliveries list shows every
  alert sent in the window and who it reached, or "nobody was notified".
  "Send test" sends a test message to every enabled channel and says what each
  answered (admin sessions; others are told why there is no button).
- **The Overview, relaid out.** Needs attention comes first, with the Alerts
  strip beside it: two lines, how many fire and where alerts go, with the door
  to Monitoring > Alerts. Lines now show since when (hover for the exact time)
  and carry the fix where one exists today: Forget a gone host, Open window
  for hosts waiting to join (admin), Re-check drift, Cancel a stuck task. The
  ones that change something ask first, in place, and the page re-reads after,
  so a line goes away only when the server says it is fixed. A calm Overview
  says what it checked: "Nothing needs you - checked 6 sources just now." The
  two donut charts are replaced by one row of numbers (hosts, watched by an
  agent, agents, in spec, changes) above the host chips. Capacity numbers
  (RAM allocated, storage use) will join that row once the backend measures
  them; until then they are not shown.
- **Fixes found on the way.** Host names on the Alerts page were plain text
  instead of links when the inventory answered after the alerts; they link now.

The Overview no longer shows the alert list (it moved to Monitoring > Alerts)
or the two donut charts; nothing else about the pages you already use moved.

### The lanes together (release integration)

- **The first request no longer blocks the server.** FastAPI builds each
  route's request handling the first time a request reaches it. On the
  versions this release pins (`uv.lock`: fastapi 0.141.1, pydantic 2.13.4,
  starlette 1.6.0) that was about 140 routes and ~290 ms of CPU on the event
  loop, inside the first request after every start (usually the container's
  own health check), so every other request and the agent hub waited. The app
  now builds the whole table when it is loaded, before the server listens.
  `tests/test_first_request_does_not_block.py` starts a fresh process, runs
  the real startup, then times the first request: 0 routes built and 1.5 ms
  now, 129 built and 289 ms with the fix removed.
  The same release gate found one more first-use stall: the Proxmox library
  (and the HTTP stack under it) was imported inside the claim's token check
  and the guest-network reads, ~180 ms on the loop on a fresh install. It is
  now loaded with the app as well.
- **`make gate` refuses a venv that does not match `uv.lock`** (`gate-lock`,
  first). The 3.9.0 gate passed in a venv that had drifted to newer
  fastapi/pydantic/starlette, which is why it never saw the stall above.
  The check is uv's own (`uv sync --locked --check --offline`) and names
  every package that differs.
- **One confirm dialog.** The Changes and the Hosts work each built a typed
  confirm dialog; there is now one (`ConfirmDialog`), a real modal dialog that
  both use, with both sets of tests.
- **The palette and the Overview follow the Inbox.** The palette offered the
  retired review queue, and the Overview's Changes number counted proposals
  only. Both now read the same Changes tabs and count what waits in the Inbox
  (proposed and ready to run).
- **The SIGTERM test and `gate-e2e` boot from a data directory the app
  accepts** (under `$HOME`, not `/tmp`, which the app refuses for artifacts);
  they only passed where `TMPDIR` pointed under `/home`.
- **Journeys on the built UI with guests.** `tests/test_e2e.py` gains
  `TestFleetJourneys`: the real app and built UI over a fake Proxmox
  (`tests/e2e_fleet_server.py`), so `make gate-e2e` now walks a row-menu Start
  to `running`, a Start refused for an address clash naming the holder, the
  390 px card list, the portal's `409 address_clash` naming no other machine,
  `Ctrl+K` "llm" Enter, Monitoring > Alerts at 1280 and 390, and approve and
  apply versus approve only.
- **Read on the walk, fixed.** The Hosts row menu reads as one list (in Day
  each item looked like a separate outlined button). The sidebar foot says
  "Signed in as <name>" and the access in words ("Full access") instead of a
  bare label over "all". On a phone, a change waiting in the Inbox keeps its
  title readable, with the buttons under it inside the card.

## 3.9.0 - 2026-11-08

Four pieces of work in one release. You can now start, shut down, reboot and
stop guests from HomePilot, with guards that refuse an unsafe action and say
why (B3). The web console has a new look and a new sidebar (UI slices 0 and 1).
Under the hood, nothing slow runs on the one event loop any more, so a slow git
push or disk can no longer freeze the API (architecture step 1). Database
migrations are checked when the app starts, and the schema is pinned in a lock
file (architecture step 2).

New setting, optional: `HP_LOOP_STALL_MS` (default 500, `0` turns it off)
logs where the app was stuck whenever the event loop is held that long. Old
console addresses (Settings tabs, `/tokens`, `/agents`) redirect to their new
pages, so bookmarks keep working.

### Operator actions

- Migration 39 runs at the first start, after the usual backup
  (`backups/pre-migration-v38.db`). It adds one empty column,
  `hosts.protected`. To go back to 3.8.0, restore that backup with
  `hp db restore`.
- Check which guests are protected. By default the guest HomePilot runs on and
  every `env = infra` guest need their name typed to shut down, reboot or stop.
- MCP clients with a tool allow-list: add `get_guest_power` and `power_guest`
  if your assistant should have them.

### Migrations cannot drop a version, and the schema is locked (architecture step 2)

Versions 1-38 are untouched: every one runs the
same statements as in 3.8.0 (each version's statements hash identically, and
the migrated schema's fingerprint is the same before and after), so an install
at any schema version upgrades exactly as before. The pre-migration
`VACUUM INTO` backup is unchanged.

- The migration chain is an ordered tuple of `Migration(version, statements)`
  instead of a `{version: [...]}` dict literal. A dict literal keeps only the
  last of two equal keys, so two lanes that each added "the next" version could
  merge into one that silently dropped the other's DDL (it nearly happened in
  3.7.0). The module now refuses a repeated version, a gap or a reorder when it
  is imported, and the runner re-checks the versions it is handed before it
  touches a database. `tests/test_migrations_contiguous.py` replaces the
  source-reading check that guarded the dict.
- `src/homepilot/db/schema.lock` records the schema the chain produces (tables,
  columns with type, NOT NULL, default and key, foreign keys, indexes, the
  normalised `CREATE` text) and a content hash of every version.
  `tests/test_schema_fingerprint.py` rebuilds it from a fresh install and from
  an upgrade off every older version (0-37), and fails on any change nobody
  recorded or on an edit to an already recorded version. After an intended
  schema change: `make schema-lock`; it refuses to rewrite a recorded version's
  hash unless `--rewrite-history` says the version was never released.
- An orderly shutdown checkpoints the WAL explicitly
  (`PRAGMA wal_checkpoint(TRUNCATE)`) once every writer has drained and before
  the database closes, on its own daemon thread and bounded at 3 s (the #795
  pattern, now one shared helper). The close then has no checkpoint left to do;
  a checkpoint that cannot finish leaves the WAL for the close or the next
  start, which is safe.

### Nothing blocking runs on the event loop (architecture review F1, step 1)

One asyncio loop carries the API, the SSE stream, the MCP transport, the agent
hub's heartbeats and every reconciler. #795 moved one sqlite close off it after
a WAL checkpoint froze the app for 16 s; the architecture review found the same
shape everywhere git was used. Fixed as a class, not a call:

- **`run_blocking(pool, fn, ...)`** (`homepilot/runtime/blocking.py`) runs
  blocking work on named, bounded thread pools: `git` (ONE thread - every
  repository operation runs there, in submission order, so commits, pushes and
  remote changes never interleave), `io` (4: artifact reads and listings, KB
  file walks, vault key derivation and writes, TLS material, sqlite integrity
  checks, agent payload hashing) and `net` (4: DNS and socket probes, so a
  hanging resolver cannot starve disk reads).
- **Moved off the loop:** every git subprocess of the artifact store (commit,
  push, pull, remote changes, the preview's `git diff`) and its `time.sleep`
  lock spin; the blocking per-artifact `flock` - each lifecycle transition's
  lock-read-check-write now runs as one serialised step on the `git` pool,
  exactly as atomic as it was on the loop; propose's "id is free" check and
  write, as one step; every artifact read and full-corpus listing (routes, MCP
  tools, the executor, task runner, drift/apply/verify reconcilers, KB
  service); startup's `git init` and store construction, hub certificate
  generation and loading; the TLS migration's certificate; vault identity
  creation/migration, passphrase rotation (two PBKDF2 derivations) and secret
  writes (fsync); the archive push (was on the default pool, racing commits);
  the agent payload digests (`/agents/dist*` hashed multi-MB binaries per
  request - presence checks no longer hash at all).
- Artifact files are now written atomically (temp file + rename): reads run
  on another thread than writes and must never see half a file.
- **Loop-stall watchdog** (`HP_LOOP_STALL_MS`, default 500, 0 = off): when one
  callback holds the loop past the threshold, the loop thread's Python stack
  is logged WHILE it is blocked, and `/admin/selfcheck` gains a
  `loop_watchdog` line (a stall in the last 15 minutes reads as a fault).
  Idle cost: four short wake-ups a second.
- **Gates:** the test session fails any test whose product code blocks an
  event loop - every loop in the process, timed per callback
  (`HP_LOOP_BLOCK_BUDGET_MS`, default 250 ms, garbage collection excluded),
  with the stack sampled while it blocks and the block attributed to the
  coroutine running it (a test's own synchronous setup is not product code) -
  and any product code reaching a blocking primitive (`assert_off_loop` in
  git, the git lock and the file lock) on a loop however fast it is. The
  allowlist is empty. A static gate lists every blocking primitive in `src/`
  by file; a new one fails until it is routed and reviewed.
- The first-run claim's code file and the claim's local CLI token are written
  (fsync) off the loop too.
- Application logging goes through a queue: a log call on the loop only
  enqueues and one listener thread writes to stderr, so a slow log pipe can
  no longer stall the loop inside `stream.write` (the detector caught a 359 ms
  one).

### Operator power controls: start, shut down, reboot, stop (lane B3, #781)

The operator could not start a stopped VM or reboot one so a pending
cloud-init change applied without writing a `proxmox-api-sequence` by hand or
leaving for the Proxmox UI. Now, for VMs and containers alike:

- **An operator acts directly.** `POST /api/inventory/{host_id}/power`
  (admin scope) with `start`, `shutdown`, `reboot` or `stop`; the host page has
  a Power card and the Hosts row menu a "Power..." entry. No propose/approve
  round trip; one audit row per action names the actor, the guest, the PVE task
  and the outcome (refusals are audited too).
- **The result is honest.** The PVE task is followed to the end and the guest's
  state read afterwards: `ok`, `failed`, `timed_out` or `unknown`. A shutdown is
  always sent with `forceStop: 0`; a guest that ignores it ends `timed_out`,
  still running, with Stop offered - never a silent escalation, never a false
  success. A reboot is Proxmox's own (pending config applies) and waits for the
  guest to come back; the pending changes are shown before it.
- **Guards refuse with a reason and send nothing:** the guest is gone, a
  template, locked, or another machine now holds its VMID (name checked live);
  Start, and a Reboot that applies a pending address, would take an address a
  RUNNING guest holds (the holder is named - the 2026-11-07 incident); Start
  would open a `/dev/...` passthrough disk a running guest on the same node has
  attached (the holder is named); shutting down, rebooting or stopping the
  machine HomePilot runs on, or a protected guest, needs its name typed back.
- **Protected guests:** new `hosts.protected` (operator-only via
  `PATCH /api/inventory/{id}`; an assistant token is refused). Unset means the
  default: the self guest and every `env = infra` machine are protected.
- **Assistants propose, never power.** MCP `power_guest` runs every guard,
  then proposes a one-step `guest-power` artifact (new kind, ARTIFACT_SPEC
  §5.8) and powers nothing; the apply re-runs the guards. Switching off the
  self guest or a protected one is never proposed. `get_guest_power` reads the
  state, the pending changes and what each action needs.
- **Approve and apply in one step** did not exist after 3.8.0, so it is added
  for this kind only: `POST /api/artifacts/{id}/approve-and-apply` (admin) and
  "Approve and apply" on the change page. Other kinds keep two steps until the
  approval UX work settles one rule.
- **Fix (lane B2's start guard):** a running guest's addresses are now read
  with `current=1`. PVE's plain config read answers with pending changes
  applied, so a running guest with a queued ipconfig change was judged by the
  address it does not hold yet, and the one it does hold went unseen.

### Web UI: the "Instrument" look and the new shell (UI slices 0 and 1)

The first two slices of the console rethink (owner decisions of 2026-11-08:
direction "Instrument", Monitoring as its own page, Agents under Hosts). Page
internals are unchanged; they moved and took the new look. No backend change.

**Look (slice 0).**
- New design tokens in `web/src/app.css`: a neutral near-black **Night** theme
  (the default) and a cool-grey **Day** theme, one phosphor-lilac accent for
  what you can touch, and red / amber / green kept for status only. A hollow
  grey lamp means "no data".
- A theme control in the sidebar foot: **System / Night / Day**, remembered per
  browser; System follows the OS live. `static/theme-boot.js` paints the choice
  before first paint (an external file, because the CSP refuses inline scripts).
- Self-hosted type under `/ui/fonts` with each family's OFL licence beside it:
  Instrument Serif (page titles and the wordmark only), Source Serif 4 (reading
  text), Atkinson Hyperlegible (chrome), JetBrains Mono (numbers, tags, ids).
  Nothing is fetched from a font service.
- Denser rows (about 26 px at 12.5 px text), 2 px panels, mono tags with the
  status colour as their edge, and new `Lamp`, `Tag`, `Panel` and `Icon`
  components. Every text glyph that stood in for an icon (refresh arrows,
  crosses, ticks, chevrons, the menu glyph) is an inline SVG icon now.
- Gates: the token test checks every text pair (WCAG AA) and every boundary
  (3:1) in **both** themes, that Day redeclares every colour, that every font is
  self-hosted, present and licensed, and that no gradient is painted; a new
  glyph gate fails on any arrow, symbol, shape or emoji in rendering code.

**Shell and navigation (slice 1).**
- The sidebar groups seven nouns as four jobs: Overview; Estate (Hosts,
  Monitoring, Guests); Change (Changes, Assistants); Record (Records, Settings).
  Monitoring shows the number of firing alerts and Changes the number of
  proposals, both from the dashboard summary; a count is absent, never zero,
  when the summary cannot be read. Guests appears once a guest or an invite
  exists (admin sessions). The maker's mark moved to the sidebar foot.
- **Monitoring** is a page: Alerts (the alerts list) and Rules (alert rules,
  notification channels and webhooks, moved out of Settings).
- **Hosts > Agents** holds the join requests, the enrol one-liner and the hub
  token; the fleet list keeps one line naming who is waiting, with the door.
- **Guests** and **Assistants** are pages. Assistants is the token list for
  now - naming assistants, limiting their tools and showing their activity are
  not built yet, and the page says so. Settings keeps Connection, Proxmox,
  Subsystems and About.
- Every old address lands on the new home, keeping its query and fragment:
  `/settings?tab=monitoring` and `#monitoring` / `#alerts` go to
  `/monitoring/rules`; `/settings?tab=guests` and `#guests` to `/guests`;
  `/settings?tab=tokens`, `#tokens` and `/tokens` to `/assistants`; `/agents`
  to `/hosts/agents`; `/monitoring` to `/monitoring/alerts`. The redirects that
  existed before all stay.

### The lanes together

- **Shutdown order.** When HomePilot stops, it now finishes background jobs
  first. Then it lets work already handed to the git, disk and network threads
  finish (up to 10 s), then folds the WAL into the database file, and only
  then closes the database. Before this, a disk job still reading the database
  could leave that last step half done.
- **No slow import at start.** The MCP server module used to be loaded the
  first time the app started up, on the event loop, holding it for about
  0.4 s while the agent hub was already running. It is now loaded before the
  loop starts.
- **Power controls follow the no-blocking rule.** The new approve-and-apply
  route reads the change from disk off the event loop, like every other route.
  The WAL checkpoint's own database call is checked the same way.
- **The power card follows the new look.** The arrow between a setting's old
  and new value is an icon now, and a screen reader says "becomes".
- **Migration 39 is part of the checked chain.** The power controls' new
  column is entry 39 in the migration list that the app checks at start. The
  schema lock records it, and every older version's record is unchanged.

## 3.8.0 - 2026-11-08

Three lanes of the 2026-11 review in one release: HomePilot says when a newer
release is out (A+), and Phase B "knows the fleet" begins - the fleet model
(B1) and every address with its clashes (B2).

**Upgrade:** two schema migrations run in one start, with the usual
pre-migration backup (`backups/pre-migration-v36.db`) taken first. Migration
37 adds `is_template`, `pool`, `pve_tags`, `maxcpu`, `maxmem_mb`,
`maxdisk_gb`, `env`, `env_source` and `is_self` to `hosts` (plus a partial
unique index allowing one `is_self` row); migration 38 adds `host_addresses`.
Both are additive and every 3.7.0 row carries over; the first inventory sync
after the upgrade fills the new facts and addresses in, so the inventory
journal records each guest as "changed" once. The release check needs no
migration. Re-import the n8n flow if you use it (`update_available` has its
own formatter). An install with no internet access should set
`HP_UPDATE_CHECK_ENABLED=0`.

### Updates available: HomePilot says when a newer release is out (review A+)

Nothing told an operator that a release existed. A new reconciler checks the
public mirror's latest GitHub release shortly after boot and then daily
(`HP_UPDATE_CHECK_INTERVAL_SECONDS`, default 86400, at least 3600):
unauthenticated, ETag-revalidated, 10 s timeout, and when GitHub says the rate
limit is spent it waits for the reset rather than asking again. Pre-releases
(by flag or by a suffixed tag) are never offered; versions compare numerically,
and a pre-release build is behind its own final release.

- When the release is newer than the running version, the Overview shows ONE
  notice line - "HomePilot X.Y.Z is available - you run A.B.C" - linking to the
  release notes (new tab), with the operator actions found in the notes listed
  under it. The comparison is made against the version running at read time,
  so the line is gone right after the upgrade.
- ONE `update_available` event per new version, through the existing event
  path: SSE, the events webhook and webhook configs (the n8n flow has its own
  formatter for it, and the every-event gate holds it), and the ntfy channels
  (tapping the push opens the notes). The announced version is stored, so a
  daily check or a restart never repeats it; a later release is announced once
  more.
- Notify only: nothing anywhere upgrades HomePilot.
- `GET /dashboard/updates`, the summary's `updates` block and the MCP read tool
  `get_update_status` return the same verdict from one function.
- `HP_UPDATE_CHECK_ENABLED=0` (or Settings -> Subsystems -> Release check)
  switches it off for an offline install. The self-check line `update_check`
  says when it is off, and when it is failing - an install with no internet
  gets that one line and one log warning, retries hourly, and never raises;
  the loop itself stays healthy so the reconcilers line does not repeat it.

### `make gate` works in the public mirror

`gate-mirror` (part of `make gate`) runs `scripts/mirror_tree_check.py`, which
the scrub removes from the export, so `make gate` failed in the mirror. In a
mirror tree (PROVENANCE present, script absent) the target now says it has
nothing to do and passes; in the private tree it runs the script as before.
`tests/test_makefile_gate_mirror.py` drives the real Makefile through `make`
in both shapes, and runs the real target when the suite runs in the mirror's
tree (which `gate-mirror` builds).

### The fleet knows what Proxmox knows (Phase B lane B1, review 02 steps 1-3)

- **Templates leave the fleet numbers.** The sync reads `/cluster/resources`
  once per sweep. A Proxmox template is flagged `is_template` and counted in no
  Overview number (total, coverage, pending adoption, by status/role/type); the
  summary reports how many were left out (`inventory.templates`). Hosts hides
  them by default with a "Show templates (N hidden)" toggle and lists them in
  their own group with a `template` chip; adopting one is refused (409, and a
  failed entry in a bulk adopt). `GET /inventory` gains
  `templates=include|exclude|only` (default `include`, unchanged for API/MCP
  clients) and returns `templates_hidden`.
- **Pool, PVE tags and allocation are read.** Each guest row carries its pool,
  PVE tags and allocated cores/RAM/disk; the host page shows them. A failed
  `/cluster/resources` read costs these facts for one cycle and nothing else:
  the sweep's completeness and the absent sweep are untouched, and a stored
  pool is not wiped by a read that failed.
- **VMID and "Open in Proxmox".** Hosts has a VMID column and an "Open in
  Proxmox" row-menu link (also on the host page), built from the address
  HomePilot talks to Proxmox on; a gone host has none.
- **Env: which VMs are prod / dev / infra / friend / lab.** A new `env` field,
  shown as a column, a filter and a "Hosts by env" line (with an `unset`
  count). It is imported from a PVE tag `hp-env-<value>` (Proxmox tags cannot
  contain `:`, so the `hp-env:<value>` spelling is impossible; `-`, `.`, `_` are
  read), a template is `template`, and an operator can set it on the host page
  or with `PATCH /inventory/{id}` / MCP `update_host` (`env`). An operator value
  is pinned against later syncs; "Use Proxmox's" (`env: ""`) withdraws it.
  `env_source` says which of the three decided. The owner decision on the
  source of truth (PVE tag vs HomePilot field vs pool) is still open; this
  implements the reviewer's recommendation (tag import + operator override).
- **HomePilot knows its own host.** `GET /inventory/self-guest` proposes the
  guest holding an address the control plane is reached at - read from the
  address table below (its config claims, its guest agent's report, or an
  operator-typed address; a bare `ip_address` guess is not evidence). On a
  default install - HomePilot in a container, no advertise address set - the
  only address the process knows for itself is its bridge address, which no
  guest holds; there the guest whose agent dials the hub from the container's
  default gateway (the docker host) is proposed. Evidence ranks: advertise
  address, then that agent, then the hub bind address, then the address the
  browser asked for (Host / X-Forwarded-Host), then the process hostname; the
  `evidence` text says which one matched. An admin confirms it on Hosts
  (or marks/unmarks any guest on its host page); nothing is marked on a match
  alone. The row carries `is_self` and a "HomePilot" marker, the Overview says
  when that host has no agent or is gone from Proxmox, and confirming/clearing
  is audited. Later power actions will use it as their guard.

### Addresses and clashes (lane B2 of the 2026-11 review)

Starting a stopped VM whose cloud-init `ipconfig0` named an address a running
VM already used silently took that address over on the LAN; another VM shared
an address with a LAN device for days. HomePilot had both configs and said
nothing, because a host had one `ip_address`, guessed, with no age and no
source.

- **Every address, with where it came from.** The inventory sync records each
  guest's static addresses from its config (VM cloud-init `ipconfigN`,
  container `netN ip=`), with the NIC's MAC, bridge and VLAN; each node's own
  bridge addresses; and an operator-typed address (a manual device, or a
  pinned IP). The enrich pass records what each running VM's guest agent
  reports, with when. Interfaces internal to a machine (docker, veth, bridges
  inside the guest) are not LAN addresses and are not recorded. A template's
  config holds no address.
- **Hosts list** shows every address a host holds, a clashing one marked
  `(!)`. **Host page -> Network** (new, read-only) lists each address with
  interface, MAC, source and age, and every clash the host is in with a link to
  the other machine. `GET /inventory/{id}/network`; `get_host` (REST and MCP)
  carries `addresses`.
- **Clashes are a critical Overview line.** Two machines whose configs claim one
  address on the same bridge/VLAN - running or not, since whichever starts
  second takes it over - raise one critical "address clash" line naming both
  (VMID and state) with the remedy. So does an address configured on one
  machine that a running guest's agent reports holding. The same address on two
  different bridges or VLANs is two networks and is not a clash; a guest
  Proxmox no longer reports holds nothing. Each clash is journaled once when it
  appears (`address_conflict`, per machine) and once when it clears.
  `GET /dashboard/summary` carries `addresses.conflicts`.
- **Provisioning refuses an address somebody holds.** A static `ipconfig0`
  written by the operator (or the provisioning default) was never checked: the
  allocator only scans when it is choosing. It is now compared, live and before
  the clone, against every guest config on the same wire (running or stopped),
  the nodes, manual devices, running guests' agent reports and other
  provisions in flight. A scan that cannot complete refuses.
- **Start precondition for the power controls (#781).**
  `InventoryService.address_conflicts_for_start(host)` returns the running
  machines holding an address the guest would claim on boot - its config plus
  pending changes, so a Reboot applying a new `ipconfig` is covered - reading the
  cluster live and raising rather than guessing when it cannot. Not yet called
  by anything: the power controls land in their own lane.
- **Not yet: the active ARP probe.** Detecting a device that answers ARP for a
  guest's address with a different MAC needs a probe on the LAN; the control
  plane's container cannot see it without new privileges. The seam
  (`NeighbourProbe`), the storage of its answers and the comparison are in and
  tested with a fake; the probe itself (through the agent on HomePilot's own
  VM) waits for the self-guest to be known.

### The lanes together

- **One address source.** The self-guest proposal (B1) reads B2's
  `host_addresses` table instead of assembling addresses live from the guest
  config and `hosts.ip_address`, so the proposal and the clash line can never
  disagree about who holds an address. A guest the sync flags as a template
  records no address claims even when its config does not say so itself.
- **The Overview lines share one shape.** The release line (A+) is the only
  one that opens a new tab and lists actions; the address-clash (B2) and
  own-host (B1) lines are doors inside the console.
- **Migrations cannot collide silently.** Both Phase B lanes wrote "version
  37"; the release numbers them 37 and 38. A gate now reads the migration
  table's source and fails when a version is written twice (Python keeps only
  the last value of a repeated dict key, which would drop one lane's schema
  without an error), and a 3.7.0 database with real hosts rows is driven
  through both migrations and both lanes' code in one start.

## 3.7.0 - 2026-11-07

Phase A of the 2026-11 review, "honest and heard": the Overview says what is
true, an alert reaches someone, and a host that wants to join is seen.

**Upgrade:** two schema migrations run in one start, with the usual
pre-migration backup (`backups/pre-migration-v34.db`) taken first.
Migration 35 adds `alert_rules.kind`, `alert_events` and
`notification_channels`; migration 36 adds `enrolment_attempts` and
`created_at`/`minted_by` on `bootstrap_tokens`. Rules and tokens from 3.6.25
carry over (existing rules are `threshold` rules; existing tokens list with no
minter). The "Host went dark" rule appears on existing installs too. Re-import
the n8n flow if you use it.

### The Overview says what is true about the estate (attention honesty)

The "Needs attention" zone counted things that were no longer true, ranked
ten-week-old retried failures above an estate with no agent, and left out what
Settings and the Hosts page already knew.

- **Drift counts only active artifacts.** The summary joins `drift_checks` to
  applied/approved artifacts - the set the drift page lists - and revoking or
  superseding an artifact deletes its stored verdict. A revoked artifact no
  longer reads "drifted" on the Overview while the page it links to shows none.
- **No agent connected is one critical line.** When every enrolled agent has
  been gone for more than 5 minutes (counted from the hub's start, so a
  restart is not an alarm), the Overview shows one line with how long, folding
  in the per-host agent lines and the "rule matches no host" line. Each
  per-host "agent offline" line now says how long, and turns critical after an
  hour. Inventory reads agent state from the live hub, not the column a crash
  leaves at "connected".
- **Self-check findings on the Overview.** Every subsystem the self-check finds
  unreachable or unverified is a line with its own consequence sentence and a
  link to Settings -> Subsystems. An outdated agent fleet is a warning; any
  other unreachable subsystem is critical.
- **Failed tasks age and settle.** A failed task is critical for 24 hours and a
  warning after. It leaves the zone once a later apply/replay/revoke of the
  same artifact succeeded, or - for an agent install - once a later install
  on that host succeeded or its agent is connected. Agent-install failures now
  record which host they were for.
- **Gone hosts.** A host Proxmox stopped reporting is a warning line with how
  long and a Forget button (two-step, as on Hosts). Hosts stamped absent before
  #613 taught the sweep to blank the status kept `online`; every full sync now
  resets an absent host's status to `unknown` unless its agent is connected.
- **Unverified drift says why.** `drift.unknown_by_reason` splits unknown
  verdicts into no agent / check failed / no Proxmox / by design / other. The
  Overview shows one line with that breakdown; artifacts that cannot be checked
  by design (describe-only, shell, ansible) raise no line of their own. A
  host-provision check on a host with no agent records `no_agent`, and a
  precheck that was asked and went unanswered records `probe_failed` instead of
  being filed as describe-only.

### Alerts are heard: "host went dark", alert history, ntfy (#660)

HomePilot evaluated alerts and told no one: nothing left the box without a
hand-built webhook, router and chat bot, a host whose agent went silent could
not alert at all, and an alert that fired and resolved overnight left no
record. Lane A2 of the 2026-11 review.

- **Host went dark.** A new `nodata` rule kind fires when a host with an
  enrolled agent has been heard from not at all for the rule's duration, and
  resolves when it is heard again. "Host went dark (5 min)" is seeded once onto
  every install (delete it and it stays deleted). Hosts that never had an agent
  are not watched by it - that is coverage, not an outage - nor are guests the
  hypervisor reports stopped; a restart of HomePilot itself does not page the
  fleet.
- **Alert history.** Every fire and recovery is appended to `alert_events`
  (kept for `retention_days`), with how long it fired and which channels
  accepted it. The Overview and each host page gain an **Alerts** list: firing
  now, and resolved in the last 24 h / 7 d, plus a line saying where alerts go.
  `GET /monitoring/alert-events`; MCP `get_alert_events`.
- **ntfy channel.** Settings -> Monitoring -> Notification channels: paste an
  ntfy topic URL (stored in the vault, never shown again), press *Send test*,
  and firing/resolved alerts reach the phone. The list shows each channel's last
  delivery; the self-check gains a `notifications` line. Admin API
  `/admin/notifications/channels`; MCP `list_notification_channels`,
  `test_notification_channel`. See `docs/notifications.md`.
- **The shipped n8n flow handles alerts.** `n8n/artifact-notification.json` had
  no case for `alert_*`; it also kept its Switch rules in the v1/v2 shape under
  `typeVersion: 3` (which does not read them) and wrote a `content` parameter
  the Set node does not have, so Matrix/Telegram got empty messages for every
  event. The Switch and formatters are now in the shape their node versions
  read, with alert, test-event and an honest "other event" fallback. A gate
  holds the flow to every event the backend emits.
- The `events_webhook: off` self-check line no longer says a resolved alert
  leaves no record.

### Agents can join: refused hosts are seen, and the one-liner installs this hub's agent

- **A host refused at the hub is no longer invisible.** An agent installed while
  the enrolment window was shut was refused every 30 s for days, and only the
  host's journal and the audit trail said so. Each refused register now leaves
  one bounded record per hostname + peer address (refusal count, first and last
  seen, reason; at most 50 rows, aged out after 7 days, wrong-token rows evicted
  first). The **Hosts** page lists them under **Waiting to join**: a host that
  holds the real token and is refused only for want of a window "wants to join",
  with an **Open enrolment window** button - its next retry gets in, nothing to
  do on the host, and its record clears. Wrong-token hosts are listed with the
  reason. Read API `GET /agents/enrolment-requests` and MCP read tool
  `get_enrolment_requests` (with summary counts). No presented token is ever
  stored: only the refusal's code and sentence.
- **Pending bootstrap tokens are listed** with who minted them (an operator, or
  "zero-touch install on <host>"), when, and when they expire - "expired unused"
  once past it. Listed by a hash prefix, never the token.
- **Waiting for a window no longer gets an agent banned.** Refusals of a valid
  shared token only because no window is open stopped counting toward the
  5-failures / 60 s peer ban; an honest agent used to spend a third of its time
  banned, and stayed locked out for up to a minute after the window opened.
  Wrong tokens still count and still ban.
- **The by-hand install one-liner installs from this control plane.** The Hosts
  page built a one-liner that fetched the installer and binary from GitHub's
  `releases/latest`, so a hand-installed agent was whatever release was newest,
  not the hub's version, and a host without internet could not be enrolled by
  hand. The server now builds the command (installer via
  `/agents/dist/install-agent.sh`, binary via `--hp-api`); the hub is shown as
  `host:port` (it is raw TCP, not `http://`). Only when the control plane cannot
  serve the payload does it fall back to GitHub, pinned to this version and
  saying why. `hp agent bootstrap` prints the same command.
- **Fix: a bootstrap token could not download the agent payload.** The
  `/agents/dist/*` routes accepted only the shared hub token or an admin API
  token, so any install pointed at the control plane with a bootstrap token -
  including the zero-touch **Install agent** path, which stages exactly that -
  failed with a 401 before fetching anything. A live bootstrap token is now
  accepted (checked, not spent; registering spends it).
- Docs: the agent-install and setup skills no longer document `hp-agent
  enroll`/`start`/`service install` or `~/.config/hp-agent/config.json`, none of
  which exist; the deployment guide shows the control-plane one-liner.

### The three together on the Overview

- **A "went dark" rule that watches no host says why.** The Overview told the
  operator to "check the metric name and the host filter" for every rule that
  matched no host. A `nodata` rule has no metric: it watches hosts with an
  enrolled agent, so matching none means no host its filter names has one. It
  now gets its own line, linked to Hosts; threshold rules keep the old advice.
  The summary gains `metrics.nodata_rules_watching_nothing`.
- **A rule not evaluated yet is not "watching nothing".** The summary counted a
  rule the evaluator had not looked at since it was created (its first pass is
  a minute after boot) as matching no host, so the upgrade that seeds "Host
  went dark" opened on a false "watches no host" line. Both counts now take
  only evaluated rules, as Settings -> Monitoring ("not evaluated yet") does.
- **Fix: no host could fetch the agent payload from the control plane.** The
  agents router is mounted behind the app-wide API-token check, so
  `/agents/dist/*` refused the enrolment credential a host presents (the hub
  token or a bootstrap token) with a 401 before the route's own check ran: the
  panel one-liner and every zero-touch **Install agent** with `--hp-api` failed.
  The payload routes are now mounted on their own, gated only by that
  enrolment check (hub token, live bootstrap token, or an admin API token).
- **The one-liner fails loudly.** It was `curl ... | bash`: a refused download
  ran bash on nothing and reported success. It now downloads the installer to
  a temporary file and runs it, chained so any failed step ends the command
  non-zero with "HomePilot agent install FAILED - see the error above".
- **Hosts waiting to join reach the Overview.** While the enrolment window is
  shut, one warning line says how many hosts hold a genuine token and are
  refused only for want of a window, linked to the Hosts page's Waiting to join
  panel. It reads the same `GET /agents/enrolment-requests` as the panel.

## 3.6.25 - 2026-11-07

### Shutdown waits for the cleanup it starts (#496)

Cancelling a provision or guest-template build at shutdown starts the cleanup
that destroys the half-made guest and records the outcome. The shutdown drain
only waited for the jobs it saw when it began, so a cleanup slower than its 1 s
grace could still be writing when the database closed. The drain now re-reads
the live job set and cancels in bounded rounds, so that cleanup is finished or
cancelled before shutdown moves on. Every background task the provision and
template services start is now checked to be tracked.

### Security: an empty `.env` key took its template comment as its value (#773)

Docker Compose's `env_file` reads `KEY=   # note` as the value `# note`; the
inline-comment rule only applies after a real value. `.env.example` shipped 33
empty keys written that way (`.env.example.agent` two more), so an install that
copied the template and left them empty ran with the comment text as the
setting. On a production install that made `HP_VAULT_PASSPHRASE` - the key the
whole vault is encrypted with - the string `# Override: an existing
passphrase`, readable by anyone who has seen the repo.

- Both templates now put every comment on its own line, and a gate test fails
  if any shipped env template has `KEY=<whitespace>#` again, commented-out
  assignments included.
- The backend (and every `hp` command) refuses to start while any `HP_*` value
  starts with `#`, naming the key and never the value. Other variables with a
  `#` value are named in the log. Settings errors no longer echo the loaded
  input.

**Operator action - check every install whose `.env` came from an older
template.** Run `grep -nE '^[A-Z0-9_]+=[[:space:]]*#' .env`: every line it
prints is affected. After upgrading, a backend that refuses to start is telling
you which key it found.

- `HP_VAULT_PASSPHRASE`: the vault is protected only by filesystem
  permissions until you rotate. There is no `hp` command for this; the
  mechanism is `VaultManager.rotate_passphrase`, and `docs/vault.md`
  ("Passphrase taken from a template comment") has the exact procedure - a
  one-off `docker compose run` that writes a generated passphrase to
  `<data_dir>/.vault_passphrase` (0600) and re-wraps the vault with it. Do this
  BEFORE clearing the `.env` line: the environment value is the only thing
  that still opens the vault. Pre-rotation copies of the vault identity still
  open with the public string; destroy them.
- `HP_MCP_TOKEN`, `HP_AGENT_HUB_AUTH_TOKEN`, `HP_PORTAL_PROXY_SECRET`,
  `SEARXNG_SECRET_KEY`: these were public strings. Clear them (or set a
  generated value) and treat anything authenticated with them as exposed; a
  new hub token means re-enrolling agents.
- `N8N_ENCRYPTION_KEY`: n8n encrypted its stored credentials with the comment
  text. n8n also records the key in its settings file (`/home/node/.n8n/config`
  in the `n8n-data` volume) and refuses to start when `N8N_ENCRYPTION_KEY`
  differs from it, so a new key is a migration: export the credentials with
  `n8n export:credentials --all --decrypted --output=<file>` (the file is
  plain text - delete it afterwards), set a generated key in both `.env` and
  that settings file, then `n8n import:credentials --input=<file>`. With few
  credentials, a fresh `n8n-data` volume (workflows re-imported, credentials
  re-entered) is simpler.
- The rest (`HP_ENV`, `HP_TRUSTED_PROXIES`, `HP_PORTAL_TRUSTED_PROXY`,
  `HP_VAULT_AUTO_INIT`, `HP_VAULT_DIR`, the `HP_PROVISION_*` and
  `HP_GUEST_NETWORK_*` defaults, `HP_EMBEDDING_*_URL`, `HP_AGENT_HUB_TLS_CA`,
  `HP_ARTIFACTS_*`, ...) changed behaviour rather than leaking anything: clear
  them and restart.

### Security: the bundled n8n is never up without an owner (#775)

The `agents` profile's n8n set `N8N_BASIC_AUTH_*`, which n8n removed in 1.0;
the pinned 2.35.3 ignores them. A production instance had never had an owner
account, so the first person to reach its UI - through a port mapping, a proxy
or a tunnel - could claim the owner and every credential stored in n8n. No port
is published by default, so nobody had reached it.

The dead variables are gone from the compose file and both env templates. n8n
now runs with `N8N_INSTANCE_OWNER_MANAGED_BY_ENV=true`: it sets the owner from
`N8N_INSTANCE_OWNER_EMAIL` and `N8N_INSTANCE_OWNER_PASSWORD_HASH` (bcrypt) at
every start, and refuses to start while either is empty.

**Operator action, `agents` profile only:** before upgrading, set both in
`.env` (`docs/deployment.md` has the commands; keep the hash in single
quotes). Without them n8n stops starting - by design. On an instance whose
owner was already claimed, the owner's email and password become the ones in
`.env` at the next start. Remove `N8N_BASIC_AUTH_USER`/`_PASSWORD` from `.env`.
If you ever published n8n's port, check in n8n that the owner and users are the
ones you expect.

### piper starts (#776)

`rhasspy/wyoming-piper` 2.4.2 takes its settings as command-line flags only
and exits with `--voice is required for the piper backend` without one; the
compose file passed them as `PIPER_*` environment, which it never reads, so
piper crash-looped. They are now its `command:` (`--voice
${PIPER_VOICE:-en_US-lessac-medium}`, `--length-scale`, `--noise-scale`,
`--noise-w-scale`, each still overridable from `.env`). Two more defects
that would have kept it unhealthy once it started: voices were mounted at
`/voices` while the image downloads into its `--data-dir /data` (so every
restart downloaded the voice again), and the healthcheck spoke HTTP to port
10200, which speaks the Wyoming protocol - it now asks the server to describe
itself and expects `info` back.

## 3.6.24 - 2026-09-02

### The fence probe waits for the guest's network (#648)

The first live run of 3.6.23's fence check, on dev, probed a guest whose
cloud-init had not yet written its address: qemu-guest-agent answers long
before the network exists, and every port came back `UNREACH`. The check now
waits for cloud-init first (best effort, bounded), and a probe that reached
NOTHING - not the isolated range, not a control - is repeated a few seconds
apart, up to five more times, because "no network yet" is not a fact about the
fence. The verdict is read off the attempt that got an answer, and the detail
says how many times it asked.

Two wording defects from the same run: the `EPERM` sentence listed every
isolated port, including one that had said `UNREACH`; it now names only the
ports that actually hit `EPERM`. And no route to anything is now named as what
it is - the network was not up - rather than as a missing route to one host.

## 3.6.23 - 2026-09-02

### The fence is verified from inside the guest (#648)

Every layer reported the guest fence as a fact about configuration: the per-VM
rules on the tap, `firewall=1` on the NIC, the datacenter firewall on, the node
on an enforcing stack. All of it was true of the first real friend's guest on
prod - and nobody had ever run a command inside a guest and watched a packet
towards the operator LAN go nowhere. Written is not established, and the fence
is the one property of this product where the difference is a stranger on your
LAN.

A provision now asks the guest itself, through qemu-guest-agent, before the
machine is recorded or handed to anyone: a TCP connect to the Proxmox host
(known alive - it just cloned this guest - and inside the isolate list) on its
API port and on tcp/53, and to the guest gateway's resolver and the guest's
nameserver as controls. The DNS port is there for SELinux-enforcing images,
whose confined qemu-guest-agent may open 53 and little else; a reset from it
proves the host was reached as well as a handshake does. The result
carries `fence: verified` when the isolated address stayed silent while a
control answered, `fence: unverified` with the reason when nothing was
established (no agent, silence on both sides, an SELinux-confined agent, no
python3 or bash, nothing known alive inside the fence), and
`guest_network_fence.verification` records every probe. A guest that REACHES
the isolated range fails the provision and is stopped and destroyed, exactly as
a guest whose rules could not be written. The check can never itself fail a
provision.

What the fence check learns about the guest agent is handed to the tailnet
join, so an image with no agent costs one bounded wait, not two.

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
items and provisioning guests off its configured PVE host.

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

- **E2E test rewrite**: `tests/test_e2e.py` completely rewritten for live server testing against the dev instance.
- **Rate limit resilience**: Session-scoped `session_auth` fixture pre-creates all tokens (session, revoke target, scope target, read-only, roundtrip) with 429 retry logic. Individual tests reuse pre-created tokens instead of creating new ones on the fly, eliminating rate-limit skips.
- **Browser cookie auth**: `auth_page` fixture authenticates via browser UI login so cookies (`hp_token`, `hp_csrf`) are set correctly in the Playwright context.
- **CSRF headers**: All cookie-authenticated mutations include `x-csrf-token` + `x-requested-with: XMLHttpRequest` headers.
- **Token prefix fix**: `test_revoke_token` uses `token[:16]` matching `PREFIX_LENGTH = 16`.
- **30 passed / 0 skipped / 0 failed** on live e2e run.

### Fixes

- **AGENTS.md model assignments**: Updated to match current `~/.config/opencode/agents/roles.md` (deepseek-v4-pro removed, kimi-k2.6 now primary for CoreSquad/ToolingSquad/QATester).
- **matrix_server.py default URL**: Changed from `example.com` to the operator's own Matrix server and regex from `@hp-([a-z]+):example\.com` to `@hp-([a-z]+):`.

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