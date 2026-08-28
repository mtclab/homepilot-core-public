"""Where the Proxmox address actually comes from.

`settings.proxmox_host` is only the ENVIRONMENT half of the answer. An install
claimed by the web UI stores its hypervisor in the vault under
'proxmox-config', and that secret WINS over the environment. Three places
resolved this independently and a fourth (the self-check) and a fifth (`hp
status`) did not resolve it at all - so a vault-configured install was told
"No hypervisor is configured, so inventory stays empty and guest provisioning
is unavailable" and "PVE host: (not configured)" while it was listing nine
inventory items and provisioning guests off that very address (found live on
dev 3.6.9).

One resolver, so a new reader cannot get a different answer than the client
that is doing the work.
"""

from __future__ import annotations

import logging
from typing import Any, NamedTuple

logger = logging.getLogger(__name__)

# The vault key the claim flow writes the hypervisor under. Named VAULT_KEY,
# not SECRET_NAME: it is a lookup key, and bandit reads a name ending in
# "SECRET" bound to a string literal as a hardcoded password (B105).
VAULT_KEY = "proxmox-config"


class ProxmoxConfig(NamedTuple):
    host: str
    port: int
    verify_ssl: bool


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in ("true", "1", "yes")
    return default


async def resolve_proxmox_config(settings: Any, vault: Any) -> ProxmoxConfig:
    """The Proxmox connection in force: environment defaults, vault overrides.

    Never raises: a locked, empty or broken vault leaves the environment's
    answer standing, which is what every caller did by hand before.
    """
    host = (getattr(settings, "proxmox_host", "") or "") if settings else ""
    port = (getattr(settings, "proxmox_port", 8006) or 8006) if settings else 8006
    verify_ssl = getattr(settings, "proxmox_verify_ssl", True) if settings else True

    if vault is not None:
        try:
            secret = await vault.get_secret(VAULT_KEY)
        except Exception:
            logger.debug("Vault '%s' unavailable, using env defaults", VAULT_KEY, exc_info=True)
        else:
            if secret:
                host = secret.get("host", host) or host
                port = secret.get("port", port) or port
                verify_ssl = _as_bool(secret.get("verify_ssl", verify_ssl), verify_ssl)

    return ProxmoxConfig(host=str(host).strip(), port=int(port), verify_ssl=bool(verify_ssl))
