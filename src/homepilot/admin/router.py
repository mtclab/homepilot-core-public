from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from ..auth.deps import require_scope
from ..config import get_settings
from ..selfcheck import selfcheck_report

logger = logging.getLogger(__name__)

router = APIRouter(tags=["admin"])

_reload_lock = asyncio.Lock()
_proxmox_version: int = 0

_require_admin_dep = Depends(require_scope("admin"))
# Reads an operator should see without admin (owner decision, wave 3): the
# selfcheck report and the (token-redacted) Proxmox wiring expose no secret. The
# PUT/test/reload routes below stay admin.
_require_read_dep = Depends(require_scope("read"))


class ProxmoxConfigIn(BaseModel):
    host: str | None = None
    port: int | None = None
    verify_ssl: bool | None = None
    token: str | None = None
    write_token: str | None = None


class ProxmoxConfigOut(BaseModel):
    host: str = ""
    port: int = 8006
    verify_ssl: bool = True
    token_configured: bool = False
    token_source: str = ""
    write_token_configured: bool = False
    write_token_source: str = ""
    connection_status: str = "not_configured"


async def _resolve_proxmox_config(state: Any) -> tuple[str, int, bool]:
    settings = getattr(state, "settings", None)
    host = getattr(settings, "proxmox_host", "") if settings else ""
    port = getattr(settings, "proxmox_port", 8006) if settings else 8006
    verify_ssl = getattr(settings, "proxmox_verify_ssl", True) if settings else True
    vault = getattr(state, "vault", None)
    if vault is not None:
        try:
            config_secret = await vault.get_secret("proxmox-config")
            if config_secret:
                host = config_secret.get("host", host) or host
                port = config_secret.get("port", port) or port
                verify_ssl = config_secret.get("verify_ssl", verify_ssl)
                if isinstance(verify_ssl, str):
                    verify_ssl = verify_ssl.lower() in ("true", "1", "yes")
        except Exception:
            logger.debug("Vault 'proxmox-config' unavailable, using defaults", exc_info=True)
    return host, port, verify_ssl


async def _resolve_proxmox_token(state: Any) -> tuple[str, str]:
    token = ""
    source = ""
    vault = getattr(state, "vault", None)
    if vault is not None:
        from ..vault import VaultError

        try:
            pve_secret = await vault.get_secret("pve-token")
            token = pve_secret.get("token", "")
            if token:
                source = "vault"
        except (VaultError, OSError):
            pass
    if not token:
        env_token = os.environ.get("PVE_API_TOKEN", "")
        if env_token:
            token = env_token
            source = "env"
    return token, source


async def _resolve_proxmox_write_token(state: Any) -> tuple[str, str]:
    write_token = ""
    source = ""
    vault = getattr(state, "vault", None)
    if vault is not None:
        from ..vault import VaultError

        try:
            secret = await vault.get_secret("pve-write-token")
            write_token = secret.get("token", "")
            if write_token:
                source = "vault"
        except (VaultError, OSError):
            pass
    if not write_token:
        read_token, _ = await _resolve_proxmox_token(state)
        if read_token:
            write_token = read_token
            source = "read_token"
    return write_token, source


@router.get("/selfcheck")
async def selfcheck(
    request: Request,
    token: dict[str, Any] = _require_read_dep,
) -> dict[str, Any]:
    """What each optional subsystem is doing, and what that costs (ADR-004 S6).

    A SIBLING of /health rather than an extension of it, for three reasons:
    /health is public and this report names the addresses an instance is wired
    to; /health is the container's liveness probe every 30s and must not grow
    outbound network probes; and /health's flat check map is a contract the UI and
    its tests already depend on. The report is computed per request so it
    describes now, not boot - the boot copy goes to the log.
    """
    settings = getattr(request.app.state, "settings", None) or get_settings()
    return await selfcheck_report(request.app.state, settings)


