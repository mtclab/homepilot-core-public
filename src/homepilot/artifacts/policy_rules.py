"""Structured policy rules: the narrow, machine-readable subset of a KB policy (#506).

A KB policy (``kind: "policy"``) is free text, and stays free text: it is shown
beside the plan on the review screen (#503) and nothing reads more into it. A
policy MAY also carry a ``policy-rule`` block - the only part HomePilot checks:

    ```policy-rule
    forbid:
      - kind: proxmox-api-sequence   # an artifact kind
        verb: DELETE                 # an operation's verb (see below)
        path: /nodes/*/qemu/*        # a glob over the operation's path
    ```

Each entry names at least one of ``kind`` / ``verb`` / ``path``; an entry
matches a change when every field it names matches. ``forbid`` may be one
entry or a list. A block that does not parse is reported, never silently
ignored (``PolicyRules.errors``).

What a change's operations are, by kind (the verbs a rule can name):

* ``proxmox-api-sequence`` / ``http-sequence`` - each step: the HTTP method
  (``GET``, ``POST``, ``PUT``, ``DELETE``) and its path, target-rendered
  (``/nodes/pve1/qemu/105``; the query string dropped);
* ``host-provision`` - ``install`` + package name, ``write`` + config file
  path, and the service's declared state (``running``, ``stopped``...) + its
  name;
* ``composite`` - each step artifact's own operations, against that step's own
  target's policies;
* any other kind - none: only ``kind`` can match it.

A match is NEVER a refusal at propose: a proposal is a draft, and refusing it
would hide from the human the very change they might mean to make. At APPROVE
it is a warning the approver must acknowledge (``acknowledge_policy=true`` on
the API, MCP and CLI; the acknowledgement is recorded on the approval and in
the journal). Which policies apply to a change is the review screen's rule
(``KBService.policies_for_target``): those targeting its host, and global ones.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from typing import Any
from urllib.parse import urlsplit

import yaml

from .models import ArtifactKind

logger = logging.getLogger(__name__)

FENCE = "policy-rule"
_BLOCK = re.compile(r"```(?:yaml\s+)?" + FENCE + r"\s*\n(.*?)```", re.DOTALL)
_FIELDS = ("kind", "verb", "path")


@dataclass(frozen=True)
class Rule:
    kind: str | None = None
    verb: str | None = None
    path: str | None = None

    def describe(self) -> str:
        parts = []
        if self.kind:
            parts.append(f"kind {self.kind}")
        if self.verb:
            parts.append(f"verb {self.verb}")
        if self.path:
            parts.append(f"path {self.path}")
        return "forbid " + ", ".join(parts)


@dataclass(frozen=True)
class PolicyRules:
    rules: list[Rule] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Operation:
    verb: str
    path: str


def parse_rules(content: str) -> PolicyRules:
    """The ``policy-rule`` blocks of one policy's text. Free text yields nothing."""
    rules: list[Rule] = []
    errors: list[str] = []
    kinds = {k.value for k in ArtifactKind}
    for n, m in enumerate(_BLOCK.finditer(content or ""), start=1):
        where = f"policy-rule block {n}"
        try:
            parsed = yaml.safe_load(m.group(1))
        except yaml.YAMLError as exc:
            errors.append(f"{where} is not valid YAML: {exc}")
            continue
        if not isinstance(parsed, dict) or set(parsed) != {"forbid"}:
            errors.append(f"{where} must be a mapping with exactly one key, `forbid`")
            continue
        entries = parsed["forbid"]
        entries = entries if isinstance(entries, list) else [entries]
        for i, entry in enumerate(entries, start=1):
            at = f"{where}, entry {i}"
            if not isinstance(entry, dict) or not entry:
                errors.append(f"{at} must name at least one of kind / verb / path")
                continue
            unknown = sorted(set(entry) - set(_FIELDS))
            if unknown:
                errors.append(f"{at} has unknown field(s) {unknown}; allowed: kind, verb, path")
                continue
            if not all(isinstance(entry[k], str) and entry[k].strip() for k in entry):
                errors.append(f"{at}: every field must be a non-empty string")
                continue
            kind = entry.get("kind")
            if kind is not None and kind not in kinds:
                errors.append(f"{at}: {kind!r} is not an artifact kind")
                continue
            verb = entry.get("verb")
            rules.append(
                Rule(kind=kind, verb=verb.strip() if verb else None, path=entry.get("path"))
            )
    return PolicyRules(rules, errors)


