from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import signal
import time
from collections import defaultdict
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

import httpx
from fastapi import Depends, FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from prometheus_client import Counter, Histogram, generate_latest

from . import __version__
from .app_state import create_app_state
from .auth.deps import require_token
from .auth.tokens import validate_token as _validate_token
from .common import APIError
from .config import get_settings
from .db.repository import Repository

logger = logging.getLogger(__name__)

_REQUEST_COUNT = Counter(
    "hp_http_requests_total", "Total HTTP requests", ["method", "path", "status"]
)
_REQUEST_DURATION = Histogram(
    "hp_http_request_duration_seconds", "HTTP request duration", ["method", "path"]
)

_RATE_LIMIT = int(os.environ.get("HP_RATE_LIMIT", "60"))
_AUTH_RATE_LIMIT = int(os.environ.get("HP_AUTH_RATE_LIMIT", "120"))
_RATE_WINDOW_SEC = 60
_MAX_TRACKED_IPS = 10000

_RATE_LIMIT_BACKEND = os.environ.get("HP_RATE_LIMIT_BACKEND", "memory")
_RATE_WINDOW: dict[str, list[float]] = defaultdict(list)
_RATE_LOCK = asyncio.Lock()

# NOTE: In-memory rate limiting stores request timestamps per IP in a process-local
# dict.  When running behind multiple uvicorn workers (or in a replicated setup),
# each worker maintains its own independent counter, so a client can exceed the
# configured limit by sending requests to different workers.
# TODO: For multi-worker production deployments, externalize rate-limit state to a
# shared store (e.g., Redis or memcached).  Set HP_RATE_LIMIT_BACKEND=redis (future)
# to use distributed rate limiting instead of this per-process dict.
if _RATE_LIMIT_BACKEND not in ("memory",):
    logger.warning(
        "HP_RATE_LIMIT_BACKEND=%s is not yet supported; falling back to memory",
        _RATE_LIMIT_BACKEND,
    )

# Security: only trust X-Forwarded-For when the peer IP matches a CIDR listed
# in HP_TRUSTED_PROXIES (comma-separated).  Default is empty — never trust
# the header — so a same-network attacker cannot spoof client IPs.  The old
# _TRUSTED_PROXY_NETWORKS that defaulted to private ranges has been removed.
_HP_TRUSTED_PROXIES_ENV = os.environ.get("HP_TRUSTED_PROXIES", "")
_trusted_proxy_networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []

_ProxyNetworks = list[ipaddress.IPv4Network | ipaddress.IPv6Network]


def _get_client_ip(request: Request, trusted_proxies: _ProxyNetworks) -> str:
    peer = request.client.host if request.client else "unknown"
    if not trusted_proxies:
        return peer
    try:
        peer_addr = ipaddress.ip_address(peer)
    except ValueError:
        return peer
    for network in trusted_proxies:
        if peer_addr in network:
            xff = request.headers.get("x-forwarded-for", "")
            if xff:
                first_client = xff.split(",")[0].strip()
                if first_client:
                    try:
                        ipaddress.ip_address(first_client)
                    except ValueError:
                        return peer
                    return first_client
            break
    return peer