async def proxmox_settings_report(state: Any) -> dict[str, Any]:
    """How this instance is wired to Proxmox, and whether that wiring works.

    Takes the state object rather than a Request so the ONE implementation
    serves both GET /admin/settings/proxmox and the `get_proxmox_settings` MCP
    tool - two surfaces answering the same question cannot be allowed to drift.

    NO TOKEN VALUE IS EVER RETURNED. The tokens are resolved only to answer
    "configured?" and "from where?"; the secrets themselves stay in the vault,
    and tests/test_mcp_read_parity.py asserts a configured token's value is
    absent from the serialized result.
    """
    host, port, verify_ssl = await _resolve_proxmox_config(state)
    token, source = await _resolve_proxmox_token(state)
    write_token, write_source = await _resolve_proxmox_write_token(state)
    proxmox = getattr(state, "proxmox", None)
    if proxmox is not None:
        try:
            connected = await proxmox.test_connection()
            status = "ok" if connected else "unreachable"
        except Exception:
            status = "error"
    elif host:
        status = "unreachable"
    else:
        status = "not_configured"
    has_separate_write = write_token != token if (token and write_token) else False
    return {
        "host": host,
        "port": port,
        "verify_ssl": verify_ssl,
        "token_configured": bool(token),
        "token_source": source if token else "",
        "write_token_configured": bool(write_token),
        "write_token_source": write_source if write_token else "",
        "write_token_is_separate": has_separate_write,
        "connection_status": status,
    }


@router.get("/settings/proxmox", dependencies=[_require_read_dep])
async def get_proxmox_settings(request: Request) -> dict[str, Any]:
    return await proxmox_settings_report(request.app.state)


@router.put("/settings/proxmox", dependencies=[Depends(require_scope("admin"))])
async def save_proxmox_settings(request: Request, config: ProxmoxConfigIn) -> dict[str, Any]:
    vault = getattr(request.app.state, "vault", None)
    if vault is None:
        return {"status": "error", "message": "Vault not configured — cannot save settings"}

    host, port, verify_ssl = await _resolve_proxmox_config(request.app.state)
    if config.host is not None:
        host = config.host
    if config.port is not None:
        port = config.port
    if config.verify_ssl is not None:
        verify_ssl = config.verify_ssl

    await vault.store_secret(
        "proxmox-config",
        {
            "host": host,
            "port": port,
            "verify_ssl": verify_ssl,
        },
    )

    if config.token is not None:
        await vault.store_secret("pve-token", {"token": config.token})

    if config.write_token is not None:
        await vault.store_secret("pve-write-token", {"token": config.write_token})

    reload_result = await _do_reload(request)
    reloaded = reload_result.get("reloaded", [])

    # Resolve actual token state from vault for response
    resolved_token, _ = await _resolve_proxmox_token(request.app.state)
    resolved_write_token, _ = await _resolve_proxmox_write_token(request.app.state)

    return {
        "status": "ok",
        "reloaded": reloaded,
        "host": host,
        "port": port,
        "verify_ssl": verify_ssl,
        "token_configured": bool(resolved_token),
        "write_token_configured": bool(resolved_write_token),
    }


