"""The shipped n8n flow accepts HomePilot's signed events and refuses forged ones (#787).

The flow verified ``X-Webhook-Signature`` inside an IF node EXPRESSION that
called ``require('crypto')`` and hashed ``$body`` - neither exists in an n8n
expression, so the check could not compute a signature at all. And its IF was
wired backwards: the "signatures equal" branch went to Reject Unauthorized.

The check now runs on n8n's own primitives and touches no environment:

* the Webhook node keeps the RAW body (Raw Body option) beside the parsed JSON;
* "Compute Signature", a Crypto node (v2, HMAC SHA256, hex), hashes that raw
  binary with the secret from an n8n Crypto credential - encrypted in n8n's
  database, never in its env, which also holds HP_MCP_TOKEN_RW;
* "Verify X-Webhook-Signature", a Code node, compares it with the header in
  constant time and sets ``signatureValid``; an IF routes a valid event on and
  answers 403 to everything else, including a missing credential (the Crypto
  node continues on error, so the compare sees no signature).

No n8n runs here. The harness reproduces the pinned Crypto node's HMAC path
(``CryptoV2.execute`` at n8n@2.35.3: ``createHmac(type, credential.hmacSecret)``
over the binary property, digest in ``encoding``, written to
``dataPropertyName``, binary dropped) FROM THE FLOW'S OWN PARAMETERS, with the
secret given as that node's credential; then it runs the Code node's JavaScript
under ``node`` with ``$input`` only - an ``$env`` or ``require`` in it throws.
The body and signature are captured from HomePilot's REAL sender
(``webhooks.send.send_webhook``, its HTTP post intercepted). A live n8n run was
NOT done (see the PR).

Teeth: restore the flow from main (no Crypto node), hash the parsed body instead
of the raw binary (``binaryData: false``: the valid case fails), drop the
Crypto node's continue-on-error (a missing credential then stops the run
instead of answering 403), or swap the IF's outputs.
"""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.tree import IS_PUBLIC_MIRROR

FLOW = Path(__file__).resolve().parents[1] / "n8n" / "artifact-notification.json"
SECRET = "n8n-signature-test-secret"  # pragma: allowlist secret - throwaway

pytestmark = [
    pytest.mark.skipif(
        IS_PUBLIC_MIRROR, reason="the public mirror does not ship n8n/ (removed by the scrub)"
    ),
]

# The two nodes, as n8n@2.35.3 runs them. The Crypto node is reproduced from the
# flow's parameters; the Code node's own JavaScript runs with `$input` only.
_HARNESS = r"""
const fs = require('fs');
const crypto = require('crypto');
const [flowPath, casePath] = process.argv.slice(2);
const flow = JSON.parse(fs.readFileSync(flowPath, 'utf8'));
const c = JSON.parse(fs.readFileSync(casePath, 'utf8'));
const only = (type) => {
  const found = flow.nodes.filter((n) => n.type === type);
  if (found.length !== 1) throw new Error(`expected one ${type}, found ${found.length}`);
  return found[0];
};
const raw = Buffer.from(c.raw_b64, 'base64');
// The Webhook node's item with Raw Body on: parsed JSON beside a binary `data`
// property (a filesystem-store reference in n8n 2.x).
const item = {
  json: { headers: c.headers, params: {}, query: {}, body: JSON.parse(raw.toString('utf8')) },
};
if (c.with_raw) item.binary = { data: { id: 'filesystem-v2:stand-in', mimeType: 'application/json' } };

// Crypto node v2, action hmac (CryptoV2.execute).
const cryptoNode = only('n8n-nodes-base.crypto');
const p = cryptoNode.parameters;
let hashed;
try {
  if (p.action !== 'hmac') throw new Error(`action ${p.action}`);
  const secret = (c.credentials[Object.keys(cryptoNode.credentials || {})[0]] || {}).hmacSecret;
  if (!secret) throw new Error('No HMAC secret set in the Crypto credentials');
  const hmac = crypto.createHmac(p.type, secret);
  let value;
  if (p.binaryData) {
    const bin = item.binary && item.binary[p.binaryPropertyName];
    if (!bin) throw new Error(`no binary property ${p.binaryPropertyName}`);  // assertBinaryData
    value = hmac.update(raw).digest(p.encoding);
  } else {
    // Hashing a text `value` instead: never the bytes HomePilot signed.
    value = hmac.update(String(p.value ?? '')).digest(p.encoding);
  }
  hashed = { json: { ...item.json, [p.dataPropertyName]: value } };  // binary not copied
} catch (err) {
  if (cryptoNode.onError !== 'continueRegularOutput') {
    process.stderr.write(`workflow stopped in ${cryptoNode.name}: ${err.message}`);
    process.exit(3);
  }
  hashed = { json: { error: { message: err.message } } };
}

const code = only('n8n-nodes-base.code').parameters.jsCode;
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
new AsyncFunction('$input', code).call({}, { all: () => [hashed] }).then(
  (out) => process.stdout.write(JSON.stringify(out)),
  (err) => { process.stderr.write(String(err && err.stack || err)); process.exit(2); },
);
"""


