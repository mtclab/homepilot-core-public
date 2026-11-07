"""The shipped n8n flow handles every event the backend emits (#660, review 03 P1).

`n8n/artifact-notification.json` is what an operator imports to get events onto
Matrix/Telegram. Its Switch listed only the eight `artifact_*` events, so an
`alert_firing` POST never reached a formatter that knew what an alert was - the
review read the phone message as "Artifact Drifted, ID: ``". It was worse than
that: the Switch declared `typeVersion: 3` but carried its rules in the v1/v2
shape (`rules.rules[].value2`), which v3 does not read at all, and every
formatter wrote a `content` parameter the Set node (v3.4) does not have - so the
Notify nodes' `$json.content` was empty for EVERY event.

This gate holds the flow to the n8n v3 Switch / v3.4 Set schema and to the
backend's own event catalogue (`webhooks.catalog.EVENT_TYPES`, itself held equal
to every `emit_event(...)` call site by test_webhooks_700.py), plus the test
event `hp webhook test` sends. A new event cannot ship without a formatter.

Not executed in a live n8n here (no n8n runtime in the gate); the shape checked
is the one n8n's own docs and exported workflows use for these node versions.

Teeth: restore the shipped flow from main, or drop the `alert_firing` case,
and this goes red.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from homepilot.webhooks.catalog import EVENT_TYPES, TEST_EVENT
from tests.tree import IS_PUBLIC_MIRROR

FLOW = Path(__file__).resolve().parents[1] / "n8n" / "artifact-notification.json"

# The scrub drops n8n/ from the public mirror on purpose (tests/tree.py).
pytestmark = pytest.mark.skipif(
    IS_PUBLIC_MIRROR, reason="the public mirror does not ship n8n/ (removed by the scrub)"
)
EMITTED = sorted(set(EVENT_TYPES) | {TEST_EVENT})
FALLBACK_FORMATTER = "Format Other Event"


@pytest.fixture(scope="module")
def flow() -> dict[str, Any]:
    return json.loads(FLOW.read_text(encoding="utf-8"))


def _nodes(flow: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {n["name"]: n for n in flow["nodes"]}


def _switch(flow: dict[str, Any]) -> dict[str, Any]:
    switches = [n for n in flow["nodes"] if n["type"] == "n8n-nodes-base.switch"]
    assert len(switches) == 1, "expected exactly one event router"
    return switches[0]


def _routes(flow: dict[str, Any]) -> list[tuple[str, int]]:
    """(event name, output index) for every rule the v3 Switch will evaluate."""
    switch = _switch(flow)
    out = []
    for index, rule in enumerate(switch["parameters"]["rules"]["values"]):
        conditions = rule["conditions"]["conditions"]
        assert len(conditions) == 1, rule
        cond = conditions[0]
        assert cond["leftValue"] == "={{ $json.body.event }}", cond
        assert cond["operator"] == {"type": "string", "operation": "equals"}, cond
        out.append((cond["rightValue"], index))
    return out


def _targets(flow: dict[str, Any], node: str, output: int) -> list[str]:
    outputs = flow["connections"].get(node, {}).get("main", [])
    if output >= len(outputs):
        return []
    return [edge["node"] for edge in outputs[output]]


def _content(node: dict[str, Any]) -> str:
    assignments = node["parameters"]["assignments"]["assignments"]
    values = [a["value"] for a in assignments if a["name"] == "content"]
    assert len(values) == 1, f"{node['name']} does not set exactly one `content`"
    return str(values[0])


def test_the_switch_is_in_the_shape_its_type_version_reads(flow):
    switch = _switch(flow)
    assert switch["typeVersion"] >= 3
    rules = switch["parameters"]["rules"]
    assert "values" in rules, "typeVersion 3 reads rules.values"
    assert "rules" not in rules, "rules.rules is the v1/v2 shape: v3 ignores it"
    assert switch["parameters"]["options"]["fallbackOutput"] == "extra"


@pytest.mark.parametrize("event", EMITTED)
def test_every_emitted_event_has_its_own_formatter(flow, event):
    routes = dict(_routes(flow))
    assert event in routes, f"{event} falls to the fallback: the flow has no case for it"
    targets = _targets(flow, _switch(flow)["name"], routes[event])
    assert len(targets) == 1, targets
    formatter = targets[0]
    assert formatter != FALLBACK_FORMATTER
    # Dedicated: no other event shares this formatter's output.
    sharing = [e for e, i in routes.items() if _targets(flow, _switch(flow)["name"], i) == targets]
    assert sharing == [event], f"{formatter} formats {sharing}"
    text = _content(_nodes(flow)[formatter])
    if event.startswith("alert_"):
        assert "Artifact" not in text, f"{event} is formatted as an artifact: {text!r}"
        assert "$json.body.hostname" in text and "$json.body.message" in text
    if event.startswith("artifact_"):
        assert "Artifact" in text


def test_the_fallback_is_honest_not_an_artifact(flow):
    switch = _switch(flow)
    fallback_index = len(switch["parameters"]["rules"]["values"])
    assert _targets(flow, switch["name"], fallback_index) == [FALLBACK_FORMATTER]
    text = _content(_nodes(flow)[FALLBACK_FORMATTER])
    assert "$json.body.event" in text
    assert "Artifact" not in text


def test_every_formatter_feeds_notifiers_that_read_its_content_and_respond(flow):
    nodes = _nodes(flow)
    formatters = [n for n in flow["nodes"] if n["type"] == "n8n-nodes-base.set"]
    assert formatters
    for fmt in formatters:
        assert "content" not in fmt["parameters"], (
            f"{fmt['name']}: `content` is not a Set-node parameter, it is silently dropped"
        )
        _content(fmt)
        # Walk the chain to the response; every notifier on it reads $json.content.
        seen, current = [], fmt["name"]
        while current != "Respond OK":
            nxt = _targets(flow, current, 0)
            assert len(nxt) == 1, f"{current} does not lead on to one node: {nxt}"
            current = nxt[0]
            assert current not in seen, f"cycle at {current}"
            seen.append(current)
            if current != "Respond OK":
                text = nodes[current]["parameters"].get("text")
                assert text == "={{ $json.content }}", f"{current} sends {text!r}"
        assert len(seen) >= 2, f"{fmt['name']} notifies nobody"


def test_no_dangling_connections(flow):
    names = set(_nodes(flow))
    for source, outs in flow["connections"].items():
        assert source in names, f"connection from missing node {source}"
        for output in outs["main"]:
            for edge in output:
                assert edge["node"] in names, f"{source} -> missing {edge['node']}"
