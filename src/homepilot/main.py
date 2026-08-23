from __future__ import annotations

import asyncio
import contextlib
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
from fastapi.dependencies.models import Dependant
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.routing import APIRoute
from fastapi.staticfiles import StaticFiles
from prometheus_client import Counter, Histogram, generate_latest

from . import __version__
from .app_state import create_app_state, get_agent_registry
from .auth.deps import SCOPE_ENFORCER_ATTR, require_token
from .auth.tokens import validate_token as _validate_token
from .common import APIError
from .config import get_settings
from .db.repository import Repository
from .instance_lock import InstanceLock
from .selfcheck import mcp_transport_running, schedule_boot_selfcheck

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


def configure_logging(level: str) -> None:
    """Apply `HP_LOG_LEVEL` (#431).

    `settings.log_level` was defined, documented in `.env.example`, and READ BY
    NOTHING: there was no `basicConfig` anywhere outside the MCP entrypoints. So
    every `logger.debug` in `/health`, the vault fallbacks and the app-state
    secret resolution was invisible in production and could not be turned on -
    the diagnostics existed and the switch did not.

    `force=True` because uvicorn installs its own handlers first; without it this
    call is a no-op under the shipped entrypoint, which is the only place it
    matters.
    """
    resolved = getattr(logging, str(level).upper(), None)
    if not isinstance(resolved, int):
        resolved = logging.INFO
        logging.getLogger(__name__).warning(
            "HP_LOG_LEVEL=%r is not a level name; using INFO", level
        )
    logging.basicConfig(
        level=resolved,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )
    logging.getLogger("homepilot").setLevel(resolved)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    settings = get_settings()
    configure_logging(settings.log_level)
    state = await create_app_state(settings)

    for cidr in (settings.trusted_proxies or _HP_TRUSTED_PROXIES_ENV).split(","):
        cidr = cidr.strip()
        if cidr:
            _trusted_proxy_networks.append(ipaddress.ip_network(cidr, strict=False))
    # Published so routes that make their own source-address decision (the
    # first-run claim) read the SAME list as the rate-limit middleware instead
    # of parsing HP_TRUSTED_PROXIES a second time.
    app.state.trusted_proxy_networks = _trusted_proxy_networks

    app.state.db = state.database
    app.state.repo = state.repo

    # ONE backend per data directory (#431). The sweep below marks every
    # pending/running task failed, so a second backend - a rolling restart, a
    # stray `docker compose up` - used to kill the first one's in-flight work
    # while it carried on running. Held for the process lifetime; the kernel
    # drops it if we die, so a crash never leaves a stale lock.
    instance_lock = InstanceLock(settings.data_dir)
    instance_lock.acquire()
    app.state.instance_lock = instance_lock

    from .tasks.repository import TaskRepository

    task_repo = TaskRepository(state.database)
    # Correct ONLY because we hold the lock: any pending/running task now really
    # is orphaned, because no other backend exists to be running it.
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
        kb_service=None,
        proxmox_host=settings.proxmox_host,
    )
    inventory_service.kb_service = state.kb_service
    app.state.inventory_service = inventory_service
    app.state.kb_service = state.kb_service
    app.state.sse_bus = state.sse_bus
    app.state.metrics_repo = state.metrics_repo

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
        if state.agent_hub is not None:
            agent_adapter = AgentAdapter(
                hub_server=state.agent_hub,
                pve_nodes=state.artifact_lifecycle._pve_nodes_list
                if hasattr(state.artifact_lifecycle, "_pve_nodes_list")
                else [],
            )

        if state.proxmox and state.vault:
            app.state.artifact_executor = ArtifactExecutor(
                store=state.artifact_store,
                lifecycle=state.artifact_lifecycle,
                repo=state.repo,
                proxmox=state.proxmox,
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
    # Native metrics upkeep (ADR-004 S5): retention and alert evaluation are
    # scheduled work, so they ride the reconciler scheduler rather than growing a
    # second timer mechanism. Both are unconditional — metrics are part of the
    # product, and a pruner that only runs when something else is configured is
    # how an unbounded table happens.
    from .metrics.alerts import AlertEvaluator
    from .metrics.retention import MetricsPruner

    metrics_pruner = MetricsPruner(state.metrics_repo, settings.metrics_retention_days)
    reconciler_scheduler.register(
        metrics_pruner,
        interval=float(settings.metrics_prune_interval_seconds),
        startup_delay=60.0,
    )
    app.state.metrics_pruner = metrics_pruner

    # Operational history has its own horizon and its own reconciler: the right
    # retention for an audit trail is not the right retention for a time series
    # (#431). Unconditional, for the same reason the metrics pruner is - a
    # pruner that only runs when something else is configured is how an
    # unbounded table happens.
    from .reconciler.retention import RetentionReconciler

    retention_reconciler = RetentionReconciler(state.repo, settings.retention_days)
    reconciler_scheduler.register(
        retention_reconciler,
        interval=float(settings.retention_interval_seconds),
        startup_delay=120.0,
    )
    app.state.retention_reconciler = retention_reconciler
    alert_evaluator = AlertEvaluator(state.metrics_repo, repo=state.repo)
    reconciler_scheduler.register(
        alert_evaluator,
        interval=float(settings.metrics_alert_interval_seconds),
        startup_delay=45.0,
    )
    app.state.alert_evaluator = alert_evaluator

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

    from .provision.service import ProvisionService

    # Always constructed, even without Proxmox: the adapter can be configured
    # later through the admin settings UI, and _do_reload rebinds .proxmox onto
    # this same instance. The router 503s while .proxmox is None.
    app.state.provision_service = ProvisionService(
        proxmox=state.proxmox,
        task_repo=task_repo,
        repo=state.repo,
    )

    from .agent_hub.enroll import AgentEnrollService

    # Always constructed, like ProvisionService: Proxmox can be configured later
    # through the admin settings UI (_do_reload rebinds .proxmox onto this same
    # instance), and the preconditions refuse with a reason until it is.
    app.state.agent_enroll_service = AgentEnrollService(
        proxmox=state.proxmox,
        task_repo=task_repo,
        repo=state.repo,
        registry=state.agent_registry,
    )

    from .portal.repository import InviteRepository

    app.state.invite_repo = InviteRepository(state.database)

    from .claim.repository import ClaimRepository

    app.state.claim_repo = ClaimRepository(state.database)

    await reconciler_scheduler.start()
    app.state.reconciler_scheduler = reconciler_scheduler
    app.state.drift_reconciler = drift_reconciler

    try:
        await state.kb_service.reindex_if_needed(reason="startup")
    except (ImportError, OSError, ConnectionError):
        logger.warning("KB startup reindex check failed", exc_info=True)

    logger.info("HomePilot v2 started — data_dir=%s", settings.data_dir)

    # Last thing before the optional subsystems, so the claim box is the final
    # block an operator sees in `docker compose logs` rather than being buried.
    from .claim.startup import ensure_claim_code

    await ensure_claim_code(
        app.state.claim_repo,
        Path(settings.data_dir),
        url_hint=f"http://<this-host>:{settings.daemon_port}/ui",
    )

    mcp_app: Any = None
    app.state.mcp_app = None
    app.state.mcp_running = False
    mcp_token = os.environ.get("HP_MCP_TOKEN", "").strip()
    if mcp_token:
        from .mcp.server import _server_context, create_http_app, create_server

        srv = create_server()

        # Best-effort PVE node list for host-targeted tools.
        if state.proxmox is not None:
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

        # Host ops (read_file_on_guest / exec_on_guest_readonly) route through the
        # agent hub. Build the SAME adapter the stdio bootstrap uses so those tools
        # resolve over the HTTP transport, not only over stdio (#385). The adapter
        # depends only on the hub, independent of Proxmox/vault, so build it
        # whenever a hub exists — not just inside the Proxmox+vault branch.
        mcp_agent = None
        if state.agent_hub is not None:
            from .adapters.agent import AgentAdapter

            mcp_agent = AgentAdapter(
                hub_server=state.agent_hub,
                pve_nodes=getattr(state.artifact_lifecycle, "_pve_nodes_list", None) or [],
            )

        if state.proxmox and state.vault:
            from .executor import ArtifactExecutor

            executor = ArtifactExecutor(
                store=state.artifact_store,
                lifecycle=state.artifact_lifecycle,
                repo=state.repo,
                proxmox=state.proxmox,
                vault=state.vault,
                pve_nodes=getattr(state.artifact_lifecycle, "_pve_nodes_list", None) or [],
                agent=mcp_agent,
            )
            state.artifact_lifecycle._executor_ref = executor

        # The HTTP tool context MUST carry the same keys the stdio bootstrap builds,
        # or agent_adapter/drift_reconciler-backed tools error in every HTTP
        # deployment (#385: read_file_on_guest, exec_on_guest_readonly,
        # check_artifact_drift). drift_reconciler is the same instance the
        # reconciler scheduler uses (built above).
        await _server_context.async_update(
            {
                "store": state.artifact_store,
                "lifecycle": state.artifact_lifecycle,
                "repo": state.repo,
                "proxmox": state.proxmox,
                "vault": state.vault,
                "database": state.database,
                "kb_service": state.kb_service,
                "inventory_service": inventory_service,
                "task_repo": task_repo,
                "agent_adapter": mcp_agent,
                "drift_reconciler": drift_reconciler,
                # The hub's live registry, for `check_host_reachable` (#427).
                "agent_registry": get_agent_registry(),
            }
        )

        mcp_app = create_http_app(srv)
        app.state.mcp_app = mcp_app
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

    # Starlette does NOT run a mounted sub-app's lifespan, so the mounted MCP app's
    # StreamableHTTPSessionManager (started inside create_http_app's own lifespan
    # via session_manager.run()) would never initialize — every POST /mcp/ then
    # 500s with "Task group is not initialized" (#382). Drive the mounted app's
    # lifespan from here so its session manager runs for the whole server lifetime.
    from contextlib import AsyncExitStack

    mcp_lifespan_stack = AsyncExitStack()
    if mcp_app is not None:
        await mcp_lifespan_stack.enter_async_context(mcp_app.router.lifespan_context(mcp_app))
        app.state.mcp_running = True

    # Scheduled, never awaited (ADR-004 S6): the report tells an operator what is
    # off and what is broken, and diagnostics must not add a single millisecond to
    # the time the app takes to start serving. It runs last so the MCP transport
    # it reports on is already up.
    selfcheck_task = schedule_boot_selfcheck(app.state, settings)

    try:
        yield
    finally:
        logger.info("HomePilot v2 shutting down — stopping reconciler, closing connections")

        if not selfcheck_task.done():
            selfcheck_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await selfcheck_task

        agent_hub = getattr(app.state, "agent_hub", None)
        if agent_hub is not None:
            try:
                # Comfortably above the hub's own internal budget. If this guard
                # fires first it cancels stop() midway and its registry.drain()
                # never runs - leaving exactly the un-drained writes that close
                # the database under themselves (#496).
                await asyncio.wait_for(agent_hub.stop(), timeout=20.0)
                logger.info("Agent hub stopped")
            except TimeoutError:
                logger.warning("Agent hub stop timed out after 20s — proceeding")
            except Exception as exc:
                # Never out of the shutdown `finally`: an exception here would
                # skip the reconciler stop, every drain, the database close and
                # the MCP teardown below.
                logger.warning("Agent hub stop error: %s", exc)

        try:
            await asyncio.wait_for(reconciler_scheduler.stop(), timeout=10.0)
        except TimeoutError:
            logger.warning("Reconciler scheduler stop timed out after 10s — proceeding")

        # Background jobs run behind an already-accepted request, so nothing else
        # awaits them. Drain them BEFORE the database closes (#496): a write that
        # outlives its loop kills aiosqlite's worker thread, and the close then
        # queues onto a thread that will never pick it up. It also stops a
        # provision from being remembered as "running" forever.
        drainable = (
            ("task runner", getattr(app.state, "task_runner", None)),
            ("provision service", getattr(app.state, "provision_service", None)),
            ("agent enrolment service", getattr(app.state, "agent_enroll_service", None)),
            # Also drained inside agent_hub.stop(), but the registry outlives a
            # DISABLED hub: create_app_state leaves agent_hub None when the
            # transport check fails (#468's upgrade case) while the registry
            # stays live and still writes. Draining twice is a no-op.
            ("agent registry", getattr(app.state, "agent_registry", None)),
        )
        for label, service in drainable:
            drain = getattr(service, "drain", None)
            if drain is None:
                continue
            try:
                await asyncio.wait_for(drain(), timeout=10.0)
            except TimeoutError:
                logger.warning("%s drain timed out after 10s — proceeding", label)
            except Exception as exc:
                logger.warning("%s drain error: %s", label, exc)

        try:
            await asyncio.wait_for(state.database.close(), timeout=10.0)
        except TimeoutError:
            logger.warning("Database close timed out after 10s — proceeding")
        except Exception as exc:
            logger.warning("Database close error (ok on restart): %s", exc)
        from .mcp.server import _server_context

        _server_context.clear()
        app.state.mcp_running = False
        # Tear down the mounted MCP session manager LAST — after the shared context
        # is cleared — so create_http_app's lifespan cleanup finds nothing to close
        # and cannot double-close the DB/Proxmox handles already closed above.
        await mcp_lifespan_stack.aclose()
        # Released LAST: while it is held, nothing else may start against this
        # data directory and run the orphan sweep over our tasks.
        instance_lock.release()
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
    client_ip = _get_client_ip(request, _trusted_proxy_networks)
    now = time.time()

    # Cheap tier first: count against the unauthenticated limit WITHOUT a token
    # lookup, so an anonymous flood never costs a DB round-trip per request. The
    # token lookup (to grant the higher authenticated limit) happens only when
    # the cheap window is already full — bounding the amplification to one
    # lookup per IP per minute past the anonymous cap.
    async with _RATE_LOCK:
        window = [ts for ts in _RATE_WINDOW[client_ip] if now - ts < _RATE_WINDOW_SEC]
        _RATE_WINDOW[client_ip] = window
        count = len(window)

    over_anon = count >= _RATE_LIMIT
    authenticated = False
    if over_anon and request.url.path != "/auth/login":
        authenticated = await _is_authenticated(request)
    limit = _AUTH_RATE_LIMIT if authenticated else _RATE_LIMIT

    async with _RATE_LOCK:
        # Re-read under lock: another request may have advanced the window.
        window = [ts for ts in _RATE_WINDOW[client_ip] if now - ts < _RATE_WINDOW_SEC]
        if len(window) >= limit:
            _RATE_WINDOW[client_ip] = window
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
        window.append(now)
        _RATE_WINDOW[client_ip] = window
        _cleanup_rate_window()
    response = cast(Response, await call_next(request))
    elapsed = time.time() - start
    # Metric label is the ROUTE TEMPLATE (e.g. /agents/{agent_id}), not the raw
    # URL, so scanner traffic and per-id paths can't explode label cardinality.
    metric_path = _metric_path(request)
    _REQUEST_COUNT.labels(
        method=request.method, path=metric_path, status=response.status_code
    ).inc()
    _REQUEST_DURATION.labels(method=request.method, path=metric_path).observe(elapsed)
    return response


def _metric_path(request: Request) -> str:
    """The matched route's path template, or a fixed bucket for unmatched URLs —
    keeps Prometheus label cardinality bounded under arbitrary traffic."""
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    if isinstance(path, str) and path:
        return path
    return "<unmatched>"


def _mcp_health_status(app: FastAPI) -> str:
    """MCP transport health for /health.

    Returns "ok" only when the mounted MCP app's StreamableHTTPSessionManager is
    ACTUALLY running (its anyio task group is live). A mounted /mcp route alone is
    NOT proof the transport works — #382 had the route present while the session
    manager was never started, so every request 500'd yet /health claimed "ok".
    The predicate lives in selfcheck so /health and the self-check report cannot
    drift into judging MCP differently.
    """
    return "ok" if mcp_transport_running(getattr(app.state, "mcp_app", None)) else "error"


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
from .claim.router import router as claim_router  # noqa: E402
from .dashboard.router import router as dashboard_router  # noqa: E402
from .inventory.router import router as inventory_router  # noqa: E402
from .kb.router import router as kb_router  # noqa: E402
from .metrics.router import router as metrics_router  # noqa: E402
from .portal.router import router as portal_router  # noqa: E402
from .provision.router import router as provision_router  # noqa: E402
from .tasks.router import router as tasks_router  # noqa: E402

app.include_router(admin_router, prefix="/admin")
app.include_router(auth_router, prefix="/auth")
app.include_router(agent_router, tags=["agents"], dependencies=[Depends(require_token)])
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
app.include_router(
    provision_router, prefix="/guests", tags=["guests"], dependencies=[Depends(require_token)]
)
app.include_router(dashboard_router, dependencies=[Depends(require_token)])
app.include_router(metrics_router, dependencies=[Depends(require_token)])
# The invite portal carries NO token dependency by design: it is authenticated
# by a client certificate the reverse proxy verified, re-checked inside every
# route (source address + shared secret + verify header + CN binding). nginx
# publishes only this prefix on the public mTLS vhost - see docs/portal.md.
app.include_router(portal_router, prefix="/invite", tags=["invite"])
# The first-run claim carries NO token dependency by design: it is the path by
# which the FIRST token comes into existence, so requiring one is a deadlock.
# Its own credential is the claim code (constant-time compared, rate limited),
# and it refuses everything once the instance is claimed - see _PUBLIC_ROUTES.
app.include_router(claim_router)


# ── Startup route-scope guard ────────────────────────────────────────────────
# Every non-public API route must carry a scope dependency (require_scope(...) or
# an explicit admin/secret gate). require_token alone ("any valid token") is not
# enough. These routes are the genuinely public surface and are exempt by an
# explicit (method, path-template) allowlist — health/metrics/root, the auth
# entry flow, and the static UI. The /mcp mount is a Starlette Mount (not an
# APIRoute) and so is skipped structurally; likewise FastAPI's own docs routes.
_PUBLIC_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", "/health"),
        ("GET", "/metrics"),
        ("GET", "/"),
        ("GET", "/ui/_app/{path:path}"),
        ("GET", "/ui/{path:path}"),
        # Auth entry flow: these authenticate by their own means (a bearer token
        # validated in-body, the admin secret, or a session cookie) rather than a
        # FastAPI scope dependency, so they carry no require_scope.
        ("GET", "/auth/me"),  # self-identity: must work for any valid token
        ("POST", "/auth/login"),
        ("POST", "/auth/logout"),
        ("POST", "/auth/tokens"),  # gated by the admin secret in the request body
        # Invite portal: authenticated by the mTLS client certificate the proxy
        # verified, not by an API token. Each route re-derives the CN through
        # portal.trust (trusted source + shared secret + verify header) and
        # refuses everything else, so require_scope has nothing to add here.
        ("GET", "/invite/{token}"),
        ("POST", "/invite/{token}"),
        ("GET", "/invite/{token}/status"),
        # First-run claim: the route that MINTS the first admin token cannot
        # require one. Its gate is the claim code - generated at first boot,
        # stored only as a sha256, constant-time compared and rate limited - and
        # it is permanently closed (410) the moment an admin credential exists,
        # so this is a public surface for exactly one instance-lifetime moment.
        ("GET", "/claim/status"),
        ("POST", "/claim"),
    }
)


