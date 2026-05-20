"""Artifact tools: query, propose, approve, status, and check drift."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

_APPROVE_RATELIMIT_MAX = 10
_APPROVE_RATELIMIT_WINDOW = 60
_APPROVE_RATELIMIT_MAX_KEYS = 10000

logger = logging.getLogger(__name__)

_approve_ratelimit: dict[str, list[float]] = {}

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "query_artifacts",
        "description": (
            "Find prior artifacts by status, kind, host, or date. "
            'Pass a JSON filter object (e.g. {"status": "applied", "kind": "ansible-playbook"}) '
            "or None for all."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "filter": {
                    "type": ["string", "null"],
                    "description": "JSON filter object or null for all artifacts",
                },
            },
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "items": {"type": "array", "items": {"type": "object"}},
                "summary": {"type": "array", "items": {"type": "object"}},
                "total": {"type": "integer"},
            },
            "required": ["items", "total"],
        },
    },
    {
        "name": "propose_artifact",
        "description": (
            "Propose a new artifact — the ONLY way an agent can initiate a mutation. "
            "Creates a fully-specified plan with status: proposed. "
            "A human must review and approve before the executor applies it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "spec": {
                    "type": "string",
                    "description": (
                        "JSON artifact spec. Must include: id, kind (ansible-playbook, "
                        "proxmox-api-sequence, http-sequence, composite, shell-script, kb-note), "
                        "intent, body, produced_by. If kind != kb-note: target and idempotence."
                    ),
                },
            },
            "required": ["spec"],
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "status": {"type": "string"},
                "kind": {"type": "string"},
                "intent": {"type": "string"},
                "message": {"type": "string"},
            },
            "required": ["id", "status"],
        },
    },
    {
        "name": "check_artifact_drift",
        "description": (
            "Check whether an applied artifact has drifted from its desired state. "
            "Returns drift status, verification log, and details. "
            "Always performs a fresh check (not cached)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "artifact_id": {
                    "type": "string",
                    "description": "Artifact ID to check for drift",
                },
            },
            "required": ["artifact_id"],
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "artifact_id": {"type": "string"},
                "drifted": {"type": "boolean"},
                "verification_log": {"type": "string"},
                "details": {"type": "object"},
            },
            "required": ["artifact_id", "drifted"],
        },
    },
    {
        "name": "approve_artifact",
        "description": (
            "Approve a proposed artifact so the executor can apply it. "
            "Requires write scope. Rate-limited to 10 calls per minute per caller."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "artifact_id": {
                    "type": "string",
                    "description": "Artifact ID to approve",
                },
            },
            "required": ["artifact_id"],
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "status": {"type": "string"},
                "kind": {"type": "string"},
                "intent": {"type": "string"},
                "approved_by": {"type": "object"},
            },
            "required": ["id", "status"],
        },
    },
    {
        "name": "get_artifact_status",
        "description": (
            "Get detailed status of a single artifact including id, kind, status, "
            "intent, last_updated timestamp, and target info. Requires read scope."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "artifact_id": {
                    "type": "string",
                    "description": "Artifact ID to look up",
                },
            },
            "required": ["artifact_id"],
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "kind": {"type": "string"},
                "status": {"type": "string"},
                "intent": {"type": "string"},
                "last_updated": {"type": "string"},
                "target": {"type": "object"},
            },
            "required": ["id", "kind", "status"],
        },
    },
]


def _check_approve_ratelimit(caller_id: str) -> None:
    now = time.monotonic()
    if len(_approve_ratelimit) > _APPROVE_RATELIMIT_MAX_KEYS:
        _approve_ratelimit.clear()
    window = _approve_ratelimit.setdefault(caller_id, [])
    window[:] = [t for t in window if now - t < _APPROVE_RATELIMIT_WINDOW]
    if len(window) >= _APPROVE_RATELIMIT_MAX:
        raise ValueError(
            f"Rate limit exceeded: max {_APPROVE_RATELIMIT_MAX} approve calls "
            f"per {_APPROVE_RATELIMIT_WINDOW}s for caller '{caller_id}'"
        )
    window.append(now)


async def handle_query_artifacts(arguments: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    filter_str = arguments.get("filter")
    filter_obj = None
    if filter_str:
        try:
            filter_obj = json.loads(filter_str)
        except json.JSONDecodeError:
            filter_obj = {"status": filter_str}

    status = filter_obj.get("status") if filter_obj else None
    kind = filter_obj.get("kind") if filter_obj else None
    target_filter = filter_obj.get("target") if filter_obj else None
    results = await asyncio.to_thread(ctx["store"].list, status=status, kind=kind)
    if target_filter:
        filtered = []
        for r in results:
            t = r.get("target")
            if t is None:
                continue
            if isinstance(t, dict):
                if t.get("host") == target_filter or t.get("node") == target_filter:
                    filtered.append(r)
            elif isinstance(t, str) and t == target_filter:
                filtered.append(r)
        results = filtered
    summary = [
        {
            "id": r.get("id", ""),
            "kind": r.get("kind", ""),
            "status": r.get("status", ""),
            "intent": r.get("intent", ""),
            "created_at": r.get("created_at", ""),
        }
        for r in results
    ]
    return {"items": results, "summary": summary, "total": len(results)}


async def handle_propose_artifact(arguments: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    spec_str = arguments["spec"]
    try:
        spec = json.loads(spec_str)
    except json.JSONDecodeError as e:
        raise ValueError("spec must be valid JSON") from e

    if not spec.get("produced_by"):
        spec["produced_by"] = {"session": "mcp", "agent": "mcp-tool", "user": "mcp"}

    lifecycle = ctx["lifecycle"]
    artifact_id = await lifecycle.propose(spec)
    fm, _ = await asyncio.to_thread(ctx["store"].read, artifact_id)
    msg = (
        f"Artifact {artifact_id} created with status: "
        f"{fm.get('status', 'unknown')}. "
        f"Review with: hp artifacts show {artifact_id}"
    )
    return {
        "id": artifact_id,
        "status": fm.get("status", "unknown"),
        "kind": fm.get("kind", ""),
        "intent": fm.get("intent", ""),
        "message": msg,
    }


async def handle_check_artifact_drift(
    arguments: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    artifact_id = arguments["artifact_id"]
    drift_reconciler = ctx.get("drift_reconciler")
    if drift_reconciler is None:
        raise RuntimeError("DriftReconciler not configured")
    try:
        result = await drift_reconciler.check_single(artifact_id)
    except FileNotFoundError:
        raise ValueError(f"Artifact not found: {artifact_id}") from None
    except Exception:  # MCP tool error handler, converts to RuntimeError for client
        logger.exception("Drift check failed for %s", artifact_id)
        raise RuntimeError("Drift check failed") from None
    return {
        "artifact_id": result.artifact_id,
        "drifted": result.drifted,
        "verification_log": result.verification_log,
        "details": result.details,
    }


async def handle_approve_artifact(arguments: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    caller_id = ctx.get("_mcp_caller_id", "mcp-stdio")
    _check_approve_ratelimit(caller_id)
    artifact_id = arguments["artifact_id"]
    lifecycle = ctx["lifecycle"]
    await lifecycle.approve(artifact_id, user=caller_id, reason="Approved via MCP")
    fm, _ = await asyncio.to_thread(ctx["store"].read, artifact_id)
    logger.info("MCP approve_artifact: %s approved by %s", artifact_id, caller_id)
    return {
        "id": artifact_id,
        "status": fm.get("status", "approved"),
        "kind": fm.get("kind", ""),
        "intent": fm.get("intent", ""),
        "approved_by": fm.get("approved_by"),
    }


async def handle_get_artifact_status(
    arguments: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    artifact_id = arguments["artifact_id"]
    try:
        fm, _ = await asyncio.to_thread(ctx["store"].read, artifact_id)
    except FileNotFoundError as exc:
        raise ValueError(f"Artifact not found: {artifact_id}") from exc
    return {
        "id": fm.get("id", ""),
        "kind": fm.get("kind", ""),
        "status": fm.get("status", ""),
        "intent": fm.get("intent", ""),
        "last_updated": fm.get("approved_at") or fm.get("created_at", ""),
        "target": fm.get("target"),
    }