@pytest.fixture(scope="module")
def flow() -> dict[str, Any]:
    return json.loads(FLOW.read_text(encoding="utf-8"))


def _node(flow: dict[str, Any], node_type: str) -> dict[str, Any]:
    (node,) = [n for n in flow["nodes"] if n["type"] == node_type]
    return node


@pytest.fixture(scope="module")
def signed() -> tuple[bytes, dict[str, str]]:
    """One event as HomePilot's real sender puts it on the wire: body + headers."""
    import asyncio
    import os
    from unittest.mock import patch

    from homepilot.config import get_settings
    from homepilot.webhooks.send import send_webhook

    captured: dict[str, Any] = {}

    async def fake_post(self: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
        captured.update(kwargs)
        return httpx.Response(200, request=httpx.Request("POST", url))

    env = {
        "HP_EVENTS_WEBHOOK_URL": "https://n8n.example.com/webhook/homepilot-artifacts",
        "HP_EVENTS_WEBHOOK_SECRET": SECRET,
    }
    fm = {
        "id": "2026-01-01-proxmox-api-sequence-web01-abc123",
        "kind": "proxmox-api-sequence",
        # Non-ASCII on purpose: Python's json.dumps escapes it, a JS
        # re-serialisation would not, so only the RAW bytes verify.
        "intent": "Käynnistä web01 (start)",
        "status": "proposed",
        "target": {"kind": "vm", "vmid": 101, "node": "pve1"},
    }
    with patch.dict(os.environ, env), patch.object(httpx.AsyncClient, "post", fake_post):
        get_settings.cache_clear()
        try:
            asyncio.run(send_webhook("artifact_proposed", fm))
        finally:
            get_settings.cache_clear()
    assert "content" in captured, "send_webhook did not post"
    headers = {k.lower(): v for k, v in captured["headers"].items()}
    assert "x-webhook-signature" in headers
    return captured["content"], headers


def _run_flow_check(
    tmp_path: Path,
    raw: bytes,
    headers: dict[str, str],
    credentials: dict[str, dict[str, str]],
    with_raw: bool = True,
) -> dict[str, Any]:
    """The item the IF receives, after Compute Signature and Verify X-Webhook-Signature."""
    node_bin = shutil.which("node")
    if node_bin is None:
        pytest.fail("node is not on PATH; the n8n Code node's JavaScript runs under node")
    (tmp_path / "harness.js").write_text(_HARNESS, encoding="utf-8")
    case = {
        "raw_b64": base64.b64encode(raw).decode(),
        "headers": headers,
        "credentials": credentials,
        "with_raw": with_raw,
    }
    (tmp_path / "case.json").write_text(json.dumps(case), encoding="utf-8")
    proc = subprocess.run(
        [node_bin, str(tmp_path / "harness.js"), str(FLOW), str(tmp_path / "case.json")],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    (item,) = json.loads(proc.stdout)
    return dict(item["json"])


def _valid(tmp_path: Path, raw: bytes, headers: dict[str, str], **kwargs: Any) -> bool:
    out = _run_flow_check(tmp_path, raw, headers, **kwargs)
    if out["signatureValid"]:
        # The event itself travels on to the router untouched.
        assert out["body"]["event"] == "artifact_proposed"
        assert "expectedSignature" not in out
    return bool(out["signatureValid"])


# The secret as the operator sets it: on the Crypto credential the node names.
CREDENTIAL = {"crypto": {"hmacSecret": SECRET}}


def test_a_signed_event_is_accepted(signed, tmp_path: Path) -> None:
    raw, headers = signed
    assert _valid(tmp_path, raw, headers, credentials=CREDENTIAL)
    upper = {**headers, "x-webhook-signature": headers["x-webhook-signature"].upper()}
    assert _valid(tmp_path, raw, upper, credentials=CREDENTIAL)


def test_a_tampered_body_is_refused(signed, tmp_path: Path) -> None:
    raw, headers = signed
    tampered = raw.replace(b'"proposed"', b'"approved"', 1)
    assert tampered != raw
    assert not _valid(tmp_path, tampered, headers, credentials=CREDENTIAL)


@pytest.mark.parametrize(
    ("case", "headers_edit", "credentials", "with_raw"),
    [
        ("wrong secret", None, {"crypto": {"hmacSecret": "not-the-secret"}}, True),
        ("no credential set", None, {}, True),
        ("credential without a secret", None, {"crypto": {"hmacSecret": ""}}, True),
        ("unsigned", "drop", CREDENTIAL, True),
        ("not hex", "garble", CREDENTIAL, True),
        ("too short", "short", CREDENTIAL, True),
        ("no raw body", None, CREDENTIAL, False),
    ],
)
def test_everything_else_is_refused(
    signed,
    tmp_path: Path,
    case: str,
    headers_edit: str | None,
    credentials: dict,
    with_raw: bool,
) -> None:
    raw, headers = signed
    headers = dict(headers)
    if headers_edit == "drop":
        del headers["x-webhook-signature"]
    elif headers_edit == "garble":
        headers["x-webhook-signature"] = "z" * 64
    elif headers_edit == "short":
        headers["x-webhook-signature"] = headers["x-webhook-signature"][:32]
    out = _run_flow_check(tmp_path, raw, headers, credentials=credentials, with_raw=with_raw)
    assert out["signatureValid"] is False, case


def test_the_flow_routes_a_valid_event_on_and_refuses_the_rest(flow) -> None:
    hook = _node(flow, "n8n-nodes-base.webhook")
    assert hook["parameters"]["options"].get("rawBody") is True, "the raw body is what was signed"
    sign = _node(flow, "n8n-nodes-base.crypto")
    assert sign["typeVersion"] == 2
    assert sign["parameters"] == {
        "action": "hmac",
        "binaryData": True,
        "binaryPropertyName": "data",
        "type": "SHA256",
        "dataPropertyName": "expectedSignature",
        "encoding": "hex",
    }
    # The secret lives in an n8n Crypto credential (type `crypto`, field hmacSecret).
    assert list(sign["credentials"]) == ["crypto"]
    assert sign["onError"] == "continueRegularOutput"
    code = _node(flow, "n8n-nodes-base.code")
    gate = _node(flow, "n8n-nodes-base.if")
    (cond,) = gate["parameters"]["conditions"]["conditions"]
    assert cond["leftValue"] == "={{ $json.signatureValid }}"
    assert cond["operator"]["type"] == "boolean" and cond["operator"]["operation"] == "true"
    conn = flow["connections"]

    def targets(node: str) -> list[list[str]]:
        return [[e["node"] for e in out] for out in conn[node]["main"]]

    assert targets(hook["name"]) == [[sign["name"]]]
    assert targets(sign["name"]) == [[code["name"]]]
    assert targets(code["name"]) == [[gate["name"]]]
    assert targets(gate["name"]) == [["Notification Targets"], ["Reject Unauthorized"]]
    assert targets("Notification Targets") == [["Route by Event Type"]]


def test_the_flow_reads_nothing_from_the_environment(flow) -> None:
    """n8n 2.x blocks env access from nodes by default, and the n8n container's
    env holds HP_MCP_TOKEN_RW: the flow must not need it opened (#787)."""
    text = FLOW.read_text(encoding="utf-8")
    assert "$env" not in text and "process.env" not in text
    assert "require(" not in text
    nodes = {n["name"]: n for n in flow["nodes"]}
    values = {
        a["name"]: a["value"]
        for a in nodes["Notification Targets"]["parameters"]["assignments"]["assignments"]
    }
    assert set(values) == {"matrixRoomId", "telegramChatId"}
    assert nodes["Notification Targets"]["parameters"]["includeOtherFields"] is True
    for node in flow["nodes"]:
        if node["type"] == "n8n-nodes-base.matrix":
            assert node["parameters"]["roomId"] == (
                "={{ $('Notification Targets').first().json.matrixRoomId }}"
            ), node["name"]
        if node["type"] == "n8n-nodes-base.telegram":
            assert node["parameters"]["chatId"] == (
                "={{ $('Notification Targets').first().json.telegramChatId }}"
            ), node["name"]


def test_the_n8n_service_does_not_open_env_access() -> None:
    import yaml

    compose = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "docker-compose.yml").read_text()
    )
    env = compose["services"]["n8n"].get("environment", {})
    assert "N8N_BLOCK_ENV_ACCESS_IN_NODE" not in env, "env access must stay blocked (default)"
    assert "HP_WEBHOOK_SECRET" not in env
