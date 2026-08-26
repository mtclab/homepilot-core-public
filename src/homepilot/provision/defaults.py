"""The provisioning defaults, resolved for the code that consumes them (#553 C3).

The design's words: "an invite stops carrying raw infra details". The operator
states the cluster's shape once - node, template, pool, bridge, VLAN, ipconfig -
and the paths that build a guest fill the gaps from it:

* invite mint takes node and template_vmid (and pool/ipconfig0) from here when
  the request does not name them, and refuses with the missing SETTING's name
  when neither side supplies one;
* the provision API and the MCP tool fill the same gaps for a direct request;
* the provision service applies net0 - and only when a default bridge is set,
  which is the ONE new capability here: before C3 the template's NIC was cloned
  untouched, so a guest VLAN could not be enforced at all.

Everything is resolved at USE time, per call, through the C2 precedence (env >
db > default), which is what lets the registry call these settings hot
reloadable and mean it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from ..app_settings import REGISTRY, SettingsResolver, bound_resolver, resolver_from_state

logger = logging.getLogger(__name__)

KEYS = (
    "provision_default_node",
    "provision_default_template_vmid",
    "provision_default_pool",
    "provision_default_bridge",
    "provision_default_vlan_tag",
    "provision_default_ipconfig",
)


@dataclass(frozen=True)
class ProvisioningDefaults:
    """What this instance says about provisioning when the caller does not.

    Empty string / 0 means "no opinion" everywhere, so a fresh install behaves
    exactly as it did before C3: the caller must name it itself.
    """

    node: str = ""
    template_vmid: int = 0
    pool: str = ""
    bridge: str = ""
    vlan_tag: int = 0
    ipconfig: str = ""

    @property
    def net0(self) -> str | None:
        """The net0 line to apply after a clone, or None to leave it alone.

        None is not a detail: without a default bridge, provisioning must not
        touch the cloned NIC at all - that is the pre-C3 behaviour and the
        operator has said nothing that would justify changing it.
        """
        if not self.bridge:
            return None
        line = f"virtio,bridge={self.bridge}"
        if self.vlan_tag:
            line += f",tag={self.vlan_tag}"
        return line


def _resolver(source: Any) -> SettingsResolver | None:
    if isinstance(source, SettingsResolver):
        return source
    if source is not None:
        resolver = resolver_from_state(source)
        if resolver is not None:
            return resolver
    return bound_resolver()


async def provisioning_defaults(source: Any = None) -> ProvisioningDefaults:
    """The defaults in force right now.

    ``source`` may be an app state, a resolver, or nothing - in which case the
    process-wide resolver the app binds at startup answers, exactly as the other
    leaf consumers of C2 settings do. With no resolver at all (a CLI process, a
    unit test) every field is empty and nothing is filled in, which is the
    honest answer for a process that cannot read the database.
    """
    resolver = _resolver(source)
    if resolver is None:
        return ProvisioningDefaults()
    values: dict[str, Any] = {}
    for key in KEYS:
        try:
            values[key] = await resolver.value(key)
        except Exception:  # pragma: no cover - a bad row never blocks a provision
            logger.warning("Could not resolve %s; treating it as unset", key)
            values[key] = REGISTRY[key].parse("") if key in REGISTRY else ""
    return ProvisioningDefaults(
        node=str(values["provision_default_node"] or ""),
        template_vmid=int(values["provision_default_template_vmid"] or 0),
        pool=str(values["provision_default_pool"] or ""),
        bridge=str(values["provision_default_bridge"] or ""),
        vlan_tag=int(values["provision_default_vlan_tag"] or 0),
        ipconfig=str(values["provision_default_ipconfig"] or ""),
    )


class MissingProvisioningDefaultError(ValueError):
    """Neither the request nor this instance says what to provision on.

    Names the SETTING, not just the field: "node is required" tells an operator
    nothing they can act on, while the setting's name is a place to go.
    """

    def __init__(self, field: str, setting: str) -> None:
        self.field = field
        self.setting = setting
        super().__init__(
            f"No {field} given and no default is set. Either name a {field} in the "
            f"request, or set {setting} on Settings -> Subsystems -> Provisioning defaults."
        )


def resolve_node(given: str | None, defaults: ProvisioningDefaults) -> str:
    node = (given or "").strip() or defaults.node
    if not node:
        raise MissingProvisioningDefaultError("node", "provision_default_node")
    return node


def resolve_template_vmid(given: int | None, defaults: ProvisioningDefaults) -> int:
    vmid = int(given or 0) or defaults.template_vmid
    if not vmid:
        raise MissingProvisioningDefaultError("template_vmid", "provision_default_template_vmid")
    return vmid


def resolve_pool(given: str | None, defaults: ProvisioningDefaults) -> str | None:
    pool = (given or "").strip() or defaults.pool
    return pool or None


def resolve_ipconfig(given: str | None, defaults: ProvisioningDefaults) -> str:
    # "ip=dhcp" is ProvisionRequest's own field default, so a caller who sent
    # nothing and a caller who sent the default are indistinguishable here. They
    # are treated the same on purpose: the instance default is the more specific
    # statement of intent, and it defaults to ip=dhcp itself.
    ipconfig = (given or "").strip()
    if ipconfig and ipconfig != "ip=dhcp":
        return ipconfig
    return defaults.ipconfig or ipconfig or "ip=dhcp"


__all__ = [
    "KEYS",
    "MissingProvisioningDefaultError",
    "ProvisioningDefaults",
    "provisioning_defaults",
    "resolve_ipconfig",
    "resolve_node",
    "resolve_pool",
    "resolve_template_vmid",
]