def _api_path(raw: str) -> str:
    """A step's path as a rule matches it: absolute, no scheme/host, no query."""
    text = raw.strip()
    if "://" in text:
        text = urlsplit(text).path
    text = text.split("?", 1)[0]
    return text if text.startswith("/") else "/" + text


def operations(fm: dict[str, Any], body: str) -> list[Operation]:
    """What a change does, as (verb, path) pairs a rule can match. Never raises."""
    from .summary import _proxmox_steps, _render

    kind = str(fm.get("kind") or "")
    if kind == ArtifactKind.PROXMOX_API_SEQUENCE.value:
        from homepilot.executor.jinja_utils import interpolation_context

        context = interpolation_context(fm.get("target") or {}, fm)
        return [
            Operation(
                str(step.get("method", "GET")).upper(),
                _api_path(_render(step.get("path", ""), context)),
            )
            for step in _proxmox_steps(body)
        ]
    if kind == ArtifactKind.HTTP_SEQUENCE.value:
        m = re.search(r"```yaml\s+http-spec\s*\n(.*?)```", body, re.DOTALL)
        try:
            parsed = yaml.safe_load(m.group(1)) if m else None
        except yaml.YAMLError:
            return []
        steps = parsed.get("steps") if isinstance(parsed, dict) else None
        return [
            Operation(str(s.get("method", "GET")).upper(), _api_path(str(s.get("path", ""))))
            for s in steps or []
            if isinstance(s, dict)
        ]
    if kind == ArtifactKind.HOST_PROVISION.value:
        from .models import parse_host_provision_spec

        try:
            spec = parse_host_provision_spec(body)
        except ValueError:
            return []
        ops = [Operation("install", p) for p in spec.packages]
        ops += [Operation("write", f.path) for f in spec.config_files]
        ops += [Operation(str(s.state), s.name) for s in spec.services]
        return ops
    return []


def matches(rule: Rule, kind: str, ops: list[Operation]) -> list[str]:
    """What in this change the rule names - empty when it does not match."""
    if rule.kind and rule.kind != kind:
        return []
    if not rule.verb and not rule.path:
        return [f"a {kind} change"]
    return [
        f"{op.verb} {op.path}"
        for op in ops
        if (not rule.verb or op.verb.upper() == rule.verb.upper())
        and (not rule.path or fnmatchcase(op.path, rule.path))
    ]


def target_host(fm: dict[str, Any]) -> str:
    """The host a change's policies are looked up for (the review screen's choice)."""
    target = fm.get("target") or {}
    if not isinstance(target, dict):
        return str(target or "")
    return str(target.get("host") or target.get("node") or "")


def _changes(
    artifact_id: str, store: Any, seen: set[str] | None = None
) -> list[tuple[str, dict[str, Any], str]]:
    """The change and, for a composite, every step artifact it would approve."""
    from .models import extract_composite_steps

    seen = seen if seen is not None else set()
    if artifact_id in seen:
        return []
    seen.add(artifact_id)
    fm, body = store.read(artifact_id)
    out = [(artifact_id, fm, body)]
    if fm.get("kind") == ArtifactKind.COMPOSITE.value:
        try:
            steps = extract_composite_steps(body)
        except ValueError:
            steps = []
        for step in steps:
            sub = str(step.get("artifact") or "")
            if sub:
                try:
                    out += _changes(sub, store, seen)
                except FileNotFoundError:
                    continue
    return out


