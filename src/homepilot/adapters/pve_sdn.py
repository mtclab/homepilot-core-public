"""The ONE place HomePilot names a PVE SDN or firewall endpoint (#553).

Owner mandate, 2026-08-26: HomePilot does not re-implement Proxmox endpoints.
The estate's own library - `homepilot-proxmox-mcp` (repo mtclab/proxmox-mcp,
public mirror mtclab/proxmox-mcp-public, importable as ``proxmox_mcp``) -
already covers SDN zones, vnets, subnets, the SDN apply and the per-VM
firewall, so this module DELEGATES to it and HomePilot's own ``ProxmoxClient``
gains no new endpoint knowledge at all.

Two shapes come out of that library and the difference decides how each call is
made here:

* **Mutations** have real functions (``sdn.create_sdn_zone``,
  ``firewall.create_vm_firewall_rule``, ...). They return a human sentence,
  which is exactly what an execution log wants, so they are called as-is.
* **Reads** also have functions, but every one of them returns a FORMATTED
  STRING that has already thrown away the fields a plan needs - the subnet
  listing prints the subnet's name and nothing else, no gateway, no SNAT, no
  DHCP range. A plan cannot be computed from that. So reads go through the
  library's own ``MultiClient`` API surface (``get_client()`` +
  ``safe_api_call``), which is structured, retried and error-mapped by the
  library. Each such case is a "needs a structured core upstream" item and is
  listed in :data:`UPSTREAM_STRUCTURED_GAPS`.

There is also a genuine COVERAGE gap: the library has no vnet-firewall
functions at all (PVE 9's ``/cluster/sdn/vnets/{vnet}/firewall/*``) and no
subnet UPDATE. Those go through the same MultiClient surface here, and are
listed in :data:`UPSTREAM_COVERAGE_GAPS` so the debt is written down rather
than remembered.

Nothing in this module knows what a guest network SHOULD look like; that is
``provision/guest_network.py``. This is the wire.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


# ── The datacenter firewall master switch (#600) ─────────────────────────────
# Enabling the PVE datacenter firewall is what makes the per-VM guest fence
# actually enforce - the tap-level rules HomePilot writes at provision time are
# stored but inert until the cluster firewall is on. It is ALSO the one way a
# node locks itself out: PVE compiles a default INPUT policy of DROP the moment
# the firewall is enabled, and it does NOT auto-allow SSH (22) or the API
# (8006), so a bare `enable=1` severed management to dev pve1 until console
# recovery (#600, live 2026-08-27). The safe primitive, verified on that same
# node: enable WITH `policy_in=ACCEPT`. The host INPUT chain stays open, the
# per-VM tap fences do the guest isolation, and nothing locks out. This module
# NEVER writes `policy_in=DROP` - the guard below refuses it.


class PveFirewallLockoutError(RuntimeError):
    """A cluster firewall change that would sever the management plane.

    Raised rather than sent to the wire: an enabled datacenter firewall with an
    input policy of DROP has no management-allow rule PVE adds for you, so it
    locks out SSH and the API. HomePilot refuses to be the thing that does that.
    """


def _fw_flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return value != 0
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _policy_in(options: Mapping[str, Any]) -> str:
    """The input policy, upper-cased, defaulting to DROP when unset.

    The default is the load-bearing part: PVE treats an ENABLED firewall with no
    explicit `policy_in` as DROP, so "absent" is the lockout case, not a safe
    blank. Reading it as DROP here is what makes the guard refuse a bare enable.
    """
    return str(options.get("policy_in", "") or "").strip().upper() or "DROP"


def datacenter_firewall_lockout_safe(options: Mapping[str, Any]) -> bool:
    """Would these cluster firewall options keep the management plane reachable?

    The one rule: an ENABLED datacenter firewall must not carry an input policy
    of DROP. At `policy_in=ACCEPT` PVE leaves the host INPUT chain open, so the
    per-VM guest fences enforce without the node fencing itself off SSH and the
    API. A disabled firewall cannot lock anyone out, so it is trivially safe.
    """
    if not _fw_flag(options.get("enable", 0)):
        return True
    return _policy_in(options) != "DROP"


def safe_datacenter_firewall_enable(options: Mapping[str, Any]) -> dict[str, Any] | None:
    """The options write that safely enables the firewall, or None if none is needed.

    ``None`` means already converged - the firewall is on AND its input policy
    is already ACCEPT, so there is nothing to write and the caller must not
    touch it. Otherwise this returns exactly ``{enable: 1, policy_in: ACCEPT}``,
    the smallest write that turns the master switch on without ever producing a
    DROP input policy (``policy_out`` is left untouched: the guest fence is
    per-VM OUT rules, not the host output policy). The returned write is passed
    back through :func:`datacenter_firewall_lockout_safe` before it is handed
    out, so a lockout-shaped write can never leave this function.
    """
    # An ALREADY-ENABLED firewall is the operator's, and HomePilot does not
    # write to it. Full stop. It used to return {enable:1, policy_in:ACCEPT}
    # for an enabled firewall whose policy was anything but ACCEPT - which
    # would have REWRITTEN a cluster input policy the operator set deliberately,
    # on a node carrying 34 of their own hand-built rules, in the name of a
    # lockout that was not happening. Enabling is the only thing HomePilot has
    # any business doing here, so a firewall that is already on needs nothing.
    if _fw_flag(options.get("enable", 0)):
        return None
    write = {"enable": 1, "policy_in": "ACCEPT"}
    if not datacenter_firewall_lockout_safe(write):  # pragma: no cover - safe by construction
        raise PveFirewallLockoutError(
            "refusing to compute a datacenter firewall enable that is not lockout-safe"
        )
    return write


# Reads whose proxmox_mcp function exists but returns a formatted string that
# has dropped the fields a plan needs. Each wants a structured core upstream
# (e.g. `list_sdn_subnets_data()` returning rows, with the string formatter
# built on top of it); until then the read goes through MultiClient here.
UPSTREAM_STRUCTURED_GAPS: tuple[str, ...] = (
    "sdn.list_sdn_zones - returns 'zone - type' lines; a plan needs dhcp/ipam/state",
    "sdn.list_sdn_vnets - returns names; a plan needs zone/alias/state",
    "sdn.list_sdn_subnets - returns names ONLY; a plan needs gateway/snat/dhcp-range",
    "firewall.get_node_firewall_options - formatted; a caller needs the nftables flag",
    "firewall.get_vm_firewall_options - formatted; a caller needs enable/policy_out",
    "firewall.list_vm_firewall_rules - formatted; a caller needs the rule rows",
    "nodes listing - proxmox_mcp.nodes formats too; a survey needs node names",
)

# Endpoints proxmox_mcp does not cover at all yet.
UPSTREAM_COVERAGE_GAPS: tuple[str, ...] = (
    "vnet firewall options: PUT/GET /cluster/sdn/vnets/{vnet}/firewall/options",
    "vnet firewall rules: POST/GET /cluster/sdn/vnets/{vnet}/firewall/rules",
    "subnet update: PUT /cluster/sdn/vnets/{vnet}/subnets/{subnet}",
)


@dataclass(frozen=True)
class PveCredentials:
    """What it takes to build a proxmox_mcp client. NEVER log or serialise this."""

    base_url: str
    token: str
    write_token: str
    verify_ssl: bool


_PVE_TOKEN_SHAPE = re.compile(r"^[^@\s]+@[^!\s]+![^=\s]+=\S+$")


def _split_token(token: str) -> tuple[str, str]:
    """`user@realm!name=secret` -> (`user@realm!name`, `secret`)."""
    token_id, _, secret = token.partition("=")
    return token_id.strip(), secret.strip()


def _usable_write_token(credentials: PveCredentials) -> str:
    """The write token, unless it is not a PVE token at all.

    Found live: a past settings save had stored an ERROR MESSAGE into the
    vault's write-token slot, and the library (rightly) refused to build a
    client around it - taking every guest-network read down with it. A
    malformed write token falls back to the read token with a warning, the
    same answer ProxmoxClient gives when there is no write token at all;
    the read token is at least KNOWN to be real, because reads work.
    """
    write = (credentials.write_token or "").strip()
    if not write:
        return credentials.token
    if _PVE_TOKEN_SHAPE.match(write):
        return write
    logger.warning(
        "The stored PVE write token is not a token (got %r...) - falling back "
        "to the read token; re-save Proxmox settings to repair it",
        write[:24],
    )
    return credentials.token


def multi_client_from(credentials: PveCredentials) -> Any:
    """Build proxmox_mcp's MultiClient from HomePilot's own PVE credentials.

    ONE factory, so the guest-network work talks to the same cluster, with the
    same tokens, as everything else HomePilot does - rather than growing a
    second configuration surface nobody remembers to keep in step.

    ``allow_elevated`` is on: the write token is what SDN.Allocate and
    VM.Config.Network live on, and this client exists to make changes. When
    HomePilot has no separate write token the read one answers for both, which
    is the same fallback ``ProxmoxClient`` already makes.
    """
    from proxmox_mcp.config import EndpointConfig, MultiConfig
    from proxmox_mcp.multi_client import MultiClient

    monitor_id, monitor_secret = _split_token(credentials.token)
    admin_id, admin_secret = _split_token(_usable_write_token(credentials))
    endpoint = EndpointConfig(
        name="homepilot",
        url=credentials.base_url,
        monitor_token_id=monitor_id,
        monitor_token_secret=monitor_secret,
        admin_token_id=admin_id,
        admin_token_secret=admin_secret,
        allow_elevated=True,
        verify=credentials.verify_ssl,
    )
    config = MultiConfig(endpoints=[endpoint], verify=credentials.verify_ssl)
    config.validate()
    return MultiClient(config)


def gateway_from(proxmox: Any) -> PveSdnGateway | None:
    """A gateway for the cluster HomePilot's ProxmoxClient already talks to.

    None when there is no Proxmox at all, which every caller must handle: an
    instance with no hypervisor wired up is a legitimate state, not an error.
    """
    if proxmox is None:
        return None
    credentials = getattr(proxmox, "credentials", None)
    if credentials is None:
        return None
    return PveSdnGateway(multi_client_from(credentials()))


def _rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        return [payload]
    return []


def _obj(payload: Any) -> dict[str, Any]:
    return payload if isinstance(payload, dict) else {}


class PveSdnGateway:
    """Named PVE operations, delegating to ``proxmox_mcp``.

    Named operations rather than (method, path) pairs on purpose: a plan step
    that carries a path is a path HomePilot owns, and owning it is the
    duplication this module exists to remove. A step carries an operation name
    and its parameters; only this class knows what that means on the wire, and
    for most of them not even this class knows - the library does.
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    # ── Reads ────────────────────────────────────────────────────────────────
    # All through the library's MultiClient surface: see UPSTREAM_STRUCTURED_GAPS.

    def _api(self) -> Any:
        return self._client.get_client(elevated=False)

    async def list_nodes(self) -> list[dict[str, Any]]:
        return _rows(await self._client.safe_api_call(self._api().nodes.get))

    async def list_zones(self) -> list[dict[str, Any]]:
        return _rows(await self._client.safe_api_call(self._api().cluster.sdn.zones.get, pending=1))

    async def list_vnets(self) -> list[dict[str, Any]]:
        return _rows(await self._client.safe_api_call(self._api().cluster.sdn.vnets.get, pending=1))

    async def list_subnets(self, vnet: str) -> list[dict[str, Any]]:
        return _rows(
            await self._client.safe_api_call(
                self._api().cluster.sdn.vnets(vnet).subnets.get, pending=1
            )
        )

    async def vnet_firewall_options(self, vnet: str) -> dict[str, Any]:
        return _obj(
            await self._client.safe_api_call(
                self._api().cluster.sdn.vnets(vnet).firewall.options.get
            )
        )

    async def vnet_firewall_rules(self, vnet: str) -> list[dict[str, Any]]:
        return _rows(
            await self._client.safe_api_call(self._api().cluster.sdn.vnets(vnet).firewall.rules.get)
        )

    async def node_firewall_options(self, node: str) -> dict[str, Any]:
        return _obj(await self._client.safe_api_call(self._api().nodes(node).firewall.options.get))

    async def node_services(self, node: str) -> list[dict[str, Any]]:
        """The node's service list, used to tell the firewall STACK apart.

        The node firewall options carry an `nftables` key only on some
        installs, so its ABSENCE proves nothing - a node running the nftables
        `proxmox-firewall` can answer options with no such key at all (seen on
        a live node). The running service is the evidence; the missing key was
        never more than a hint.
        """
        return _rows(await self._client.safe_api_call(self._api().nodes(node).services.get))

    async def cluster_firewall_options(self) -> dict[str, Any]:
        # The datacenter firewall master switch (#600). Whether it is on, and
        # whether its input policy is lockout-safe, is what decides if the guest
        # fence actually ENFORCES - so a survey reads it, never guesses it.
        return _obj(await self._client.safe_api_call(self._api().cluster.firewall.options.get))

    # ── Mutations ────────────────────────────────────────────────────────────
    # Through proxmox_mcp's own functions wherever it has one. `confirm=True` is
    # the library's guard against an accidental call, not a question to a human:
    # the human decision happened at artifact approval, which is upstream of any
    # of this running at all.

    async def create_zone(self, **params: Any) -> str:
        from proxmox_mcp import sdn

        zone = str(params.pop("zone", ""))
        zone_type = str(params.pop("type", "simple"))
        return str(
            await sdn.create_sdn_zone(
                self._client, zone=zone, type=zone_type, confirm=True, **params
            )
        )

    async def update_zone(self, **params: Any) -> str:
        from proxmox_mcp import sdn

        zone = str(params.pop("zone", ""))
        return str(await sdn.update_sdn_zone(self._client, zone=zone, confirm=True, **params))

    async def create_vnet(self, **params: Any) -> str:
        from proxmox_mcp import sdn

        vnet = str(params.pop("vnet", ""))
        zone = str(params.pop("zone", ""))
        return str(
            await sdn.create_sdn_vnet(self._client, vnet=vnet, zone=zone, confirm=True, **params)
        )

    async def create_subnet(self, **params: Any) -> str:
        from proxmox_mcp import sdn

        vnet = str(params.pop("vnet", ""))
        subnet = str(params.pop("subnet", ""))
        return str(
            await sdn.create_sdn_subnet(
                self._client, vnet=vnet, subnet=subnet, confirm=True, **params
            )
        )

    async def update_subnet(self, **params: Any) -> str:
        # COVERAGE GAP: proxmox_mcp has create/delete for subnets but no update.
        vnet = str(params.pop("vnet", ""))
        subnet = str(params.pop("subnet", ""))
        elevated = self._client.get_client(elevated=True)
        await self._client.safe_api_call(
            elevated.cluster.sdn.vnets(vnet).subnets(subnet).put,
            elevated=True,
            **params,
        )
        return f"SDN subnet {subnet!r} updated in VNet {vnet!r}"

    async def set_vnet_firewall_options(self, **params: Any) -> str:
        # COVERAGE GAP: proxmox_mcp has no vnet firewall support at all.
        vnet = str(params.pop("vnet", ""))
        elevated = self._client.get_client(elevated=True)
        await self._client.safe_api_call(
            elevated.cluster.sdn.vnets(vnet).firewall.options.put,
            elevated=True,
            **params,
        )
        opts = ", ".join(f"{k}={v!r}" for k, v in sorted(params.items()))
        return f"vnet {vnet!r} firewall options set: {opts}"

    async def create_vnet_firewall_rule(self, **params: Any) -> str:
        # COVERAGE GAP: see above.
        vnet = str(params.pop("vnet", ""))
        elevated = self._client.get_client(elevated=True)
        await self._client.safe_api_call(
            elevated.cluster.sdn.vnets(vnet).firewall.rules.post,
            elevated=True,
            **params,
        )
        return (
            f"vnet {vnet!r} forward rule created: {params.get('action')} "
            f"-> {params.get('dest', 'any')}"
        )

    async def apply_sdn(self, **_params: Any) -> str:
        from proxmox_mcp import sdn

        return str(await sdn.apply_sdn(self._client, confirm=True))

    async def ensure_datacenter_firewall_enabled(self, **_params: Any) -> str:
        """Turn the datacenter firewall on SAFELY, or say it was already on (#600).

        Reads the current cluster options first, then writes at most once:

        * already on with ``policy_in=ACCEPT`` -> no write, idempotent no-op;
        * anything else -> ONE options PUT of ``{enable: 1, policy_in: ACCEPT}``,
          so the master switch and the safe input policy land in the SAME write
          and there is never a window where the firewall is on with a DROP INPUT.

        It never sets ``policy_in=DROP``; the write is run back through the guard
        before it is sent, and a would-be lockout raises
        :class:`PveFirewallLockoutError` rather than reaching the cluster.
        """
        current = await self.cluster_firewall_options()
        write = safe_datacenter_firewall_enable(current)
        if write is None:
            return (
                "datacenter firewall already enabled with policy_in=ACCEPT "
                "(host INPUT stays open; the per-VM guest fences enforce) - nothing to do"
            )
        if not datacenter_firewall_lockout_safe(write):  # pragma: no cover - guarded above
            raise PveFirewallLockoutError(
                "refusing to enable the datacenter firewall: the computed write would lock out "
                "management"
            )
        elevated = self._client.get_client(elevated=True)
        await self._client.safe_api_call(
            elevated.cluster.firewall.options.put,
            elevated=True,
            **write,
        )
        return (
            "datacenter firewall enabled with policy_in=ACCEPT (host INPUT stays open, so no "
            "lockout; the per-VM guest fences now enforce)"
        )

    # ── The per-VM fence ─────────────────────────────────────────────────────

    async def set_vm_firewall_options(self, node: str, vmid: int, **options: Any) -> str:
        from proxmox_mcp import firewall

        return str(
            await firewall.set_vm_firewall_options(
                self._client, node=node, vmid=vmid, confirm=True, **options
            )
        )

    async def create_vm_firewall_rule(self, node: str, vmid: int, **rule: Any) -> str:
        from proxmox_mcp import firewall

        # The library spells the rule's direction `dptype` (PVE's `type`), so the
        # translation happens HERE, once, rather than in every caller.
        rule = dict(rule)
        if "type" in rule:
            rule["dptype"] = rule.pop("type")
        return str(
            await firewall.create_vm_firewall_rule(
                self._client, node=node, vmid=vmid, confirm=True, **rule
            )
        )


__all__ = [
    "UPSTREAM_COVERAGE_GAPS",
    "UPSTREAM_STRUCTURED_GAPS",
    "PveCredentials",
    "PveFirewallLockoutError",
    "PveSdnGateway",
    "datacenter_firewall_lockout_safe",
    "gateway_from",
    "multi_client_from",
    "safe_datacenter_firewall_enable",
]