def _cleanup_rate_window() -> None:
    now = time.time()
    expired = []
    for ip, timestamps in _RATE_WINDOW.items():
        _RATE_WINDOW[ip] = [ts for ts in timestamps if now - ts < _RATE_WINDOW_SEC]
        if not _RATE_WINDOW[ip]:
            expired.append(ip)
    for ip in expired:
        del _RATE_WINDOW[ip]
    if len(_RATE_WINDOW) > _MAX_TRACKED_IPS:
        sorted_ips = sorted(
            _RATE_WINDOW,
            key=lambda k: _RATE_WINDOW[k][-1] if _RATE_WINDOW[k] else 0,
        )
        for ip in sorted_ips[: len(_RATE_WINDOW) - _MAX_TRACKED_IPS]:
            del _RATE_WINDOW[ip]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    settings = get_settings()
    state = await create_app_state(settings)

    for cidr in (settings.trusted_proxies or _HP_TRUSTED_PROXIES_ENV).split(","):
        cidr = cidr.strip()
        if cidr:
            _trusted_proxy_networks.append(ipaddress.ip_network(cidr, strict=False))

    app.state.db = state.database
    app.state.repo = state.repo

    from .tasks.repository import TaskRepository

    task_repo = TaskRepository(state.database)
    await task_repo.fail_orphaned_tasks()
    app.state.task_repo = task_repo

    app.state.vault = state.vault
    app.state.settings = settings
    app.state.pve_token_source = state.pve_token_source

    if state.proxmox is not None:
        app.state.proxmox = state.proxmox

    app.state.artifact_store = state.artifact_store
    app.state.artifact_lifecycle = state.artifact_lifecycle
    app.state.artifact_executor = None

    from .inventory.service import InventoryService

    inventory_service = InventoryService(
        state.repo,
        proxmox=state.proxmox,
        ssh=state.ssh,
        kb_service=None,
        proxmox_host=settings.proxmox_host,
    )
    inventory_service.kb_service = state.kb_service
    app.state.inventory_service = inventory_service
    app.state.kb_service = state.kb_service
    app.state.sse_bus = state.sse_bus

    if state.agent_hub is not None:
        app.state.agent_hub = state.agent_hub
        app.state.agent_registry = state.agent_registry
        hub_task = asyncio.create_task(state.agent_hub.start())
        app.state.agent_hub_task = hub_task
        logger.info(
            "Agent hub task started on %s:%s",
            settings.agent_hub_host,
            settings.agent_hub_port,
        )
    else:
        app.state.agent_hub = None
        app.state.agent_registry = None
        app.state.agent_hub_task = None

    try:
        from .adapters.agent import AgentAdapter
        from .executor import ArtifactExecutor

        agent_adapter = None
        if state.agent_hub is not None and state.ssh is not None:
            agent_adapter = AgentAdapter(
                hub_server=state.agent_hub,
                jump_client=state.jump_client,
                pve_nodes=state.artifact_lifecycle._pve_nodes_list
                if hasattr(state.artifact_lifecycle, "_pve_nodes_list")
                else [],
            )

        if state.proxmox and state.ssh and state.vault:
            app.state.artifact_executor = ArtifactExecutor(
                store=state.artifact_store,
                lifecycle=state.artifact_lifecycle,
                repo=state.repo,
                proxmox=state.proxmox,
                ssh=state.ssh,
                vault=state.vault,
                agent=agent_adapter,
            )
    except (ImportError, OSError, ConnectionError):
        logger.warning("Could not initialize artifact executor", exc_info=True)

    from .reconciler import (
        ApplyReconciler,
        DriftReconciler,
        InventoryReconciler,
        ReconcilerScheduler,
    )

    reconciler_scheduler = ReconcilerScheduler()
    inventory_reconciler = InventoryReconciler(
        inventory_service=inventory_service,
        repo=state.repo,
    )
    reconciler_scheduler.register(
        inventory_reconciler,
        interval=float(settings.inventory_interval_seconds),
        startup_delay=0.0,
    )
    drift_reconciler = DriftReconciler(
        store=state.artifact_store,
        repo=state.repo,
        executor=app.state.artifact_executor,
    )
    reconciler_scheduler.register(
        drift_reconciler,
        interval=float(settings.drift_interval_seconds),
        startup_delay=30.0,
    )
    apply_reconciler = None
    if app.state.artifact_executor is not None:
        apply_reconciler = ApplyReconciler(
            store=state.artifact_store,
            repo=state.repo,
            executor=app.state.artifact_executor,
        )
    app.state.apply_reconciler = apply_reconciler

    if apply_reconciler is not None and settings.auto_apply_enabled:
        reconciler_scheduler.register(
            apply_reconciler,
            interval=float(settings.auto_apply_interval_seconds),
            startup_delay=15.0,
        )
        logger.info(
            "Auto-apply reconciler registered (interval=%ds)", settings.auto_apply_interval_seconds
        )

    from .tasks.runner import TaskRunner

    task_runner = TaskRunner(
        repo=task_repo,
        lifecycle=state.artifact_lifecycle,
        executor=app.state.artifact_executor,
        apply_reconciler=apply_reconciler,
        store=state.artifact_store,
    )
    app.state.task_runner = task_runner

    await reconciler_scheduler.start()
    app.state.reconciler_scheduler = reconciler_scheduler
    app.state.drift_reconciler = drift_reconciler

    try:
        await state.kb_service.reindex_if_needed(reason="startup")
    except (ImportError, OSError, ConnectionError):
        logger.warning("KB startup reindex check failed", exc_info=True)

    logger.info("HomePilot v2 started — data_dir=%s", settings.data_dir)

    mcp_token = os.environ.get("HP_MCP_TOKEN", "").strip()
    if mcp_token:
        from .mcp.server import _server_context, create_http_app, create_server

        srv = create_server()
        await _server_context.async_update(
            {
                "store": state.artifact_store,
                "lifecycle": state.artifact_lifecycle,
                "repo": state.repo,
                "proxmox": state.proxmox,
                "ssh_adapter": state.ssh,
                "vault": state.vault,
                "database": state.database,
                "kb_service": state.kb_service,
                "inventory_service": inventory_service,
                "task_repo": task_repo,
            }
        )
        if state.proxmox and state.ssh and state.vault:
            try:
                pve_data = await state.proxmox.read("/nodes")
                pve_nodes = [
                    n.get("node") or n.get("name", "")
                    for n in (
                        pve_data.get("data", pve_data) if isinstance(pve_data, dict) else pve_data
                    )
                ]
                state.artifact_lifecycle._pve_nodes_list = pve_nodes
            except (httpx.HTTPError, OSError, ConnectionError):
                state.artifact_lifecycle._pve_nodes_list = []
            from .adapters.agent import AgentAdapter
            from .executor import ArtifactExecutor

            mcp_agent = None
            if state.agent_hub is not None and state.jump_client is not None:
                mcp_agent = AgentAdapter(
                    hub_server=state.agent_hub,
                    jump_client=state.jump_client,
                    pve_nodes=state.artifact_lifecycle._pve_nodes_list or [],
                )

            executor = ArtifactExecutor(
                store=state.artifact_store,
                lifecycle=state.artifact_lifecycle,
                repo=state.repo,
                proxmox=state.proxmox,
                ssh=state.ssh,
                vault=state.vault,
                pve_nodes=state.artifact_lifecycle._pve_nodes_list or [],
                agent=mcp_agent,
            )
            state.artifact_lifecycle._executor_ref = executor
        mcp_app = create_http_app(srv)
        app.mount("/mcp", mcp_app)
        logger.info("MCP server mounted at /mcp")
    else:
        logger.info("MCP server not mounted — HP_MCP_TOKEN not set")

    # Register SIGTERM handler for graceful shutdown in Docker
    _shutdown_event = asyncio.Event()

    def _sigterm_handler() -> None:
        logger.info("Received SIGTERM — initiating graceful shutdown")
        _shutdown_event.set()

    loop = asyncio.get_running_loop()
    try:
        loop.add_signal_handler(signal.SIGTERM, _sigterm_handler)
    except (OSError, ValueError):
        logger.debug("Could not register SIGTERM handler (ok in non-Docker env)")

    yield

    logger.info("HomePilot v2 shutting down — stopping reconciler, closing connections")

    agent_hub = getattr(app.state, "agent_hub", None)
    if agent_hub is not None:
        try:
            await asyncio.wait_for(agent_hub.stop(), timeout=10.0)
            logger.info("Agent hub stopped")
        except TimeoutError:
            logger.warning("Agent hub stop timed out after 10s — proceeding")

    try:
        await asyncio.wait_for(reconciler_scheduler.stop(), timeout=10.0)
    except TimeoutError:
        logger.warning("Reconciler scheduler stop timed out after 10s — proceeding")
    try:
        await asyncio.wait_for(state.database.close(), timeout=10.0)
    except TimeoutError:
        logger.warning("Database close timed out after 10s — proceeding")
    except Exception as exc:
        logger.warning("Database close error (ok on restart): %s", exc)
    from .mcp.server import _server_context

    _server_context.clear()
    logger.info("HomePilot v2 shut down")


