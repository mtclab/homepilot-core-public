from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
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
    files = [f for f in dict.fromkeys(candidates) if Path(f).exists()]
    if files:
        import logging as _logging

        _logging.getLogger(__name__).debug("Loading config from: %s", ", ".join(files))
    return files


class Settings(BaseSettings):
    model_config = {"env_prefix": "HP_", "env_file": _env_files(), "extra": "ignore"}

    data_dir: str = str(Path.home() / ".hp")
    artifacts_dir: str = str(Path.home() / ".hp" / "artifacts")
    artifacts_remote: str = ""
    artifacts_ssh_key: str = ""
    secret_key: str = ""
    secret_key_file: str = ""
    admin_secret: str = ""

    env: str = ""

    proxmox_host: str = ""
    proxmox_port: int = 8006
    proxmox_verify_ssl: bool = True

    vault_dir: str = ""
    vault_passphrase: str = ""
    vault_passphrase_file: str = ""

    ssh_key_dir: str = ""

    allowed_http_domains: str = ""

    events_webhook_url: str | None = None
    events_webhook_secret: str | None = None
    n8n_api_key: str = ""

    auto_approve_nonmutating: bool = True
    auto_apply_on_approve: bool = True

    embedding_service_url: str = "http://llm-embed:8081/v1/embeddings"
    embedding_model: str = "bge-m3"
    embedding_fallback_url: str = "http://localhost:11434/api/embeddings"
    embedding_fallback_model: str = "nomic-embed-text"

    inventory_interval_seconds: int = 300
    drift_interval_seconds: int = 1800
    auto_apply_enabled: bool = False
    auto_apply_interval_seconds: int = 300

    daemon_host: str = Field(default="0.0.0.0", validation_alias="HP_HOST")
    daemon_port: int = Field(default=8000, validation_alias="HP_PORT")
    log_level: str = "info"
    agent_hub_enabled: bool = False
    agent_hub_host: str = "0.0.0.0"
    agent_hub_port: int = 8443
    # Address agents should DIAL to reach the hub. agent_hub_host is the bind
    # address (often 0.0.0.0) and is not routable; behind a reverse proxy the
    # browser hostname isn't the raw-hub address either. Set this to the host
    # (optionally host:port) agents can reach the hub on.
    agent_hub_advertise_host: str = ""
    agent_hub_auth_token: str = ""
    agent_hub_heartbeat_interval: int = 30
    agent_hub_tls: bool = False
    agent_hub_tls_cert: str = ""
    agent_hub_tls_key: str = ""
    agent_hub_tls_ca: str = ""

    trusted_proxies: str = ""
    cors_origins: str = (
        "http://localhost:5173,http://localhost:4173,http://127.0.0.1:5173,http://127.0.0.1:4173"
    )
    cookie_secure: bool = True
    rate_limit_backend: str = "memory"

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

    def _auto_generate_secret_key(self) -> tuple[str, bool]:
        import logging
        import secrets as secrets_mod

        logger = logging.getLogger(__name__)
        persisted_key_path = Path(self.data_dir) / ".secret_key"
        try:
            if persisted_key_path.exists():
                key = persisted_key_path.read_text().strip()
                logger.warning(
                    "HP_SECRET_KEY not set — loaded persisted key from %s",
                    persisted_key_path,
                )
                return key, False
            key = secrets_mod.token_urlsafe(32)
            persisted_key_path.parent.mkdir(parents=True, exist_ok=True)
            persisted_key_path.write_text(key)
            persisted_key_path.chmod(0o600)
            logger.warning(
                "HP_SECRET_KEY not set — auto-generated and persisted to %s. "
                "Set HP_SECRET_KEY or HP_SECRET_KEY_FILE in .env for stable key.",
                persisted_key_path,
            )
            return key, True
        except OSError:
            key = secrets_mod.token_urlsafe(32)
            logger.warning(
                "HP_SECRET_KEY not set — auto-generated (could not persist to %s). "
                "Key will change on restart.",
                persisted_key_path,
            )
            return key, True

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
        except Exception:
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
        if not self.ssh_key_dir:
            self.ssh_key_dir = str(Path(self.data_dir) / "ssh")

        # ── Resolve vault passphrase FIRST (vault reads depend on it) ──
        if not self.vault_passphrase and not self.vault_passphrase_file:
            auto_init = _os.environ.get("HP_VAULT_AUTO_INIT", "").lower() in ("1", "true", "yes")
            if auto_init:
                self.vault_passphrase = self._auto_generate_passphrase()
            else:
                logger.info(
                    "Vault passphrase not set and HP_VAULT_AUTO_INIT not enabled — vault disabled"
                )
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

        # ── Resolve secrets: vault → file → auto-generate ──
        _secret_key_auto_generated = False

        if not self.secret_key:
            secret_key_file = _os.environ.get("HP_SECRET_KEY_FILE", "")
            if secret_key_file:
                try:
                    self.secret_key = Path(secret_key_file).read_text().strip()
                except FileNotFoundError:
                    raise ConfigError(f"HP_SECRET_KEY_FILE not found: {secret_key_file}") from None
            else:
                _vault_key = self._try_vault_secret("secret-key")
                if _vault_key:
                    self.secret_key = _vault_key
                    logger.info("HP_SECRET_KEY loaded from vault")
                else:
                    self.secret_key, _secret_key_auto_generated = self._auto_generate_secret_key()

        if not self.admin_secret:
            _admin = self._try_vault_secret("admin-secret")
            if _admin:
                self.admin_secret = _admin
                logger.info("HP_ADMIN_SECRET loaded from vault")

        if self.env == "production" and _secret_key_auto_generated:
            raise ConfigError(
                "HP_SECRET_KEY or HP_SECRET_KEY_FILE must be set when HP_ENV=production. "
                "Auto-generated keys are not allowed in production."
            )


@lru_cache
def get_settings() -> Settings:
    return Settings()
