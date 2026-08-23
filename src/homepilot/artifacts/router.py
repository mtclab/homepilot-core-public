from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import subprocess
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

from ..auth.deps import require_scope, require_token
from ..sse import bus as sse_bus
from .lifecycle import ArtifactLifecycle, ConflictError, LifecycleError
from .store import ArtifactStore

logger = logging.getLogger(__name__)

router = APIRouter()


# Module-level so the dependency is not constructed in an argument default (B008).
_TokenDep = Annotated[dict[str, Any], Depends(require_token)]


def _get_store(request: Request) -> ArtifactStore:
    store: ArtifactStore = request.app.state.artifact_store
    return store


def _get_lifecycle(request: Request) -> ArtifactLifecycle:
    lifecycle: ArtifactLifecycle = request.app.state.artifact_lifecycle
    return lifecycle


@router.get("", dependencies=[Depends(require_scope("read"))])
async def list_artifacts(
    request: Request,
    status: str | None = Query(None),
    kind: str | None = Query(None),
    q: str | None = Query(None, description="Free text over id, intent, kind, target, tags"),
    limit: int = Query(100, ge=1, le=1000),
) -> dict[str, Any]:
    store = _get_store(request)
    items = store.list(status=status, kind=kind, q=q)
    return {"items": items[:limit], "total": len(items)}


