from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sse_starlette.sse import EventSourceResponse

from ..auth.deps import require_scope, require_token
from ..auth.principal import audit_actor, principal_from_token
from ..client_ip import client_ip as _client_ip
from ..runtime.blocking import run_blocking
from ..sse import bus as sse_bus
from .lifecycle import ArtifactLifecycle, ConflictError, LifecycleError
from .models import ApprovalRefusedError, ArtifactBusyError
from .store import ArtifactStore, GitOperationError

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


# ── Request bodies (#703) ─────────────────────────────────────────────────────
# Declared, so a malformed body is FastAPI's 422 (value-stripped by the app's
# handler, #628) instead of a JSONDecodeError/AttributeError 500, and so the
# OpenAPI schema says what each route takes. Unknown fields are IGNORED, not
# refused: clients before #681 sent `user` / `approved_by`, which the server no
# longer reads (identity is the authenticated credential) - refusing them would
# break an older CLI or UI for no gain.

_REASON = Field(default=None, max_length=2000, description="Why - recorded on the artifact")


class ApproveBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    reason: str | None = _REASON
    approval_code: str | None = Field(
        default=None,
        max_length=64,
        description=(
            "The per-artifact code a human relayed. Required unless the caller is "
            "an operator (admin) credential (#681)."
        ),
    )


class ReasonBody(BaseModel):
    """Reject / revoke: the only thing a caller supplies is why."""

    model_config = ConfigDict(extra="ignore")

    reason: str | None = _REASON


class ApplyBody(BaseModel):
    """Apply takes nothing from the body; declared so a malformed one is a 422."""

    model_config = ConfigDict(extra="ignore")


class ArtifactActionError(Exception):
    """A domain error raised by the shared artifact callables (plan/preview).

    Carries the HTTP-ish ``status`` so one shared function serves both the HTTP
    route (which maps it to an HTTPException of the same status) and the MCP tool
    (which maps every one to a ValueError the client sees)."""

    def __init__(self, status: int, detail: str) -> None:
        self.status = status
        self.detail = detail
        super().__init__(detail)


@router.get("", dependencies=[Depends(require_scope("read"))])
async def list_artifacts(
    request: Request,
    status: str | None = Query(None),
    kind: str | None = Query(None),
    q: str | None = Query(None, description="Free text over id, intent, kind, target, tags"),
    limit: int = Query(100, ge=1, le=1000),
) -> dict[str, Any]:
    store = _get_store(request)
    items = await run_blocking("io", store.list, status=status, kind=kind, q=q)
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
async def get_artifact(request: Request, artifact_id: str, token: _TokenDep) -> dict[str, Any]:
    store = _get_store(request)
    try:
        fm, body = await run_blocking("io", store.read, artifact_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Artifact not found: {artifact_id}") from None
    task_repo = request.app.state.task_repo
    active_task = await task_repo.get_active_task(artifact_id)
    # Approval code, for the operator review surface only: returned to an
    # OPERATOR credential, never to an assistant's write/read token (#681) - the
    # same token that proposed could otherwise read it back and approve its own
    # change. Only while PROPOSED (the one state a coded approval applies to);
    # backfilled lazily for artifacts proposed before the feature shipped.
    # `locked` drives the reset control.
    approval_code: str | None = None
    approval_locked = False
    if fm.get("status") == "proposed":
        repo = getattr(request.app.state, "repo", None)
        if repo is not None:
            from .approval_code import ensure_approval_code, format_for_display

            code = await ensure_approval_code(repo, artifact_id)
            if principal_from_token(token).is_operator:
                approval_code = format_for_display(code)
            row = await repo.get_approval_code_row(artifact_id)
            approval_locked = bool(row and int(row["locked"]))
    from .summary import summarize

    return {
        "frontmatter": fm,
        "body": body,
        "active_task": active_task,
        "approval_code": approval_code,
        "approval_locked": approval_locked,
        # What it does, in words, above the raw spec (owner feedback 2026-11-08).
        "summary": summarize(fm, body),
        # How the last run ended - the outcome the Changes page states in plain
        # words, partial applies (#796) and PVE warnings included.
        "last_task": await last_task_outcome(task_repo, artifact_id),
    }


async def last_task_outcome(task_repo: Any, artifact_id: str) -> dict[str, Any] | None:
    """The artifact's most recent task with its result parsed - minus the raw
    execution log, which the artifact body already carries in full."""
    rows = await task_repo.list_tasks(artifact_id=artifact_id, limit=1)
    if not rows:
        return None
    row = dict(rows[0])
    result: dict[str, Any] = {}
    raw = row.pop("result_json", None)
    if raw:
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, dict):
            result = {k: v for k, v in parsed.items() if k != "execution_log"}
    row["result"] = result
    return row