def validate_cors_config(settings: Any) -> dict[str, Any]:
    """Validate CORS configuration and return a status dict.

    If ``allow_credentials=True`` is combined with wildcard origins the browser
    spec forbids this; we force credentials off and log a CRITICAL warning.
    """
    origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
    has_wildcard = "*" in origins
    allow_credentials = True

    if has_wildcard and allow_credentials:
        logger.critical(
            "SECURITY: CORS allow_credentials=True with wildcard origin (*) is forbidden by spec. "
            "Disabling credentials. Set HP_CORS_ORIGINS to explicit domains to enable credentials."
        )
        allow_credentials = False

    return {
        "origins": origins,
        "has_wildcard": has_wildcard,
        "allow_credentials": allow_credentials,
        "misconfigured": has_wildcard and not allow_credentials,
    }


_cors_validation: dict[str, Any] = {}
_settings = get_settings()

app = FastAPI(title="HomePilot", version=__version__, lifespan=lifespan)

_cors_validation = validate_cors_config(_settings)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_validation["origins"],
    allow_credentials=_cors_validation["allow_credentials"],
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    max_age=600,
    allow_headers=["content-type", "authorization", "x-requested-with", "x-csrf-token"],
)


@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next: Any) -> Response:
    if request.url.path.startswith("/ui/") or request.url.path in ("/health", "/metrics"):
        return cast(Response, await call_next(request))

    start = time.time()
    authenticated = False
    if request.url.path != "/auth/login":
        authenticated = await _is_authenticated(request)

    client_ip = _get_client_ip(request, _trusted_proxy_networks)
    limit = _AUTH_RATE_LIMIT if authenticated else _RATE_LIMIT
    now = time.time()
    async with _RATE_LOCK:
        window = _RATE_WINDOW[client_ip]
        _RATE_WINDOW[client_ip] = [ts for ts in window if now - ts < _RATE_WINDOW_SEC]
        if len(_RATE_WINDOW[client_ip]) >= limit:
            logger.debug(
                "rate_limited path=%s client_ip=%s authenticated=%s",
                request.url.path,
                client_ip,
                authenticated,
            )
            return JSONResponse(
                status_code=429,
                content={"detail": "rate limit exceeded"},
                headers={"Retry-After": str(_RATE_WINDOW_SEC)},
            )
        _RATE_WINDOW[client_ip].append(now)
        _cleanup_rate_window()
    response = cast(Response, await call_next(request))
    elapsed = time.time() - start
    _REQUEST_COUNT.labels(
        method=request.method, path=request.url.path, status=response.status_code
    ).inc()
    _REQUEST_DURATION.labels(method=request.method, path=request.url.path).observe(elapsed)
    return response