async def probe_proxmox_connection(state: Any, config: ProxmoxConfigIn) -> dict[str, Any]:
    """Build a Proxmox client from the stored wiring (with any supplied
    overrides) and probe /version, returning {status, message[, version]}.

    Takes a state object rather than a Request so the ONE implementation serves
    both POST /admin/settings/proxmox/test and the `test_proxmox_connection` MCP
    tool - the two surfaces must give the same verdict. Probes only; stores
    nothing and mutates no live client."""
    host, port, verify_ssl = await _resolve_proxmox_config(state)
    if config.host is not None:
        host = config.host
    if config.port is not None:
        port = config.port
    if config.verify_ssl is not None:
        verify_ssl = config.verify_ssl

    if not host:
        return {"status": "error", "message": "No Proxmox host configured"}

    token = config.token or ""
    if not token:
        token, _source = await _resolve_proxmox_token(state)
    if not token:
        return {"status": "error", "message": "No API token provided or stored"}

    write_token = config.write_token or ""
    if not write_token:
        write_token_raw, _ = await _resolve_proxmox_write_token(state)
        write_token = write_token_raw or token

    from ..adapters.proxmox import ProxmoxClient

    base_url = f"https://{host}:{port}"
    client = ProxmoxClient(
        base_url=base_url, token=token, verify_ssl=verify_ssl, write_token=write_token
    )
    try:
        version_info = await client.read("/version")
        await client.close()
        return {"status": "ok", "message": "Connection successful", "version": version_info}
    except Exception as exc:
        with contextlib.suppress(Exception):
            await client.close()
        return {"status": "error", "message": f"Connection failed: {exc}"}


@router.post("/settings/proxmox/test", dependencies=[Depends(require_scope("admin"))])
async def test_proxmox_settings(request: Request, config: ProxmoxConfigIn) -> dict[str, Any]:
    return await probe_proxmox_connection(request.app.state, config)