@router.post(
    "/{artifact_id}/approval-code/reset",
    dependencies=[Depends(require_scope("admin"))],
)
async def reset_approval_code(request: Request, artifact_id: str) -> dict[str, Any]:
    """Operator reset of a brute-force approval lock (present-human action).

    After too many wrong codes, approval for the artifact locks. A human at the
    UI/CLI clears the lock here; the code itself is unchanged, so the operator
    can re-read and re-relay it. Admin scope = an operator credential (#681): at
    `write` the assistant that tripped the lock could clear it and keep guessing."""
    repo = getattr(request.app.state, "repo", None)
    if repo is None:
        raise HTTPException(status_code=503, detail="approval-code store unavailable")
    existed = await repo.reset_approval_lock(artifact_id)
    if not existed:
        raise HTTPException(
            status_code=404,
            detail=f"No active approval code for artifact: {artifact_id}",
        )
    return {"id": artifact_id, "locked": False}


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
    # trail's whole value is that it names who really did this. The CREDENTIAL,
    # not just the display name - every minted token shares the one user (#681).
    produced_by["user"] = audit_actor(token)
    filled["produced_by"] = produced_by
    return filled


@router.post("", dependencies=[Depends(require_scope("write"))])
async def propose_artifact(
    request: Request,
    token: _TokenDep,
    spec: Annotated[
        dict[str, Any],
        Body(description="The artifact spec (ARTIFACT_SPEC.md); id/produced_by are filled in"),
    ],
) -> dict[str, Any]:
    lifecycle = _get_lifecycle(request)
    try:
        artifact_id = await lifecycle.propose(_fill_proposal_defaults(spec, token))
    except LifecycleError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"id": artifact_id}


async def _policies_for(kb_service: Any, host: str) -> list[dict[str, Any]]:
    """The rules that BIND this host, for an approver to read beside the plan.

    Selected by what a policy is ABOUT, not by text similarity to the hostname.
    Free-text search got this wrong in both directions, reproduced on dev 3.6.17
    while reviewing a change to `hp-test-server`:

      * shown - a policy whose target was `dev-ct-db` and whose own text read
        *"This rule is about dev-ct-db only"*, because it happened to name
        `hp-test-server` in passing. The keyword pass was
        `content LIKE '%hp-test-server%'`.
      * not shown - *"No package installs on any managed machine without a
        snapshot taken first. Applies to every host in the fleet."*, a GLOBAL
        policy, against a plan that installs packages. It never named the host,
        so nothing matched it. Every one of the 19 policies `hp policy init`
        writes is global and none of them could ever have appeared here.

    With embeddings on it was worse: a k-nearest query always returns k rows, so
    the screen showed the five nearest policies for ANY host - verified, both
    policies in the KB returned for `mail-relay-99`, which is mentioned by
    neither, at the maximum-distance floor.

    A policy applies to a machine because of its target. So: policies targeting
    this host, plus policies targeting nothing (or `global`), which is what
    "applies to everything" is stored as. Each entry carries `applies_via` so
    the screen can say why it is there.
    """
    if kb_service is None or not host:
        return []
    try:
        rows: list[dict[str, Any]] = await kb_service.policies_for_target(host)
    except Exception:
        logger.warning("KB policy lookup failed for %s", host, exc_info=True)
        return []
    return rows


