from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Request

from ..auth.deps import require_scope

logger = logging.getLogger(__name__)

router = APIRouter(tags=["admin"])

_reload_lock = asyncio.Lock()
_proxmox_version: int = 0

_require_admin_dep = Depends(require_scope("admin"))


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
        global _proxmox_version

        vault = getattr(request.app.state, "vault", None)
        if vault is None:
            return {
                "status": "error",
                "message": "Vault is not configured",
                "reloaded": [],
                "timestamp": datetime.now(UTC).isoformat(),
            }

        reloaded: list[str] = []
        settings = request.app.state.settings if hasattr(request.app.state, "settings") else None
        proxmox_host = getattr(settings, "proxmox_host", "") if settings else ""
        proxmox_port = getattr(settings, "proxmox_port", 8006) if settings else 8006
        proxmox_verify_ssl = getattr(settings, "proxmox_verify_ssl", True) if settings else True

        if proxmox_host:
            from ..vault import VaultError

            new_token = ""
            new_source = ""
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

            old_proxmox = getattr(request.app.state, "proxmox", None)
            old_token_source = getattr(request.app.state, "pve_token_source", "")
            token_changed = False

            if new_token:
                if old_proxmox is not None:
                    old_token = getattr(old_proxmox, "_token", None)
                    if old_token != new_token:
                        token_changed = True
                else:
                    token_changed = True

                if token_changed or old_proxmox is None:
                    from ..adapters.proxmox import ProxmoxClient

                    base_url = f"https://{proxmox_host}:{proxmox_port}"
                    new_proxmox = ProxmoxClient(
                        base_url=base_url,
                        token=new_token,
                        verify_ssl=proxmox_verify_ssl,
                    )

                    try:
                        await new_proxmox.read("/version")
                    except Exception as exc:
                        logger.error("New ProxmoxClient validation failed: %s", exc, exc_info=True)
                        await new_proxmox.close()
                        return {
                            "status": "error",
                            "message": f"New Proxmox client validation failed: {exc}",
                            "timestamp": datetime.now(UTC).isoformat(),
                        }

                    if old_proxmox is not None:
                        try:
                            await old_proxmox.close()
                        except (OSError, httpx.HTTPError):
                            logger.debug("Error closing old ProxmoxClient", exc_info=True)

                    request.app.state.proxmox = new_proxmox
                    _proxmox_version += 1
                    request.app.state.proxmox_version = _proxmox_version

                    if (
                        hasattr(request.app.state, "inventory_service")
                        and request.app.state.inventory_service is not None
                    ):
                        request.app.state.inventory_service.proxmox = new_proxmox

                    if (
                        hasattr(request.app.state, "artifact_executor")
                        and request.app.state.artifact_executor is not None
                    ):
                        request.app.state.artifact_executor.proxmox = new_proxmox

                    request.app.state.artifact_lifecycle._executor_ref.proxmox = new_proxmox

                    try:
                        from ..mcp.server import _server_context

                        await _server_context.set("proxmox", new_proxmox)
                    except (ImportError, OSError, AttributeError) as e:
                        logger.warning("Failed to update MCP server context: %s", e)

                    logger.info(
                        "Proxmox client reloaded (token from %s, changed=%s, version=%d)",
                        new_source,
                        token_changed,
                        _proxmox_version,
                    )

                reloaded.append("pve-token")
                if new_source != old_token_source:
                    request.app.state.pve_token_source = new_source
                    reloaded.append("pve-token-source")

        logger.info(
            "Secrets reload completed: reloaded=%s, by=%s",
            reloaded,
            token.get("prefix", "unknown"),
        )

        return {
            "status": "ok",
            "reloaded": reloaded,
            "timestamp": datetime.now(UTC).isoformat(),
        }