async def _is_authenticated(request: Request) -> bool:
    raw_token: str | None = None
    using_cookie = False

    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        raw_token = auth_header[7:]
    elif "hp_token" in request.cookies:
        raw_token = request.cookies["hp_token"]
        using_cookie = True

    if not raw_token:
        return False

    repo: Repository | None = getattr(request.app.state, "repo", None)
    if repo is None:
        return False

    prefix = raw_token[:16]
    row = await repo.get_token_by_prefix(prefix)
    if row is None or not _validate_token(raw_token, row["hash"]):
        return False

    if row.get("expires_at"):
        from datetime import UTC, datetime

        expires = datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00"))
        if expires <= datetime.now(UTC):
            return False

    if using_cookie:
        csrf_header = request.headers.get("x-csrf-token")
        csrf_cookie = request.cookies.get("hp_csrf")
        if not csrf_cookie or csrf_header != csrf_cookie:
            return False
        if request.method not in {"GET", "HEAD", "OPTIONS"} and not request.headers.get(
            "x-requested-with"
        ):
            return False

    return True


@app.exception_handler(APIError)
async def api_error_handler(request: Request, exc: APIError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content=exc.to_dict())


from .admin.router import router as admin_router  # noqa: E402
from .agent_hub.router import router as agent_router  # noqa: E402
from .artifacts.router import router as artifacts_router  # noqa: E402
from .audit.router import router as audit_router  # noqa: E402
from .auth.router import router as auth_router  # noqa: E402
from .inventory.router import router as inventory_router  # noqa: E402
from .kb.router import router as kb_router  # noqa: E402
from .tasks.router import router as tasks_router  # noqa: E402

