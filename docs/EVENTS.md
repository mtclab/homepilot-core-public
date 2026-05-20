# HomePilot Event Payload Schema v1

All events emitted by HomePilot conform to a unified envelope plus per-type payload fields. This is the implemented and stable schema — events are emitted by `emit_event()` in `src/homepilot/events.py` and streamed via the SSE bus in `src/homepilot/sse.py`.

## Envelope

Every event carries these top-level fields:

| Field           | Type   | Description                                  |
|-----------------|--------|----------------------------------------------|
| `schema_version`| string | `"1"` — bumps when the envelope shape changes|
| `type`          | string | One of the defined event type strings        |
| `id`            | string | UUID v4, globally unique per event           |
| `timestamp`     | string | ISO 8601 (e.g. `2026-05-13T10:30:00Z`)       |

## Event Types

### `artifact_proposed`

A new artifact has been proposed for a host.

| Additional field | Type   | Description                                                    |
|------------------|--------|----------------------------------------------------------------|
| `id`             | string | Artifact identifier (e.g. `2026-05-08-install-nginx-web1-a3f9c2`) |
| `kind`           | string | Artifact kind: `ansible-playbook`, `proxmox-api-sequence`, `http-sequence`, `composite`, `shell-script`, or `kb-note` |
| `intent`         | string | One-line human description of the desired state                |
| `status`         | string | `proposed`                                                     |
| `mutating`       | bool   | Whether the artifact mutates the target                       |

**Example:**

```json
{
  "schema_version": "1",
  "type": "artifact_proposed",
  "id": "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d",
  "timestamp": "2026-05-13T10:30:00Z",
  "id": "2026-05-08-install-nginx-web1-a3f9c2",
  "kind": "ansible-playbook",
  "intent": "Install nginx on web1",
  "status": "proposed",
  "mutating": true
}
```

### `artifact_approved`

A proposed artifact has been approved for application.

| Additional field | Type   | Description                                                    |
|------------------|--------|----------------------------------------------------------------|
| `artifact_id`    | string | Artifact identifier                                             |
| `approved_by`    | string | Approver identifier                                             |
| `host`           | string | Target host identifier                                          |

**Example:**

```json
{
  "schema_version": "1",
  "type": "artifact_approved",
  "id": "b2c3d4e5-f6a7-4b8c-9d0e-1f2a3b4c5d6e",
  "timestamp": "2026-05-13T10:35:00Z",
  "artifact_id": "2026-05-08-install-nginx-web1-a3f9c2",
  "approved_by": "ops-lead",
  "host": "web1"
}
```

### `artifact_applied`

An approved artifact has been successfully applied to a host.

| Additional field | Type   | Description                                                    |
|------------------|--------|----------------------------------------------------------------|
| `artifact_id`    | string | Artifact identifier                                             |
| `host`           | string | Target host identifier                                          |

**Example:**

```json
{
  "schema_version": "1",
  "type": "artifact_applied",
  "id": "c3d4e5f6-a7b8-4c9d-0e1f-2a3b4c5d6e7f",
  "timestamp": "2026-05-13T10:40:00Z",
  "artifact_id": "2026-05-08-install-nginx-web1-a3f9c2",
  "host": "web1"
}
```

### `drift_detected`

Actual state on a host has diverged from the desired state defined by an applied artifact.

| Additional field | Type   | Description                                                    |
|------------------|--------|----------------------------------------------------------------|
| `artifact_id`    | string | Artifact identifier                                             |
| `drift_type`     | string | `"version"`, `"config"`, `"missing"`, or `"extra"`            |
| `host`           | string | Host where drift was detected                                   |
| `expected`       | string | Expected state value                                            |
| `actual`         | string | Actual state value found                                        |

**Example:**

```json
{
  "schema_version": "1",
  "type": "drift_detected",
  "id": "d4e5f6a7-b8c9-4d0e-1f2a-3b4c5d6e7f80",
  "timestamp": "2026-05-13T10:45:00Z",
  "artifact_id": "2026-05-08-install-nginx-web1-a3f9c2",
  "drift_type": "config",
  "host": "web1",
  "expected": "worker_processes auto",
  "actual": "worker_processes 1"
}
```

## SSE Streaming

Clients connect to `GET /events` to receive events in real time via Server-Sent Events. Each SSE message has `event` (the type), `data` (JSON payload), and `id` fields.

## Webhooks

Webhook endpoints can be registered via `hp webhook add` or the API. Each webhook configuration specifies:

- `url` — target endpoint URL
- `event_types` — list of event type strings to receive (or `["*"]` for all)
- `secret` — HMAC-SHA256 signing key (optional)
- `max_retries` — delivery retry count (default 3)

Deliveries use exponential backoff (1s, 5s, 30s). The `X-Webhook-Signature` header contains the HMAC-SHA256 of the request body when a secret is configured. Delivery status and attempts are tracked in the `webhook_deliveries` database table.