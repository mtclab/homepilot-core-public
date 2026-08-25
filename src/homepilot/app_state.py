from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .config import get_settings
from .db.connection import Database
from .db.migrations import run_migrations
from .db.repository import Repository

if TYPE_CHECKING:
    from .agent_hub.registry import AgentRegistry

logger = logging.getLogger(__name__)

_agent_registry: AgentRegistry | None = None  # module-level singleton for agent hub registry


def get_agent_registry() -> AgentRegistry | None:
    return _agent_registry


_PVE_TOKEN_RE = re.compile(r"^[^@]+@[^!]+![^=]+=.+$")


def _validate_pve_token(token: str) -> bool:
    if not _PVE_TOKEN_RE.match(token):
        logger.warning(
            "PVE_API_TOKEN format invalid — expected 'user@realm!tokenid=uuid', got '%s...'",
            token[:12],
        )
        return False
    return True


@dataclass
class AppState:
    settings: Any
    database: Database
    repo: Repository
    vault: Any = None
    proxmox: Any = None
    artifacts_dir: Path = field(default_factory=Path)
    artifact_store: Any = None
    artifact_lifecycle: Any = None
    kb_service: Any = None
    sse_bus: Any = None
    pve_token_source: str = ""
    webhook_secret_source: str = ""
    n8n_key_source: str = ""
    agent_hub: Any = None
    agent_registry: Any = None
    metrics_repo: Any = None
    # The mounted MCP Starlette app, assigned by main.py once it exists. The
    # selfcheck report reads it to say whether the MCP transport is RUNNING, so
    # a state object without it reports MCP as broken - which is why it is a
    # field here rather than an attribute only `app.state` carries (#514).
    mcp_app: Any = None
    # Why the hub is not running despite being enabled, in the operator's words.
    # Empty when the hub is off by configuration or running normally.
    agent_hub_disabled_reason: str = ""


