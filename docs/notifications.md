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

## Add an ntfy channel

[ntfy](https://ntfy.sh) pushes a message to a phone app for a *topic*. You can
run your own ntfy server on your network, or use a public one.

1. In the ntfy app, subscribe to a topic. Pick a long, unguessable name: on
   most servers anyone who knows the topic can read it. The topic URL looks
   like `https://ntfy.example.com/CHANGE_ME_matrix_room_alias-7f3a`.
2. In HomePilot: **Settings -> Monitoring -> Notification channels** (admin
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