@router.get("/drift", dependencies=[Depends(require_scope("read"))])
async def get_drift_status(
    request: Request,
    refresh: bool = Query(False),
    artifact_id: str | None = Query(None),
    drifted: bool | None = Query(None),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    from homepilot.reconciler import DriftReconciler

    if refresh:
        drift_reconciler: DriftReconciler | None = getattr(
            request.app.state, "drift_reconciler", None
        )
        if drift_reconciler is None:
            raise HTTPException(status_code=501, detail="DriftReconciler not configured")

        if artifact_id:
            try:
                await drift_reconciler.check_single(artifact_id)
            except FileNotFoundError:
                raise HTTPException(
                    status_code=404,
                    detail=f"Artifact not found: {artifact_id}",
                ) from None
            except Exception as exc:
                raise HTTPException(status_code=500, detail="Drift check failed") from exc
            row = await request.app.state.repo.get_drift_check(artifact_id)
            items = [row] if row else []
        else:
            cycle_result = await drift_reconciler.run()
            if not cycle_result.success:
                raise HTTPException(
                    status_code=500,
                    detail="Drift cycle failed",
                )
            items = await request.app.state.repo.get_drift_checks(
                drifted=drifted, limit=limit, offset=offset
            )
    else:
        if artifact_id:
            row = await request.app.state.repo.get_drift_check(artifact_id)
            items = [row] if row else []
        else:
            items = await request.app.state.repo.get_drift_checks(
                drifted=drifted, limit=limit, offset=offset
            )

    return {"items": items, "total": len(items)}


@router.get("/{artifact_id}", dependencies=[Depends(require_scope("read"))])
async def get_artifact(request: Request, artifact_id: str) -> dict[str, Any]:
    store = _get_store(request)
    try:
        fm, body = store.read(artifact_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Artifact not found: {artifact_id}") from None
    task_repo = request.app.state.task_repo
    active_task = await task_repo.get_active_task(artifact_id)
    return {"frontmatter": fm, "body": body, "active_task": active_task}


def _slugify(text: str, limit: int = 40) -> str:
    """A dated-artifact-id slug: lowercase, hyphen-separated, ASCII only."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:limit].strip("-") or "artifact"


def _fill_proposal_defaults(spec: dict[str, Any], token: dict[str, Any]) -> dict[str, Any]:
    """Supply the bookkeeping a HUMAN should not have to hand-craft (#445 A2).

    `id` must match a dated-slug pattern and `produced_by` needs a
    session/agent/user triple. An MCP client or the CLI has those to hand; a
    person filling in a form does not, and asking them to invent an id that
    matches a regex is how a create-artifact screen becomes unusable.

    Derived here rather than in the browser so every client - web, CLI, MCP -
    gets the same identity rules, and so `user` comes from the AUTHENTICATED
    token instead of whatever a caller claims. Anything the caller did supply is
    left exactly as it is: this fills gaps, it does not overwrite intent.
    """
    filled = dict(spec)
    if not filled.get("id"):
        stamp = datetime.now(UTC).strftime("%Y-%m-%d")
        suffix = secrets.token_hex(3)
        filled["id"] = f"{stamp}-{_slugify(str(filled.get('intent', '')))}-{suffix}"

    produced_by = dict(filled.get("produced_by") or {})
    produced_by.setdefault("agent", "web")
    produced_by.setdefault("session", f"web-{secrets.token_hex(4)}")
    # The authenticated identity wins over anything the client sent: the audit
    # trail's whole value is that it names who really did this.
    produced_by["user"] = str(token.get("display_name") or token.get("user_id") or "web")
    filled["produced_by"] = produced_by
    return filled


@router.post("", dependencies=[Depends(require_scope("write"))])
async def propose_artifact(request: Request, token: _TokenDep) -> dict[str, Any]:
    lifecycle = _get_lifecycle(request)
    spec = await request.json()
    if not isinstance(spec, dict):
        raise HTTPException(status_code=400, detail="Artifact spec must be an object")
    try:
        artifact_id = await lifecycle.propose(_fill_proposal_defaults(spec, token))
    except LifecycleError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"id": artifact_id}


async def _policies_for(request: Request, host: str) -> list[dict[str, Any]]:
    """KB entries an approver should read before letting a change run on `host`.

    Kept to `kind == "policy"`: the plan already says what will change, so what
    an operator needs beside it is the rules they wrote about this machine - not
    every note that mentions its name.
    """
    kb_service = getattr(request.app.state, "kb_service", None)
    if kb_service is None or not host:
        return []
    try:
        results = await kb_service.search(host, kind="policy", limit=5)
    except Exception:
        logger.warning("KB policy lookup failed for %s", host, exc_info=True)
        return []
    return [
        {
            "id": entry.get("id"),
            "title": entry.get("title", ""),
            "content": entry.get("content", ""),
            "target": entry.get("target"),
        }
        for entry in results
    ]


@router.post("/{artifact_id}/plan", dependencies=[Depends(require_scope("read"))])
async def plan_artifact(request: Request, artifact_id: str) -> dict[str, Any]:
    """What applying this artifact would actually change on the host (#445 A1).

    Approval was blind. `/preview` returns a git diff of the artifact FILE, which
    describes how the spec text changed - useful when reviewing an edit, useless
    for deciding whether to let it run. Meanwhile `host_provision.check_drift`
    was a working plan engine no UI could reach. This is that engine, answering
    the question an approver actually has: what happens to the machine.

    Read-only by construction: it calls the same probe drift does, which uses
    only `exec_readonly` and `read_file`. It mutates nothing, which is what makes
    it safe to run automatically when an approval screen opens - and #419 is the
    reminder of what a "preview" that quietly mutates costs.
    """
    store = _get_store(request)
    try:
        fm, body = store.read(artifact_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Artifact not found: {artifact_id}") from None

    kind = str(fm.get("kind", ""))
    if kind != "host-provision":
        # Said plainly rather than returning an empty plan: an empty plan reads
        # as "nothing will change", which for an unsupported kind is a lie.
        raise HTTPException(
            status_code=422,
            detail=(
                f"No plan engine for artifact kind {kind!r}. A real plan exists for "
                "host-provision; other kinds would need their own read-only probe."
            ),
        )

    from ..artifacts.models import parse_host_provision_spec
    from ..executor.host_provision import probe

    try:
        spec = parse_host_provision_spec(body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid host-provision spec: {exc}") from exc

    target = fm.get("target", {}) or {}
    host = str(target.get("host") or target.get("node") or "")
    if not host:
        raise HTTPException(
            status_code=400, detail="This artifact declares no target host to plan against"
        )

    agent = getattr(request.app.state, "artifact_executor", None)
    agent = getattr(agent, "agent", None) if agent is not None else None
    if agent is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "No agent transport is available, so the host cannot be inspected. "
                "A plan without a live probe would be a guess."
            ),
        )

    try:
        items = await probe(agent, host, spec)
    except Exception as exc:
        # A failed probe must not read as "nothing to do".
        raise HTTPException(status_code=502, detail=f"Could not inspect {host}: {exc}") from exc

    changes = [item for item in items if item["changes"]]
    return {
        "artifact_id": artifact_id,
        "host": host,
        "kind": kind,
        "items": items,
        "change_count": len(changes),
        "in_spec": not changes,
        # The policies that apply to this host, beside the plan (#429). Reviewing
        # is meant to be an informed decision, and the operator's own recorded
        # rules for a machine are the half the plan cannot supply. Best-effort:
        # a KB that is down must not block an approval screen from showing what
        # will change.
        "policies": await _policies_for(request, host),
        "summary": (
            f"{len(changes)} of {len(items)} item(s) would change on {host}"
            if changes
            else f"{host} already matches this spec; applying would change nothing"
        ),
    }


@router.post("/{artifact_id}/preview", dependencies=[Depends(require_scope("read"))])
async def preview_artifact(request: Request, artifact_id: str) -> dict[str, Any]:
    store = _get_store(request)
    try:
        fm, body = store.read(artifact_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Artifact not found: {artifact_id}") from None

    if "/" in artifact_id or "\\" in artifact_id or ".." in artifact_id:
        raise HTTPException(
            status_code=400, detail="Invalid artifact_id: path separators not allowed"
        )

    diff = ""
    try:
        path = store.resolve_path(artifact_id)
        resolved = path.resolve()
        if not str(resolved).startswith(str(store.root.resolve())):
            raise HTTPException(
                status_code=400,
                detail="Invalid artifact_id: path traversal detected",
            )
        rel = path.relative_to(store.root)
        result = subprocess.run(
            ["git", "diff", "HEAD~1", "--", str(rel)],
            cwd=store.root,
            capture_output=True,
            text=True,
            timeout=30,
        )
        diff = result.stdout
        if result.returncode != 0:
            logger.warning("git diff failed (rc=%d): %s", result.returncode, result.stderr.strip())
    except subprocess.SubprocessError:
        logger.warning("git diff timed out for artifact %s", artifact_id)
        diff = ""
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("git diff error for artifact %s: %s", artifact_id, exc)
        diff = ""

    return {
        "id": artifact_id,
        "status": fm.get("status", ""),
        "kind": fm.get("kind", ""),
        "intent": fm.get("intent", ""),
        "diff": diff,
        "body": body,
        "frontmatter": fm,
    }


@router.post("/{artifact_id}/approve", dependencies=[Depends(require_scope("write"))])
async def approve_artifact(request: Request, artifact_id: str) -> dict[str, Any]:
    lifecycle = _get_lifecycle(request)
    body = await request.json()
    user = body.get("user", "system")
    reason = body.get("reason")
    try:
        await lifecycle.approve(artifact_id, user, reason)
    except ConflictError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except LifecycleError as e:
        msg = str(e)
        if "Invalid transition" in msg:
            raise HTTPException(status_code=409, detail=msg) from e
        raise HTTPException(status_code=400, detail=msg) from e
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Artifact not found: {artifact_id}") from None
    return {"id": artifact_id, "status": "approved"}


@router.post("/{artifact_id}/reject", dependencies=[Depends(require_scope("write"))])
async def reject_artifact(request: Request, artifact_id: str) -> dict[str, Any]:
    lifecycle = _get_lifecycle(request)
    body = await request.json()
    user = body.get("user", "system")
    reason = body.get("reason")
    try:
        await lifecycle.reject(artifact_id, user, reason)
    except ConflictError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except LifecycleError as e:
        msg = str(e)
        if "Invalid transition" in msg:
            raise HTTPException(status_code=409, detail=msg) from e
        raise HTTPException(status_code=400, detail=msg) from e
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Artifact not found: {artifact_id}") from None
    return {"id": artifact_id, "status": "rejected"}


@router.post("/{artifact_id}/apply", dependencies=[Depends(require_scope("write"))])
async def apply_artifact(
    request: Request,
    artifact_id: str,
    sync: bool = Query(False),
) -> Any:
    from ..tasks.runner import TaskRunner

    task_runner: TaskRunner = request.app.state.task_runner
    body = await request.json()
    approved_by = body.get("approved_by", "system")

    try:
        task_info = await task_runner.start_apply(artifact_id, approved_by)
    except ConflictError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except LifecycleError as e:
        msg = str(e)
        if "Invalid transition" in msg:
            raise HTTPException(status_code=409, detail=msg) from e
        raise HTTPException(status_code=400, detail=msg) from e
    except ValueError as e:
        msg = str(e)
        if "not found" in msg.lower():
            raise HTTPException(status_code=404, detail=msg) from e
        raise HTTPException(status_code=400, detail=msg) from e

    if sync:
        try:
            task_result = await task_runner.await_task(task_info["task_id"])
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        if task_result["status"] == "succeeded":
            store = _get_store(request)
            fm, body_text = store.read(artifact_id)
            task_repo = request.app.state.task_repo
            active_task = await task_repo.get_active_task(artifact_id)
            return {"frontmatter": fm, "body": body_text, "active_task": active_task}
        return task_result

    return JSONResponse(status_code=202, content=task_info)


@router.post("/{artifact_id}/replay", dependencies=[Depends(require_scope("write"))])
async def replay_artifact(
    request: Request,
    artifact_id: str,
    sync: bool = Query(False),
) -> Any:
    """Re-apply an applied artifact through the one engine (#423).

    Replay existed only on the CLI, which ran a second, weaker apply path - so it
    bypassed the `replay_safe: false` and replay-only guards that
    `ArtifactExecutor.replay` enforces. Those guards are the reason this endpoint
    exists: with one way in, they cannot be walked around.
    """
    from ..tasks.runner import TaskRunner

    task_runner: TaskRunner = request.app.state.task_runner

    try:
        task_info = await task_runner.start_replay(artifact_id)
    except ConflictError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except LifecycleError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except ValueError as e:
        msg = str(e)
        if "apply_in_progress" in msg:
            active = await request.app.state.task_repo.get_active_task(artifact_id)
            raise HTTPException(
                status_code=409,
                detail={"reason": "apply_in_progress", "task_id": active["id"] if active else None},
            ) from e
        if "not found" in msg.lower():
            raise HTTPException(status_code=404, detail=msg) from e
        raise HTTPException(status_code=400, detail=msg) from e

    if sync:
        try:
            task_result = await task_runner.await_task(task_info["task_id"])
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        return task_result

    return JSONResponse(status_code=202, content=task_info)


@router.delete("/{artifact_id}", dependencies=[Depends(require_scope("write"))])
async def revoke_artifact(
    request: Request,
    artifact_id: str,
    sync: bool = Query(False),
) -> Any:
    from ..tasks.runner import TaskRunner

    task_runner: TaskRunner = request.app.state.task_runner
    raw = await request.body()
    body = json.loads(raw) if raw else {}
    user = body.get("user", "system")
    reason = body.get("reason")

    try:
        task_info = await task_runner.start_revoke(artifact_id, user, reason)
    except ConflictError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except LifecycleError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except ValueError as e:
        msg = str(e)
        if "apply_in_progress" in msg:
            active = await request.app.state.task_repo.get_active_task(artifact_id)
            raise HTTPException(
                status_code=409,
                detail={"reason": "apply_in_progress", "task_id": active["id"] if active else None},
            ) from e
        if "not found" in msg.lower():
            raise HTTPException(status_code=404, detail=msg) from e
        raise HTTPException(status_code=400, detail=msg) from e

    if sync:
        try:
            task_result = await task_runner.await_task(task_info["task_id"])
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        if task_result["status"] == "succeeded":
            store = _get_store(request)
            fm, body_text = store.read(artifact_id)
            return {"frontmatter": fm, "body": body_text}
        return task_result

    return JSONResponse(status_code=202, content=task_info)


_SSE_MAX_CONN_PER_IP = 3
_SSE_CONN_TIMEOUT_SEC = 3600
_sse_conn_count: dict[str, int] = {}
_sse_conn_lock = asyncio.Lock()


@router.get("/events/stream", dependencies=[Depends(require_scope("read"))])
async def sse_stream(request: Request) -> EventSourceResponse:
    client_ip = request.client.host if request.client else "unknown"
    async with _sse_conn_lock:
        if _sse_conn_count.get(client_ip, 0) >= _SSE_MAX_CONN_PER_IP:
            raise HTTPException(status_code=429, detail="SSE connection limit reached")

    q = sse_bus.subscribe()
    if q is None:
        raise HTTPException(status_code=503, detail="SSE subscriber limit reached")

    async with _sse_conn_lock:
        _sse_conn_count[client_ip] = _sse_conn_count.get(client_ip, 0) + 1

    async def event_generator() -> AsyncIterator[dict[str, str]]:
        start = time.time()
        try:
            while True:
                if await request.is_disconnected():
                    break
                if (time.time() - start) > _SSE_CONN_TIMEOUT_SEC:
                    yield {"event": "ping", "data": json.dumps({"reason": "timeout"})}
                    break
                try:
                    event = await asyncio.wait_for(q.get(), timeout=15)
                    yield event.to_sse()
                except TimeoutError:
                    yield {"event": "ping", "data": ""}
        finally:
            sse_bus.unsubscribe(q)
            async with _sse_conn_lock:
                _sse_conn_count[client_ip] = max(0, _sse_conn_count.get(client_ip, 1) - 1)
                if _sse_conn_count.get(client_ip, 0) <= 0:
                    _sse_conn_count.pop(client_ip, None)

    return EventSourceResponse(event_generator())
