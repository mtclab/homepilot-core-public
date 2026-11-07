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
import os
import re
from collections.abc import Mapping
from typing import Any, NamedTuple

logger = logging.getLogger(__name__)

# The vault key the claim flow writes the hypervisor under. Named VAULT_KEY,
# not SECRET_NAME: it is a lookup key, and bandit reads a name ending in
# "SECRET" bound to a string literal as a hardcoded password (B105).
VAULT_KEY = "proxmox-config"


# The one shape of a PVE API token: `user@realm!tokenid=secret`. ONE rule for
# every reader (#715): the client, the SDN/firewall gateway, the settings report
# and the save path used three, and so disagreed about which credential was in
# force.
_PVE_TOKEN_SHAPE = re.compile(r"^[^@\s]+@[^!\s]+![^=\s]+=\S+$")

# Where the tokens live (named *_SLOT/*_VAR, not *_TOKEN: bandit reads a
# name like that bound to a string literal as a hardcoded password, B105).
READ_SLOT = "pve-token"
WRITE_SLOT = "pve-write-token"
ENV_VAR = "PVE_API_TOKEN"


def is_pve_token(value: Any) -> bool:
    """True when `value` has the shape of a PVE API token."""
    return isinstance(value, str) and bool(_PVE_TOKEN_SHAPE.match(value.strip()))


class ProxmoxTokens(NamedTuple):
    """The credentials in force, where each came from, and what was refused.

    `token_source` is "vault", "env" or "" (none). `write_source` is "vault",
    "read_token" (no usable write token, so the read one answers for writes -
    the same fallback every consumer makes) or "". `rejected` names each
    stored slot that held something that is NOT a token, so a report can say
    so instead of claiming a credential it will not use.
    """

    token: str
    token_source: str
    write_token: str
    write_source: str
    rejected: tuple[str, ...]


async def _vault_token(vault: Any, key: str) -> str:
    if vault is None:
        return ""
    try:
        secret = await vault.get_secret(key)
    except Exception:
        logger.debug("Vault '%s' unavailable", key, exc_info=True)
        return ""
    if not isinstance(secret, dict):
        return ""
    return str(secret.get("token", "") or "").strip()


async def resolve_proxmox_tokens(
    vault: Any, environ: Mapping[str, str] | None = None
) -> ProxmoxTokens:
    """The Proxmox tokens in force - the ONE resolver (#715).

    Read token: the vault's, when it is a token; else the environment's, when
    it is one. Write token: the vault's, when it is a token; else the read
    token. A stored value that is not a token is never used - by anything -
    and is named in `rejected`. Never raises.
    """
    env = os.environ if environ is None else environ
    rejected: list[str] = []

    token, token_source = "", ""
    stored = await _vault_token(vault, READ_SLOT)
    if stored:
        if is_pve_token(stored):
            token, token_source = stored, "vault"
        else:
            rejected.append(READ_SLOT)
            logger.warning("The stored %s is not a PVE token - ignoring it", READ_SLOT)
    if not token:
        from_env = str(env.get(ENV_VAR, "") or "").strip()
        if from_env:
            if is_pve_token(from_env):
                token, token_source = from_env, "env"
            else:
                rejected.append(ENV_VAR)
                logger.warning("%s is not a PVE token - ignoring it", ENV_VAR)

    write_token, write_source = "", ""
    stored_write = await _vault_token(vault, WRITE_SLOT)
    if stored_write:
        if is_pve_token(stored_write):
            write_token, write_source = stored_write, "vault"
        else:
            rejected.append(WRITE_SLOT)
            logger.warning(
                "The stored %s is not a PVE token - writes use the read token; "
                "re-save Proxmox settings to repair it",
                WRITE_SLOT,
            )
    if not write_token and token:
        write_token, write_source = token, "read_token"

    return ProxmoxTokens(token, token_source, write_token, write_source, tuple(rejected))


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
