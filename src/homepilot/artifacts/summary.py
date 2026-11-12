"""A change in plain words: what it does, to what, and whether it can be undone.

Owner feedback 2026-11-08: every change gets a plain-language summary at the top
("Back up VM 104 to fast1"), the raw spec behind a disclosure. The approver read
YAML, and an approved destroy left the queue with nothing on screen saying what
it would do. This module turns an artifact into that summary, from the spec
itself - never from a guess: a step it does not recognise is said as the call
it is ("DELETE /nodes/pve1/..."), not paraphrased into something it might not be.

It also answers the two questions the Changes page asks before it offers the
one-tap "Approve and apply":

* ``two_step`` - a composite or a ``replay_safe: false`` change keeps approve
  and apply as two separate acts (review 06 3.2, UI_PLAN 3.6);
* ``danger`` - a destroy or a forced stop, which the page guards with a typed
  confirmation of ``confirm_name`` (UI_PLAN 3.10).

Read-only and side-effect free: it parses the artifact text and nothing else,
so it is safe on every read of an artifact.
"""

from __future__ import annotations

import re
from typing import Any

import yaml

from .models import ArtifactKind, parse_guest_power_spec

_GUEST_PATH = re.compile(
    r"^/?nodes/(?P<node>[^/]+)/(?P<type>qemu|lxc)/(?P<vmid>[^/?]+)(?P<rest>/[^?]*)?"
)
_VZDUMP_PATH = re.compile(r"^/?nodes/(?P<node>[^/]+)/vzdump/?$")

_GUEST_WORD = {"qemu": "VM", "lxc": "container"}

_KIND_SENTENCE = {
    ArtifactKind.ANSIBLE_PLAYBOOK.value: "Run an Ansible playbook on {target}",
    ArtifactKind.SHELL_SCRIPT.value: "Run a shell script on {target}",
    ArtifactKind.HOST_PROVISION.value: (
        "Bring {target} to a declared state (packages, files, services)"
    ),
    ArtifactKind.HTTP_SEQUENCE.value: "Make HTTP calls to {target}",
    ArtifactKind.GUEST_NETWORK.value: "Set up the guest network",
    ArtifactKind.KB_NOTE.value: "Record a note in the knowledge base",
}

_POWER_WORDS = {
    "start": "Start {guest}",
    "shutdown": "Shut down {guest} (gracefully; nothing is forced)",
    "reboot": "Reboot {guest}",
    "stop": "Stop {guest} forced, like pulling the power (unsaved state is lost)",
    "suspend": "Suspend {guest}",
    "resume": "Resume {guest}",
}


def target_label(target: dict[str, Any] | None) -> str:
    """The target as a person names it: ``web-01 (VM 101 on pve1)``."""
    t = target or {}
    kind = str(t.get("kind") or "")
    name = t.get("host") or t.get("name") or t.get("service") or t.get("network")
    vmid = t.get("vmid")
    node = t.get("node")
    word = {"vm": "VM", "lxc": "container"}.get(kind, "guest")
    where = ""
    if vmid not in (None, ""):
        where = f"{word} {vmid}" + (f" on {node}" if node else "")
    elif node and kind in ("node", "cluster", ""):
        where = f"node {node}"
    if name and where:
        return f"{name} ({where})"
    if name:
        return str(name)
    if where:
        return where
    return kind or "no target"


def confirm_name(target: dict[str, Any] | None) -> str | None:
    """The name the artifact gives its target, or None (UI_PLAN 3.10, #813).

    Never the VMID: a VMID is exactly what a different machine at the same
    number shares, so typing it back confirms nothing. The confirm dialog asks
    for the guest's name AS READ LIVE (``GET /artifacts/{id}/target``); this
    is only the name the change was written with."""
    t = target or {}
    for key in ("host", "name"):
        if t.get(key):
            return str(t[key])
    return None


def _proxmox_steps(body: str) -> list[dict[str, Any]]:
    m = re.search(r"```yaml\s+proxmox-api-spec\s*\n(.*?)```", body, re.DOTALL)
    if not m:
        return []
    try:
        parsed = yaml.safe_load(m.group(1).strip())
    except yaml.YAMLError:
        return []
    if not isinstance(parsed, dict) or not isinstance(parsed.get("steps"), list):
        return []
    return [s for s in parsed["steps"] if isinstance(s, dict)]


def _render(text: Any, context: dict[str, Any]) -> str:
    """Interpolate ``{{ target.vmid }}`` and friends the way the executor will;
    a template that does not render is shown as written."""
    from homepilot.executor.jinja_utils import InterpolationError, _interpolate

    raw = str(text or "")
    if "{{" not in raw:
        return raw
    try:
        return _interpolate(raw, context)
    except InterpolationError:
        return raw


