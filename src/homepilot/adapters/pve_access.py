"""Can the WRITE token actually provision? Asked without mutating anything (#624).

`test_proxmox_connection` and the settings probe read `/version` with the READ
token, so a write token that no longer authenticates - or never held the
privileges a clone needs - stayed invisible until the first mutating call. On
prod that call was a friend's first invite redemption: "connection ok",
`write_token_configured: true`, and every clone answered 401.

`ProxmoxClient.check_tokens` already proves each token AUTHENTICATES. This
module answers the other half: which privileges the write token holds. PVE's
`GET /access/permissions`, asked AS the token, returns its effective
privileges per path - a read, so a probe may make it.

Per the owner mandate recorded in ``pve_sdn`` (HomePilot does not re-implement
Proxmox endpoints), the call goes through the estate's ``proxmox_mcp`` library:
its ``MultiClient`` is built by the same ``multi_client_from`` factory, and the
read is made on the library's ELEVATED (write) client. The library's own
``check_permissions`` formats a string from the MONITOR token, which is neither
the credential under test nor structured - so, like pve_sdn's structured reads,
this uses the library's API surface directly. Upstream gap: a structured,
token-selectable permissions read.

Bounded honestly. A privilege present on SOME path is reported as held; that
it covers the exact template, storage and pool a provision will use is not
established, because only an attempt establishes that. A privilege present on
NO path is a certain failure, and that is what this exists to say early.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

# What a guest provision asks PVE to do with the write token: clone the
# template into a new id, write its config (disk, cpu, memory, network,
# cloud-init, options), size its disk on a datastore, and start/stop it.
PROVISION_PRIVILEGES: tuple[str, ...] = (
    "VM.Allocate",
    "VM.Clone",
    "VM.Config.Disk",
    "VM.Config.CPU",
    "VM.Config.Memory",
    "VM.Config.Network",
    "VM.Config.Cloudinit",
    "VM.Config.Options",
    "VM.PowerMgmt",
    "Datastore.AllocateSpace",
)


def _granted(permissions: Any) -> set[str]:
    """Every privilege the answer grants on any path (value truthy)."""
    held: set[str] = set()
    if isinstance(permissions, dict):
        for privs in permissions.values():
            if isinstance(privs, dict):
                held.update(str(name) for name, on in privs.items() if on)
    return held


def _status_of(exc: BaseException) -> int:
    """The HTTP status behind a library error, when there is one."""
    seen: BaseException | None = exc
    while seen is not None:
        code = getattr(seen, "status_code", None)
        if isinstance(code, int):
            return code
        seen = seen.__cause__
    return 0


async def probe_write_privileges(
    credentials: Any,
    *,
    client_factory: Callable[[Any], Any] | None = None,
) -> dict[str, Any]:
    """The write token's verdict: ``{status, detail, missing, separate}``.

    ``status`` is one of:

    * ``ok`` - authenticates and holds every provisioning privilege somewhere;
    * ``missing_privileges`` - authenticates, but ``missing`` are held nowhere;
    * ``refused`` - the cluster refused the token (stale, rotated, deleted);
    * ``unknown`` - the cluster could not be asked; nothing is claimed.

    Never raises, never mutates, never returns a token value.
    """
    from .pve_sdn import _usable_write_token, multi_client_from

    factory = client_factory or multi_client_from
    separate = False
    try:
        separate = _usable_write_token(credentials) != credentials.token
        client = factory(credentials)
        api = client.get_client(elevated=True)
        permissions = await client.safe_api_call(api.access.permissions.get, elevated=True)
    except Exception as exc:
        code = _status_of(exc)
        if code == 401 or type(exc).__name__ == "ProxmoxPermissionError":
            return {
                "status": "refused",
                "detail": (
                    "the cluster refused the write token"
                    + (f" ({code})" if code else "")
                    + " - every clone, config write and power action will fail; "
                    "re-save a current token in Settings"
                ),
                "missing": [],
                "separate": separate,
            }
        logger.warning("Could not read the write token's permissions: %s", type(exc).__name__)
        return {
            "status": "unknown",
            "detail": f"the write token's permissions could not be read ({type(exc).__name__})",
            "missing": [],
            "separate": separate,
        }
    held = _granted(permissions)
    missing = [p for p in PROVISION_PRIVILEGES if p not in held]
    if missing:
        return {
            "status": "missing_privileges",
            "detail": (
                "the write token authenticates but holds no grant of "
                f"{', '.join(missing)} on any path - provisioning will fail"
            ),
            "missing": missing,
            "separate": separate,
        }
    return {
        "status": "ok",
        "detail": (
            "the write token authenticates and holds every provisioning privilege "
            "(on at least one path each)"
        ),
        "missing": [],
        "separate": separate,
    }
