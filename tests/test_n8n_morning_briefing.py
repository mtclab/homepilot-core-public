"""The shipped morning-briefing flow runs without n8n's environment (#787 follow-up).

``n8n/morning-briefing.json`` read ``$env.HP_BASE_URL``, ``$env.MATRIX_ROOM_ID``
and ``$env.TELEGRAM_CHAT_ID``. n8n 2.x blocks env access from nodes by default,
and it stays blocked here because the n8n container's environment holds
HP_MCP_TOKEN_RW - so all three came out empty. Two more defects were in the
same five nodes:

* the briefing printed ``r.snippet``, a field ``GET /kb/search`` has never
  returned (its hits carry ``title`` and ``content``), so every line read
  ``undefined``;
* "Post to Telegram" is chained after "Post to Matrix" and sent ``$json.text``
  - the MATRIX node's output, which is the Matrix API's response (an event id,
  n8n@2.35.3 ``Matrix.node.ts`` returns ``responseData``), so Telegram got
  ``undefined`` too.

Now: a "Notification Targets" Set node holds the HomePilot base URL, the Matrix
room and the Telegram chat (set once after import, stored in n8n's database);
the API token is the HTTP node's Header Auth credential; the briefing reads
``content``; Telegram reads the briefing from "Format Briefing".

No n8n runs here. The test walks the flow's own connections and renders every
n8n expression it meets under ``node`` with ONLY ``$json``, ``$input`` and ``$``
(node lookup) defined - an ``$env`` or ``process.env`` throws. The KB response
is what HomePilot's real ``GET /kb/search`` route returns for seeded documents.

Teeth: restore the flow from main (``$env`` throws), print ``r.snippet`` again,
or point Telegram back at ``$json.text`` - and a test here fails.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from tests.test_kb_service import _insert_doc
from tests.tree import IS_PUBLIC_MIRROR

FLOW = Path(__file__).resolve().parents[1] / "n8n" / "morning-briefing.json"
BASE_URL = "https://homepilot.example.com"
ROOM = "!briefing:example.com"
CHAT = "-1001234567890"

pytestmark = pytest.mark.skipif(
    IS_PUBLIC_MIRROR, reason="the public mirror does not ship n8n/ (removed by the scrub)"
)

# Renders n8n parameter values: "=..." with {{ expr }} segments. Each expression
# is a JS function body with ONLY $json, $input and $ in scope.
_RENDER = r"""
const fs = require('fs');
const cases = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const out = cases.map(({ value, json, nodes }) => {
  if (typeof value !== 'string' || !value.startsWith('=')) return value;
  const $ = (name) => {
    if (!(name in nodes)) throw new Error(`node ${name} has not run`);
    return { first: () => ({ json: nodes[name] }), item: { json: nodes[name] } };
  };
  const $input = { first: () => ({ json }), all: () => [{ json }] };
  const evaluate = (body) => new Function('$json', '$input', '$', `return (${body});`)(json, $input, $);
  const tpl = value.slice(1);
  const whole = tpl.match(/^\{\{([\s\S]*)\}\}$/);
  if (whole) return evaluate(whole[1]);
  return tpl.replace(/\{\{([\s\S]*?)\}\}/g, (_, body) => String(evaluate(body)));
});
process.stdout.write(JSON.stringify(out));
"""


def _render(tmp_path: Path, cases: list[dict[str, Any]]) -> list[Any]:
    node_bin = shutil.which("node")
    if node_bin is None:
        pytest.fail("node is not on PATH; the flow's expressions are rendered under node")
    (tmp_path / "render.js").write_text(_RENDER, encoding="utf-8")
    (tmp_path / "cases.json").write_text(json.dumps(cases), encoding="utf-8")
    proc = subprocess.run(
        [node_bin, str(tmp_path / "render.js"), str(tmp_path / "cases.json")],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return list(json.loads(proc.stdout))


@pytest.fixture
async def kb_client(tmp_path: Path):
    """HomePilot's real KB router over a real database (auth stood in), as
    tests/test_kb_service.py builds it - the source of the briefing's input."""
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
    await run_migrations(db)
    repo = Repository(db)
    (tmp_path / "artifacts").mkdir()
    store = ArtifactStore(tmp_path / "artifacts")
    app = FastAPI()
    for _path, route, include_deps in _walk_api_routes(list(kb_router.routes)):
        for dep in [*getattr(route, "dependencies", []), *include_deps]:
            app.dependency_overrides[dep.dependency] = lambda: {
                "user_id": "briefing",
                "token_id": "briefing-token",
                "display_name": "n8n",
                "scope": "read",
            }
    app.include_router(kb_router, prefix="/kb")
    app.state.repo = repo
    app.state.kb_service = KBService(
        repo=repo, store=store, lifecycle=ArtifactLifecycle(store, repo)
    )
    yield TestClient(app, raise_server_exceptions=True), repo
    await db.close()


@pytest.fixture(scope="module")
def flow() -> dict[str, Any]:
    return json.loads(FLOW.read_text(encoding="utf-8"))