async def _do_reload(request: Request) -> dict[str, Any]:
    global _proxmox_version
    settings = request.app.state.settings if hasattr(request.app.state, "settings") else None
    proxmox_host, proxmox_port, proxmox_verify_ssl = await _resolve_proxmox_config(
        request.app.state
    )
    if settings:
        settings.proxmox_host = proxmox_host
        settings.proxmox_port = proxmox_port
        settings.proxmox_verify_ssl = proxmox_verify_ssl

    reloaded: list[str] = []

    vault = getattr(request.app.state, "vault", None)
    if vault is None:
        return {"status": "error", "message": "Vault not configured", "reloaded": reloaded}

    if settings and not getattr(settings, "admin_secret", ""):
        from ..vault import VaultError

        try:
            admin_data = await vault.get_secret("admin-secret")
            admin_val = admin_data.get("secret", "") or admin_data.get("value", "")
            if not admin_val:
                for v in admin_data.values():
                    if isinstance(v, str) and v:
                        admin_val = v
                        break
            if admin_val:
                settings.admin_secret = admin_val
                reloaded.append("admin-secret")
        except (VaultError, OSError):
            logger.debug("Vault 'admin-secret' unavailable during reload", exc_info=True)
    if not reloaded or "admin-secret" not in reloaded:
        admin_env = os.environ.get("HP_ADMIN_SECRET", "")
        if admin_env and settings and not getattr(settings, "admin_secret", ""):
            settings.admin_secret = admin_env
            reloaded.append("admin-secret")

    new_token = ""
    new_source = ""
    if proxmox_host:
        from ..vault import VaultError

        try:
            pve_secret = await vault.get_secret("pve-token")
            new_token = pve_secret.get("token", "")
            if new_token:
                new_source = "vault"
        except (VaultError, OSError):
            logger.debug("Vault 'pve-token' unavailable during reload", exc_info=True)

        if not new_token:
            env_token = os.environ.get("PVE_API_TOKEN", "")
            if env_token:
                new_token = env_token
                new_source = "env"

        new_write_token = ""
        new_write_source = ""
        try:
            write_secret = await vault.get_secret("pve-write-token")
            new_write_token = write_secret.get("token", "")
            if new_write_token:
                new_write_source = "vault"
        except (VaultError, OSError):
            logger.debug("Vault 'pve-write-token' unavailable during reload", exc_info=True)

        old_proxmox = getattr(request.app.state, "proxmox", None)
        old_token = getattr(old_proxmox, "_token", None) if old_proxmox else None
        old_write_token = getattr(old_proxmox, "_write_token", None) if old_proxmox else None
        token_changed = False

        if new_token:
            if (old_proxmox is not None and old_token != new_token) or old_proxmox is None:
                token_changed = True

            write_changed = (
                new_write_token != old_write_token
                if new_write_token and old_write_token
                else bool(new_write_token) != bool(old_write_token)
            )
            if token_changed or write_changed or old_proxmox is None:
                from ..adapters.proxmox import ProxmoxClient

                effective_write_token = new_write_token or new_token
                base_url = f"https://{proxmox_host}:{proxmox_port}"
                new_proxmox = ProxmoxClient(
                    base_url=base_url,
                    token=new_token,
                    verify_ssl=proxmox_verify_ssl,
                    write_token=effective_write_token,
                )
                try:
                    await new_proxmox.read("/version")
                except Exception as exc:
                    logger.error("New ProxmoxClient validation failed: %s", exc, exc_info=True)
                    await new_proxmox.close()
                    if old_proxmox is not None:
                        with contextlib.suppress(OSError, httpx.HTTPError):
                            await old_proxmox.close()
                    return {
                        "status": "error",
                        "message": f"Proxmox client validation failed: {exc}",
                        "reloaded": reloaded,
                    }

                if old_proxmox is not None:
                    try:
                        await old_proxmox.close()
                    except (OSError, httpx.HTTPError):
                        logger.debug("Error closing old ProxmoxClient", exc_info=True)

                request.app.state.proxmox = new_proxmox
                _proxmox_version += 1
                request.app.state.proxmox_version = _proxmox_version

                inventory_service = getattr(request.app.state, "inventory_service", None)
                if inventory_service is not None:
                    inventory_service.proxmox = new_proxmox
                artifact_executor = getattr(request.app.state, "artifact_executor", None)
                if artifact_executor is not None:
                    artifact_executor.proxmox = new_proxmox
                provision_service = getattr(request.app.state, "provision_service", None)
                if provision_service is not None:
                    provision_service.proxmox = new_proxmox
                enroll_service = getattr(request.app.state, "agent_enroll_service", None)
                if enroll_service is not None:
                    enroll_service.proxmox = new_proxmox
                # `_executor_ref` defaults to None (lifecycle.py:33) and is only
                # assigned inside main.py's `if mcp_token and state.proxmox and
                # state.vault` block, so on a fresh install it is still None here.
                # Assigning through it raised AttributeError -> 500 on the primary
                # "Settings -> paste Proxmox token -> Save" flow, and it raised
                # AFTER the client swap and version bump, so the UI reported
                # failure on a partly-applied change (#388). Guarded like the two
                # lookups above it.
                lifecycle = getattr(request.app.state, "artifact_lifecycle", None)
                executor_ref = getattr(lifecycle, "_executor_ref", None)
                if executor_ref is not None:
                    executor_ref.proxmox = new_proxmox
                try:
                    from ..mcp.server import _server_context

                    await _server_context.set("proxmox", new_proxmox)
                except (ImportError, OSError, AttributeError) as e:
                    logger.warning("Failed to update MCP server context: %s", e)

                logger.info(
                    "Proxmox reloaded (token from %s, write_token=%s, changed=%s, version=%d)",
                    new_source,
                    new_write_source or "same-as-read",
                    token_changed,
                    _proxmox_version,
                )

            reloaded.append("pve-token")
            if new_source != getattr(request.app.state, "pve_token_source", ""):
                request.app.state.pve_token_source = new_source
                reloaded.append("pve-token-source")

    logger.info("Secrets reload completed: reloaded=%s", reloaded)
    return {"status": "ok", "reloaded": reloaded}


@router.post("/reload-secrets")
async def reload_secrets(
    request: Request,
    token: dict[str, Any] = _require_admin_dep,
) -> dict[str, Any]:
    if _reload_lock.locked():
        return {
            "status": "already_running",
            "message": "Secret reload is already in progress",
            "timestamp": datetime.now(UTC).isoformat(),
        }

    async with _reload_lock:
        result = await _do_reload(request)
        result["timestamp"] = datetime.now(UTC).isoformat()
        return result
