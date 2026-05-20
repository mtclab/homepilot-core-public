from __future__ import annotations

import logging
import os
import secrets
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic_settings import BaseSettings

_logger = logging.getLogger(__name__)


class ConfigError(ValueError):
    pass


def _env_files() -> list[str]:
    candidates = [
        str(Path.home() / ".hp" / ".env"),
        str(Path(os.environ.get("HP_DATA_DIR", str(Path.home() / ".hp"))) / ".env"),
        ".env",
    ]
    files = [f for f in dict.fromkeys(candidates) if Path(f).exists()]
    if files:
        _logger.debug("Loading config from: %s", ", ".join(files))
    return files


class Settings(BaseSettings):
    model_config = {"env_prefix": "HP_", "env_file": _env_files()}

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
    vault_passphrase: str | None = None
    vault_passphrase_file: str = ""

    jump_server_host: str = "jumpserver"
    jump_server_port: int = 50051
    jump_server_tls: bool = False
    jump_server_tls_ca: str = ""
    jump_server_tls_cert: str = ""
    jump_server_tls_key: str = ""

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

    daemon_host: str = "0.0.0.0"
    daemon_port: int = 8000
    log_level: str = "info"
    trusted_proxies: str = ""
    cors_origins: str = (
        "http://localhost:5173,http://localhost:4173,http://127.0.0.1:5173,http://127.0.0.1:4173"
    )
    cookie_secure: bool = True
    rate_limit_backend: str = "memory"

    def _auto_generate_passphrase(self, secrets_mod: Any, logger: logging.Logger) -> str:
        passphrase_path = Path(self.data_dir) / ".vault_passphrase"
        try:
            if passphrase_path.exists():
                passphrase = passphrase_path.read_text().strip()
                if passphrase:
                    logger.info("Vault passphrase loaded from %s", passphrase_path)
                    return passphrase
            passphrase = str(secrets_mod.token_urlsafe(32))
            passphrase_path.parent.mkdir(parents=True, exist_ok=True)
            passphrase_path.write_text(passphrase)
            passphrase_path.chmod(0o600)
            logger.info(
                "No HP_VAULT_PASSPHRASE — auto-generated and saved to %s",
                passphrase_path,
            )
            return passphrase
        except OSError:
            passphrase = str(secrets_mod.token_urlsafe(32))
            logger.warning(
                "No HP_VAULT_PASSPHRASE — auto-generated (could not persist to %s). "
                "Passphrase will change on restart.",
                passphrase_path,
            )
            return passphrase

    def _auto_generate_secret_key(
        self, secrets_mod: Any, logger: logging.Logger
    ) -> tuple[str, bool]:
        persisted_key_path = Path(self.data_dir) / ".secret_key"
        try:
            if persisted_key_path.exists():
                key = persisted_key_path.read_text().strip()
                logger.warning(
                    "HP_SECRET_KEY not set — loaded persisted key from %s",
                    persisted_key_path,
                )
                return key, False
            key = str(secrets_mod.token_urlsafe(32))
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
            key = str(secrets_mod.token_urlsafe(32))
            logger.warning(
                "HP_SECRET_KEY not set — auto-generated (could not persist to %s). "
                "Key will change on restart.",
                persisted_key_path,
            )
            return key, True

    def _try_vault_secret(self, name: str) -> str:
        try:
            from .vault import VaultManager

            vault_dir = Path(self.vault_dir) if self.vault_dir else Path(self.data_dir) / "vault"
            if not vault_dir.exists():
                return ""
            passphrase = self.vault_passphrase or ""
            if not passphrase and self.vault_passphrase_file:
                passphrase = Path(self.vault_passphrase_file).read_text().strip()
            if not passphrase:
                return ""
            vault = VaultManager(Path(self.data_dir), passphrase)
            secret = __import__("asyncio").run(vault.get_secret(name))
            for key in ("value", "secret", "key", "token"):
                val: Any = secret.get(key)
                if val:
                    return str(val)
            if secret:
                first_val = next(iter(secret.values()), "")
                if isinstance(first_val, str) and first_val:
                    return first_val
            return ""
        except Exception:
            return ""

    def model_post_init(self, __context: object) -> None:
        logger = logging.getLogger(__name__)

        if os.environ.get("HP_AUTO_APPLY_MUTATING"):
            logger.warning(
                "HP_AUTO_APPLY_MUTATING is no longer supported. "
                "All mutating artifacts require explicit approval via 'hp artifacts approve'."
            )

        if not self.vault_dir:
            self.vault_dir = str(Path(self.data_dir) / "vault")
        if not self.ssh_key_dir:
            self.ssh_key_dir = str(Path(self.data_dir) / "ssh")

        # ── Resolve vault passphrase FIRST (vault reads depend on it) ──
        if self.vault_passphrase is None and not self.vault_passphrase_file:
            self.vault_passphrase = self._auto_generate_passphrase(secrets, logger)
        else:
            if self.vault_passphrase_file:
                try:
                    self.vault_passphrase = Path(self.vault_passphrase_file).read_text().strip()
                except FileNotFoundError as exc:
                    raise ConfigError(
                        f"HP_VAULT_PASSPHRASE_FILE not found: {self.vault_passphrase_file}"
                    ) from exc
            elif self.vault_passphrase:
                logger.debug("HP_VAULT_PASSPHRASE is set — using provided passphrase")

        # ── Resolve secrets: vault → file → auto-generate ──
        _secret_key_auto_generated = False

        if not self.secret_key:
            secret_key_file = os.environ.get("HP_SECRET_KEY_FILE", "")
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
                    self.secret_key, _secret_key_auto_generated = self._auto_generate_secret_key(
                        secrets, logger
                    )

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
