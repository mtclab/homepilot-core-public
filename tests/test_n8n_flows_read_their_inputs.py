"""Every expression in the shipped n8n flows reads what its input carries (#840).

The flows chain nodes - Format -> Notify Matrix -> Notify Telegram -> Respond
OK - and an n8n expression's ``$json`` is the PREVIOUS node's output. Three
times in one PR a node read a field from the wrong one: the Telegram nodes and
"Respond OK" read ``$json.content`` / ``$json.body`` from a chat API's response,
and the briefing read ``snippet`` from a KB search that never returns it. Each
was found by eye, one node at a time.

This gate removes the eye. ``tests/n8n_support.simulate`` runs a flow offline
the way n8n executes it and renders EVERY expression of EVERY node it reaches
against the strict output of the node before it (the chat APIs' response
shapes, the formatter's fields, the HTTP body); a read of a field that output
does not carry fails here, naming the node and the parameter. Both flows, every
event HomePilot emits, the refusal paths, and the briefing with HomePilot's
real ``/kb/search`` answer.

Teeth: point "Respond OK" back at ``$json.body.event``, or one Telegram node
back at ``$json.content``, and the matching cases fail with
"reads $json.body, which its input does not carry".
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from homepilot.webhooks.catalog import EVENT_TYPES, TEST_EVENT
from tests.n8n_support import (
    BRIEFING_FLOW,
    EVENT_FLOW,
    REPO_ROOT,
    TARGETS_SEEN,
    artifact_payloads,
    kb_route,
    on_the_wire,
    other_payloads,
    simulate,
)
from tests.tree import IS_PUBLIC_MIRROR

pytestmark = pytest.mark.skipif(
    IS_PUBLIC_MIRROR, reason="the public mirror does not ship n8n/ (removed by the scrub)"
)

SECRET = "n8n-flow-gate-secret"  # pragma: allowlist secret - throwaway
TARGETS = {
    "hpBaseUrl": "https://homepilot.example.com",
    "matrixRoomId": "!events:example.com",
    "telegramChatId": "-1001234567890",
}
EVENTS = sorted(set(EVENT_TYPES) | {TEST_EVENT})


def _cases() -> list[Any]:
    """Every event HomePilot sends, with the payload its emit site builds; the
    artifact events once per target shape (a VM, a service, no target)."""
    cases = []
    for shape, target in TARGETS_SEEN.items():
        for event, payload in artifact_payloads(target).items():
            cases.append(pytest.param(event, payload, id=f"{event}-{shape}"))
    for event, payload in other_payloads().items():
        cases.append(pytest.param(event, payload, id=event))
    assert {c.values[0] for c in cases} == set(EVENTS), "an event has no payload case"
    return cases


def _configured(path: Path) -> dict[str, Any]:
    """The flow as an operator leaves it after import: its targets filled in."""
    flow = json.loads(path.read_text(encoding="utf-8"))
    for node in flow["nodes"]:
        if node["name"] == "Notification Targets":
            for a in node["parameters"]["assignments"]["assignments"]:
                a["value"] = TARGETS[a["name"]]
    return flow


def _webhook_item(raw: bytes, headers: dict[str, str]) -> dict[str, Any]:
    return {"headers": headers, "params": {}, "query": {}, "body": json.loads(raw)}


def _run_event(tmp_path: Path, raw: bytes, headers: dict[str, str], **kwargs: Any) -> dict:
    return simulate(
        tmp_path,
        EVENT_FLOW,
        "Webhook Trigger",
        _webhook_item(raw, headers),
        raw=raw,
        flow_override=_configured(EVENT_FLOW),
        **kwargs,
    )


@pytest.mark.parametrize(("event", "payload"), _cases())
def test_every_event_reaches_both_chats_and_is_answered(
    tmp_path: Path, event: str, payload: dict[str, Any]
) -> None:
    raw, headers = on_the_wire(event, payload, SECRET)
    run = _run_event(tmp_path, raw, headers, credentials={"crypto": {"hmacSecret": SECRET}})
    assert run["errors"] == [], "\n".join(run["errors"])
    by_chat = {s["chat"]: s for s in run["sent"]}
    assert sorted(s["chat"] for s in run["sent"]) == ["matrix", "telegram"], run["sent"]
    assert by_chat["matrix"]["to"] == TARGETS["matrixRoomId"]
    assert by_chat["telegram"]["to"] == TARGETS["telegramChatId"]
    text = by_chat["matrix"]["text"]
    # A field the payload does not carry, read into the message, shows here.
    assert text and not any(bad in text for bad in ("undefined", "null", "[object Object]")), text
    assert by_chat["telegram"]["text"] == text
    (response,) = run["responses"]
    assert response["status"] == 200, response
    assert json.loads(response["body"]) == {"status": "ok", "event": event}


@pytest.mark.parametrize(
    ("case", "credentials", "tamper"),
    [
        ("tampered body", {"crypto": {"hmacSecret": SECRET}}, True),
        ("no credential", {}, False),
    ],
)
def test_a_refused_event_is_answered_403_and_notifies_nobody(
    tmp_path: Path, case: str, credentials: dict, tamper: bool
) -> None:
    raw, headers = on_the_wire(
        "artifact_proposed", artifact_payloads(TARGETS_SEEN["vm"])["artifact_proposed"], SECRET
    )
    if tamper:
        raw = raw.replace(b'"proposed"', b'"approved"', 1)
    run = _run_event(tmp_path, raw, headers, credentials=credentials)
    assert run["errors"] == [], "\n".join(run["errors"])
    assert run["sent"] == [], case
    (response,) = run["responses"]
    assert response["status"] == 403, (case, response)


async def test_the_briefing_reads_what_each_node_hands_it(tmp_path: Path) -> None:
    async with kb_route(tmp_path) as (client, repo):
        await repo.create_doc_metadata(
            source="test:daily", title="Daily summary", content="summary: three hosts", kind="note"
        )
        with patch(
            "homepilot.kb.service._get_embedding", new_callable=AsyncMock, return_value=None
        ):
            kb = client.get("/kb/search", params={"q": "summary"}).json()
    run = simulate(
        tmp_path,
        BRIEFING_FLOW,
        "Daily 8am Trigger",
        {"timestamp": "2026-11-18T08:00:00Z"},
        http={"Fetch KB Summary": kb},
        flow_override=_configured(BRIEFING_FLOW),
    )
    assert run["errors"] == [], "\n".join(run["errors"])
    (request,) = run["requests"]
    assert request["url"] == f"{TARGETS['hpBaseUrl']}/kb/search"
    matrix, telegram = run["sent"]
    assert matrix["text"] == telegram["text"]
    assert "• Daily summary: summary: three hosts" in matrix["text"]


def test_the_simulation_catches_a_read_its_input_does_not_carry(tmp_path: Path) -> None:
    """The gate's own teeth, independent of today's flows: a Telegram node that
    reads the Matrix node's output as if it were the formatter's."""
    flow = _configured(EVENT_FLOW)
    for node in flow["nodes"]:
        if node["name"] == "Notify Telegram (Proposed)":
            node["parameters"]["text"] = "={{ $json.content }}"
    raw, headers = on_the_wire(
        "artifact_proposed", artifact_payloads(TARGETS_SEEN["vm"])["artifact_proposed"], SECRET
    )
    run = simulate(
        tmp_path,
        EVENT_FLOW,
        "Webhook Trigger",
        _webhook_item(raw, headers),
        raw=raw,
        credentials={"crypto": {"hmacSecret": SECRET}},
        flow_override=flow,
    )
    assert run["errors"] == [
        "Notify Telegram (Proposed).text: reads $json.content, which its input does not carry"
    ], run["errors"]


def _emit_site_keys(relpath: str, function: str | None = None) -> dict[str, set[str]]:
    """event -> the keys of the dict literal passed to emit_event (or assigned to
    `payload` in `function`) at the real emit site."""
    import ast

    tree = ast.parse((REPO_ROOT / "src" / "homepilot" / relpath).read_text())
    out: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and getattr(node.func, "id", None) == "emit_event"
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[1], ast.Dict)
        ):
            out[node.args[0].value] = {
                k.value for k in node.args[1].keys if isinstance(k, ast.Constant)
            }
        if (
            function
            and isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            and node.name == function
        ):
            for sub in ast.walk(node):
                if (
                    isinstance(sub, ast.AnnAssign | ast.Assign)
                    and isinstance(sub.value, ast.Dict)
                    and "payload" in ast.unparse(sub)
                ):
                    out[function] = {k.value for k in sub.value.keys if isinstance(k, ast.Constant)}
    return out


def test_the_payloads_here_are_the_ones_homepilot_sends() -> None:
    """The cases above are only as good as their payloads: held to the emit sites."""
    lifecycle = _emit_site_keys("artifacts/lifecycle.py")
    built = artifact_payloads(TARGETS_SEEN["vm"])
    for event, keys in lifecycle.items():
        assert set(built[event]) == keys, (event, set(built[event]) ^ keys)
    drift_base = _emit_site_keys("reconciler/drift.py", "_emit_drift_event")["_emit_drift_event"]
    assert (
        drift_base
        <= set(built["artifact_drifted"])
        <= drift_base
        | {
            "details",
            "kind",
            "intent",
            "target",
        }
    )
    update = _emit_site_keys("updates/checker.py", "_emit")["_emit"]
    assert set(other_payloads()["update_available"]) == update
