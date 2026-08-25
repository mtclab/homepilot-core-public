from __future__ import annotations

import contextlib
import ipaddress
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ..auth.deps import SCOPE_ENFORCER_ATTR, require_scope
from .audit import ActionType, set_audit_caller
from .enroll import AgentEnrollService, EnrollConflictError, EnrollPreconditionError
from .enrolment_window import (
    DEFAULT_WINDOW_MINUTES,
    MAX_WINDOW_MINUTES,
    close_window,
    open_window,
)
from .enrolment_window import (
    payload as window_payload,
)
from .registry import AgentRegistry

if TYPE_CHECKING:
    from ..adapters.agent import AgentAdapter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agents", tags=["agents"])

_admin_only = Depends(require_scope("admin"))
_admin_token = Depends(require_scope("admin"))
# Plain fleet READS an operator should see without admin (owner decision, wave 3):
# they expose no secret and relax nothing. The state-changing routes below
# (enrol window open/close, revoke, forget, migrate, exec/write) stay admin.
_read_only = Depends(require_scope("read"))


def _caller_label(token: dict[str, Any]) -> str:
    """A short, attributable label for the authenticated admin caller, used in
    the agent-hub audit trail (#381)."""
    name = token.get("display_name") or token.get("user_id") or "unknown"
    token_id = token.get("token_id")
    if token_id:
        return f"{name} (token:{str(token_id)[:8]})"
    return str(name)


async def _capture_audit_caller(token: dict[str, Any] = _admin_token) -> None:
    """Admin-auth dependency for command-dispatch endpoints that ALSO records the
    caller identity into the request-scoped contextvar the AuditLog reads. Runs in
    the same task/context as the endpoint, so the downstream audit.log() call sees
    the caller without threading it through send_command/adapter signatures."""
    set_audit_caller(_caller_label(token))


# Admin auth + caller capture, for the command-dispatch endpoints.
_admin_audit = Depends(_capture_audit_caller)

# A resolved hub host is rendered into a `curl … | bash` root one-liner shown in
# the UI, so an attacker-controllable value (the client Host header) is a command
# injection into the operator's shell. This placeholder is deliberately NOT a
# dialable host so the UI/agent can detect it and refuse to run the one-liner.
_INVALID_HUB_HOST_PLACEHOLDER = "unresolved-hub-host.invalid"

# RFC-952/1123 hostname: 1-253 chars, dot-separated labels of 1-63 chars, each
# alphanumeric-or-hyphen and not starting/ending with a hyphen.
_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)(?!-)[A-Za-z0-9-]{1,63}(?<!-)(?:\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))*$"
)


def _is_valid_hub_host(host: str) -> bool:
    """True only for a strict hostname or IPv4/IPv6 literal — never for a value
    carrying shell metacharacters, whitespace, CRLF, or a URL path."""
    candidate = (host or "").strip()
    if not candidate:
        return False
    inner = candidate[1:-1] if candidate.startswith("[") and candidate.endswith("]") else candidate
    try:
        ipaddress.ip_address(inner)
        return True
    except ValueError:
        pass
    return bool(_HOSTNAME_RE.match(candidate))


def _advertised_hub(request: Request, bind_host: str, bind_port: int) -> tuple[str, int]:
    """The host:port an agent should dial to reach the hub.

    Resolution order:
      1. HP_AGENT_HUB_ADVERTISE_HOST (operator-set; may be "host" or "host:port") —
         the only reliable source behind a reverse proxy.
      2. the configured bind host, if it is not a wildcard.
      3. the request hostname (the address the operator reached the API on) — but
         this comes from the untrusted client Host header, so it is validated
         against a strict hostname/IP pattern before use.

    This is the ONE place hub-address resolution lives; the generated
    certificate's SAN list (agent_hub/selfconfig.py) covers the same candidates
    rather than re-deriving the answer.
    """
    from homepilot.config import get_settings

    advertise = (get_settings().agent_hub_advertise_host or "").strip()
    if advertise:
        advertise = advertise.removeprefix("http://").removeprefix("https://")
        if ":" in advertise:
            host, _, port = advertise.rpartition(":")
            try:
                return host, int(port)
            except ValueError:
                return advertise, bind_port
        return advertise, bind_port

    # The "0.0.0.0" literal here is a comparison, not a socket bind.
    if bind_host and bind_host not in ("0.0.0.0", "::", ""):  # nosec B104
        return bind_host, bind_port

    # Bind host is a wildcard: the only remaining candidate is the request host,
    # which is attacker-controllable. Emit it only if it is a valid hostname/IP.
    request_host = request.url.hostname or ""
    if _is_valid_hub_host(request_host):
        return request_host, bind_port
    logger.warning(
        "Refusing to advertise unvalidated hub host from request (Host header); "
        "returning a non-executable placeholder"
    )
    return _INVALID_HUB_HOST_PLACEHOLDER, bind_port


