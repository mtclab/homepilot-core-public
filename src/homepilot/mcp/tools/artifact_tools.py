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
                        "JSON artifact spec. Must include: id, kind (host-provision, "
                        "guest-network, ansible-playbook, proxmox-api-sequence, "
                        "http-sequence, composite, shell-script, kb-note), intent, body, "
                        "produced_by. If kind != kb-note: "
                        "target and idempotence. host-provision is the native, working way to "
                        "install packages, manage services and write config on a managed host "
                        "(it runs over the agent and is the only kind with a real pre-apply plan "
                        "and a captured rollback); prefer it for host configuration. "
                        "guest-network builds and fences the guest subnet (SDN zone, vnet, "
                        "subnet, vnet firewall) from a ```yaml guest-network-spec``` block "
                        "whose omitted fields come from this instance's guest_network_* "
                        "settings; it is the ONLY way to change the guest network, and "
                        "query_guest_network shows the plan it would run."
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
            "Answers `state`: in_spec, drifted, or unknown - `unknown` means the "
            "check could not establish anything (no spec, no host, a read that "
            "failed), which is NOT the same as in spec. `drifted` is the boolean "
            "view and is false for both of the other two. "
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
                "state": {"type": "string"},
                "verification_log": {"type": "string"},
                "details": {"type": "object"},
            },
            "required": ["artifact_id", "drifted", "state"],
        },
    },
    {
        "name": "approve_artifact",
        "description": (
            "Approve a proposed artifact so the executor can apply it. Requires a "
            "human decision: the operator reads the artifact's approval_code from "
            "the web review screen, `hp artifacts show`, or a proposal "
            "notification, and relays it to you. You cannot approve without it - "
            "the code is never shown over this transport, so a valid code proves a "
            "human approved. Five wrong codes lock approval until an operator "
            "resets it. Requires write scope; rate-limited to 10 calls per minute "
            "per caller."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "artifact_id": {
                    "type": "string",
                    "description": "Artifact ID to approve",
                },
                "approval_code": {
                    "type": "string",
                    "description": (
                        "The per-artifact approval code a human relayed to you. "
                        "Grouping and case do not matter."
                    ),
                },
            },
            "required": ["artifact_id", "approval_code"],
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
    {
        "name": "get_artifact",
        "description": (
            "Read a whole artifact: its frontmatter AND its body, including the execution "
            "log appended when it was applied. This is how an agent sees what actually "
            "happened to something it proposed - `get_artifact_status` returns a status "
            "string and nothing else. Also the way to use a prior artifact as a pattern. "
            "Requires read scope."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "artifact_id": {"type": "string", "description": "Artifact ID to read"},
            },
            "required": ["artifact_id"],
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "kind": {"type": "string"},
                "status": {"type": "string"},
                "frontmatter": {"type": "object"},
                "body": {"type": "string"},
                "truncated": {"type": "boolean"},
            },
            "required": ["id", "body"],
        },
    },
    {
        "name": "get_task_result",
        "description": (
            "The outcome of ANY task this install ran - apply, replay, revoke, provision, "
            "tailnet_join, guest-template build: status, error, the execution log the "
            "runner kept, and `result`, the task's own recorded outcome (a provision's "
            "vmid/ip/tailnet, a tailnet_join's tailnet/tailnet_detail). Pass a task_id, or "
            "an artifact_id to get its most recent task. Without this an agent can start "
            "work and never learn whether it succeeded. Requires read scope."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID"},
                "artifact_id": {
                    "type": "string",
                    "description": "Artifact ID - returns its most recent task",
                },
            },
        },
        # artifact_id is NULLABLE and must say so. A provision, a tailnet re-join
        # and a guest-template build all carry no artifact, so the handler
        # returned null against a schema that promised a string - and every one
        # of those tasks came back over MCP as "Structured content does not match
        # the tool's output schema: data/artifact_id must be string", which is
        # to say the ONLY tool that reports an outcome could not report the
        # outcome of the tools that most need one (#628).
        "outputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "artifact_id": {"type": ["string", "null"]},
                "action": {"type": "string"},
                "status": {"type": "string"},
                "error": {"type": ["string", "null"]},
                "execution_log": {"type": "string"},
                "result": {"type": ["object", "null"]},
            },
            "required": ["id", "status"],
        },
    },
    {
        "name": "get_fleet_drift",
        "description": (
            "The STORED drift results for the whole fleet: for each checked artifact, "
            "its state (in_spec, drifted, or unverifiable), when it was checked, and "
            "the detail behind that verdict. These are the results of earlier checks, "
            "not a fresh probe - an artifact that has never been checked has no row "
            "here. Use check_artifact_drift to probe ONE artifact live. Read-only."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "artifact_id": {
                    "type": ["string", "null"],
                    "description": "Only this artifact's stored result, or null for the fleet",
                },
                "drifted": {
                    "type": ["boolean", "null"],
                    "description": "true for drifted only, false for non-drifted, null for all",
                },
                "limit": {"type": "integer", "description": "Page size (1-1000, default 100)"},
                "offset": {"type": "integer", "description": "Rows to skip (default 0)"},
            },
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "items": {"type": "array", "items": {"type": "object"}},
                "total": {"type": "integer"},
            },
            "required": ["items", "total"],
        },
    },
    # ── Artifact actions (MCP<->API parity, wave 2). ───────────────────────────
    # approve_artifact IS reachable over MCP (human-relay approval, #385 follow-up)
    # but is gated by a per-artifact approval code a human relays - the assistant
    # cannot see the code over MCP, so it still cannot approve its own change.
    # plan/preview are API `read` routes (read_only tier); reject/apply/replay are
    # `write` (full tier).
    {
        "name": "plan_artifact",
        "description": (
            "What applying this host-provision artifact would actually change on the "
            "host: the per-item before/after, a change count, whether the host already "
            "matches, and the policies you recorded for it. A READ-ONLY probe over the "
            "agent - it changes nothing on the host and is read-scoped, so a read-only "
            "MCP token may call it. Only host-provision artifacts have a plan engine; "
            "other kinds are refused rather than answered with an empty plan."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "artifact_id": {"type": "string", "description": "The artifact's id"},
            },
            "required": ["artifact_id"],
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "artifact_id": {"type": "string"},
                "host": {"type": "string"},
                "items": {"type": "array", "items": {"type": "object"}},
                "change_count": {"type": "integer"},
                "in_spec": {"type": "boolean"},
            },
        },
    },
    {
        "name": "preview_artifact",
        "description": (
            "A git diff of the artifact FILE against its previous committed version, "
            "plus its status, kind, intent, body and frontmatter. This describes how "
            "the spec TEXT changed - use plan_artifact to see what applying it would do "
            "to the host. Changes nothing and is read-scoped, so a read-only MCP token "
            "may call it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "artifact_id": {"type": "string", "description": "The artifact's id"},
            },
            "required": ["artifact_id"],
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "status": {"type": "string"},
                "diff": {"type": "string"},
            },
        },
    },
    {
        "name": "reject_artifact",
        "description": (
            "Reject a proposed artifact, marking it rejected so it will not be applied. "
            "This only changes the proposal's own state - it touches no host - which is "
            "why it is allowed over MCP where approve is not: rejecting cannot enact a "
            "change, so it does not collapse the propose->human-approve model. Optional "
            "reason. An artifact not in a rejectable state is refused."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "artifact_id": {"type": "string", "description": "The artifact's id"},
                "reason": {"type": ["string", "null"], "description": "Why it was rejected"},
            },
            "required": ["artifact_id"],
        },
        "outputSchema": {
            "type": "object",
            "properties": {"id": {"type": "string"}, "status": {"type": "string"}},
            "required": ["id", "status"],
        },
    },
    {
        "name": "apply_artifact",
        "description": (
            "Apply an already-APPROVED artifact, running its change on the host through "
            "the one execution engine. Allowed over MCP because the human approval is "
            "the gate and approval CANNOT be given over MCP - so applying here is "
            "sanctioned execution of a reviewed change, not an unreviewed one. An "
            "artifact that is not in the approved state is refused (nothing runs). "
            "Starts the task and returns its handle; poll get_task_result for the "
            "outcome."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "artifact_id": {"type": "string", "description": "The artifact's id"},
            },
            "required": ["artifact_id"],
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "artifact_id": {"type": "string"},
                "action": {"type": "string"},
                "status": {"type": "string"},
            },
            "required": ["task_id", "status"],
        },
    },
    {
        "name": "replay_artifact",
        "description": (
            "Re-apply an already-applied (or approved) artifact through the one "
            "execution engine, honouring its replay guards. Allowed over MCP for the "
            "same reason as apply: it re-runs a change that already cleared human "
            "approval, and approval cannot be given over MCP. Refused while an apply or "
            "replay of the same artifact is in flight. Starts the task and returns its "
            "handle; poll get_task_result for the outcome."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "artifact_id": {"type": "string", "description": "The artifact's id"},
            },
            "required": ["artifact_id"],
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "artifact_id": {"type": "string"},
                "action": {"type": "string"},
                "status": {"type": "string"},
            },
            "required": ["task_id", "status"],
        },
    },
    {
        "name": "revoke_artifact",
        "description": (
            "Roll an APPLIED (or approved/failed) artifact back on its host through the "
            "one execution engine, and mark it revoked. Refused for anything not in a "
            "revocable state, and while an apply or replay of the same artifact is in "
            "flight. Like apply/replay it re-runs a change that already cleared human "
            "approval and cannot itself grant approval, so it is `full`-scoped, not "
            "admin. Starts the task and returns its handle; poll get_task_result."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "artifact_id": {"type": "string", "description": "The artifact's id"},
                "reason": {
                    "type": ["string", "null"],
                    "description": "Optional reason recorded on the revocation",
                },
            },
            "required": ["artifact_id"],
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "artifact_id": {"type": "string"},
                "action": {"type": "string"},
                "status": {"type": "string"},
            },
            "required": ["task_id", "status"],
        },
    },
]

