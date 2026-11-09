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

import asyncio
import functools
import inspect
import logging
import threading
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

# The PVE library is loaded with this module, not inside the functions that use
# it: those run on the event loop (the claim's and Settings' write-token probe,
# the guest-network gateway), and importing it there - proxmoxer, requests,
# charset detection - held the loop 265 ms on a fresh venv (3.10.0 gate,
# test_first_run_claim). homepilot.main imports this module before uvicorn
# starts the loop; tests/test_no_blocking_on_loop.py holds both.
from proxmox_mcp import firewall, sdn
from proxmox_mcp.config import EndpointConfig, MultiConfig
from proxmox_mcp.exceptions import ProxmoxNotFoundError
from proxmox_mcp.multi_client import MultiClient

from .pve_firewall_guard import (
    LOCKOUT_POLICIES,
    OptionsScope,
    PveFirewallLockoutError,
    check_firewall_options_write,
    fw_flag,
    management_source,
    policy_in,
)

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
# NEVER writes `policy_in=DROP`, and never overwrites one an operator staged: an
# enable over a staged DROP/REJECT is sent only over a VERIFIED management-allow
# (pve_firewall_guard, the same check ProxmoxClient runs on every write, #707).


# The error, the policy reading and the management-allow verification live in
# ONE module that both wire clients call (#707) - see pve_firewall_guard.


def datacenter_firewall_lockout_safe(options: Mapping[str, Any]) -> bool:
    """Is this cluster firewall state safe WITHOUT any rule to rely on?

    An ENABLED datacenter firewall is safe on its own only at an input policy
    that is neither DROP nor REJECT (unset reads as DROP - PVE's default).
    REJECT severs management exactly as DROP does. A disabled firewall cannot
    lock anyone out. Anything else needs a VERIFIED management-allow, which is
    :func:`pve_firewall_guard.check_firewall_options_write`'s question, not
    this one's.
    """
    if not fw_flag(options.get("enable", 0)):
        return True
    return policy_in(options) not in LOCKOUT_POLICIES


def staged_lockout_policy(options: Mapping[str, Any]) -> str | None:
    """An input policy the operator has STAGED on a disabled firewall, if it drops.

    An explicit DROP/REJECT set while the switch is off is a decision someone
    made - typically with management-allow rules prepared - and HomePilot does
    not overwrite it to make its own enable easier (#707).
    """
    raw = str(options.get("policy_in", "") or "").strip().upper()
    return raw if raw in LOCKOUT_POLICIES else None


def safe_datacenter_firewall_enable(options: Mapping[str, Any]) -> dict[str, Any] | None:
    """The options write that enables the firewall, or None if none is needed.

    ``None`` means the firewall is already on, which makes it the operator's:
    HomePilot never writes to an enabled firewall (owner rule).

    For a DISABLED firewall:

    * no input policy, or ACCEPT -> ``{enable: 1, policy_in: ACCEPT}``, the
      switch and the open input policy in ONE write, so there is never a
      window with a DROP INPUT;
    * an operator-STAGED DROP/REJECT -> ``{enable: 1}`` and nothing else. The
      staged policy is respected, never overwritten (#707), and this write is
      lockout-shaped - so the caller must pass it through the guard, which
      sends it only over a VERIFIED management-allow and refuses otherwise.
    """
    # An ALREADY-ENABLED firewall is the operator's, and HomePilot does not
    # write to it. Full stop. It used to return {enable:1, policy_in:ACCEPT}
    # for an enabled firewall whose policy was anything but ACCEPT - which
    # would have REWRITTEN a cluster input policy the operator set deliberately,
    # on a node carrying 34 of their own hand-built rules, in the name of a
    # lockout that was not happening. Enabling is the only thing HomePilot has
    # any business doing here, so a firewall that is already on needs nothing.
    if fw_flag(options.get("enable", 0)):
        return None
    if staged_lockout_policy(options) is not None:
        return {"enable": 1}
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
    "cluster resources - a fence survey needs every guest's node/vmid/template",
)