def _hub_tls_fields(hub: Any) -> dict[str, Any]:
    """Transport facts an enrolling agent needs.

    ``hub_cert_sha256`` is the fingerprint of the certificate this hub serves.
    The hub's certificate is normally self-signed, so it chains to nothing an
    agent already trusts; handing the fingerprint out over the authenticated
    admin API is what lets the agent PIN this exact hub instead of accepting any
    certificate. Empty when TLS is off, in which case there is nothing to pin."""
    return {
        "hub_tls": bool(getattr(hub, "tls_enabled", False)),
        "hub_cert_sha256": getattr(hub, "cert_fingerprint", "") or "",
    }


def _get_registry() -> AgentRegistry:
    from homepilot.app_state import get_agent_registry

    registry = get_agent_registry()
    if registry is None:
        raise HTTPException(status_code=503, detail="Agent hub not enabled")
    return registry


def _get_agent_adapter() -> AgentAdapter:
    from homepilot.adapters.agent import AgentAdapter
    from homepilot.app_state import get_agent_registry

    registry = get_agent_registry()
    if registry is None:
        raise HTTPException(status_code=503, detail="Agent hub not enabled")
    hub = registry.hub_server
    if hub is None:
        raise HTTPException(status_code=503, detail="Agent hub server not available")

    from ..main import app

    pve_nodes: list[str] = []
    lifecycle = getattr(app.state, "artifact_lifecycle", None) if app else None
    if lifecycle and hasattr(lifecycle, "_pve_nodes_list"):
        pve_nodes = lifecycle._pve_nodes_list or []

    adapter = AgentAdapter(
        hub_server=hub,
        pve_nodes=pve_nodes,
    )
    return adapter


class HostExecRequest(BaseModel):
    host: str
    command: str
    timeout: int = 30


class HostReadFileRequest(BaseModel):
    host: str
    path: str


class HostWriteFileRequest(BaseModel):
    host: str
    path: str
    content: str