def _nodes(flow: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {n["name"]: n for n in flow["nodes"]}


def _chain(flow: dict[str, Any]) -> list[str]:
    """The run order from the trigger, following each node's single output."""
    (trigger,) = [n["name"] for n in flow["nodes"] if n["type"].endswith("scheduleTrigger")]
    order, current = [trigger], trigger
    while current in flow["connections"]:
        (nxt,) = [e["node"] for out in flow["connections"][current]["main"] for e in out]
        order.append(nxt)
        current = nxt
    return order


def _run(flow: dict[str, Any], tmp_path: Path, kb_response: dict[str, Any]) -> dict[str, Any]:
    """Simulate one run; returns what each HTTP/notifier node was asked to do."""
    nodes = _nodes(flow)
    outputs: dict[str, Any] = {}
    asked: dict[str, Any] = {}
    json_in: dict[str, Any] = {}
    for name in _chain(flow):
        node, params = nodes[name], nodes[name]["parameters"]
        kind = node["type"].rsplit(".", 1)[1]
        if kind == "scheduleTrigger":
            out: dict[str, Any] = {"timestamp": "2026-11-18T08:00:00Z"}
        elif kind == "set":
            values = [a["value"] for a in params["assignments"]["assignments"]]
            rendered = _render(
                tmp_path, [{"value": v, "json": json_in, "nodes": outputs} for v in values]
            )
            fields = {
                a["name"]: r
                for a, r in zip(params["assignments"]["assignments"], rendered, strict=True)
            }
            out = {**json_in, **fields} if params.get("includeOtherFields") else fields
        elif kind == "httpRequest":
            (url,) = _render(
                tmp_path, [{"value": params["url"], "json": json_in, "nodes": outputs}]
            )
            query = {q["name"]: q["value"] for q in params["queryParameters"]["parameters"]}
            asked[name] = {"url": url, "query": query}
            out = kb_response
        elif kind in ("matrix", "telegram"):
            target = "roomId" if kind == "matrix" else "chatId"
            to, text = _render(
                tmp_path,
                [
                    {"value": params[target], "json": json_in, "nodes": outputs},
                    {"value": params["text"], "json": json_in, "nodes": outputs},
                ],
            )
            asked[name] = {"to": to, "text": text}
            # What the node outputs: the chat API's response, not the message.
            out = {"event_id": "$stand-in"} if kind == "matrix" else {"message_id": 1}
        else:
            pytest.fail(f"unexpected node {name} ({node['type']})")
        outputs[name] = out
        json_in = out
    return asked


def _configured(flow: dict[str, Any]) -> dict[str, Any]:
    """The flow as an operator leaves it after import: targets filled in."""
    flow = json.loads(json.dumps(flow))
    for a in _nodes(flow)["Notification Targets"]["parameters"]["assignments"]["assignments"]:
        a["value"] = {"hpBaseUrl": BASE_URL, "matrixRoomId": ROOM, "telegramChatId": CHAT}[
            a["name"]
        ]
    return flow


async def _kb_search(kb_client: Any, seed: bool) -> dict[str, Any]:
    """What HomePilot's real GET /kb/search answers the briefing's query."""
    client, repo = kb_client
    if seed:
        await _insert_doc(
            repo,
            "Daily summary",
            "summary of the estate:\n  three hosts running,   one backup pending " + "x" * 300,
        )
        await _insert_doc(repo, "Backup summary", "nightly summary: all backups green")
    with patch("homepilot.kb.service._get_embedding", new_callable=AsyncMock, return_value=None):
        resp = client.get("/kb/search", params={"q": "summary"})
    assert resp.status_code == 200, resp.text
    return dict(resp.json())


async def test_a_briefing_reaches_both_chats_with_the_kb_lines(
    flow,
    kb_client,
    tmp_path: Path,
) -> None:
    kb = await _kb_search(kb_client, seed=True)
    assert kb["results"] and "snippet" not in kb["results"][0]
    asked = _run(_configured(flow), tmp_path, kb)

    assert asked["Fetch KB Summary"] == {
        "url": f"{BASE_URL}/kb/search",
        "query": {"q": "summary"},
    }
    matrix, telegram = asked["Post to Matrix"], asked["Post to Telegram"]
    assert matrix["to"] == ROOM and telegram["to"] == CHAT
    text = matrix["text"]
    assert text.startswith("🌅 **Morning Briefing**")
    assert "• Daily summary: summary of the estate: three hosts running, one backup pending" in text
    assert "• Backup summary: nightly summary: all backups green" in text
    assert "undefined" not in text
    # One line per hit, each cut to a readable length.
    line = next(ln for ln in text.splitlines() if ln.startswith("• Daily summary"))
    assert len(line) <= len("• Daily summary: ") + 160
    # Telegram gets the same briefing, not the Matrix node's API response.
    assert telegram["text"] == text


async def test_an_empty_kb_says_so(flow, kb_client, tmp_path: Path) -> None:
    kb = await _kb_search(kb_client, seed=False)
    assert kb["results"] == []
    asked = _run(_configured(flow), tmp_path, kb)
    assert 'Nothing in the knowledge base matched "summary".' in asked["Post to Matrix"]["text"]


def test_the_flow_reads_nothing_from_the_environment(flow) -> None:
    text = FLOW.read_text(encoding="utf-8")
    assert "$env" not in text and "process.env" not in text and "require(" not in text
    nodes = _nodes(flow)
    assert _chain(flow) == [
        "Daily 8am Trigger",
        "Notification Targets",
        "Fetch KB Summary",
        "Format Briefing",
        "Post to Matrix",
        "Post to Telegram",
    ]
    names = {
        a["name"] for a in nodes["Notification Targets"]["parameters"]["assignments"]["assignments"]
    }
    assert names == {"hpBaseUrl", "matrixRoomId", "telegramChatId"}
    # The HomePilot token is an n8n credential (encrypted in n8n's database).
    fetch = nodes["Fetch KB Summary"]
    assert fetch["parameters"]["authentication"] == "genericCredentialType"
    assert fetch["parameters"]["genericAuthType"] == "httpHeaderAuth"
    assert list(fetch["credentials"]) == ["httpHeaderAuth"]