# Endpoints proxmox_mcp does not cover at all yet.
UPSTREAM_COVERAGE_GAPS: tuple[str, ...] = (
    "vnet firewall options: PUT/GET /cluster/sdn/vnets/{vnet}/firewall/options",
    "vnet firewall rules: POST/GET/DELETE /cluster/sdn/vnets/{vnet}/firewall/rules",
    "rule delete with digest: delete_vm_firewall_rule takes no digest, so the gateway "
    "re-reads the rule at pos before deleting it",
    "subnet update: PUT /cluster/sdn/vnets/{vnet}/subnets/{subnet}",
)


@dataclass(frozen=True)
class PveCredentials:
    """What it takes to build a proxmox_mcp client. NEVER log or serialise this."""

    base_url: str
    token: str
    write_token: str
    verify_ssl: bool


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
    from ..proxmox_config import is_pve_token

    write = (credentials.write_token or "").strip()
    if not write:
        return credentials.token
    # The ONE token rule (#715), shared with ProxmoxClient and the resolver, so
    # this gateway and the client can never pick different write credentials.
    if is_pve_token(write):
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
    creds = credentials()
    return PveSdnGateway(multi_client_from(creds), base_url=creds.base_url)


# ── Off the event loop (#708) ────────────────────────────────────────────────
# proxmox_mcp's coroutines are async in name only: `safe_api_call` calls the
# synchronous proxmoxer/requests client INSIDE the coroutine, so every SDN and
# firewall read froze HomePilot's whole event loop - the API, the agent hub, the
# reconcilers - for as long as PVE took to answer (measured: a 2 s answer
# stalled the loop 2.08 s; an unreachable cluster is a 30 s library timeout per
# read). The fix belongs upstream (mtclab/proxmox-mcp#223); until the pinned
# mirror carries it, every library coroutine is run HERE, at the boundary, on a
# worker thread with its own event loop. The pool is bounded so a dead cluster
# ties up a fixed number of threads, never the loop and never an unbounded set.

PVE_LIBRARY_WORKERS = 4
_pve_pool: ThreadPoolExecutor | None = None
_pve_pool_lock = threading.Lock()


def _pool() -> ThreadPoolExecutor:
    global _pve_pool
    with _pve_pool_lock:
        if _pve_pool is None:
            _pve_pool = ThreadPoolExecutor(
                max_workers=PVE_LIBRARY_WORKERS, thread_name_prefix="pve-library"
            )
        return _pve_pool


