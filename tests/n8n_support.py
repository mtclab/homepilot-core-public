"""Offline runs of the shipped n8n flows (n8n/*.json), for the n8n gates.

No n8n runs in the gate. ``simulate`` walks a flow from its trigger the way
n8n 2.35.3 executes it and, at EVERY node, renders every parameter written as
an n8n expression (``=...{{ }}``) with ONLY ``$json``, ``$input`` and ``$``
in scope - an ``$env`` or ``process.env`` throws. The inputs are STRICT: a
node's ``$json`` is exactly what the node before it outputs, and an expression
that reads a field that output does not carry fails with the node and the
parameter named. That is the defect class #840 kept finding one node at a time:
a node chained after a chat node reads ``$json.content`` / ``$json.body`` while
its input is the chat API's response.

What each node type outputs (n8n@2.35.3 source):

* Webhook (Raw Body on): ``{headers, params, query, body}`` plus the raw bytes
  as binary ``data``. The webhook ``body`` - HomePilot's event payload - is the
  one permissive object: its fields are optional by design (absent ones are
  omitted), and the formatters read them with ``?.`` / ``??``.
* Crypto v2 hmac: the input JSON plus ``dataPropertyName``; binary dropped.
* Code: whatever its JavaScript returns (run here with ``$input`` only).
* IF / Switch / Respond to Webhook: the input item, passed through.
* Set: the assigned fields, plus the input's when ``includeOtherFields``.
* Matrix: the Matrix API response (``{event_id}``); Telegram: the Bot API
  response (``{ok, result}``); HTTP Request: the response body given.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
EVENT_FLOW = REPO_ROOT / "n8n" / "artifact-notification.json"
BRIEFING_FLOW = REPO_ROOT / "n8n" / "morning-briefing.json"

_SIM = r"""
const fs = require('fs');
const nodeCrypto = require('crypto');
const run = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const flow = JSON.parse(fs.readFileSync(run.flow, 'utf8'));
const byName = Object.fromEntries(flow.nodes.map((n) => [n.name, n]));
const PERMISSIVE = new WeakSet();

function strict(value, path) {
  if (value === null || typeof value !== 'object' || PERMISSIVE.has(value)) return value;
  return new Proxy(value, {
    get(target, prop, receiver) {
      if (typeof prop === 'symbol' || prop in target || prop === 'toJSON' || prop === 'then') {
        const v = Reflect.get(target, prop, receiver);
        return typeof v === 'function' ? v : strict(v, `${path}.${String(prop)}`);
      }
      throw new Error(`reads ${path}.${String(prop)}, which its input does not carry`);
    },
  });
}

const outputs = {};
function render(value, input) {
  if (typeof value !== 'string' || !value.startsWith('=')) return value;
  const $json = strict(input, '$json');
  const $input = { first: () => ({ json: $json }), all: () => [{ json: $json }], item: { json: $json } };
  const $ = (name) => {
    if (!(name in outputs)) throw new Error(`reads $('${name}'), which has not run`);
    const json = strict(outputs[name], `$('${name}').json`);
    return { first: () => ({ json }), item: { json }, all: () => [{ json }] };
  };
  const evaluate = (body) => new Function('$json', '$input', '$', `return (${body});`)($json, $input, $);
  const tpl = value.slice(1);
  const whole = tpl.match(/^\{\{([\s\S]*)\}\}$/);
  if (whole) return evaluate(whole[1]);
  return tpl.replace(/\{\{([\s\S]*?)\}\}/g, (_, body) => String(evaluate(body)));
}

function renderAll(node, params, input, path, errors) {
  if (typeof params === 'string') {
    try {
      return render(params, input);
    } catch (err) {
      errors.push(`${node}${path}: ${err.message}`);
      return undefined;
    }
  }
  if (Array.isArray(params)) return params.map((p, i) => renderAll(node, p, input, `${path}[${i}]`, errors));
  if (params && typeof params === 'object') {
    return Object.fromEntries(
      Object.entries(params).map(([k, v]) => [k, renderAll(node, v, input, `${path}.${k}`, errors)]),
    );
  }
  return params;
}

const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const record = { errors: [], sent: [], responses: [], requests: [], path: [] };
const raw = run.raw_b64 ? Buffer.from(run.raw_b64, 'base64') : null;

