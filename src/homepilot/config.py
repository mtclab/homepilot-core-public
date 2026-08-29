from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings


class ConfigError(ValueError):
    pass


def _env_files() -> list[str]:
    import os as _os

    candidates = [
        str(Path.home() / ".hp" / ".env"),
        str(Path(_os.environ.get("HP_DATA_DIR", str(Path.home() / ".hp"))) / ".env"),
        ".env",
    ]
    import logging as _logging

    log = _logging.getLogger(__name__)
    files = []
    for f in dict.fromkeys(candidates):
        if not Path(f).exists():
            continue
        # An unreadable dotenv must not brick the boot with a raw traceback
        # (seen live: a root-owned .env in the data volume - written by a root
        # `docker exec` - stopped the 3.0.0 container from ever starting).
        # The file's SETTINGS are skipped, and that is worth shouting about,
        # but a config file the process cannot read is the operator's to fix,
        # not a reason to refuse to serve everything configured via env vars.
        if not _os.access(f, _os.R_OK):
            log.error(
                "Config file %s exists but is NOT READABLE by this process "
                "(uid %s) - its settings are being IGNORED. Fix the ownership "
                "(e.g. `chown homepilot:homepilot %s` in the container).",
                f,
                _os.getuid(),
                f,
            )
            continue
        files.append(f)
    if files:
        log.debug("Loading config from: %s", ", ".join(files))
    return files