# An artifact body carries the execution log, which can be long. Truncate from the
# FRONT: the end of a log is where the failure is (the same rule the task runner
# applies when it stores one).
_MAX_BODY_CHARS = 40000


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

    from homepilot.artifacts.lifecycle import ConflictError, LifecycleError

    lifecycle = ctx["lifecycle"]
    try:
        artifact_id = await lifecycle.propose(spec)
    except (LifecycleError, ConflictError) as exc:
        # A LifecycleError NAMES what is wrong and how to fix it ("intent must be
        # 1-200 chars"). Letting it escape turned that into a bare "Internal
        # server error" over MCP, where the caller cannot read the log either -
        # so a one-line correction became a log-diving exercise for someone who
        # has no log (#635). Every other refusal on this transport says what it
        # refused and why; this one now does too.
        raise ValueError(str(exc)) from None
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
        # Stays deliberately opaque: an arbitrary exception's text can carry
        # internal paths and secrets, and this transport is where an untrusted
        # caller reads it (gated by test_drift_api_mcp's no-leak assertion). The
        # REASON now reaches the caller by the right door instead - the verifier
        # answers UNKNOWN with an explanation rather than raising, so this path
        # is the last resort rather than the ordinary one.
        logger.exception("Drift check failed for %s", artifact_id)
        raise RuntimeError("Drift check failed") from None
    return {
        "artifact_id": result.artifact_id,
        "drifted": result.drifted,
        # The tri-state, not just the boolean: `drifted: false` alone cannot tell
        # "checked and in spec" from "could not be checked" (#425), and this
        # transport is where an agent decides what to do next.
        "state": result.state.value,
        "verification_log": result.verification_log,
        "details": result.details,
    }