async function execute(node, item) {
  const n = byName[node];
  const kind = n.type.split('.').pop();
  const input = item.json;
  if (kind === 'code') {
    const out = await new AsyncFunction('$input', n.parameters.jsCode).call({}, { all: () => [item] });
    return { output: 0, item: { json: out[0].json } };
  }
  const p = renderAll(node, n.parameters, input, '', record.errors);
  switch (kind) {
    case 'webhook':
    case 'scheduleTrigger':
      return { output: 0, item };
    case 'crypto': {
      try {
        const secret = (run.credentials[Object.keys(n.credentials || {})[0]] || {}).hmacSecret;
        if (!secret) throw new Error('No HMAC secret set in the Crypto credentials');
        if (!p.binaryData || !item.binary || !item.binary[p.binaryPropertyName]) {
          throw new Error(`no binary property ${p.binaryPropertyName}`);
        }
        const value = nodeCrypto.createHmac(p.type, secret).update(raw).digest(p.encoding);
        return { output: 0, item: { json: { ...input, [p.dataPropertyName]: value } } };
      } catch (err) {
        if (n.onError !== 'continueRegularOutput') throw err;
        return { output: 0, item: { json: { error: { message: err.message } } } };
      }
    }
    case 'if': {
      const cond = p.conditions.conditions[0];
      const op = cond.operator;
      const ok = op.type === 'boolean' && op.operation === 'true'
        ? cond.leftValue === true
        : cond.leftValue === cond.rightValue;
      return { output: ok ? 0 : 1, item };
    }
    case 'switch': {
      const rules = p.rules.values;
      const hit = rules.findIndex((r) => r.conditions.conditions[0].leftValue === r.conditions.conditions[0].rightValue);
      return { output: hit === -1 ? rules.length : hit, item };
    }
    case 'set': {
      const fields = Object.fromEntries(p.assignments.assignments.map((a) => [a.name, a.value]));
      return { output: 0, item: { json: p.includeOtherFields ? { ...input, ...fields } : fields } };
    }
    case 'matrix':
      record.sent.push({ node, chat: 'matrix', to: p.roomId, text: p.text });
      return { output: 0, item: { json: { event_id: '$stand-in-event' } } };
    case 'telegram':
      record.sent.push({ node, chat: 'telegram', to: p.chatId, text: p.text });
      return { output: 0, item: { json: { ok: true, result: { message_id: 1, date: 0, text: p.text } } } };
    case 'respondToWebhook':
      record.responses.push({ node, status: (p.options || {}).statusCode || 200, body: p.responseBody });
      return { output: 0, item };
    case 'httpRequest': {
      record.requests.push({ node, url: p.url, query: p.queryParameters });
      if (!(node in run.http)) throw new Error(`no response stood in for ${node}`);
      return { output: 0, item: { json: run.http[node] } };
    }
    default:
      throw new Error(`${node}: node type ${n.type} has no offline behaviour`);
  }
}