async def policy_warnings(
    artifact_id: str,
    store: Any,
    kb_service: Any,
    known: dict[str, list[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    """Every structured policy rule this change (and a composite's steps) matches.

    One entry per (policy, rule, change) that matches, naming what matched. A
    policy whose ``policy-rule`` block does not parse yields an entry with
    ``errors`` and no match - said, never silently dropped. No KB, no warnings:
    the free-text policies are still on the review screen.
    """
    from homepilot.runtime.blocking import run_blocking

    if kb_service is None:
        return []
    changes = await run_blocking("io", _changes, artifact_id, store)
    out: list[dict[str, Any]] = []
    # `known`: policies the caller already looked up, by host - the plan has
    # them for its own list, and asking the KB twice for one screen is waste.
    cache: dict[str, list[dict[str, Any]]] = dict(known or {})
    for change_id, fm, body in changes:
        host = target_host(fm)
        if host not in cache:
            try:
                cache[host] = await kb_service.policies_for_target(host) if host else []
            except (sqlite3.Error, OSError, RuntimeError):
                # The same best effort as the review screen's list (#503): an
                # unreadable KB is logged loudly, and the approval is not held
                # hostage to it - the rules are warnings, not a lock.
                logger.warning("KB policy lookup failed for %s", host, exc_info=True)
                cache[host] = []
        kind = str(fm.get("kind") or "")
        ops = operations(fm, body)
        for policy in cache[host]:
            parsed = parse_rules(str(policy.get("content") or ""))
            for rule in parsed.rules:
                hits = matches(rule, kind, ops)
                if hits:
                    out.append(
                        {
                            "policy_id": policy.get("id"),
                            "policy_title": policy.get("title", ""),
                            "applies_via": policy.get("applies_via"),
                            "rule": rule.describe(),
                            "artifact_id": change_id,
                            "matched": hits,
                        }
                    )
            if parsed.errors and change_id == artifact_id:
                out.append(
                    {
                        "policy_id": policy.get("id"),
                        "policy_title": policy.get("title", ""),
                        "applies_via": policy.get("applies_via"),
                        "rule": None,
                        "artifact_id": change_id,
                        "matched": [],
                        "errors": parsed.errors,
                    }
                )
    return out


class PolicyAcknowledgementRequiredError(Exception):
    """An approval of a change a structured policy rule matches, not acknowledged.

    Not a refusal of the change: the same approval with the acknowledgement
    goes through. ``warnings`` are the matches, for the caller to show.
    """

    def __init__(self, warnings: list[dict[str, Any]]) -> None:
        self.warnings = blocking(warnings)
        super().__init__(warning_sentence(warnings))


def blocking(warnings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The warnings an approver must acknowledge: the ones that matched."""
    return [w for w in warnings if w.get("matched")]


def warning_sentence(warnings: list[dict[str, Any]]) -> str:
    """The refusal's text: each matched policy and what it matched, in one line each."""
    lines = [
        f'"{w["policy_title"] or w["policy_id"]}" ({w["rule"]}) matches {w["artifact_id"]}: '
        + "; ".join(w["matched"][:5])
        + (f" and {len(w['matched']) - 5} more" if len(w["matched"]) > 5 else "")
        for w in blocking(warnings)
    ]
    return (
        "This change matches a policy - approving it needs the policy acknowledged "
        "(acknowledge_policy=true; in the CLI --acknowledge-policy):\n  " + "\n  ".join(lines)
    )


__all__ = [
    "FENCE",
    "Operation",
    "PolicyAcknowledgementRequiredError",
    "PolicyRules",
    "Rule",
    "blocking",
    "matches",
    "operations",
    "parse_rules",
    "policy_warnings",
    "target_host",
    "warning_sentence",
]