app.include_router(admin_router, prefix="/admin")
app.include_router(auth_router, prefix="/auth")
app.include_router(
    agent_router, prefix="/api", tags=["agents"], dependencies=[Depends(require_token)]
)
app.include_router(
    audit_router, prefix="/audit", tags=["audit"], dependencies=[Depends(require_token)]
)
app.include_router(
    inventory_router, prefix="/inventory", tags=["inventory"], dependencies=[Depends(require_token)]
)
app.include_router(
    artifacts_router, prefix="/artifacts", tags=["artifacts"], dependencies=[Depends(require_token)]
)
app.include_router(
    tasks_router, prefix="/tasks", tags=["tasks"], dependencies=[Depends(require_token)]
)
app.include_router(kb_router, prefix="/kb", tags=["kb"], dependencies=[Depends(require_token)])


@app.get("/health")
async def health(request: Request) -> JSONResponse:
    checks: dict[str, str] = {}

    db_status = "ok"
    try:
        db = getattr(request.app.state, "db", None)
        if db is None:
            db_status = "error"
        else:
            await db.execute("SELECT 1")
    except Exception as exc:
        logger.debug("database health check failed: %s", exc)
        db_status = "error"
    checks["database"] = db_status

    proxmox = getattr(request.app.state, "proxmox", None)
    settings = getattr(request.app.state, "settings", None)
    proxmox_host = getattr(settings, "proxmox_host", "") if settings else ""
    if proxmox is None:
        proxmox_status = "unreachable" if proxmox_host else "not_configured"
    else:
        try:
            connected = await proxmox.test_connection()
            proxmox_status = "ok" if connected else "unreachable"
        except Exception as exc:
            logger.debug("proxmox health check failed: %s", exc)
            proxmox_status = "unreachable"
    checks["proxmox"] = proxmox_status

    vault_status: str
    vault = getattr(request.app.state, "vault", None)
    if vault is None:
        vault_status = "not_configured"
    else:
        try:
            await vault.list_secrets()
            vault_status = "ok"
        except Exception as exc:
            logger.debug("vault health check failed: %s", exc)
            vault_status = "locked"
    checks["vault"] = vault_status

    agent_registry = getattr(request.app.state, "agent_registry", None)
    if agent_registry is not None:
        connected = agent_registry.list_connected()
        checks["agent_hub"] = "ok"
        checks["agents_connected"] = str(len(connected))
    elif settings and settings.agent_hub_enabled:
        checks["agent_hub"] = "error"
    else:
        checks["agent_hub"] = "not_configured"

    cors_val = _cors_validation or validate_cors_config(
        getattr(request.app.state, "settings", None) or _settings
    )
    if cors_val.get("misconfigured"):
        cors_status = "misconfigured"
    elif cors_val.get("has_wildcard"):
        cors_status = "wildcard_no_credentials"
    else:
        cors_status = "ok"
    checks["cors"] = cors_status

    degraded_statuses = {"error", "unreachable", "locked", "misconfigured"}
    has_down = any(v in degraded_statuses for v in checks.values())
    if has_down:
        overall = "down"
    elif all(v == "ok" or v == "not_configured" for v in checks.values()):
        overall = "ok"
    else:
        overall = "degraded"

    status_code = 503 if overall == "down" else 200
    return JSONResponse(
        status_code=status_code,
        content={"status": overall, "version": __version__, "checks": checks},
    )


@app.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    output = generate_latest()
    return PlainTextResponse(content=output, media_type="text/plain; version=0.0.4; charset=utf-8")


_UI_CANDIDATES = [
    Path(__file__).parent.parent.parent / "web" / "dist",
    Path("/app/web/dist"),
]
_UI_DIR = next((p for p in _UI_CANDIDATES if p.exists()), None)
if _UI_DIR is not None:
    from fastapi.responses import FileResponse

    _ui_dir: Path = _UI_DIR
    _ui_static = StaticFiles(directory=str(_ui_dir))

    @app.get("/ui/_app/{path:path}", include_in_schema=False)
    async def ui_static_assets(path: str, request: Request) -> Response:
        return await _ui_static.get_response(f"_app/{path}", request.scope)

    @app.get("/ui/{path:path}", include_in_schema=False)
    async def ui_spa(path: str, request: Request) -> Response:
        full = _ui_dir / path
        if full.exists() and full.is_file():
            return await _ui_static.get_response(path, request.scope)
        return FileResponse(_ui_dir / "index.html")
