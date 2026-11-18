# Alert notifications and history

HomePilot evaluates alert rules every minute. Each time a rule starts firing on
a host, or stops, three things happen:

1. the transition is appended to the **alert history** (`alert_events`);
2. it is pushed to every enabled **notification channel** (ntfy);
3. it is emitted as an `alert_firing` / `alert_resolved` event (SSE stream,
   the events webhook and any webhook config - see `docs/EVENTS.md`).

A fresh install has no channel, so (2) reaches nobody until one is added. The
Overview says so in words ("no notification channel is set up"), and the
startup self-check line `notifications` reports `off`.

## Phone alerts: the bundled ntfy server

**Monitoring -> Channels -> Get alerts on your phone** (admin session) offers
three ways to get alerts onto a phone. Nothing extra runs until you choose one.
On an install without `HP_NTFY_INTERNAL_URL` (set by the shipped compose files),
the first is shown as not available, with that reason; the other two still work.

1. **HomePilot's own ntfy server** (recommended). The release ships an opt-in
   `ntfy` compose service (profile `ntfy`, image pinned by tag and digest, data
   in `<data dir>/ntfy/`, published on one address of the control plane's host
   only). Choosing it makes HomePilot:
   * generate two ntfy access tokens - a *publisher* (write-only on the
     `homepilot` topic) and an *admin* (manages phone logins) - and keep both in
     the vault. Neither is ever shown, logged or put in an environment;
   * write ntfy's config (`<data dir>/ntfy/server.yml`): anonymous access
     `deny-all`, no signup, no web app, no attachments, the two accounts and
     their tokens declared in it;
   * create its own notification channel for the publisher.

   Start the service **once** on the control plane's host:

   ```bash
   docker compose --profile ntfy up -d   # or COMPOSE_PROFILES=ntfy in .env
   ```

   The service waits for HomePilot's config, and restarts or stops ntfy by
   itself whenever that file changes or goes away - this command is the only
   one. The page shows when the server is up with HomePilot's config.
   (`deploy/control-plane` layout: enable it in the page first - the service
   mounts `ntfy/` of the `hp-data` volume as a subpath, Docker Engine >= 26,
   and that directory must exist when the container is created.)

   Then:
   * **The address your phone will use.** HomePilot cannot make a DNS name or
     a public proxy appear. Give the address the phone reaches the server at -
     a LAN/VPN address, or a public name behind a reverse proxy
     (`deploy/ntfy/nginx-ntfy.conf` is the reference vhost: websocket headers,
     long timeouts, the admin API blocked). HomePilot checks it answers as this
     server - an ntfy that refuses anonymous readers - before it calls the setup
     done. It never sends a token to that address.
   * **Add a phone.** A name, and HomePilot creates a read-only login for it
     through ntfy's admin API. The password is shown **once**, on that screen;
     HomePilot keeps no copy. In the ntfy app: add the login under Settings ->
     Manage users (server address, name, password), then scan the code or open
     the link to subscribe. Lost the password? Remove the phone and add it
     again.
   * **Send a test push**, and watch the phone.

   **The honest limit:** this server runs on the same host as HomePilot. If that
   host goes down, nothing is left to push "this host is down". The channel
   says so, and offers a second channel that does not depend on this host.

   *Disable* removes the channel, the config and the tokens; ntfy's own database
   (the phone logins) stays in `<data dir>/ntfy/`, so enabling again keeps the
   phones - with new tokens.

2. **An ntfy server you already run** - its topic URL and an access token, the
   form below.

3. **ntfy.sh** - the public server. HomePilot picks a long random topic and
   shows its link once. Alert text (host names, rule names, values) leaves your
   network, and anyone who learns the topic can read it.

### Adopting an ntfy you already run next to HomePilot

If you built an ntfy server by hand beside HomePilot, either keep it (choice 2)
or move it under HomePilot:

1. Stop your own ntfy container.
2. Copy its database files (`user.db`, and `cache.db` if you want its message
   history) into `<data dir>/ntfy/` (`./data/hp/ntfy/` on the root compose).