(async () => {
  const trigger = run.trigger;
  const item = { json: run.item };
  if (raw) item.binary = { data: { id: 'filesystem-v2:stand-in', mimeType: 'application/json' } };
  if (item.json.body && typeof item.json.body === 'object') PERMISSIVE.add(item.json.body);
  const queue = [[trigger, item]];
  while (queue.length) {
    const [node, it] = queue.shift();
    record.path.push(node);
    const { output, item: out } = await execute(node, it);
    outputs[node] = out.json;
    const edges = ((flow.connections[node] || {}).main || [])[output] || [];
    for (const e of edges) queue.push([e.node, out]);
  }
  process.stdout.write(JSON.stringify(record));
})().catch((err) => {
  process.stderr.write(String((err && err.stack) || err));
  process.exit(2);
});
"""


def simulate(
    tmp_path: Path,
    flow: Path,
    trigger: str,
    item: dict[str, Any],
    *,
    raw: bytes | None = None,
    credentials: dict[str, dict[str, str]] | None = None,
    http: dict[str, Any] | None = None,
    flow_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One offline run: what was sent, answered and requested, and every
    expression that read a field its input does not carry (``errors``)."""
    node_bin = shutil.which("node")
    if node_bin is None:
        pytest.fail("node is not on PATH; the n8n flows are simulated under node")
    if flow_override is not None:
        flow = tmp_path / "flow.json"
        flow.write_text(json.dumps(flow_override), encoding="utf-8")
    (tmp_path / "sim.js").write_text(_SIM, encoding="utf-8")
    spec = {
        "flow": str(flow),
        "trigger": trigger,
        "item": item,
        "raw_b64": None if raw is None else __import__("base64").b64encode(raw).decode(),
        "credentials": credentials or {},
        "http": http or {},
    }
    (tmp_path / "run.json").write_text(json.dumps(spec), encoding="utf-8")
    proc = subprocess.run(
        [node_bin, str(tmp_path / "sim.js"), str(tmp_path / "run.json")],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return dict(json.loads(proc.stdout))


def on_the_wire(event: str, payload: dict[str, Any], secret: str) -> tuple[bytes, dict[str, str]]:
    """One event exactly as HomePilot POSTs it (its HTTP post intercepted): the
    raw body and the headers, signature included. Catalogue events go through
    the real ``events.emit_event``; the test event through the real
    ``deliver_with_retry``, which is how ``hp webhook test`` sends it."""
    from homepilot.config import get_settings
    from homepilot.events import deliver_with_retry, emit_event
    from homepilot.webhooks.catalog import TEST_EVENT

    captured: dict[str, Any] = {}

    async def fake_post(self: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
        captured.update(kwargs)
        return httpx.Response(200, request=httpx.Request("POST", url))

    url = "https://n8n.example.com/webhook/homepilot-artifacts"
    env = {"HP_EVENTS_WEBHOOK_URL": url, "HP_EVENTS_WEBHOOK_SECRET": secret}
    with patch.dict(os.environ, env), patch.object(httpx.AsyncClient, "post", fake_post):
        get_settings.cache_clear()
        try:
            if event == TEST_EVENT:
                asyncio.run(deliver_with_retry(url, payload, secret, max_retries=0))
            else:
                asyncio.run(emit_event(event, payload))
        finally:
            get_settings.cache_clear()
    assert "content" in captured, f"nothing was posted for {event}"
    return captured["content"], {k.lower(): v for k, v in captured["headers"].items()}


# ── The payloads HomePilot emits, built the way each emit site builds them ────
# tests/test_n8n_flows_read_their_inputs.py holds the artifact and update ones
# to the emit sites' own key sets (AST), so a payload change shows up here.

TARGETS_SEEN = {
    "vm": {"kind": "vm", "host": "web01", "vmid": 101, "node": "pve1"},
    "service": {"kind": "service", "host": "web01", "service": "nginx"},
    "none": None,
}
_FM: dict[str, Any] = {
    "id": "2026-01-01-proxmox-api-sequence-web01-abc123",
    "kind": "proxmox-api-sequence",
    "intent": "Start web01",
    "mutating": True,
    "idempotence": "declared-natural",
    "produced_by": {"session": "s1", "agent": "assistant", "user": "owner"},
}
_BY = {"user": "owner", "at": "2026-01-01T08:00:00+00:00"}


def artifact_payloads(target: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """artifacts/lifecycle.py and reconciler/drift.py, field for field."""
    fm = {**_FM, "target": target}
    common = {"kind": fm["kind"], "intent": fm["intent"]}
    drift: dict[str, Any] = {
        "id": fm["id"],
        "status": "drifted",
        "drifted": True,
        "drift_summary": "nginx.conf differs from the applied artifact",
        **common,
    }
    if target:
        drift["target"] = target
    return {
        "artifact_proposed": {
            "id": fm["id"],
            "kind": fm["kind"],
            "intent": fm["intent"],
            "status": "proposed",
            "mutating": fm["mutating"],
            "target": target,
            "idempotence": fm["idempotence"],
            "source": fm["produced_by"],
        },
        "artifact_approved": {"id": fm["id"], "status": "approved", "approved_by": _BY, **common},
        "artifact_rejected": {"id": fm["id"], "status": "rejected", "rejected_by": _BY, **common},
        "artifact_applied": {"id": fm["id"], "status": "applied", **common, "target": target},
        "artifact_failed": {"id": fm["id"], "status": "failed", **common},
        "artifact_superseded": {"id": fm["id"], "superseded_by": fm["id"] + "-v2", **common},
        "artifact_withdrawn": {
            "id": fm["id"],
            "status": "withdrawn",
            "withdrawn_by": _BY,
            **common,
        },
        "artifact_revoked": {"id": fm["id"], "status": "revoked", "revoked_by": _BY, **common},
        "artifact_drifted": drift,
    }


def other_payloads() -> dict[str, dict[str, Any]]:
    """metrics/alerts.py (its real _payload and describe_alert), updates/checker.py
    (its real describe_update) and webhooks/service.py's test event."""
    from homepilot.metrics.alerts import (
        EVENT_FIRING,
        EVENT_RESOLVED,
        AlertEvaluator,
        describe_alert,
    )
    from homepilot.updates.checker import describe_update
    from homepilot.webhooks.catalog import TEST_EVENT

    rule = {
        "id": 3,
        "name": "CPU high",
        "kind": "threshold",
        "metric": "cpu_percent",
        "comparison": ">",
        "threshold": 90.0,
        "for_seconds": 300,
    }
    base = AlertEvaluator._payload(None, rule, "web01", 97.5)  # type: ignore[arg-type]
    firing = {**base, "since": "2026-01-01T07:55:00+00:00"}
    firing["message"] = describe_alert(EVENT_FIRING, firing)
    firing["alert_event_id"] = 11
    resolved = {
        **base,
        "since": "2026-01-01T07:55:00+00:00",
        "resolved_at": "2026-01-01T08:10:00+00:00",
        "duration_seconds": 900,
        "reason": "",
    }
    resolved["message"] = describe_alert(EVENT_RESOLVED, resolved)
    resolved["alert_event_id"] = 12
    release = {
        "version": "3.14.0",
        "tag": "v3.14.0",
        "url": "https://github.example.com/releases/v3.14.0",
        "published_at": "2026-01-01T00:00:00Z",
        "operator_actions": ["Take the new compose file."],
    }
    update = {
        "version": release["version"],
        "tag": release["tag"],
        "running_version": "3.13.0",
        "url": release["url"],
        "published_at": release["published_at"],
        "operator_actions": list(release["operator_actions"]),
        "message": describe_update(release, "3.13.0"),
    }
    test = {
        "event": TEST_EVENT,
        "message": "Test event for webhook #1",
        "timestamp": "2026-01-01T08:00:00+00:00",
    }
    return {
        EVENT_FIRING: firing,
        EVENT_RESOLVED: resolved,
        "update_available": update,
        TEST_EVENT: test,
    }


@asynccontextmanager
async def kb_route(tmp_path: Path) -> AsyncIterator[tuple[Any, Any]]:
    """HomePilot's real KB router over a real database (auth stood in), as
    tests/test_kb_service.py builds it: (TestClient, Repository)."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from homepilot.artifacts.lifecycle import ArtifactLifecycle
    from homepilot.artifacts.store import ArtifactStore
    from homepilot.db.connection import Database
    from homepilot.db.migrations import run_migrations
    from homepilot.db.repository import Repository
    from homepilot.kb.router import router as kb_router
    from homepilot.kb.service import KBService
    from homepilot.main import _walk_api_routes

    db = Database(str(tmp_path / "kb.db"))
    await db.connect()
    try:
        await run_migrations(db)
        repo = Repository(db)
        (tmp_path / "artifacts").mkdir(exist_ok=True)
        store = ArtifactStore(tmp_path / "artifacts")
        app = FastAPI()
        for _path, route, include_deps in _walk_api_routes(list(kb_router.routes)):
            for dep in [*getattr(route, "dependencies", []), *include_deps]:
                app.dependency_overrides[dep.dependency] = lambda: {
                    "user_id": "n8n",
                    "token_id": "n8n-token",
                    "display_name": "n8n",
                    "scope": "read",
                }
        app.include_router(kb_router, prefix="/kb")
        app.state.repo = repo
        app.state.kb_service = KBService(
            repo=repo, store=store, lifecycle=ArtifactLifecycle(store, repo)
        )
        yield TestClient(app, raise_server_exceptions=True), repo
    finally:
        await db.close()