def _proxmox_step_sentence(
    step: dict[str, Any], context: dict[str, Any], label: str
) -> tuple[str, str | None]:
    """One step in words, and the danger it carries (``destroy``/``stop``/None)."""
    method = str(step.get("method", "GET")).upper()
    path = _render(step.get("path", ""), context)
    raw_body = step.get("body")
    body: dict[str, Any] = raw_body if isinstance(raw_body, dict) else {}
    bare = path.split("?", 1)[0].rstrip("/")

    vz = _VZDUMP_PATH.match(bare)
    if vz and method == "POST":
        what = _render(body.get("vmid", ""), context)
        target_vmid = str((context.get("target") or {}).get("vmid", ""))
        if what and what == target_vmid:
            what = label
        elif what:
            what = f"guest{'s' if ',' in what else ''} {what}"
        else:
            what = "guests (all, or as selected by the job)"
        to = _render(body.get("storage", ""), context)
        return f"Back up {what}" + (f" to {to}" if to else ""), None

    g = _GUEST_PATH.match(bare)
    if g:
        guest = f"{_GUEST_WORD[g['type']]} {g['vmid']}"
        rest = (g["rest"] or "").strip("/")
        if method == "DELETE" and not rest:
            return f"Destroy {guest} on {g['node']}, with its disks", "destroy"
        if method == "POST" and rest.startswith("status/"):
            action = rest.split("/", 1)[1]
            words = _POWER_WORDS.get(action)
            if words:
                return words.format(guest=guest), ("stop" if action == "stop" else None)
        if rest == "config" and method in ("PUT", "POST"):
            deletes = body.get("delete")
            removed = (
                [k.strip() for k in deletes.split(",") if k.strip()]
                if isinstance(deletes, str)
                else [str(k) for k in deletes or []]
            )
            changed = [k for k in body if k not in ("delete", "digest", "skiplock", "revert")]
            parts = []
            if changed:
                parts.append("set " + ", ".join(changed))
            if removed:
                parts.append("remove " + ", ".join(removed))
            return f"Change the config of {guest}: " + ("; ".join(parts) or "no keys"), None
        if rest == "snapshot" and method == "POST":
            name = _render(body.get("snapname", ""), context)
            return f"Take a snapshot of {guest}" + (f" named {name}" if name else ""), None
        if rest.startswith("snapshot/") and method == "DELETE":
            return f"Delete snapshot {rest.split('/', 1)[1]} of {guest}", None
        if rest == "clone" and method == "POST":
            return f"Clone {guest} to a new guest {body.get('newid', '')}".rstrip(), None
        if rest == "migrate" and method == "POST":
            return f"Move {guest} to node {body.get('target', '?')}", None
        if rest == "resize" and method == "PUT":
            return f"Grow disk {body.get('disk', '?')} of {guest} to {body.get('size', '?')}", None
    if method == "GET":
        return f"Read {path}", None
    return f"{method} {path}", None


def summarize(fm: dict[str, Any], body: str) -> dict[str, Any]:
    """The plain-words summary of one artifact.

    ``{headline, target, steps, danger, confirm_name, two_step, two_step_reason}``:
    `headline` is the intent, `steps` what it does in order, `danger` the worst
    irreversible thing among them.
    """
    from homepilot.executor.jinja_utils import interpolation_context

    kind = str(fm.get("kind", ""))
    target = fm.get("target") or {}
    label = target_label(target)
    steps: list[str] = []
    danger: str | None = None
    # The guest the danger falls on: whose live name the confirm asks for.
    danger_vmid: Any = target.get("vmid")

    if kind == ArtifactKind.PROXMOX_API_SEQUENCE.value:
        context = interpolation_context(target, fm)
        for step in _proxmox_steps(body):
            sentence, step_danger = _proxmox_step_sentence(step, context, label)
            steps.append(sentence)
            if step_danger == "destroy" or (step_danger and danger is None):
                danger = step_danger
                g = _GUEST_PATH.match(_render(step.get("path", ""), context).split("?", 1)[0])
                if g:
                    danger_vmid = g["vmid"]
    elif kind == ArtifactKind.GUEST_POWER.value:
        try:
            action = parse_guest_power_spec(body).action
        except ValueError:
            action = ""
        words = _POWER_WORDS.get(action)
        if words:
            steps.append(words.format(guest=label))
        if action == "stop":
            danger = "stop"
    elif kind == ArtifactKind.COMPOSITE.value:
        steps.append("Run several changes in order (each is its own change, listed in the spec)")
    elif kind in _KIND_SENTENCE:
        steps.append(_KIND_SENTENCE[kind].format(target=label))

    two_step_reason: str | None = None
    if kind == ArtifactKind.COMPOSITE.value:
        two_step_reason = (
            "A composite runs several changes; it is approved first and applied as a separate step."
        )
    elif fm.get("replay_safe") is False:
        two_step_reason = (
            "This change cannot be run twice (replay_safe: false); it is approved first "
            "and applied as a separate step."
        )

    return {
        "headline": str(fm.get("intent", "")),
        "target": label,
        "steps": steps,
        "danger": danger,
        "confirm_name": confirm_name(target) if danger else None,
        "danger_vmid": int(danger_vmid) if danger and str(danger_vmid or "").isdigit() else None,
        "two_step": two_step_reason is not None,
        "two_step_reason": two_step_reason,
    }