3. Choose **HomePilot's own ntfy server** and start the profile. HomePilot's
   config replaces yours - `deny-all`, its own two accounts. Your existing
   logins survive; the phone list shows them, and **Allow** gives one read
   access to HomePilot's topic.
4. Delete the old channel that pointed at your hand-built server.

Anything your old config set (a public `base-url`, upstream iOS push, other
topics' access) is not carried over; HomePilot's config is the whole config.

## Add an ntfy channel

[ntfy](https://ntfy.sh) pushes a message to a phone app for a *topic*. You can
run your own ntfy server on your network, or use a public one.

1. In the ntfy app, subscribe to a topic. Pick a long, unguessable name: on
   most servers anyone who knows the topic can read it. The topic URL looks
   like `https://ntfy.example.com/hp-alerts-7f3a`.
2. In HomePilot: **Monitoring -> Channels -> Notification channels** (admin
   session). Give the channel a name, paste the topic URL, and an access token
   if the topic is protected (`tk_...`; sent as `Authorization: Bearer`).
3. Press **Send test**. The phone should buzz with "HomePilot test
   notification". The *Last delivery* column then reads `ok`; if the server
   refused, it reads `failed` with the reason (an HTTP status or a connection
   error), and the self-check line turns `unreachable`.

Over the API: `POST /admin/notifications/channels` with
`{"kind": "ntfy", "name": "phone", "url": "<topic URL>", "token": "<optional>"}`,
then `POST /admin/notifications/channels/{id}/test`. `PATCH .../{id}` with
`{"enabled": false}` pauses a channel; `DELETE .../{id}` removes it.

### The topic URL is a credential

Whoever holds an ntfy topic URL can read the topic and post to it. So:

- the URL and token are stored in the **vault** (`notify-channel-<id>`), never
  in the database; a channel cannot be created while the vault is missing or
  locked;
- every list, log line, audit entry and self-check shows the endpoint as
  `scheme://host[:port]` only - the topic is never shown again after you type it;
- creating, pausing and deleting a channel are console/API actions only. MCP has
  `list_notification_channels` and `test_notification_channel`, never a tool
  that takes the URL as input.

Messages carry host and rule names. A public ntfy server sees them; a server on
your own network does not.

### What is sent

ntfy's JSON publish form (`POST <server>/` with the topic in the body, so names
in any language survive):

| | Title | Priority | Tags |
|---|---|---|---|
| firing | `Alert: <rule> on <host>` | 4 (high) | `homepilot`, `firing` |
| resolved | `Resolved: <rule> on <host>` | 3 (default) | `homepilot`, `resolved` |
| test | `HomePilot test notification` | 3 | `homepilot`, `test` |
| update available | `HomePilot <version> is available` | 3 | `homepilot`, `update` |

Besides alerts, a channel receives `update_available` - ONCE per new HomePilot
release, never daily (see "Updates available" below); tapping it opens the
release notes.

The body is one sentence built by the backend, e.g. `web01 went dark: nothing
heard for 6 min (Host went dark)` or `Disk almost full on web01: disk.free_gb is
0.8, below 1 for 10 min`. A failed delivery is retried twice (after 1 s and
5 s) and then recorded on the channel as failed.

## The alert history

Every fire and recovery is kept, append-only, for `retention_days` (the
operational-history horizon, default 90 days - not the 7-day metrics horizon).
A recovery row carries how long the alert fired and, for a dark host, why it
stopped counting. Each row records which channels accepted it (`delivered_to`:
`[]` means nobody was told).

- **Overview -> Alerts** and **Host -> Overview -> Alerts**: firing now, and
  resolved in the last 24 h or 7 d. The estate view also says where alerts go.
- `GET /monitoring/alert-events?hours=24&hostname=<host>` (read scope), and the
  MCP tool `get_alert_events`.

## "Host went dark"

A `nodata` rule fires when a host **with an enrolled agent** has been heard
from (heartbeat or metrics frame) not at all for the rule's duration. It is
seeded on every install as **Host went dark (5 min, every host)**, once; delete
it and it stays deleted.

- A host that never had an agent is not watched by it: that is missing
  coverage, not an outage, and would otherwise page forever.
- A guest the hypervisor reports **stopped** does not fire; a dark host that is
  then stopped resolves, with the reason recorded.
- Silence is measured only over time the control plane has been running, so
  restarting HomePilot does not page the whole fleet.
- It resolves when the agent is heard again; the history records how long the
  host was dark.

Create more in Settings -> Monitoring -> Alert rules by choosing *Nothing heard
(host went dark)* as the metric, or over the API with
`{"name": "...", "kind": "nodata", "for_seconds": 600, "host_filter": "db-*"}`
(no metric, comparison or threshold; at least 120 s).

## Updates available

Once shortly after boot and then daily (`HP_UPDATE_CHECK_INTERVAL_SECONDS`, at
least an hour), HomePilot asks GitHub for the public mirror's latest release -
one unauthenticated request, revalidated with its ETag. Pre-releases are never
offered. When the release is newer than the running version:

- the Overview shows ONE notice line, "HomePilot X.Y.Z is available - you run
  A.B.C", linking to the release notes, with the release's operator actions
  (`**Upgrade:**` / `**Operator action...:**` paragraphs and "Upgrade notes"
  sections of the notes) listed under it; the line goes away as soon as this
  install runs that version;
- one `update_available` event goes to the channels, the events webhook and the
  n8n flow - once per version: the announced version is stored, so neither the
  daily check nor a restart repeats it.

Nothing is ever upgraded automatically. `GET /dashboard/updates` and the MCP
tool `get_update_status` return the same verdict. The self-check line
`update_check` says when the check is off or failing (for example on an install
with no internet access: one line and one log warning, then an hourly retry).
Switch it off with `HP_UPDATE_CHECK_ENABLED=0` or in Settings -> Subsystems ->
Release check.

## The n8n flow

`n8n/artifact-notification.json` (workflow "HomePilot Event Notifications")
remains an optional router to Matrix and Telegram for every event, alerts
and `update_available` included. ntfy needs no n8n.

Setting it up (n8n 2.x, the `agents` compose profile):

1. Import `n8n/artifact-notification.json`.
2. **Compute Signature** node: create a credential of type *Crypto* (name it
   "HomePilot webhook secret") and put HomePilot's webhook signing secret in its
   **Hmac Secret** field - the value of `HP_EVENTS_WEBHOOK_SECRET`, or the vault's
   `webhook-secret`. Credentials are stored encrypted in n8n's database.
3. **Notification Targets** node: set `matrixRoomId` (e.g. `!room:example.com`)
   and `telegramChatId`. Every Matrix and Telegram node reads them from here.
4. Give the Matrix and Telegram nodes their n8n credentials, and activate the
   workflow. Point HomePilot's events webhook at its production URL
   (`.../webhook/homepilot-artifacts`) and send `hp webhook test`.

The flow checks `X-Webhook-Signature` - HMAC-SHA256 over the raw request body -
in constant time and answers 403 to an unsigned, tampered or wrongly signed
event, and to every event while the credential is missing. It reads nothing from
n8n's environment: keep `N8N_BLOCK_ENV_ACCESS_IN_NODE` at its default (blocked),
because that environment holds HomePilot's MCP tokens.

`n8n/morning-briefing.json` (workflow "HomePilot Morning Briefing") posts the
knowledge base's `summary` hits to the same chats at 08:00. After importing it:
fill its own **Notification Targets** node (`hpBaseUrl`, e.g.
`https://homepilot.example.com`, plus `matrixRoomId` and `telegramChatId`), and
give **Fetch KB Summary** a *Header Auth* credential ("HomePilot API token":
name `Authorization`, value `Bearer hp_...` - a read-scope token).