class OffLoopClient:
    """proxmox_mcp's MultiClient with every coroutine method run off the event loop.

    Plain attributes and synchronous methods (``get_client``,
    ``raise_if_not_elevated``, ``default_endpoint``) pass straight through: they
    build request paths and do no I/O. Every coroutine method - ``safe_api_call``,
    ``resolve_node``, ``wait_for_task``, ``cluster_call`` and anything the library
    adds - runs to completion on a pool thread under ``asyncio.run``, so the
    blocking HTTP inside it never touches the caller's loop. The library's own
    module functions (``sdn.create_sdn_zone(client, ...)``) are handed this
    object, so their calls are moved off the loop the same way.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    @property
    def inner(self) -> Any:
        return self._inner

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._inner, name)
        if not inspect.iscoroutinefunction(attr):
            return attr

        async def off_loop(*args: Any, **kwargs: Any) -> Any:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                _pool(), functools.partial(asyncio.run, attr(*args, **kwargs))
            )

        return off_loop


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

    def __init__(self, client: Any, base_url: str = "") -> None:
        # Every library coroutine runs off the event loop (#708); wrapping here,
        # at the one door, means no gateway method can forget to.
        self._client = client if isinstance(client, OffLoopClient) else OffLoopClient(client)
        # Only to work out which local address HomePilot reaches PVE from, for
        # the lockout guard's management source (#707). Never sent anywhere.
        self._base_url = base_url

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

    async def cluster_firewall_rules(self) -> list[dict[str, Any]]:
        # Read by the lockout guard: a management-allow is verified on the
        # cluster's own rules before a lockout-shaped enable is sent (#707).
        return _rows(await self._client.safe_api_call(self._api().cluster.firewall.rules.get))

    async def node_firewall_rules(self, node: str) -> list[dict[str, Any]]:
        return _rows(await self._client.safe_api_call(self._api().nodes(node).firewall.rules.get))

    async def node_names(self) -> list[str]:
        return sorted(str(r["node"]) for r in await self.list_nodes() if r.get("node"))

    async def list_guest_vms(self) -> list[dict[str, Any]]:
        """Every guest on the cluster (node, vmid, type, template), in one read (#713)."""
        return _rows(await self._client.safe_api_call(self._api().cluster.resources.get, type="vm"))

    async def vm_firewall_rules(self, node: str, vmid: int) -> list[dict[str, Any]] | None:
        """A qemu guest's firewall rules as PVE holds them, or None if the guest is gone.

        Read so the per-VM guest fence is verified after provision, not trusted
        forever (#713). A guest destroyed between the listing and this read is
        not an error - there is nothing left to fence.
        """
        try:
            payload = await self._client.safe_api_call(
                self._api().nodes(node).qemu(vmid).firewall.rules.get
            )
        except ProxmoxNotFoundError:
            return None
        return _rows(payload)

    # ── Mutations ────────────────────────────────────────────────────────────
    # Through proxmox_mcp's own functions wherever it has one. `confirm=True` is
    # the library's guard against an accidental call, not a question to a human:
    # the human decision happened at artifact approval, which is upstream of any
    # of this running at all.

    async def create_zone(self, **params: Any) -> str:
        zone = str(params.pop("zone", ""))
        zone_type = str(params.pop("type", "simple"))
        return str(
            await sdn.create_sdn_zone(
                self._client, zone=zone, type=zone_type, confirm=True, **params
            )
        )

    async def update_zone(self, **params: Any) -> str:
        zone = str(params.pop("zone", ""))
        return str(await sdn.update_sdn_zone(self._client, zone=zone, confirm=True, **params))

    async def create_vnet(self, **params: Any) -> str:
        vnet = str(params.pop("vnet", ""))
        zone = str(params.pop("zone", ""))
        return str(
            await sdn.create_sdn_vnet(self._client, vnet=vnet, zone=zone, confirm=True, **params)
        )

    async def create_subnet(self, **params: Any) -> str:
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

    async def delete_vnet_firewall_rule(
        self, vnet: str, pos: int, expect: Mapping[str, Any] | None = None
    ) -> str:
        # COVERAGE GAP: proxmox_mcp has no vnet firewall support at all.
        api = self._api().cluster.sdn.vnets(vnet).firewall.rules(pos)
        rule = await self._expected_rule(api, f"vnet {vnet!r}", pos, expect)
        elevated = self._client.get_client(elevated=True)
        params = {"digest": rule["digest"]} if rule.get("digest") else {}
        await self._client.safe_api_call(
            elevated.cluster.sdn.vnets(vnet).firewall.rules(pos).delete,
            elevated=True,
            **params,
        )
        return f"vnet {vnet!r} firewall rule {pos} deleted"

    async def apply_sdn(self, **_params: Any) -> str:
        return str(await sdn.apply_sdn(self._client, confirm=True))

    async def ensure_datacenter_firewall_enabled(self, **_params: Any) -> str:
        """Turn the datacenter firewall on SAFELY, or say it was already on (#600).

        Reads the current cluster options first, then writes at most once:

        * already on -> no write at all: an enabled firewall is the operator's;
        * off, input policy unset or ACCEPT -> ONE options PUT of
          ``{enable: 1, policy_in: ACCEPT}``, so the master switch and the safe
          input policy land in the SAME write and there is never a window where
          the firewall is on with a DROP INPUT;
        * off, with a DROP/REJECT input policy the operator STAGED -> the staged
          policy is kept (#707), and ``{enable: 1}`` is sent only if the lockout
          guard VERIFIES a management-allow on the cluster's rules. Otherwise
          :class:`PveFirewallLockoutError` is raised and nothing reaches the wire.
        """
        current = await self.cluster_firewall_options()
        write = safe_datacenter_firewall_enable(current)
        if write is None:
            return (
                "datacenter firewall already enabled - it is the operator's, so HomePilot "
                "writes nothing to it - nothing to do"
            )
        # The same guard ProxmoxClient runs on every options write: a write that
        # leaves the firewall lockout-shaped is sent only over a VERIFIED allow.
        parsed = urlparse(self._base_url if "://" in self._base_url else f"//{self._base_url}")
        api_port = parsed.port or 8006
        verified = await check_firewall_options_write(
            OptionsScope("cluster"),
            write,
            self,
            ports=(22, api_port),
            source=await management_source(parsed.hostname, api_port),
            current=current,
        )
        elevated = self._client.get_client(elevated=True)
        await self._client.safe_api_call(
            elevated.cluster.firewall.options.put,
            elevated=True,
            **write,
        )
        if verified is not None:
            return (
                f"datacenter firewall enabled at the operator's staged policy_in="
                f"{policy_in(current)} ({verified}; the per-VM guest fences now enforce)"
            )
        return (
            "datacenter firewall enabled with policy_in=ACCEPT (host INPUT stays open, so no "
            "lockout; the per-VM guest fences now enforce)"
        )

    # ── The per-VM fence ─────────────────────────────────────────────────────

    async def set_vm_firewall_options(self, node: str, vmid: int, **options: Any) -> str:
        return str(
            await firewall.set_vm_firewall_options(
                self._client, node=node, vmid=vmid, confirm=True, **options
            )
        )

    async def delete_vm_firewall_rule(
        self, node: str, vmid: int, pos: int, expect: Mapping[str, Any] | None = None
    ) -> str:
        api = self._api().nodes(node).qemu(vmid).firewall.rules(pos)
        await self._expected_rule(api, f"guest {vmid}", pos, expect)
        return str(
            await firewall.delete_vm_firewall_rule(
                self._client, node=node, vmid=vmid, pos=pos, confirm=True
            )
        )

    async def _expected_rule(
        self, api: Any, where: str, pos: int, expect: Mapping[str, Any] | None
    ) -> dict[str, Any]:
        """The rule at ``pos``, re-read and checked against what the plan expects.

        A delete addresses a rule by POSITION, and positions move whenever
        anyone edits the chain. So every delete names the rule it means, and is
        refused unless that exact rule is still there: a chain that changed
        between the survey and this step must cost the apply, never an
        operator's rule (#713).
        """
        rule = _obj(await self._client.safe_api_call(api.get))
        for key, value in (expect or {}).items():
            if str(rule.get(key, "")) != str(value):
                raise RuntimeError(
                    f"refusing to delete {where} firewall rule {pos}: it is not the rule the "
                    f"plan read ({key}={rule.get(key)!r}, expected {value!r}) - the chain "
                    "changed since the survey; nothing was deleted, re-run the apply"
                )
        return rule

    async def create_vm_firewall_rule(self, node: str, vmid: int, **rule: Any) -> str:
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
    "PVE_LIBRARY_WORKERS",
    "UPSTREAM_COVERAGE_GAPS",
    "UPSTREAM_STRUCTURED_GAPS",
    "OffLoopClient",
    "PveCredentials",
    "PveFirewallLockoutError",
    "PveSdnGateway",
    "datacenter_firewall_lockout_safe",
    "gateway_from",
    "multi_client_from",
    "safe_datacenter_firewall_enable",
    "staged_lockout_policy",
]