class Settings(BaseSettings):
    model_config = {"env_prefix": "HP_", "env_file": _env_files(), "extra": "ignore"}

    data_dir: str = str(Path.home() / ".hp")
    artifacts_dir: str = str(Path.home() / ".hp" / "artifacts")
    artifacts_remote: str = ""
    # How often the artifact store is pushed to `artifacts_remote` (#442
    # follow-up). Only meaningful when a remote is set; the push also runs
    # shortly after boot so a fresh restore is mirrored without waiting.
    artifacts_push_interval_seconds: int = 3600
    artifacts_ssh_key: str = ""
    admin_secret: str = ""

    env: str = ""

    proxmox_host: str = ""
    proxmox_port: int = 8006
    proxmox_verify_ssl: bool = True

    # Provisioning defaults (#553 C3). Every one of them is EMPTY by default,
    # which means "this instance has no opinion" - the caller must say it
    # itself, exactly as before C3. They exist so an invite stops carrying raw
    # infra details: the operator states the cluster's shape once, here or in
    # the UI, and the mint/redemption paths fill it in.
    #
    # The two numeric ones use 0 as "unset" rather than None: the settings
    # registry stores strings and a blanked field has to read back as "no
    # opinion", which an int field can only say with a sentinel.
    provision_default_node: str = ""
    provision_default_template_vmid: int = 0
    provision_default_pool: str = ""
    # Target storage for the clone's disks. Empty means "inherit the template's
    # storage", which is what PVE does when the clone call carries no storage -
    # and is the behaviour every install had before this setting existed.
    provision_default_storage: str = ""
    provision_tailscale_install: int = 1
    # Only when a bridge is set does provisioning touch net0 at all; unset
    # leaves the template's own NIC exactly as it was cloned.
    provision_default_bridge: str = ""
    provision_default_vlan_tag: int = 0
    provision_default_ipconfig: str = "ip=dhcp"

    # The guest network (#553). Empty subnet/gateway means this instance
    # describes no guest network at all: nothing is surveyed and provisioning
    # writes no per-VM fence. The zone/vnet names carry defaults because they
    # are names, not decisions - they mean nothing until a subnet exists.
    guest_network_zone: str = "guest"
    guest_network_vnet: str = "innkeep"
    guest_network_subnet: str = ""
    guest_network_gateway: str = ""
    guest_network_snat: int = 1
    guest_network_dhcp: int = 1
    guest_network_dhcp_range: str = ""
    guest_network_dhcp_dns_server: str = ""
    # The LAN a guest must never reach. Non-empty by default on purpose: a
    # guest network with no isolate list is a guest network with no fence, and
    # the default has to fail closed.
    guest_network_isolate_cidrs: str = ""

    vault_dir: str = ""
    vault_passphrase: str = ""
    vault_passphrase_file: str = ""

    allowed_http_domains: str = ""

    events_webhook_url: str | None = None
    events_webhook_secret: str | None = None
    n8n_api_key: str = ""

    # Both URLs default to EMPTY, which means "no embedding service" and puts KB
    # search in keyword mode (ADR-004 corollary 3: an optional service works out
    # of the box or is off and says so). Neither service exists in a stock
    # install - llm-embed lives in the docker-compose.agent.yml overlay behind the
    # gpu/cpu profiles and needs a model file the repo does not ship, and
    # localhost:11434 inside the backend container is the container itself, not an
    # Ollama host. The overlay sets the primary URL for the backend when it is
    # enabled; the startup self-check states the consequence when it is not.
    embedding_service_url: str = ""
    embedding_model: str = "bge-m3"
    embedding_fallback_url: str = ""
    embedding_fallback_model: str = "nomic-embed-text"

    inventory_interval_seconds: int = 300
    drift_interval_seconds: int = 1800
    auto_apply_enabled: bool = False
    auto_apply_interval_seconds: int = 300

    daemon_port: int = Field(default=8000, validation_alias="HP_PORT")
    log_level: str = "info"

    # How long operational HISTORY is kept: audit_log, agent_audit, finished
    # tasks and webhook deliveries. Nothing pruned any of them (#431), and each
    # gains a row per operation - a year on a homelab VM is a multi-GB SQLite
    # file and a backup too big to move.
    #
    # 90 days rather than the metrics window (7): an audit trail answers "who
    # changed this, and when" months later, which a time series does not have to.
    # Artifacts are never pruned - they are the record of intent, not history.
    retention_days: int = 90
    retention_interval_seconds: int = 21600
    # Host management is the product, so the hub is on unless an operator turned
    # it off (ADR-004 S3). Everything it needs that an operator would otherwise
    # have to decide - the shared token below, the TLS certificate - generates
    # itself on first boot.
    agent_hub_enabled: bool = True
    agent_hub_host: str = "0.0.0.0"
    agent_hub_port: int = 8443
    # Address agents should DIAL to reach the hub. agent_hub_host is the bind
    # address (often 0.0.0.0) and is not routable; behind a reverse proxy the
    # browser hostname isn't the raw-hub address either. Set this to the host
    # (optionally host:port) agents can reach the hub on.
    agent_hub_advertise_host: str = ""
    agent_hub_auth_token: str = ""
    # TLS is on by default; with no cert/key supplied the hub generates a
    # self-signed pair on first boot (agent_hub/selfconfig.py). Setting
    # HP_AGENT_HUB_TLS=false explicitly still turns it off, and an install that
    # opted into the insecure override keeps plaintext (see app_state).
    agent_hub_tls: bool = True
    agent_hub_tls_cert: str = ""
    agent_hub_tls_key: str = ""
    agent_hub_tls_ca: str = ""
    # Fail-closed override: allow the hub to run WITHOUT TLS on a non-loopback
    # bind. Only for a trusted, isolated network — logs a loud warning. Accepts
    # HP_HUB_ALLOW_INSECURE (spec name) or HP_AGENT_HUB_ALLOW_INSECURE.
    agent_hub_allow_insecure: bool = Field(
        default=False,
        validation_alias=AliasChoices("HP_HUB_ALLOW_INSECURE", "HP_AGENT_HUB_ALLOW_INSECURE"),
    )

    # ── Native metrics (ADR-004 S5) ──────────────────────────────────────────
    # How long raw samples are kept. Seven days is the ADR's deliberate starting
    # point: HomePilot now owns metric storage, and a window it can size honestly
    # beats a window that quietly outgrows the disk. There is NO rollup — measure
    # a real week first, then decide whether one earns its complexity.
    metrics_retention_days: int = 7
    metrics_prune_interval_seconds: int = 3600
    metrics_alert_interval_seconds: int = 60

    # ── Invite portal (#442 stage 2) ─────────────────────────────────────────
    # The /invite/* pages trust a client-certificate identity ONLY when all
    # three of these are set and the request satisfies all three: it arrives
    # from portal_trusted_proxy, carries the shared secret header, and carries
    # the proxy's verify + subject-DN headers. Any of them unset = every portal
    # route returns 503. The header NAMES are configurable because they belong
    # to the operator's existing nginx vhost, not to HomePilot.
    portal_cn_header: str = "ssl-client-subject-dn"
    portal_verify_header: str = "ssl-client-verify"
    portal_trusted_proxy: str = ""
    portal_proxy_secret: str = ""
    # Public origin of the mTLS vhost, used ONLY to print a complete invite URL
    # at mint time. Nothing at request time depends on it.
    portal_base_url: str = ""

    trusted_proxies: str = ""
    cors_origins: str = (
        "http://localhost:5173,http://localhost:4173,http://127.0.0.1:5173,http://127.0.0.1:4173"
    )
    cookie_secure: bool = True

    def _auto_generate_passphrase(self) -> str:
        import logging
        import secrets as secrets_mod

        logger = logging.getLogger(__name__)
        passphrase_path = Path(self.data_dir) / ".vault_passphrase"
        try:
            if passphrase_path.exists():
                passphrase = passphrase_path.read_text().strip()
                if passphrase:
                    logger.info("Vault passphrase loaded from %s", passphrase_path)
                    return passphrase
            passphrase = secrets_mod.token_urlsafe(32)
            passphrase_path.parent.mkdir(parents=True, exist_ok=True)
            passphrase_path.write_text(passphrase)
            passphrase_path.chmod(0o600)
            logger.info(
                "No HP_VAULT_PASSPHRASE — auto-generated and saved to %s",
                passphrase_path,
            )
            return passphrase
        except OSError:
            passphrase = secrets_mod.token_urlsafe(32)
            logger.warning(
                "No HP_VAULT_PASSPHRASE — auto-generated (could not persist to %s). "
                "Passphrase will change on restart.",
                passphrase_path,
            )
            return passphrase

    def _auto_generate_hub_token(self) -> str:
        """The shared agent-hub token, persisted next to the other generated
        material so it survives restarts.

        Same shape as ``_auto_generate_passphrase``: reuse the persisted value
        if one exists, else mint and persist one. A token that changed on restart
        would invalidate every pending enrolment one-liner."""
        import logging
        import secrets as secrets_mod

        logger = logging.getLogger(__name__)
        token_path = Path(self.data_dir) / ".agent_hub_token"
        try:
            if token_path.exists():
                token = token_path.read_text().strip()
                if token:
                    logger.info("Agent hub token loaded from %s", token_path)
                    return token
            token = secrets_mod.token_urlsafe(32)
            token_path.parent.mkdir(parents=True, exist_ok=True)
            token_path.write_text(token)
            token_path.chmod(0o600)
            logger.info(
                "No HP_AGENT_HUB_AUTH_TOKEN - auto-generated and saved to %s",
                token_path,
            )
            return token
        except OSError:
            token = secrets_mod.token_urlsafe(32)
            logger.warning(
                "No HP_AGENT_HUB_AUTH_TOKEN - auto-generated (could not persist to %s). "
                "Enrolled agents will have to re-enroll after a restart.",
                token_path,
            )
            return token

    def _try_vault_secret(self, name: str) -> str:
        """Attempt to load a secret from the vault.

        Uses a best-effort approach: if called inside a running event loop
        (e.g., during Settings init via lru_cache), the async vault call
        cannot run directly, so we dispatch it via a thread pool executor.
        This avoids the ``asyncio.run()`` crash when Settings is created
        inside an already-running event loop.
        """
        try:
            import asyncio
            import concurrent.futures

            from .vault import VaultManager

            vault_dir = Path(self.vault_dir) if self.vault_dir else Path(self.data_dir) / "vault"
            if not vault_dir.exists():
                return ""
            passphrase = self.vault_passphrase
            if not passphrase and self.vault_passphrase_file:
                passphrase = Path(self.vault_passphrase_file).read_text().strip()
            if not passphrase:
                return ""
            vault = VaultManager(Path(self.data_dir), passphrase)
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop is not None:
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    future = pool.submit(asyncio.run, vault.get_secret(name))
                secret = future.result(timeout=5)
            else:
                secret = asyncio.run(vault.get_secret(name))
            for key in ("value", "secret", "key", "token"):
                if secret.get(key):
                    return str(secret[key])
            if secret:
                first_val = next(iter(secret.values()), "")
                if isinstance(first_val, str) and first_val:
                    return first_val
            return ""
        except FileNotFoundError:
            # Genuinely "not configured": there is no such secret.
            return ""
        except Exception as exc:
            # A bare `except: return ""` made a WRONG PASSPHRASE or a corrupt
            # identity indistinguishable from "not configured" (#431), so
            # admin_secret came back silently empty and the operator saw a 403
            # with nothing in the log to explain it. Still best-effort - config
            # must load - but no longer silent.
            import logging as _logging

            _logging.getLogger(__name__).warning(
                "vault lookup for %r failed (%s: %s); continuing without it",
                name,
                type(exc).__name__,
                exc,
            )
            return ""

    def model_post_init(self, __context: object) -> None:
        import logging
        import os as _os

        logger = logging.getLogger(__name__)

        if _os.environ.get("HP_AUTO_APPLY_MUTATING"):
            logger.warning(
                "HP_AUTO_APPLY_MUTATING is no longer supported. "
                "All mutating artifacts require explicit approval via 'hp artifacts approve'."
            )

        if not self.vault_dir:
            self.vault_dir = str(Path(self.data_dir) / "vault")

        # ── Resolve vault passphrase FIRST (vault reads depend on it) ──
        if not self.vault_passphrase and not self.vault_passphrase_file:
            # Self-generating is the DEFAULT (ADR-004): the operator supplies a
            # Proxmox address and token and nothing else, and that token needs a
            # vault to live in. An install that silently has no vault cannot
            # store the one secret it was given. Production still refuses to
            # invent key material: there the passphrase must be supplied,
            # because an auto-generated one that lives only on that host is not
            # a credential anyone can restore.
            # An empty value means "unset", not "off": `HP_VAULT_AUTO_INIT=` is
            # how a .env spells a variable it does not care about, and reading
            # that as opt-out would silently disable the vault on a stock file.
            # Only an explicit falsy value turns it off.
            raw_auto_init = _os.environ.get("HP_VAULT_AUTO_INIT", "").strip().lower()
            auto_init = raw_auto_init not in ("0", "false", "no")
            if auto_init and self.env != "production":
                self.vault_passphrase = self._auto_generate_passphrase()
            elif self.env == "production":
                logger.warning(
                    "HP_ENV=production and no vault passphrase set — vault disabled. "
                    "Set HP_VAULT_PASSPHRASE or HP_VAULT_PASSPHRASE_FILE."
                )
            else:
                logger.info("Vault passphrase not set and HP_VAULT_AUTO_INIT=0 — vault disabled")
        else:
            if self.vault_passphrase:
                logger.info("Vault passphrase loaded from HP_VAULT_PASSPHRASE env var")
            if self.vault_passphrase_file:
                try:
                    self.vault_passphrase = Path(self.vault_passphrase_file).read_text().strip()
                except FileNotFoundError as exc:
                    raise ConfigError(
                        f"HP_VAULT_PASSPHRASE_FILE not found: {self.vault_passphrase_file}"
                    ) from exc

        # ── Resolve secrets: vault → env ──
        if not self.admin_secret:
            _admin = self._try_vault_secret("admin-secret")
            if _admin:
                self.admin_secret = _admin
                logger.info("HP_ADMIN_SECRET loaded from vault")

        # The hub's shared token is fleet material, not an operator decision:
        # vault first (where it can be rotated), else a persisted file.
        if self.agent_hub_enabled and not self.agent_hub_auth_token:
            _vault_hub_token = self._try_vault_secret("agent-hub-token")
            if _vault_hub_token:
                self.agent_hub_auth_token = _vault_hub_token
                logger.info("HP_AGENT_HUB_AUTH_TOKEN loaded from vault")
            else:
                self.agent_hub_auth_token = self._auto_generate_hub_token()


@lru_cache
def get_settings() -> Settings:
    return Settings()