async def create_app_state(settings: Any | None = None) -> AppState:
    if settings is None:
        settings = get_settings()

    data_dir = Path(settings.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)

    db_path = data_dir / "homepilot.db"
    database = Database(str(db_path))
    await database.connect()
    await run_migrations(database)
    repo = Repository(database)

    vault: Any = None
    if settings.vault_passphrase:
        from .vault import VaultManager

        vault_dir = Path(settings.vault_dir) if settings.vault_dir else data_dir / "vault"
        vault_dir.mkdir(parents=True, exist_ok=True)
        vault = VaultManager(data_dir, settings.vault_passphrase)
        await vault.ensure_master_identity()
        logger.info("Vault unlocked")
    else:
        logger.warning("Vault passphrase not set — secrets unavailable until configured")

    from .metrics.repository import MetricsRepository

    metrics_repo = MetricsRepository(database)

    proxmox: Any = None

    pve_token_source = ""
    # Bound BEFORE the try (#431). It was assigned inside, and line 426 reads it
    # after the except - so on the ImportError path the backend died with a
    # NameError naming the wrong thing entirely, instead of the import that
    # actually failed.
    proxmox_host = settings.proxmox_host

    try:
        from .adapters.proxmox import ProxmoxClient

        # Resolve Proxmox config: vault overrides env
        proxmox_port = settings.proxmox_port
        proxmox_verify_ssl = settings.proxmox_verify_ssl
        if vault:
            try:
                proxmox_config_secret = await vault.get_secret("proxmox-config")
                if proxmox_config_secret:
                    proxmox_host = proxmox_config_secret.get("host", proxmox_host) or proxmox_host
                    proxmox_port = proxmox_config_secret.get("port", proxmox_port) or proxmox_port
                    proxmox_verify_ssl_val = proxmox_config_secret.get(
                        "verify_ssl", proxmox_verify_ssl
                    )
                    if isinstance(proxmox_verify_ssl_val, str):
                        proxmox_verify_ssl = proxmox_verify_ssl_val.lower() in ("true", "1", "yes")
                    elif isinstance(proxmox_verify_ssl_val, bool):
                        proxmox_verify_ssl = proxmox_verify_ssl_val
            except Exception:
                logger.debug(
                    "Vault 'proxmox-config' unavailable, using env defaults", exc_info=True
                )

        if proxmox_host:
            token = ""
            if vault:
                from .vault import VaultError

                try:
                    pve_secret = await vault.get_secret("pve-token")
                    token = pve_secret.get("token", "")
                    if token:
                        if not _validate_pve_token(token):
                            logger.warning("PVE token from vault has invalid format — skipping")
                            token = ""
                        else:
                            pve_token_source = "vault"
                except (VaultError, OSError):
                    logger.debug(
                        "Vault 'pve-token' unavailable, falling back to env",
                        exc_info=True,
                    )
                if not token:
                    env_token = os.environ.get("PVE_API_TOKEN", "")
                    if env_token:
                        if not _validate_pve_token(env_token):
                            logger.warning("PVE_API_TOKEN from env has invalid format — skipping")
                        else:
                            pve_token_source = "env"
                            logger.warning(
                                "PVE_API_TOKEN via env var — use vault for better security"
                            )
                            token = env_token
            elif os.environ.get("PVE_API_TOKEN"):
                env_token = os.environ["PVE_API_TOKEN"]
                if _validate_pve_token(env_token):
                    pve_token_source = "env"
                    logger.warning("PVE_API_TOKEN via env var — use vault for better security")
                    token = env_token
            if token:
                write_token = ""
                if vault:
                    from .vault import VaultError

                    try:
                        write_secret = await vault.get_secret("pve-write-token")
                        write_token = write_secret.get("token", "")
                    except (VaultError, OSError):
                        logger.debug("Vault 'pve-write-token' unavailable", exc_info=True)
                if not write_token:
                    write_token = token
                logger.info("Proxmox adapter initialized (token from %s)", pve_token_source)
                base_url = f"https://{proxmox_host}:{proxmox_port}"
                proxmox = ProxmoxClient(
                    base_url=base_url,
                    token=token,
                    verify_ssl=proxmox_verify_ssl,
                    write_token=write_token,
                )
            else:
                logger.warning("Proxmox host configured but no token available — Proxmox disabled")
    except (ImportError, OSError, ConnectionError) as exc:
        logger.warning("Could not initialize Proxmox adapter: %s", exc, exc_info=True)

    webhook_secret_source = ""
    if vault:
        from .vault import VaultError as _VaultErrorWebhook

        try:
            webhook_secret = await vault.get_secret("webhook-secret")
            secret_val = webhook_secret.get("secret", "")
            if secret_val:
                settings.events_webhook_secret = secret_val
                webhook_secret_source = "vault"  # pragma: allowlist secret
        except (_VaultErrorWebhook, OSError):
            logger.debug(
                "Vault 'webhook-secret' unavailable, falling back to env",
                exc_info=True,
            )
    if not webhook_secret_source and os.environ.get("HP_EVENTS_WEBHOOK_SECRET"):
        webhook_secret_source = "env"  # pragma: allowlist secret
        logger.warning("HP_EVENTS_WEBHOOK_SECRET via env var — use vault for better security")
    if webhook_secret_source:
        logger.info("Webhook secret resolved (from %s)", webhook_secret_source)

    n8n_key_source = ""
    if vault:
        from .vault import VaultError as _VaultErrorN8n

        try:
            n8n_secret = await vault.get_secret("n8n-key")
            n8n_val = n8n_secret.get("key", "")
            if n8n_val:
                settings.n8n_api_key = n8n_val
                n8n_key_source = "vault"
        except (_VaultErrorN8n, OSError):
            logger.debug(
                "Vault 'n8n-key' unavailable, falling back to env",
                exc_info=True,
            )
    if not n8n_key_source and os.environ.get("HP_N8N_API_KEY"):
        n8n_key_source = "env"
        logger.warning("HP_N8N_API_KEY via env var — use vault for better security")
    if n8n_key_source:
        logger.info("n8n API key resolved (from %s)", n8n_key_source)

    data_dir_resolved = Path(settings.data_dir).resolve()
    artifacts_dir = Path(settings.artifacts_dir).resolve()
    protected_paths: tuple[str, ...] = (
        "/",
        "/etc",
        "/usr",
        "/var",
        "/sys",
        "/boot",
        "/proc",
        "/tmp",
    )
    if not str(data_dir_resolved).startswith("/home"):
        protected_paths = (*protected_paths, "/home")
    for blocked in protected_paths:
        if str(artifacts_dir) == blocked or str(artifacts_dir).startswith(blocked + "/"):
            raise ValueError(f"artifacts_dir ({artifacts_dir}) points to a protected path")
    if not str(artifacts_dir).startswith(str(data_dir_resolved)):
        raise ValueError(
            f"artifacts_dir ({artifacts_dir}) is outside data_dir ({data_dir_resolved})"
        )
    if not artifacts_dir.is_dir():
        artifacts_dir.mkdir(parents=True, exist_ok=True)
    if not (artifacts_dir / ".git").exists():
        try:
            import subprocess

            subprocess.run(
                ["git", "init"],
                cwd=str(artifacts_dir),
                capture_output=True,
                check=False,
            )
            if (
                subprocess.run(
                    ["git", "rev-parse", "--is-inside-work-tree"],
                    cwd=str(artifacts_dir),
                    capture_output=True,
                ).returncode
                != 0
            ):
                subprocess.run(
                    ["git", "init"],
                    cwd=str(artifacts_dir),
                    capture_output=True,
                    check=False,
                )
        except FileNotFoundError:
            logger.warning("git not found — artifacts directory will not be versioned")

    from .artifacts.lifecycle import ArtifactLifecycle
    from .artifacts.store import ArtifactStore

    artifact_store = ArtifactStore(
        artifacts_dir,
        remote=settings.artifacts_remote,
        ssh_key=settings.artifacts_ssh_key,
    )
    artifact_lifecycle = ArtifactLifecycle(artifact_store, repository=repo)

    from .kb.service import KBService

    kb_service = KBService(repo=repo, store=artifact_store, lifecycle=artifact_lifecycle)
    artifact_lifecycle._kb_service = kb_service

    from .sse import bus as sse_bus

    agent_hub = None
    agent_registry = None
    agent_hub_disabled_reason = ""
    if settings.agent_hub_enabled:
        from .agent_hub.registry import AgentRegistry
        from .agent_hub.server import AgentHubServer
        from .agent_hub.tokens import BootstrapTokenStore

        agent_registry = AgentRegistry(repo=repo, metrics_repo=metrics_repo)
        global _agent_registry
        _agent_registry = agent_registry

        _token_store = BootstrapTokenStore(db=database)
        await _token_store.load_from_db()

        # An install that explicitly opted into plaintext keeps plaintext: the
        # TLS-by-default change must not silently break a fleet whose agents
        # dial the hub without TLS. Only an operator who never touched
        # HP_AGENT_HUB_TLS gets the new default.
        _tls_set_explicitly = "agent_hub_tls" in settings.model_fields_set
        _tls_wanted = settings.agent_hub_tls and not (
            settings.agent_hub_allow_insecure and not _tls_set_explicitly
        )

        # ...and neither may the default itself. TLS-by-default belongs to a NEW
        # install; an install that already had a fleet when it first met this
        # code keeps the transport those agents enrolled with, decided once and
        # remembered (#468). A legacy install therefore also carries the plaintext
        # allowance the fail-closed check demands - it is not a downgrade, it is
        # the transport that install was already running.
        _hub_legacy_plaintext = False
        if _tls_wanted:
            from .agent_hub.tls_mode import MODE_LEGACY_PLAINTEXT, resolve_hub_tls_mode

            _hub_legacy_plaintext = (
                await resolve_hub_tls_mode(
                    repo,
                    tls_set_explicitly=_tls_set_explicitly,
                    bind=f"{settings.agent_hub_host}:{settings.agent_hub_port}",
                )
                == MODE_LEGACY_PLAINTEXT
            )
            if _hub_legacy_plaintext:
                _tls_wanted = False

        ssl_ctx = None
        hub_cert_fingerprint = ""
        if _tls_wanted:
            import ssl as _ssl

            from .agent_hub.selfconfig import certificate_fingerprint, ensure_hub_certificate

            cert_file = settings.agent_hub_tls_cert
            key_file = settings.agent_hub_tls_key
            if not (cert_file and key_file):
                # No operator-supplied material: generate once and reuse, so the
                # fail-closed transport check passes on its own merits rather
                # than being weakened or overridden (ADR-004 S3).
                _cert_path, _key_path = ensure_hub_certificate(
                    data_dir,
                    extra_hosts=(settings.agent_hub_advertise_host, settings.agent_hub_host),
                )
                cert_file, key_file = str(_cert_path), str(_key_path)
            ssl_ctx = _ssl.SSLContext(_ssl.PROTOCOL_TLS_SERVER)
            ssl_ctx.load_cert_chain(certfile=cert_file, keyfile=key_file)
            hub_cert_fingerprint = certificate_fingerprint(Path(cert_file))
            if settings.agent_hub_tls_ca:
                # Mutual TLS: verify each connecting agent's client certificate
                # against the configured CA.
                ssl_ctx.load_verify_locations(settings.agent_hub_tls_ca)
                ssl_ctx.verify_mode = _ssl.CERT_REQUIRED
                logger.info("Agent hub TLS enabled with mutual client-cert verification")
            else:
                # Server-authenticated TLS only: agents verify THIS server's
                # certificate, but we do not require a client certificate because
                # per-agent identity / client mTLS is a follow-up (see #362). This
                # is NOT the silent CERT_NONE MITM downgrade — that was the AGENT
                # (client) side, which now verifies the server by default. Here
                # CERT_NONE only means "no client cert demanded", stated explicitly.
                ssl_ctx.verify_mode = _ssl.CERT_NONE
                logger.info(
                    "Agent hub TLS enabled (server-authenticated; client mTLS pending #362)"
                )
        else:
            logger.warning("Agent hub running without TLS — enable HP_AGENT_HUB_TLS for production")

        agent_hub = AgentHubServer(
            host=settings.agent_hub_host,
            port=settings.agent_hub_port,
            auth_token=settings.agent_hub_auth_token,
            registry=agent_registry,
            ssl_context=ssl_ctx,
            token_store=_token_store,
            allow_insecure=settings.agent_hub_allow_insecure or _hub_legacy_plaintext,
            cert_fingerprint=hub_cert_fingerprint,
        )
        # Fail closed early (before serving) on an exposed plaintext transport,
        # so misconfiguration surfaces before any credential crosses the wire
        # rather than as a swallowed exception inside the background serve task.
        #
        # Closing the HUB is the whole point; killing the CONTROL PLANE is not.
        # Raising here took the API, the UI, inventory and provisioning down with
        # it and left the container restart-looping, which is the worst available
        # answer to "your TLS setting is unusual" - and it is what an existing
        # install with HP_AGENT_HUB_TLS=false does on upgrade (#468). The hub is
        # one optional subsystem; it goes dark, says why, and everything else
        # keeps serving so the operator can read the reason and act on it.
        try:
            agent_hub.check_transport_security()
        except RuntimeError as exc:
            agent_hub_disabled_reason = str(exc)
            agent_hub = None
            logger.error(
                "Agent hub DISABLED - the control plane continues without it. %s",
                agent_hub_disabled_reason,
            )
        else:
            agent_registry.hub_server = agent_hub
            logger.info(
                "Agent hub initialized on %s:%s",
                settings.agent_hub_host,
                settings.agent_hub_port,
            )

    return AppState(
        settings=settings,
        database=database,
        repo=repo,
        vault=vault,
        proxmox=proxmox,
        artifacts_dir=artifacts_dir,
        artifact_store=artifact_store,
        artifact_lifecycle=artifact_lifecycle,
        kb_service=kb_service,
        sse_bus=sse_bus,
        pve_token_source=pve_token_source if proxmox_host else "",
        webhook_secret_source=webhook_secret_source,
        n8n_key_source=n8n_key_source,
        agent_hub=agent_hub,
        agent_registry=agent_registry,
        metrics_repo=metrics_repo,
        agent_hub_disabled_reason=agent_hub_disabled_reason,
    )