def _iter_sub_dependants(dependant: Dependant) -> list[Dependant]:
    """Flatten a Dependant's whole sub-dependency tree (FastAPI's
    get_flat_dependant collapses params but drops the sub-dependant callables we
    need, so walk it ourselves)."""
    collected: list[Dependant] = []
    for sub in dependant.dependencies:
        collected.append(sub)
        collected.extend(_iter_sub_dependants(sub))
    return collected


def _route_has_scope_dep(route: APIRoute) -> bool:
    for sub in _iter_sub_dependants(route.dependant):
        if getattr(sub.call, SCOPE_ENFORCER_ATTR, False):
            return True
    return False


def find_unscoped_routes(target_app: FastAPI) -> list[tuple[str, str]]:
    """Return (method, path) for every non-public APIRoute lacking a scope dep."""
    missing: list[tuple[str, str]] = []
    for route in target_app.routes:
        if not isinstance(route, APIRoute):
            continue  # Mounts (/mcp), static files, framework docs — not scope-checkable
        for method in route.methods or set():
            if method in ("HEAD", "OPTIONS"):
                continue
            if (method, route.path) in _PUBLIC_ROUTES:
                continue
            if not _route_has_scope_dep(route):
                missing.append((method, route.path))
    return sorted(missing)


def assert_all_routes_scoped(target_app: FastAPI) -> None:
    """Fail fast at construction if any non-public route ships without a scope
    dependency, so a future unscoped route can't silently reach production."""
    missing = find_unscoped_routes(target_app)
    if missing:
        formatted = ", ".join(f"{m} {p}" for m, p in missing)
        raise RuntimeError(
            "Route scope guard: these API routes have no scope dependency and are "
            f"not in the public allowlist: {formatted}. Add require_scope(...) / an "
            "admin dep, or add the route to _PUBLIC_ROUTES if it is genuinely public."
        )


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

    mcp_status: str = "not_configured"
    mcp_token = os.environ.get("HP_MCP_TOKEN", "").strip()
    if mcp_token:
        # Report "ok" only when the mounted MCP app's session manager is ACTUALLY
        # running. A mounted route alone is not proof the transport works: #382 had
        # the mount present while its session manager was never started, so every
        # request 500'd yet /health still claimed "ok". Inspect the live session
        # manager task group instead of merely checking a route exists.
        try:
            mcp_status = _mcp_health_status(request.app)
        except Exception as exc:
            logger.debug("mcp health check failed: %s", exc)
            mcp_status = "error"
    checks["mcp"] = mcp_status

    # This endpoint is the LIVENESS probe: the compose healthcheck calls it every
    # 30s, and anything reading it (a `depends_on: service_healthy`, an
    # orchestrator's restart policy, an uptime monitor) acts on the answer by
    # restarting or pulling traffic. So it answers one question - can this process
    # serve requests - and only the database failing means no.
    #
    # It used to answer "is every subsystem happy", which is a different question
    # with a worse consequence: a vault that needs unlocking, an unreachable
    # embedding service or a hub that refused its transport (#468) each made a
    # perfectly serving instance report `down` and 503, so the container was
    # marked unhealthy over something no restart can repair - a restart loop
    # chasing a config file. Subsystem trouble is real and stays visible as
    # `degraded` here, in the per-check map, and in full at /admin/selfcheck,
    # which exists to draw exactly these distinctions (#470).
    #
    # The check map is unchanged, so the UI contract that reads it is untouched.
    # `agents_connected` is a COUNT that happens to live in a map of statuses, so
    # it must not be read as one. It only appears when the hub is running, which
    # is why turning the hub on by default (S3) quietly made every healthy
    # instance report `degraded`: "0" is neither "ok" nor "not_configured", so it
    # fell through to the catch-all. Informational entries are named here rather
    # than being moved out of `checks`, because the UI reads that map.
    informational = {"agents_connected"}
    statuses = [value for key, value in checks.items() if key not in informational]

    subsystem_trouble = {"error", "unreachable", "locked", "misconfigured"}
    database_failed = checks.get("database") != "ok"
    if database_failed:
        overall = "down"
    elif any(value in subsystem_trouble for value in statuses):
        overall = "degraded"
    elif all(value == "ok" or value == "not_configured" for value in statuses):
        overall = "ok"
    else:
        overall = "degraded"

    status_code = 503 if overall == "down" else 200
    return JSONResponse(
        status_code=status_code,
        content={"status": overall, "version": __version__, "checks": checks},
    )


@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    """Bare root sends the operator to the web UI (proxy-agnostic; #284)."""
    return RedirectResponse(url="/ui/", status_code=307)


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


# Enforce scope coverage once the full route table (routers + app-level routes +
# optional UI routes) is assembled. Raises at import/construction on any gap.
assert_all_routes_scoped(app)