@router.post("/host/exec", dependencies=[_admin_audit])
async def exec_on_host(body: HostExecRequest) -> dict[str, Any]:
    adapter = _get_agent_adapter()
    try:
        exit_code, stdout, stderr = await adapter.exec(
            body.host,
            body.command,
            timeout=body.timeout,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None
    return {"exit_code": exit_code, "stdout": stdout, "stderr": stderr}


@router.post("/host/read-file", dependencies=[_admin_audit])
async def read_file_from_host(body: HostReadFileRequest) -> dict[str, Any]:
    adapter = _get_agent_adapter()
    try:
        content = await adapter.read_file(body.host, body.path)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None
    return {"content": content}


@router.post("/host/write-file", dependencies=[_admin_audit])
async def write_file_to_host(body: HostWriteFileRequest) -> dict[str, Any]:
    adapter = _get_agent_adapter()
    try:
        result = await adapter.write_file(body.host, body.path, body.content)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None
    return result


@router.get("/test/adapter", dependencies=[_admin_only])
async def test_adapter() -> dict[str, Any]:
    from homepilot.adapters.agent import AgentAdapterError

    registry = _get_registry()
    connected = registry.list_connected()
    agent_host = connected[0]["hostname"] if connected else None

    adapter = _get_agent_adapter()
    results: dict[str, Any] = {}

    if agent_host:
        try:
            exit_code, stdout, stderr = await adapter.exec(agent_host, "hostname", timeout=10)
            results["agent_connected"] = {
                "host": agent_host,
                "exit_code": exit_code,
                "stdout": stdout,
                "stderr": stderr,
                "status": "ok",
            }
        except AgentAdapterError as exc:
            results["agent_connected"] = {"host": agent_host, "status": "error", "error": str(exc)}
        except Exception as exc:
            results["agent_connected"] = {"host": agent_host, "status": "error", "error": str(exc)}
    else:
        results["agent_connected"] = {"status": "no_agents_connected"}

    try:
        exit_code, stdout, stderr = await adapter.exec(
            "nonexistent.test.local",
            "hostname",
            timeout=5,
        )
        results["fallback_no_agent"] = {
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
            "status": "ok",
        }
    except AgentAdapterError as exc:
        results["fallback_no_agent"] = {"status": "error", "error": str(exc)}
    except Exception as exc:
        results["fallback_no_agent"] = {"status": "error", "error": str(exc)}

    results["adapter_info"] = {
        "hub_available": adapter._hub is not None,
        "pve_nodes": adapter._pve_nodes,
        "connected_agents": len(connected),
    }

    return results


async def fleet_listing(registry: AgentRegistry, repo: Any) -> list[dict[str, Any]]:
    """Live connections overlaid on the persisted registry, so agents that are
    mid-reconnect after a backend restart show as known/disconnected rather than
    vanishing (and inventory coverage doesn't flap to 'uncovered').

    Shared by GET /agents/ and the `list_agents` MCP tool: one fleet, one answer.
    """
    # A live agent has no outstanding failure by definition - upsert_agent clears
    # the reason on a successful register - so the key is present and null rather
    # than absent, and the UI has one shape to render (#430).
    live = {
        a["agent_id"]: {**a, "connected": True, "last_error": None, "last_error_at": None}
        for a in registry.list_connected()
    }

    if repo is not None:
        for row in await repo.list_agents():
            if row["agent_id"] in live:
                continue  # live record is authoritative
            live[row["agent_id"]] = {
                "agent_id": row["agent_id"],
                "hostname": row["hostname"],
                "system_info": row.get("system_info") or {},
                "state": row.get("state") or {},
                "connected_at": row.get("connected_at"),
                "last_heartbeat": row.get("last_heartbeat"),
                "connected": False,
                "disconnected_at": row.get("disconnected_at"),
                # WHY it is not here, not just that it is not here (#430).
                "last_error": row.get("last_error"),
                "last_error_at": row.get("last_error_at"),
            }
    return list(live.values())


@router.get("/", dependencies=[_read_only])
async def list_agents(request: Request) -> list[dict[str, Any]]:
    return await fleet_listing(_get_registry(), getattr(request.app.state, "repo", None))


@router.get("/token", dependencies=[_admin_only])
async def get_hub_token(request: Request) -> dict[str, Any]:
    registry = _get_registry()
    hub = registry.hub_server
    if hub is None:
        raise HTTPException(status_code=503, detail="Agent hub server not available")
    token = hub.auth_token or ""
    host, port = _advertised_hub(request, hub.host, hub.port)
    return {
        "auth_token": token,
        "hub_host": host,
        "hub_port": port,
        **_hub_tls_fields(hub),
    }


class MigrateTLSRequest(BaseModel):
    # Naming the stranding explicitly, rather than letting a bare retry escalate
    # into one: force means "flip even though these agents will need re-enrolling
    # by hand", which is a sentence an operator should have to write down.
    force: bool = False


async def _enrolment_credential(request: Request) -> None:
    """Accept the hub's enrolment token, or any valid API token.

    The guest fetching this payload holds exactly the enrolment token and nothing
    else - that is the whole reason the control plane serves it (#464) - so that
    is the credential this route is built around. An API token is also accepted,
    for the operator running the one-liner by hand.

    Compared in constant time, and refused outright when the hub has no token
    configured, so a misconfiguration cannot quietly become an open download.
    """
    import secrets

    presented = (request.headers.get("x-hp-agent-token") or "").strip()
    if not presented:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            presented = auth[7:].strip()
    if not presented:
        raise HTTPException(status_code=401, detail="Missing enrolment credential")

    registry = getattr(request.app.state, "agent_registry", None)
    hub = getattr(registry, "hub_server", None) if registry is not None else None
    hub_token = str(getattr(hub, "auth_token", "") or "")
    if hub_token and secrets.compare_digest(presented, hub_token):
        return

    repo = getattr(request.app.state, "repo", None)
    if repo is not None:
        from ..auth.tokens import validate_token as _validate_api_token

        row = await repo.get_token_by_prefix(presented[:16])
        if row is not None and _validate_api_token(presented, row["hash"]):
            return
    raise HTTPException(status_code=401, detail="Invalid enrolment credential")


# Marked as a scope enforcer so the startup guard (#405) sees these routes as
# GUARDED rather than unscoped. They are not public - they demand the hub's
# enrolment token or an API token - they simply enforce a credential the scope
# system does not model, because the caller is a guest being enrolled and holds
# nothing else. Listing them as public instead would have been a lie the guard
# would then have stopped checking.
setattr(_enrolment_credential, SCOPE_ENFORCER_ATTR, True)
_enrolment_only = Depends(_enrolment_credential)


@router.get("/dist", dependencies=[_admin_only])
async def agent_dist_manifest() -> dict[str, Any]:
    """What agent payload this image can hand to a guest, with digests (#464)."""
    from .dist import manifest

    return {"artifacts": manifest()}


def _dist_response(path: Path, media_type: str) -> FileResponse:
    """Serve a payload file with its digest in a header.

    The digest travels WITH the bytes so the installer can verify what it just
    received without a second round trip - and so a proxy that rewrites one
    cannot leave the other looking right.
    """
    from .dist import sha256

    return FileResponse(
        path,
        media_type=media_type,
        filename=path.name,
        headers={"x-hp-sha256": sha256(path)},
    )


@router.get("/dist/install-agent.sh", dependencies=[_enrolment_only])
async def agent_installer() -> FileResponse:
    """The installer, served by the control plane doing the enrolling.

    Authenticated by the hub's own enrolment token rather than an operator
    session: the guest fetching this has exactly that token and nothing else,
    which is the whole point of serving it here (#464). An admin token works too,
    for the operator running the one-liner by hand.
    """
    from .dist import DistUnavailableError, installer

    try:
        return _dist_response(installer(), "text/x-shellscript")
    except DistUnavailableError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/dist/hp-agent-linux-{arch}", dependencies=[_enrolment_only])
async def agent_binary_download(arch: str) -> FileResponse:
    """The architecture-matched agent binary, from this image."""
    from .dist import DistUnavailableError, agent_binary

    try:
        return _dist_response(agent_binary(arch), "application/octet-stream")
    except DistUnavailableError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/migrate-tls", dependencies=[_admin_only])
async def preview_tls_migration(request: Request) -> dict[str, Any]:
    """Who would move onto TLS, and who would be stranded - changing nothing."""
    from .migrate_tls import plan_migration

    repo = getattr(request.app.state, "repo", None)
    if repo is None:
        raise HTTPException(status_code=503, detail="Repository not available")
    registry = _get_registry()
    plan = await plan_migration(repo, registry)
    hub = registry.hub_server
    plan["hub_tls_enabled"] = bool(hub is not None and hub.tls_enabled)
    return plan


@router.post("/migrate-tls", dependencies=[_admin_only])
async def migrate_fleet_tls(request: Request, body: MigrateTLSRequest) -> dict[str, Any]:
    """Push the hub's certificate to the fleet, then flip the transport (#468).

    The alternative this replaces is editing /etc/homepilot/agent.env on every
    managed host, which ADR-004 exists to abolish.
    """
    from .migrate_tls import MigrationRefusedError, migrate_fleet_to_tls

    repo = getattr(request.app.state, "repo", None)
    settings = getattr(request.app.state, "settings", None)
    if repo is None or settings is None:
        raise HTTPException(status_code=503, detail="Control plane not fully initialised")
    registry = _get_registry()
    try:
        return await migrate_fleet_to_tls(
            repo,
            registry,
            registry.hub_server,
            settings.data_dir,
            settings=settings,
            force=body.force,
        )
    except MigrationRefusedError as exc:
        # 409: the request is well formed, the fleet is simply not in a state
        # where flipping is safe. The message names every agent involved.
        raise HTTPException(status_code=409, detail=str(exc)) from exc


# POST, not GET: this route MINTS a credential. The CSRF gate in auth/deps.py
# deliberately skips safe methods, so a state-changing endpoint reachable by GET
# is a state change a cookie-authenticated browser can be tricked into making
# from any origin. Minting is a mutation; it gets a mutating method and the CSRF
# gate that comes with it.
@router.post("/bootstrap", dependencies=[_admin_only])
async def create_bootstrap_token(request: Request) -> dict[str, Any]:
    registry = _get_registry()
    hub = registry.hub_server
    if hub is None:
        raise HTTPException(status_code=503, detail="Agent hub server not available")
    token = await hub._token_store.create()
    host, port = _advertised_hub(request, hub.host, hub.port)
    return {
        "bootstrap_token": token,
        "hub_host": host,
        "hub_port": port,
        **_hub_tls_fields(hub),
    }


# ── Enrolment window (#537) ──────────────────────────────────────────────────
# Declared BEFORE the `/{agent_id}` routes at the bottom of this module: FastAPI
# matches in declaration order, so `/enrolment-window` registered after them
# would be swallowed as an agent id.


class EnrolmentWindowRequest(BaseModel):
    minutes: int = Field(
        default=DEFAULT_WINDOW_MINUTES,
        ge=1,
        le=MAX_WINDOW_MINUTES,
        description="How long the window stays open, from now. Capped at 24h.",
    )


def _window_repo(request: Request) -> Any:
    repo = getattr(request.app.state, "repo", None)
    if repo is None:
        raise HTTPException(status_code=503, detail="Enrolment window store not available")
    return repo


@router.get("/enrolment-window", dependencies=[_read_only])
async def get_enrolment_window(request: Request) -> dict[str, Any]:
    """Whether the shared fleet token can currently enrol a NEW host."""
    return await window_payload(_window_repo(request))


@router.post("/enrolment-window", dependencies=[_admin_only])
async def open_enrolment_window(
    request: Request,
    body: EnrolmentWindowRequest | None = None,
    token: dict[str, Any] = _admin_token,
) -> dict[str, Any]:
    """Open (or extend) the enrolment window.

    While it is open the shared fleet token enrols hostnames this install has
    never seen. That is the whole exposure a leaked shared token used to carry
    permanently, so opening it is audited with the operator who did it.
    """
    repo = _window_repo(request)
    minutes = (body or EnrolmentWindowRequest()).minutes
    result = await open_window(repo, minutes)
    _audit_window("enrolment_window_opened", token, f"minutes={result['minutes']}")
    return await window_payload(repo)


@router.delete("/enrolment-window", dependencies=[_admin_only])
async def close_enrolment_window(
    request: Request, token: dict[str, Any] = _admin_token
) -> dict[str, Any]:
    """Close the enrolment window now. Idempotent."""
    repo = _window_repo(request)
    await close_window(repo)
    _audit_window("enrolment_window_closed", token, "")
    return await window_payload(repo)


def _audit_window(action: ActionType, token: dict[str, Any], detail: str) -> None:
    """Record who widened (or narrowed) who may join the fleet.

    Best-effort like every other audit write: a hub that is not running must not
    stop an operator from closing the window."""
    from homepilot.app_state import get_agent_registry

    audit_log = getattr(get_agent_registry(), "audit_log", None)
    if audit_log is None:
        return
    audit_log.log(
        agent_id="",
        action=action,
        command_or_path=detail,
        result="success",
        caller=_caller_label(token),
    )


class InstallAgentRequest(BaseModel):
    host_id: str


def _get_enroll_service(request: Request) -> AgentEnrollService:
    service = getattr(request.app.state, "agent_enroll_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="Agent install not available")
    enroll_service: AgentEnrollService = service
    return enroll_service


async def _load_host(request: Request, host_id: str) -> dict[str, Any]:
    repo = getattr(request.app.state, "repo", None)
    host = await repo.get_host(host_id) if repo is not None else None
    if host is None:
        raise HTTPException(status_code=404, detail=f"Host not found: {host_id}")
    return dict(host)


# Declared BEFORE the `/{agent_id}` routes at the bottom of this module: FastAPI
# matches in declaration order, so a literal path registered after them would be
# swallowed as an agent id.
@router.get("/install/{host_id}", dependencies=[_admin_only])
async def agent_install_eligibility(request: Request, host_id: str) -> dict[str, Any]:
    """Whether this host can be enrolled over qemu-guest-agent, and if not, why.

    The reason is the product here: a host that cannot be enrolled this way must
    say what to do about it instead of leaving the operator to click and find out.
    """
    host = await _load_host(request, host_id)
    service = _get_enroll_service(request)
    registry = getattr(request.app.state, "agent_registry", None)
    hub = getattr(registry, "hub_server", None) if registry is not None else None
    hub_host, hub_port = _advertised_hub(
        request,
        getattr(hub, "host", "") or "",
        int(getattr(hub, "port", 0) or 0),
    )
    payload: dict[str, Any] = {
        "host_id": host_id,
        "hostname": host.get("hostname"),
        "in_flight": service.is_inflight(host_id),
    }
    try:
        await service.check(host, hub_host)
    except EnrollPreconditionError as exc:
        return {**payload, "eligible": False, "reason": exc.code, "message": exc.message}
    return {
        **payload,
        "eligible": True,
        "reason": None,
        "message": (
            f"HomePilot can install and enrol the agent on {host.get('hostname')} through "
            f"qemu-guest-agent, pointing it at {hub_host}:{hub_port}."
        ),
    }


@router.post("/install", status_code=202)
async def install_agent_on_host(
    request: Request,
    body: InstallAgentRequest,
    token: dict[str, Any] = _admin_token,
) -> dict[str, Any]:
    """Install and enrol hp-agent inside a managed guest. Progress: /tasks/{id}."""
    host = await _load_host(request, body.host_id)
    service = _get_enroll_service(request)
    registry = getattr(request.app.state, "agent_registry", None)
    hub = getattr(registry, "hub_server", None) if registry is not None else None
    hub_host, hub_port = _advertised_hub(
        request,
        getattr(hub, "host", "") or "",
        int(getattr(hub, "port", 0) or 0),
    )
    try:
        task_id = await service.start(
            host, hub_host, hub_port, actor=token.get("user_id") or "system"
        )
    except EnrollPreconditionError as exc:
        # 409, not 400: the request is well-formed, the host is not in a state
        # that admits it. The code lets the UI keep its own wording; the message
        # is what a curl caller sees.
        raise HTTPException(
            status_code=409, detail={"reason": exc.code, "message": exc.message}
        ) from None
    except EnrollConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"task_id": task_id, "status": "pending", "host_id": body.host_id}


@router.get("/audit", dependencies=[_read_only])
async def get_audit_log(
    limit: int = Query(default=100, ge=1, le=1000),
    agent_id: str | None = Query(default=None, description="Only this agent's trail"),
    action: str | None = Query(
        default=None, description="Only this action, e.g. register_rejected"
    ),
) -> list[dict[str, Any]]:
    registry = _get_registry()
    # Prefer the durable DB trail (survives restart, carries caller attribution);
    # falls back to the in-memory deque when no repo is wired.
    return await registry.audit_log.query_persisted(limit=limit, agent_id=agent_id, action=action)


def agent_detail(agent: Any) -> dict[str, Any]:
    """One connected agent, as GET /agents/{agent_id} and /agents/hostname/{name}
    return it - and as the `get_agent` MCP tool returns it."""
    return {
        "agent_id": agent.agent_id,
        "hostname": agent.hostname,
        "system_info": agent.system_info,
        "state": agent.state,
        "connected_at": agent.connected_at.isoformat(),
        "last_heartbeat": agent.last_heartbeat.isoformat(),
    }


@router.get("/hostname/{hostname}", dependencies=[_read_only])
async def get_agent_by_hostname(hostname: str) -> dict[str, Any]:
    agent = _get_registry().get_by_hostname(hostname)
    if not agent:
        raise HTTPException(status_code=404, detail=f"Agent for hostname {hostname} not connected")
    return agent_detail(agent)


@router.get("/hostname/{hostname}/connected", dependencies=[_read_only])
async def is_agent_connected(hostname: str) -> dict[str, Any]:
    registry = _get_registry()
    connected = registry.is_connected(hostname)
    return {"hostname": hostname, "connected": connected}


class ExecRequest(BaseModel):
    command: str
    timeout: int = 30


class WriteFileRequest(BaseModel):
    path: str
    content: str


@router.post("/{agent_id}/exec", dependencies=[_admin_audit])
async def exec_on_agent(agent_id: str, body: ExecRequest) -> dict[str, Any]:
    registry = _get_registry()
    hub = registry.hub_server
    if hub is None:
        raise HTTPException(status_code=503, detail="Agent hub server not available")
    agent = registry.get(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")
    try:
        result = await hub.send_command(agent_id, body.command, timeout=body.timeout)
    except ConnectionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None
    except TimeoutError:
        raise HTTPException(status_code=504, detail="Command timed out") from None
    return result


@router.post("/{agent_id}/read-file", dependencies=[_admin_audit])
async def read_file_from_agent(agent_id: str, path: str = Query(...)) -> dict[str, Any]:
    registry = _get_registry()
    hub = registry.hub_server
    if hub is None:
        raise HTTPException(status_code=503, detail="Agent hub server not available")
    agent = registry.get(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")
    try:
        result = await hub.send_read_file(agent_id, path)
    except ConnectionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None
    except TimeoutError:
        raise HTTPException(status_code=504, detail="Read timed out") from None
    return result


@router.post("/{agent_id}/write-file", dependencies=[_admin_audit])
async def write_file_to_agent(agent_id: str, body: WriteFileRequest) -> dict[str, Any]:
    registry = _get_registry()
    hub = registry.hub_server
    if hub is None:
        raise HTTPException(status_code=503, detail="Agent hub server not available")
    agent = registry.get(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")
    try:
        result = await hub.send_write_file(agent_id, body.path, body.content)
    except ConnectionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None
    except TimeoutError:
        raise HTTPException(status_code=504, detail="Write timed out") from None
    return result


@router.post("/{agent_id}/revoke", dependencies=[_admin_only])
async def revoke_agent(agent_id: str, request: Request) -> dict[str, Any]:
    """Revoke an agent's per-agent credential (admin only).

    A revoked credential fails hub authentication, so the agent cannot reconnect
    until it is re-enrolled with a fresh bootstrap/shared token.

    The live channel is closed too (#430). The hub connection is long-lived by
    design, so leaving it open meant a revoked - possibly compromised - agent
    kept a fleet-root exec/write channel until it happened to reconnect, which
    may be never. Revoke now means revoked, not "revoked from the next connect".
    """
    repo = getattr(request.app.state, "repo", None)
    if repo is None:
        raise HTTPException(status_code=503, detail="Agent credential store not available")
    revoked = await repo.revoke_agent_credential(agent_id)
    if not revoked:
        raise HTTPException(
            status_code=404,
            detail=f"No active credential to revoke for agent {agent_id}",
        )
    # Evict AFTER the credential is dead, so the agent cannot win a reconnect
    # race against its own revocation.
    registry = getattr(request.app.state, "agent_registry", None)
    evicted = False
    if registry is not None:
        evicted = registry.disconnect(
            agent_id, "credential revoked by an operator; the live channel was closed"
        )
    return {"agent_id": agent_id, "revoked": True, "channel_closed": evicted}


@router.delete("/{agent_id}", dependencies=[_admin_only])
async def forget_agent(agent_id: str, request: Request) -> dict[str, Any]:
    """Forget a decommissioned agent entirely (#415).

    There was no way to remove one. `unregister()` only drops an in-memory entry
    for a LIVE connection, and since #343 agents are persisted and listed
    overlaid, so a scrapped host stayed in the list forever and kept being
    counted as known.

    Worse than untidy: the `agents` table doubles as the per-agent credential
    store, so that row is a credential a decommissioned box can still
    authenticate with. Removal therefore REVOKES first and then deletes - if the
    delete were to fail, the credential is already dead rather than the reverse.

    Refuses while the agent is connected. Removing a live agent would delete the
    credential out from under an open channel and leave it reconnecting into a
    hub that no longer knows it; stop or revoke it first, deliberately.
    """
    repo = getattr(request.app.state, "repo", None)
    if repo is None:
        raise HTTPException(status_code=503, detail="Agent store not available")

    registry = getattr(request.app.state, "agent_registry", None)
    live = {a["agent_id"] for a in registry.list_connected()} if registry is not None else set()
    if agent_id in live:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Agent {agent_id} is connected right now. Revoke it or stop the agent "
                "first - removing a live agent would pull its credential out from under "
                "an open connection."
            ),
        )

    await repo.revoke_agent_credential(agent_id)
    deleted = await repo.delete_agent(agent_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")
    if registry is not None:
        # Drop any stale in-memory record too, so the list does not resurrect it
        # from the live overlay until the next restart.
        with contextlib.suppress(Exception):
            registry.unregister(agent_id)
    logger.warning("Agent %s forgotten by an operator; its credential is revoked", agent_id)
    return {"agent_id": agent_id, "forgotten": True}


@router.get("/{agent_id}", dependencies=[_read_only])
async def get_agent(agent_id: str) -> dict[str, Any]:
    agent = _get_registry().get(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")
    return agent_detail(agent)