async def handle_approve_artifact(arguments: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """Approve a proposed artifact, gated by a human-relayed approval code (#385).

    The assistant proposes but must not approve its own change. A per-artifact
    approval code the assistant cannot see (random, never returned by any MCP
    read) is the bridge: a human reads it from an operator surface and relays it,
    and a valid code is proof a human decided. Wrong codes are counted and lock
    approval for the artifact after `_APPROVE_LOCK_THRESHOLD` tries, so even a
    weak code cannot be brute-forced over MCP. The refusal never reveals the
    code."""
    from homepilot.artifacts.approval_code import LOCK_THRESHOLD, verify_code

    caller_id = ctx.get("_mcp_caller_id", "mcp-stdio")
    _check_approve_ratelimit(caller_id)
    artifact_id = arguments["artifact_id"]

    # The code is mandatory. Absent/blank -> refused WITHOUT touching lifecycle:
    # this is the self-approve guard - the assistant calling the tool alone
    # cannot approve (#385 gate 4).
    approval_code = arguments.get("approval_code")
    if not approval_code or not str(approval_code).strip():
        raise ValueError(
            "approve_artifact requires an approval_code a human relays to you — "
            "the assistant cannot approve its own proposal. Ask the operator to "
            "read the code from the review screen or `hp artifacts show`."
        )

    repo = ctx.get("repo")
    if repo is None:
        raise RuntimeError("approval-code store unavailable")

    row = await repo.get_approval_code_row(artifact_id)
    if row is None:
        # No live code: either the artifact is not awaiting approval or it was
        # already decided. Do not distinguish - and do not leak whether the id
        # even exists as PROPOSED.
        raise ValueError(
            f"Artifact '{artifact_id}' is not awaiting a coded approval "
            "(no approval code is active for it)."
        )
    if int(row["locked"]):
        raise ValueError(
            f"Approval for '{artifact_id}' is locked after too many wrong codes. "
            "An operator must reset it from the web UI or "
            "`hp artifacts reset-approval` before it can be approved over MCP."
        )

    if not verify_code(str(approval_code), str(row["code"])):
        state = await repo.record_failed_approval(artifact_id, LOCK_THRESHOLD)
        # Never echo the code or how close the guess was beyond locked/not.
        if state["locked"]:
            raise ValueError(
                f"Incorrect approval code. Approval for '{artifact_id}' is now "
                "locked; an operator must reset it before any further attempt."
            )
        raise ValueError("Incorrect approval code.")

    # Verified: a human approved. Record the actor as an operator-via-code, not
    # the assistant. lifecycle.approve clears the (now spent) code.
    lifecycle = ctx["lifecycle"]
    await lifecycle.approve(
        artifact_id, user="operator-code via MCP", reason="Approved via MCP approval code"
    )
    fm, _ = await asyncio.to_thread(ctx["store"].read, artifact_id)
    logger.info("MCP approve_artifact: %s approved via operator code", artifact_id)
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


async def handle_get_artifact(arguments: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """The whole artifact, body included (#427).

    An agent could propose an artifact and then never read it back: the execution
    log is appended to the BODY on apply, and no MCP tool returned a body. So the
    AI half of "AI-first" could not see what happened when its own artifact ran,
    and could not use a prior artifact as a pattern.
    """
    artifact_id = arguments["artifact_id"]
    try:
        fm, body = await asyncio.to_thread(ctx["store"].read, artifact_id)
    except FileNotFoundError as exc:
        raise ValueError(f"Artifact not found: {artifact_id}") from exc

    truncated = len(body) > _MAX_BODY_CHARS
    if truncated:
        body = (
            f"[earlier content truncated, showing the last {_MAX_BODY_CHARS} chars]\n"
            + body[-_MAX_BODY_CHARS:]
        )
    return {
        "id": fm.get("id", artifact_id),
        "kind": fm.get("kind", ""),
        "status": fm.get("status", ""),
        "frontmatter": fm,
        "body": body,
        "truncated": truncated,
    }


async def handle_get_task_result(arguments: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """What an apply/replay/revoke actually did (#427).

    Task results lived only under /tasks with no MCP equivalent, so an agent could
    start work and never learn the outcome.
    """
    task_repo = ctx.get("task_repo")
    if task_repo is None:
        raise RuntimeError("Task repository not configured")

    task_id = arguments.get("task_id")
    artifact_id = arguments.get("artifact_id")
    if not task_id and not artifact_id:
        raise ValueError("pass task_id or artifact_id")

    task = None
    if task_id:
        task = await task_repo.get_task(task_id)
    else:
        tasks = await task_repo.list_tasks(artifact_id=artifact_id, limit=1)
        task = tasks[0] if tasks else None
        if task is not None:
            task = await task_repo.get_task(task["id"])
    if task is None:
        raise ValueError(f"Task not found: {task_id or artifact_id}")

    execution_log = ""
    result: dict[str, Any] | None = None
    raw = task.get("result_json")
    if raw:
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, dict):
            result = parsed
            execution_log = str(parsed.get("execution_log", ""))
    return {
        "id": task.get("id", ""),
        # `.get(key, "")` returns the STORED None, not the default, because the
        # key is present: every provision / tailnet_join / template build has a
        # null artifact_id and every one of them broke this tool's own output
        # schema. Nullable in the schema, nullable here (#628).
        "artifact_id": task.get("artifact_id"),
        "action": task.get("action", ""),
        "status": task.get("status", ""),
        "error": task.get("error"),
        "execution_log": execution_log,
        # The task's whole recorded outcome. `execution_log` is the artifact
        # runner's field and only the artifact runner writes it, so reading a
        # provision through this tool used to answer with four empty strings and
        # nothing at all about the machine it built.
        "result": result,
    }


async def handle_get_fleet_drift(arguments: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """The stored drift table, as GET /artifacts/drift returns it without refresh.

    Distinct from check_artifact_drift, which RUNS a check against one artifact's
    host. This reads what previous cycles recorded - including the absence of a
    row, which means "never checked" and is exactly the thing a drift percentage
    must not quietly count as healthy.

    Deliberately no `refresh` parameter: a refresh runs a verification cycle
    across the fleet, which is work with real side effects on managed hosts, and
    a read-scoped tool must not be able to start it.
    """
    repo = ctx.get("repo")
    if repo is None:
        raise RuntimeError("Repository not configured")

    artifact_id = arguments.get("artifact_id")
    if artifact_id:
        row = await repo.get_drift_check(str(artifact_id))
        items = [row] if row else []
    else:
        raw_limit = arguments.get("limit")
        limit = 100 if raw_limit is None else max(1, min(1000, int(raw_limit)))
        items = await repo.get_drift_checks(
            drifted=arguments.get("drifted"),
            limit=limit,
            offset=max(0, int(arguments.get("offset") or 0)),
        )
    return {"items": items, "total": len(items)}


# ── Mutators (wave 2). Same shared callables / lifecycle / task-runner methods the
# artifact management routes call. Every LifecycleError becomes a ValueError so the
# MCP client sees a clean refusal (the same 409/400 the route returns). ───────────


def _require_task_runner(ctx: dict[str, Any]) -> Any:
    runner = ctx.get("task_runner")
    if runner is None:
        raise RuntimeError("Task runner not configured")
    return runner


async def handle_plan_artifact(arguments: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    from homepilot.artifacts.router import ArtifactActionError, build_artifact_plan

    executor = getattr(ctx.get("lifecycle"), "_executor_ref", None)
    agent = getattr(executor, "agent", None) if executor is not None else ctx.get("agent_adapter")
    try:
        return await build_artifact_plan(
            str(arguments["artifact_id"]),
            store=ctx["store"],
            agent=agent,
            kb_service=ctx.get("kb_service"),
        )
    except ArtifactActionError as exc:
        raise ValueError(exc.detail) from exc


async def handle_preview_artifact(arguments: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    from homepilot.artifacts.router import ArtifactActionError, build_artifact_preview

    try:
        return await asyncio.to_thread(
            build_artifact_preview, str(arguments["artifact_id"]), store=ctx["store"]
        )
    except ArtifactActionError as exc:
        raise ValueError(exc.detail) from exc


async def handle_reject_artifact(arguments: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    from homepilot.artifacts.lifecycle import LifecycleError

    artifact_id = str(arguments["artifact_id"])
    reason = arguments.get("reason")
    caller_id = ctx.get("_mcp_caller_id", "mcp-stdio")
    lifecycle = ctx["lifecycle"]
    try:
        await lifecycle.reject(artifact_id, caller_id, reason)
    except FileNotFoundError as exc:
        raise ValueError(f"Artifact not found: {artifact_id}") from exc
    except LifecycleError as exc:
        raise ValueError(str(exc)) from exc
    return {"id": artifact_id, "status": "rejected"}


async def handle_apply_artifact(arguments: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    from homepilot.artifacts.lifecycle import LifecycleError

    artifact_id = str(arguments["artifact_id"])
    caller_id = ctx.get("_mcp_caller_id", "mcp-stdio")
    runner = _require_task_runner(ctx)
    try:
        # start_apply refuses a non-APPROVED artifact BEFORE creating a task
        # (ConflictError -> ValueError here), which is the human-approval gate the
        # MCP transport cannot bypass. Returns the task handle; the caller polls
        # get_task_result for the outcome.
        task_info: dict[str, Any] = await runner.start_apply(artifact_id, caller_id)
        return task_info
    except LifecycleError as exc:
        raise ValueError(str(exc)) from exc


async def handle_replay_artifact(arguments: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    from homepilot.artifacts.lifecycle import LifecycleError

    artifact_id = str(arguments["artifact_id"])
    runner = _require_task_runner(ctx)
    try:
        task_info: dict[str, Any] = await runner.start_replay(artifact_id)
        return task_info
    except LifecycleError as exc:
        raise ValueError(str(exc)) from exc


async def handle_revoke_artifact(arguments: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    from homepilot.artifacts.lifecycle import LifecycleError

    artifact_id = str(arguments["artifact_id"])
    reason = arguments.get("reason")
    caller_id = ctx.get("_mcp_caller_id", "mcp-stdio")
    runner = _require_task_runner(ctx)
    try:
        # start_revoke refuses a non-revocable artifact and an apply-in-progress
        # BEFORE creating a task (ConflictError/ValueError here), exactly as
        # DELETE /artifacts/{id} does. Returns the task handle; poll for outcome.
        task_info: dict[str, Any] = await runner.start_revoke(artifact_id, caller_id, reason)
        return task_info
    except LifecycleError as exc:
        raise ValueError(str(exc)) from exc