async def build_artifact_plan(
    artifact_id: str, *, store: ArtifactStore, agent: Any, kb_service: Any
) -> dict[str, Any]:
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

    Shared by the HTTP route and the MCP ``plan_artifact`` tool; raises
    ``ArtifactActionError`` with the status the caller should surface.
    """
    try:
        fm, body = await run_blocking("io", store.read, artifact_id)
    except FileNotFoundError:
        raise ArtifactActionError(404, f"Artifact not found: {artifact_id}") from None

    kind = str(fm.get("kind", ""))
    if kind != "host-provision":
        # Said plainly rather than returning an empty plan: an empty plan reads
        # as "nothing will change", which for an unsupported kind is a lie.
        raise ArtifactActionError(
            422,
            f"No plan engine for artifact kind {kind!r}. A real plan exists for "
            "host-provision; other kinds would need their own read-only probe.",
        )

    from ..artifacts.models import parse_host_provision_spec
    from ..executor.host_provision import probe

    try:
        spec = parse_host_provision_spec(body)
    except ValueError as exc:
        raise ArtifactActionError(400, f"Invalid host-provision spec: {exc}") from exc

    target = fm.get("target", {}) or {}
    host = str(target.get("host") or target.get("node") or "")
    if not host:
        raise ArtifactActionError(400, "This artifact declares no target host to plan against")

    if agent is None:
        raise ArtifactActionError(
            503,
            "No agent transport is available, so the host cannot be inspected. "
            "A plan without a live probe would be a guess.",
        )

    try:
        items = await probe(agent, host, spec)
    except Exception as exc:
        # A failed probe must not read as "nothing to do".
        raise ArtifactActionError(502, f"Could not inspect {host}: {exc}") from exc

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
        "policies": await _policies_for(kb_service, host),
        "summary": (
            f"{len(changes)} of {len(items)} item(s) would change on {host}"
            if changes
            else f"{host} already matches this spec; applying would change nothing"
        ),
    }


async def build_artifact_preview(artifact_id: str, *, store: ArtifactStore) -> dict[str, Any]:
    """A git diff of the artifact FILE against its previous committed version.

    Shared by the HTTP route and the MCP ``preview_artifact`` tool; raises
    ``ArtifactActionError`` with the status the caller should surface.
    """
    try:
        fm, body = await run_blocking("io", store.read, artifact_id)
    except FileNotFoundError:
        raise ArtifactActionError(404, f"Artifact not found: {artifact_id}") from None

    if "/" in artifact_id or "\\" in artifact_id or ".." in artifact_id:
        raise ArtifactActionError(400, "Invalid artifact_id: path separators not allowed")

    diff = ""
    try:
        path = store.resolve_path(artifact_id)
        resolved = path.resolve()
        if not str(resolved).startswith(str(store.root.resolve())):
            raise ArtifactActionError(400, "Invalid artifact_id: path traversal detected")
        rel = path.relative_to(store.root)
        # On the git pool: serialised with the writes, never on the loop.
        result = await run_blocking("git", store.diff_previous, str(rel))
        diff = result.stdout.decode("utf-8", errors="replace")
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace").strip()
            logger.warning("git diff failed (rc=%d): %s", result.returncode, stderr)
    except GitOperationError as exc:
        logger.warning("git diff failed for artifact %s: %s", artifact_id, exc)
        diff = ""
    except ArtifactActionError:
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


def _plan_agent(request: Request) -> Any:
    executor = getattr(request.app.state, "artifact_executor", None)
    return getattr(executor, "agent", None) if executor is not None else None


@router.post("/{artifact_id}/plan", dependencies=[Depends(require_scope("read"))])
async def plan_artifact(request: Request, artifact_id: str) -> dict[str, Any]:
    try:
        return await build_artifact_plan(
            artifact_id,
            store=_get_store(request),
            agent=_plan_agent(request),
            kb_service=getattr(request.app.state, "kb_service", None),
        )
    except ArtifactActionError as e:
        raise HTTPException(status_code=e.status, detail=e.detail) from e


@router.post("/{artifact_id}/preview", dependencies=[Depends(require_scope("read"))])
async def preview_artifact(request: Request, artifact_id: str) -> dict[str, Any]:
    try:
        return await build_artifact_preview(artifact_id, store=_get_store(request))
    except ArtifactActionError as e:
        raise HTTPException(status_code=e.status, detail=e.detail) from e


@router.post("/{artifact_id}/approve", dependencies=[Depends(require_scope("write"))])
async def approve_artifact(
    request: Request,
    artifact_id: str,
    token: _TokenDep,
    body: Annotated[ApproveBody | None, Body()] = None,
) -> dict[str, Any]:
    """Approve on the authenticated credential's authority (#681).

    An operator credential approves directly; any other write token must carry
    the human-relayed `approval_code`. The approver recorded is the credential,
    never a `user` named in the body. The rule itself lives in
    ArtifactLifecycle.approve so every transport meets the same one."""
    lifecycle = _get_lifecycle(request)
    body = body or ApproveBody()
    try:
        await lifecycle.approve(
            artifact_id,
            principal_from_token(token),
            body.reason,
            approval_code=body.approval_code,
        )
    except ApprovalRefusedError as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
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
async def reject_artifact(
    request: Request,
    artifact_id: str,
    token: _TokenDep,
    body: Annotated[ReasonBody | None, Body()] = None,
) -> dict[str, Any]:
    lifecycle = _get_lifecycle(request)
    # Who rejected is the authenticated credential, never the body (#681).
    user = audit_actor(token)
    reason = (body or ReasonBody()).reason
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
    token: _TokenDep,
    sync: bool = Query(False),
    _body: Annotated[ApplyBody | None, Body()] = None,
) -> Any:
    from ..tasks.runner import TaskRunner

    task_runner: TaskRunner = request.app.state.task_runner
    # Who ran it is the authenticated credential; a body `approved_by` is
    # ignored (#681). Apply never grants approval: the runner refuses anything
    # that is not already approved.
    approved_by = audit_actor(token)

    try:
        task_info = await task_runner.start_apply(artifact_id, approved_by)
    except ArtifactBusyError as e:
        raise HTTPException(status_code=409, detail=e.detail()) from e
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
            fm, body_text = await run_blocking("io", store.read, artifact_id)
            task_repo = request.app.state.task_repo
            active_task = await task_repo.get_active_task(artifact_id)
            return {"frontmatter": fm, "body": body_text, "active_task": active_task}
        return task_result

    return JSONResponse(status_code=202, content=task_info)


@router.post("/{artifact_id}/approve-and-apply", dependencies=[Depends(require_scope("admin"))])
async def approve_and_apply_artifact(
    request: Request,
    artifact_id: str,
    token: _TokenDep,
    sync: bool = Query(False),
    body: Annotated[ApproveBody | None, Body()] = None,
) -> Any:
    """Approve a proposed change and apply it at once - the owner's one tap.

    Every kind (owner decision 5, 2026-11-08; generalised from guest-power,
    #781). Two exceptions keep approve and apply as two explicit acts, as review
    06 3.2 says: a composite (it approves and runs several changes) and a
    ``replay_safe: false`` change (it cannot be run twice, so its one run is
    started deliberately). ``summary.two_step`` names the reason, and the
    Changes page offers "Approve" then "Apply" for those.

    Admin scope: approving AND running in one step is the operator's authority,
    never an assistant's (an assistant approves with a relayed code, then
    applies - two separate, separately recorded acts). The guards are exactly
    approve's (hash, composite cascade) and then apply's (task runner, one run
    slot): the run is a task with an id like any ``/apply``. If the approval
    lands and the run cannot start (another run holds the slot), the answer says
    so: the change stays approved and Ready to run, never lost.
    """
    from ..tasks.runner import TaskRunner
    from .summary import summarize

    store = _get_store(request)
    try:
        # Off the loop like every other artifact read (runtime/blocking.py).
        fm, artifact_body = await run_blocking("io", store.read, artifact_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Artifact not found: {artifact_id}") from None
    if fm.get("status") != "proposed":
        raise HTTPException(
            status_code=409,
            detail=f"{artifact_id} is {fm.get('status')}, not proposed: only a proposed "
            "change can be approved and applied in one step",
        )
    summary = summarize(fm, artifact_body)
    if summary["two_step"]:
        raise HTTPException(status_code=400, detail=summary["two_step_reason"])
    lifecycle = _get_lifecycle(request)
    body = body or ApproveBody()
    try:
        await lifecycle.approve(artifact_id, principal_from_token(token), body.reason)
    except ApprovalRefusedError as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    except (ConflictError, LifecycleError) as e:
        raise HTTPException(status_code=409, detail=str(e)) from e

    task_runner: TaskRunner = request.app.state.task_runner
    try:
        task_info = await task_runner.start_apply(artifact_id, audit_actor(token))
    except ArtifactBusyError as e:
        busy = e.detail()
        busy["message"] = f"Approved, but not started: {busy['message']} It is ready to run."
        raise HTTPException(status_code=409, detail=busy) from e
    except (ConflictError, LifecycleError, ValueError) as e:
        # A run the guard refused (#813) has sent the change back to review;
        # anything else leaves it approved and ready.
        after, _ = await run_blocking("io", store.read, artifact_id)
        where = " It is ready to run." if after.get("status") == "approved" else ""
        raise HTTPException(status_code=409, detail=f"Approved, but not started: {e}{where}") from e
    if sync:
        try:
            return await task_runner.await_task(task_info["task_id"])
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
    return JSONResponse(status_code=202, content=task_info)


@router.get("/{artifact_id}/target", dependencies=[Depends(require_scope("read"))])
async def get_artifact_target(request: Request, artifact_id: str) -> dict[str, Any]:
    """Who the change is for, who is there NOW, and how old its approval is (#813).

    What the Inbox shows on a Ready-to-run card and what the confirm dialog of a
    destroy or a forced stop asks against: the typed confirmation is the
    guest's name as read live here, never its VMID. A read, nothing is sent;
    the same check the executor refuses on (``target_identity.check``).
    """
    from homepilot.inventory.service import same_guest

    from . import target_identity
    from .summary import summarize

    store = _get_store(request)
    try:
        fm, body = await run_blocking("io", store.read, artifact_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Artifact not found: {artifact_id}") from None
    verdict = await target_identity.check(getattr(request.app.state, "proxmox", None), fm, body)
    limit = await target_identity.approval_limit_days()
    approved = fm.get("status") == "approved"
    age = target_identity.approval_age_days(fm) if approved else None
    stale = target_identity.stale_reason(fm, limit) if approved else None

    confirm: dict[str, Any] | None = None
    danger_vmid = summarize(fm, body).get("danger_vmid")
    guest = next((g for g in verdict.guests if g["vmid"] == danger_vmid), None)
    if guest is not None and guest.get("live") and not guest.get("problem"):
        live = dict(guest["live"])
        repo = getattr(request.app.state, "repo", None)
        if repo is not None:
            for row in await repo.list_hosts_by_proxmox_id(int(live["vmid"])):
                if same_guest(dict(row), live.get("name")):
                    live["owner"] = dict(row).get("owner")
                    break
        confirm = live
    return {
        "identity": verdict.as_dict(),
        "approval": {
            "approved_at": (fm.get("approved_by") or {}).get("at") if approved else None,
            "age_days": round(age, 2) if age is not None else None,
            "limit_days": limit,
            "stale": stale,
        },
        # The live guest whose name a destroy / forced stop is confirmed with;
        # None when there is no such step or the machine is not the recorded one.
        "confirm": confirm,
        "may_run": verdict.ok and not stale,
    }


@router.post("/{artifact_id}/recheck", dependencies=[Depends(require_scope("write"))])
async def recheck_artifact(request: Request, artifact_id: str, token: _TokenDep) -> dict[str, Any]:
    """Ask the run guard now, sending nothing (#813).

    An approved change the guard refuses - its approval too old, its machine
    changed or gone, nothing recorded for it - goes back to review with the
    reason, exactly as an apply would have sent it; one it would let run is
    left as it is. Write scope like withdraw: this can only stop a change. The
    one way back to review is the guard itself, so this can never move a
    change the guard would have run.
    """
    store = _get_store(request)
    try:
        fm, body = await run_blocking("io", store.read, artifact_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Artifact not found: {artifact_id}") from None
    if fm.get("status") != "approved":
        raise HTTPException(
            status_code=409, detail=f"{artifact_id} is {fm.get('status')}, not approved"
        )
    executor = getattr(request.app.state, "artifact_executor", None)
    if executor is None:
        raise HTTPException(status_code=503, detail="this instance has no executor")
    active = await request.app.state.task_repo.get_active_task(artifact_id)
    if active is not None:
        # A run in flight already passed the guard; it is not moved under it.
        raise HTTPException(
            status_code=409, detail=f"{artifact_id} is running ({active['action']}); wait for it"
        )
    refusal = await executor.check_run(fm, body, "apply")
    if refusal is None:
        return {"id": artifact_id, "may_run": True, "status": "approved"}
    refused = await executor.refuse_run(artifact_id, fm, "recheck", audit_actor(token), *refusal)
    after, _ = await run_blocking("io", store.read, artifact_id)
    return {
        "id": artifact_id,
        "may_run": False,
        "code": refusal[0],
        "reason": refused.failure_reason,
        "status": after.get("status"),
    }


@router.post("/{artifact_id}/withdraw", dependencies=[Depends(require_scope("write"))])
async def withdraw_artifact(
    request: Request,
    artifact_id: str,
    token: _TokenDep,
    body: Annotated[ReasonBody | None, Body()] = None,
) -> dict[str, Any]:
    """Take back an approved change that has not run (#797): approved -> withdrawn.

    Runs nothing. `reject` is for a proposal and refuses an approved change;
    `revoke` undoes an applied one. Write scope like reject: withdrawing can
    only stop a change, never make one. Who withdrew is the credential (#681).
    """
    from ..tasks.runner import TaskRunner

    task_runner: TaskRunner = request.app.state.task_runner
    reason = (body or ReasonBody()).reason
    try:
        return await task_runner.withdraw(artifact_id, audit_actor(token), reason)
    except ArtifactBusyError as e:
        raise HTTPException(status_code=409, detail=e.detail()) from e
    except (ConflictError, LifecycleError) as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


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
    except ArtifactBusyError as e:
        # Names the run holding the artifact's one slot (#710).
        raise HTTPException(status_code=409, detail=e.detail()) from e
    except ConflictError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except LifecycleError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
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
        return task_result

    return JSONResponse(status_code=202, content=task_info)


@router.delete("/{artifact_id}", dependencies=[Depends(require_scope("write"))])
async def revoke_artifact(
    request: Request,
    artifact_id: str,
    token: _TokenDep,
    sync: bool = Query(False),
    body: Annotated[ReasonBody | None, Body()] = None,
) -> Any:
    from ..tasks.runner import TaskRunner

    task_runner: TaskRunner = request.app.state.task_runner
    # Who revoked is the authenticated credential, never the body (#681).
    user = audit_actor(token)
    reason = (body or ReasonBody()).reason

    try:
        task_info = await task_runner.start_revoke(artifact_id, user, reason)
    except ArtifactBusyError as e:
        # Names the run holding the artifact's one slot (#710).
        raise HTTPException(status_code=409, detail=e.detail()) from e
    except ConflictError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except LifecycleError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
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
            fm, body_text = await run_blocking("io", store.read, artifact_id)
            return {"frontmatter": fm, "body": body_text}
        return task_result

    return JSONResponse(status_code=202, content=task_info)


_SSE_MAX_CONN_PER_IP = 3
_SSE_CONN_TIMEOUT_SEC = 3600
_sse_conn_count: dict[str, int] = {}
_sse_conn_lock = asyncio.Lock()


@router.get("/events/stream", dependencies=[Depends(require_scope("read"))])
async def sse_stream(request: Request) -> EventSourceResponse:
    # Keyed on the REAL client behind a trusted proxy (#705); on the raw peer,
    # every operator behind the proxy shared one bucket of three.
    client_ip = _client_ip(request)
    # Check and take the slot in ONE critical section, and hand it back if the
    # bus is full, so a burst of opens cannot all pass the check before any of
    # them counts.
    async with _sse_conn_lock:
        if _sse_conn_count.get(client_ip, 0) >= _SSE_MAX_CONN_PER_IP:
            raise HTTPException(status_code=429, detail="SSE connection limit reached")
        q = sse_bus.subscribe()
        if q is None:
            raise HTTPException(status_code=503, detail="SSE subscriber limit reached")
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
                if sse_bus.missed(q) and q.empty():
                    # Events for this client were dropped. Deliver what did fit
                    # (above), then end the stream: the client reconnects and
                    # re-reads, instead of trusting a view with a hole in it
                    # (#701).
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
