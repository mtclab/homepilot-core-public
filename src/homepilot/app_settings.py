"""Operator settings that live in the DB, with env as an explicit override (#553 C2).

Precedence is binding and identical for every entry (the `hub_tls_mode`
precedent, generalised): **an explicitly-set env var wins and records nothing;
otherwise the DB value; otherwise the code default.** An env var the operator
named is the operator speaking through the channel they chose - persisting a UI
edit on top of it would produce a stored value that silently contradicts the
environment on the next boot, which is exactly the surprise `tls_mode` exists to
prevent. So a PUT against an env-set key is REFUSED, loudly, rather than saved
and ignored.

Only NON-SECRET settings appear here. Secrets (webhook signing secret, tokens,
passphrases) are not in the registry at all, so no route built on the registry
can list one, echo one, or accept one - the discipline is structural rather than
a filter someone has to remember to apply.

Each entry declares whether it is hot-reloadable. That is a claim about the
CONSUMER, not a wish: an entry is only hot_reloadable when every code path that
uses it re-resolves at use time (per push, per run, per send, per call). The UI
labels the rest "restart required" and means it.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - see _cluster_probe below
    from .provision.probes import ProbeContext, ProbeFn, ProbeResult

logger = logging.getLogger(__name__)

# Every DB row this module writes carries this prefix, so an operator setting can
# never collide with the settings table's other tenants (`hub_tls_mode`, the
# archive-push outcome keys) and a stray key cannot be mistaken for one.
DB_KEY_PREFIX = "setting:"  # pragma: allowlist secret - a key namespace, not a credential

SOURCE_ENV = "env"
SOURCE_DB = "db"
SOURCE_DEFAULT = "default"


class SettingError(ValueError):
    """A value the registry refuses: unknown key, or one the type rejects."""


class EnvOverrideError(RuntimeError):
    """A write against a key the environment already decides."""

    def __init__(self, key: str, env_var: str) -> None:
        self.key = key
        self.env_var = env_var
        super().__init__(
            f"{key} is overridden by {env_var}; records nothing. "
            f"Unset {env_var} and restart to manage this setting from here."
        )


def _parse_str(raw: Any) -> str:
    if raw is None:
        return ""
    if not isinstance(raw, str | int | float):
        raise SettingError("expected a string")
    return str(raw).strip()


def _positive_int(raw: Any) -> int:
    if isinstance(raw, bool):  # bool is an int; "true" is not a number of seconds
        raise SettingError("expected a whole number")
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise SettingError(f"expected a whole number, got {raw!r}") from exc
    if value < 1:
        raise SettingError("must be at least 1")
    return value


def _unset_or_positive_int(raw: Any) -> int:
    """A whole number where 0 means "no default" (#553 C3).

    The registry stores strings, so an emptied field has to come back as a
    value: 0 is that value, and every consumer reads it as "the caller must say
    it itself" rather than as a vmid or a VLAN.
    """
    if isinstance(raw, bool):
        raise SettingError("expected a whole number")
    text = "" if raw is None else str(raw).strip()
    if text == "":
        return 0
    try:
        value = int(text)
    except (TypeError, ValueError) as exc:
        raise SettingError(f"expected a whole number, got {raw!r}") from exc
    if value < 0:
        raise SettingError("must be 0 (unset) or a positive whole number")
    return value


def _template_vmid(raw: Any) -> int:
    value = _unset_or_positive_int(raw)
    if value and value < 100:
        raise SettingError("a PVE vmid is 100 or greater")
    return value


def _vlan_tag(raw: Any) -> int:
    value = _unset_or_positive_int(raw)
    if value > 4094:
        raise SettingError("a VLAN tag is 1-4094 (0 means untagged)")
    return value


# PVE's ipconfigN takes ip=dhcp / ip=<CIDR>[,gw=<addr>] (and the v6 spellings).
# Deliberately narrow: this string is handed to cloud-init inside the guest, and
# a shape nobody checked here becomes a guest with no network and no explanation.
_IPCONFIG_RE = re.compile(
    r"^ip6?=(dhcp|auto|"
    r"\d{1,3}(?:\.\d{1,3}){3}/\d{1,2}|"
    r"[0-9A-Fa-f:]+/\d{1,3})"
    r"(,gw6?=(\d{1,3}(?:\.\d{1,3}){3}|[0-9A-Fa-f:]+))?$"
)


def _parse_ipconfig(raw: Any) -> str:
    value = _parse_str(raw)
    if value == "":
        return ""
    if not _IPCONFIG_RE.match(value):
        raise SettingError(
            f"{value!r} is not a PVE ipconfig0: expected ip=dhcp, or "
            "ip=<address>/<prefix> optionally followed by ,gw=<address>"
        )
    return value


# What may decide a guest's address (#630). Two words, and nothing else: a
# typo'd mode that fell through to "whatever isn't static" would silently put
# an install back on the DHCP server it does not run.
IP_MODES = ("static", "dhcp")


def _parse_ip_mode(raw: Any) -> str:
    value = _parse_str(raw).lower()
    if value == "":
        # An emptied field is not "no opinion" here: something has to decide the
        # address. The code default is the honest answer.
        return "static"
    if value not in IP_MODES:
        raise SettingError(f"expected one of {', '.join(IP_MODES)}, got {value!r}")
    return value


def _parse_ipv4(raw: Any) -> str:
    """A bare IPv4 address, or the empty string.

    Checked here rather than only at use time: this value is written into a
    guest's cloud-init as its resolver, and a typo becomes a guest that has an
    address, a route, and no name resolution - the hardest of the three to
    diagnose from inside.
    """
    value = _parse_str(raw)
    if value == "":
        return ""
    try:
        addr = ipaddress.ip_address(value)
    except ValueError as exc:
        raise SettingError(f"expected an IPv4 address, got {value!r}: {exc}") from exc
    if not isinstance(addr, ipaddress.IPv4Address):
        raise SettingError(f"expected an IPv4 address; {value!r} is IPv6")
    return str(addr)


# A PVE storage id, as PVE itself accepts it (#618). The same shape
# provision.models.STORAGE_RE enforces on the per-request field - spelled here
# too rather than imported, because provision.models reaches back into this
# registry through provision.defaults and an import in this direction would be
# a cycle. tests/test_provisioning_defaults.py pins the two to each other.
_STORAGE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{0,62}$")


def _parse_storage(raw: Any) -> str:
    value = _parse_str(raw)
    if value == "":
        return ""
    if not _STORAGE_RE.match(value):
        raise SettingError(
            f"{value!r} is not a PVE storage id: a letter followed by up to 62 "
            "letters, digits, dots, dashes or underscores"
        )
    return value


def _zero_or_one(raw: Any) -> int:
    """A switch stored as a number (#553 guest network).

    The registry stores strings, and "on"/"off" has to survive the round trip as
    something a consumer can act on without guessing. 0 and 1 are the whole
    domain: anything else is refused rather than truthified, because "2" meaning
    "on" is exactly the kind of quiet reinterpretation a firewall setting must
    not do.
    """
    if isinstance(raw, bool):
        return 1 if raw else 0
    text = "" if raw is None else str(raw).strip().lower()
    if text in ("", "0", "false", "no", "off"):
        return 0
    if text in ("1", "true", "yes", "on"):
        return 1
    raise SettingError("expected 0 or 1")


def _parse_cidr_list(raw: Any) -> str:
    """A comma-separated list of IPv4 CIDRs, normalised, or the empty string.

    Parsed here rather than only at use time: this list IS the fence, and a
    typo in it is a network a guest can still reach. Stored back in one
    canonical spelling so the value an operator reads is the value the rules
    are built from.
    """
    from .provision.guest_network import GuestNetworkError, split_cidrs, validate_network

    parts = split_cidrs(raw)
    if not parts:
        return ""
    try:
        return ",".join(str(validate_network(part, "isolate cidr")) for part in parts)
    except GuestNetworkError as exc:
        raise SettingError(str(exc)) from exc


def _cluster_probe(key: str) -> ProbeFn:
    """The live check for one setting, bound at CALL time (#553 C3).

    The probes live in the provision package, which reads this registry, so
    importing them here at module scope is a cycle - hence the late import.
    The key is looked up in ``PROBES`` rather than passed as a function so a
    spec can never be wired to a probe for a different setting.
    """

    async def run(value: Any, ctx: ProbeContext) -> ProbeResult:
        from .provision.probes import PROBES

        return await PROBES[key](value, ctx)

    return run


@dataclass(frozen=True)
class SettingSpec:
    """One operator-editable setting.

    ``key`` is the ``Settings`` field name, which is also what determines the env
    var (``HP_`` + upper case) - one name, three sources, no mapping table to
    fall out of date.
    """

    key: str
    type_: str
    description: str
    hot_reloadable: bool
    parse: Callable[[Any], Any]
    # An optional LIVE check against the cluster, run before the value is
    # stored (#553 C3). A spec without one is saved on its parse alone, which
    # is all a value with nothing to verify against can honestly claim.
    probe: ProbeFn | None = None

    @property
    def env_var(self) -> str:
        return f"HP_{self.key.upper()}"


REGISTRY: dict[str, SettingSpec] = {
    spec.key: spec
    for spec in (
        SettingSpec(
            key="artifacts_remote",
            type_="str",
            description=(
                "Git remote the artifact store is pushed to, so the record of intent "
                "survives the loss of this instance's volume."
            ),
            hot_reloadable=True,
            parse=_parse_str,
        ),
        SettingSpec(
            key="artifacts_push_interval_seconds",
            type_="int",
            description="How often the artifact store is pushed to its remote.",
            hot_reloadable=True,
            parse=_positive_int,
        ),
        SettingSpec(
            key="embedding_service_url",
            type_="str",
            description=(
                "Embedding service KB search ranks with. Empty means KB search stays keyword-only."
            ),
            hot_reloadable=True,
            parse=_parse_str,
        ),
        SettingSpec(
            key="embedding_model",
            type_="str",
            description="Model name asked of the embedding service.",
            hot_reloadable=True,
            parse=_parse_str,
        ),
        SettingSpec(
            key="retention_days",
            type_="int",
            description=(
                "How long operational history (audit log, agent audit, finished tasks, "
                "webhook deliveries) is kept before the retention reconciler prunes it."
            ),
            hot_reloadable=True,
            parse=_positive_int,
        ),
        SettingSpec(
            key="metrics_retention_days",
            type_="int",
            description=(
                "How long raw metric samples are kept. A separate horizon from the "
                "operational history above on purpose: the right window for a time "
                "series is not the right window for an audit trail."
            ),
            hot_reloadable=True,
            parse=_positive_int,
        ),
        SettingSpec(
            key="events_webhook_url",
            type_="str",
            description=(
                "URL artifact and task events are POSTed to. The signing secret is NOT "
                "settable here - it stays in the environment or the vault."
            ),
            hot_reloadable=True,
            parse=_parse_str,
        ),
        # ── Provisioning defaults (#553 C3) ──────────────────────────────────
        # Each carries a probe, so a value the cluster refutes is never stored.
        # All hot-reloadable: every consumer (invite mint, redemption, the
        # provision service's net0 step) resolves at use time, per call.
        SettingSpec(
            key="provision_default_node",
            type_="str",
            description=(
                "Proxmox node guests are cloned on when a request does not name one. "
                "Empty means every provision and every invite must say it itself."
            ),
            hot_reloadable=True,
            parse=_parse_str,
            probe=_cluster_probe("provision_default_node"),
        ),
        SettingSpec(
            key="provision_default_template_vmid",
            type_="int",
            description=(
                "VMID of the template cloned when a request does not name one. 0 means no default."
            ),
            hot_reloadable=True,
            parse=_template_vmid,
            probe=_cluster_probe("provision_default_template_vmid"),
        ),
        SettingSpec(
            key="provision_default_pool",
            type_="str",
            description=("PVE resource pool provisioned guests join. Empty means no pool."),
            hot_reloadable=True,
            parse=_parse_str,
            probe=_cluster_probe("provision_default_pool"),
        ),
        SettingSpec(
            key="provision_default_storage",
            type_="str",
            description=(
                "PVE storage the clone's disks land on. Empty inherits the template's "
                "own storage, which is what every install did before this setting "
                "existed. Only ever applied to a FULL clone - a linked clone cannot "
                "leave its template's storage at all."
            ),
            hot_reloadable=True,
            parse=_parse_storage,
            probe=_cluster_probe("provision_default_storage"),
        ),
        SettingSpec(
            key="provision_tailscale_install",
            type_="int",
            description=(
                "1 to install tailscale in a guest that has none before joining it to "
                "the requester's tailnet. Nothing used to install it, so a join against "
                "a stock cloud image could never succeed. 0 is for an image that ships "
                "tailscale itself, or a guest with no route to the internet - the join "
                "is then reported failed rather than attempted."
            ),
            hot_reloadable=True,
            parse=_zero_or_one,
        ),
        SettingSpec(
            key="provision_default_bridge",
            type_="str",
            description=(
                "Bridge the guest NIC is put on (net0). Setting this is what makes "
                "provisioning touch net0 at all; empty leaves the template's own NIC "
                "exactly as it was cloned."
            ),
            hot_reloadable=True,
            parse=_parse_str,
            probe=_cluster_probe("provision_default_bridge"),
        ),
        SettingSpec(
            key="provision_default_vlan_tag",
            type_="int",
            description=(
                "VLAN tag applied to the guest NIC. 0 means untagged. Only ever "
                "applied together with the default bridge."
            ),
            hot_reloadable=True,
            parse=_vlan_tag,
            probe=_cluster_probe("provision_default_vlan_tag"),
        ),
        SettingSpec(
            key="provision_default_ipconfig",
            type_="str",
            description=(
                "cloud-init ipconfig0 used when a request does not give one, e.g. "
                "ip=dhcp or ip=10.0.0.5/24,gw=10.0.0.1."
            ),
            hot_reloadable=True,
            parse=_parse_ipconfig,
            probe=_cluster_probe("provision_default_ipconfig"),
        ),
        SettingSpec(
            key="provision_ip_mode",
            type_="str",
            description=(
                "Who decides a guest's address. 'static' (the default) has HomePilot "
                "allocate a free address from the guest subnet at provision time and "
                "write it into cloud-init; 'dhcp' writes ip=dhcp and leaves the answer "
                "to a DHCP server on the wire. A PVE SDN zone only serves DHCP through "
                "dnsmasq, so an install whose node lacks that package must stay static."
            ),
            hot_reloadable=True,
            parse=_parse_ip_mode,
            probe=_cluster_probe("provision_ip_mode"),
        ),
        SettingSpec(
            key="provision_default_nameserver",
            type_="str",
            description=(
                "Resolver written into a statically-addressed guest's cloud-init. "
                "Empty leaves the guest with no nameserver of its own, which on a "
                "subnet with no DHCP means no name resolution at all."
            ),
            hot_reloadable=True,
            parse=_parse_ipv4,
            probe=_cluster_probe("provision_default_nameserver"),
        ),
        # ── The guest network (#553) ─────────────────────────────────────────
        # What the guest subnet IS. Together these are the desired state a
        # `guest-network` artifact carries when a propose leaves a field out,
        # and the fence the provision service writes per VM. The probes are
        # LOCAL shape checks and say so - there is no cluster question to ask
        # about a subnet that does not exist yet, and the cluster is asked by
        # the survey/plan on GET /admin/guest-network instead.
        SettingSpec(
            key="guest_network_zone",
            type_="str",
            description=(
                "SDN zone the guest network lives in. Created as a 'simple' zone with "
                "dnsmasq DHCP when the guest-network artifact is applied. 1-8 lower-case "
                "letters and digits, because PVE stores it in an 8-character field."
            ),
            hot_reloadable=True,
            parse=_parse_str,
            probe=_cluster_probe("guest_network_zone"),
        ),
        SettingSpec(
            key="guest_network_vnet",
            type_="str",
            description=(
                "Vnet guests attach to - the bridge name their NIC gets. Setting "
                "provision_default_bridge to this name is what makes a provisioned guest "
                "land on the guest network and get its per-VM fence."
            ),
            hot_reloadable=True,
            parse=_parse_str,
            probe=_cluster_probe("guest_network_vnet"),
        ),
        SettingSpec(
            key="guest_network_subnet",
            type_="str",
            description=(
                "The guest subnet in CIDR form, e.g. 198.51.100.0/24. Empty means this "
                "instance describes no guest network: nothing is surveyed, nothing is "
                "planned, and provisioning writes no fence."
            ),
            hot_reloadable=True,
            parse=_parse_str,
            probe=_cluster_probe("guest_network_subnet"),
        ),
        SettingSpec(
            key="guest_network_gateway",
            type_="str",
            description=(
                "The address the guest subnet routes through, which must be INSIDE the "
                "subnet above. It is also the only host a fenced guest may talk to, and "
                "then only for DHCP and DNS."
            ),
            hot_reloadable=True,
            parse=_parse_str,
            probe=_cluster_probe("guest_network_gateway"),
        ),
        SettingSpec(
            key="guest_network_snat",
            type_="int",
            description=(
                "1 to source-NAT guest traffic out of the node, so guests reach the "
                "internet without the rest of the network knowing about their subnet. "
                "0 leaves routing to the operator."
            ),
            hot_reloadable=True,
            parse=_zero_or_one,
        ),
        SettingSpec(
            key="guest_network_dhcp",
            type_="int",
            description=(
                "1 to have the zone run dnsmasq and hand out addresses from the range "
                "below. Needs the dnsmasq package on the node - the apply repeats the "
                "cluster's own words if it is missing. 0 means guests are addressed by "
                "cloud-init alone."
            ),
            hot_reloadable=True,
            parse=_zero_or_one,
        ),
        SettingSpec(
            key="guest_network_dhcp_range",
            type_="str",
            description=(
                "The addresses DHCP may hand out, as '<start>-<end>', e.g. "
                "198.51.100.100-198.51.100.199. Both ends must be inside the subnet and "
                "neither may be the gateway."
            ),
            hot_reloadable=True,
            parse=_parse_str,
            probe=_cluster_probe("guest_network_dhcp_range"),
        ),
        SettingSpec(
            key="guest_network_dhcp_dns_server",
            type_="str",
            description=(
                "Resolver handed to guests by DHCP. Empty means the gateway resolves for "
                "them, which is what the fence allows; naming an address here means also "
                "allowing it, so leave it empty unless you know you need it."
            ),
            hot_reloadable=True,
            parse=_parse_str,
            probe=_cluster_probe("guest_network_dhcp_dns_server"),
        ),
        SettingSpec(
            key="guest_network_isolate_cidrs",
            type_="str",
            description=(
                "The networks a guest must never reach, comma separated - "
                "typically the operator LAN. This is the fence: every provisioned "
                "guest on the guest vnet gets a per-VM DROP towards each of these. "
                "EMPTY refuses to provision onto the guest vnet at all: a shipped "
                "default cannot know your LAN, so the fence must be named before "
                "the first guest."
            ),
            hot_reloadable=True,
            parse=_parse_cidr_list,
            probe=_cluster_probe("guest_network_isolate_cidrs"),
        ),
    )
}

# Named so a reader can see the omission is deliberate rather than an oversight,
# and asserted on by tests/test_app_settings.py: these must never gain a spec.
FORBIDDEN_KEYS: frozenset[str] = frozenset(
    {
        "events_webhook_secret",
        "admin_secret",
        "agent_hub_auth_token",
        "artifacts_ssh_key",
        "vault_passphrase",
        "vault_passphrase_file",
        "n8n_api_key",
        "portal_proxy_secret",
    }
)


def spec_for(key: str) -> SettingSpec:
    spec = REGISTRY.get(key)
    if spec is None:
        raise SettingError(f"unknown setting {key!r}")
    return spec


def env_is_explicit(key: str, settings: Any) -> bool:
    """Whether the operator named this setting in the environment themselves.

    Two ways of saying the same thing, and both count: the variable is present in
    the process environment, or pydantic recorded the field as SET when it built
    ``Settings`` - which is how a value from a ``.env`` file arrives. Reading only
    ``os.environ`` would treat a documented ``.env`` line as if it were the code
    default and let the UI overwrite it.
    """
    spec = spec_for(key)
    if spec.env_var in os.environ:
        return True
    fields_set = getattr(settings, "model_fields_set", None)
    return bool(fields_set and key in fields_set)


@dataclass(frozen=True)
class Resolved:
    key: str
    value: Any
    source: str


class SettingsResolver:
    """Resolves registry settings against a live repository.

    Holds no cache. Every consumer that wants hot reload asks at use time, and a
    cache here would quietly make that a lie.
    """

    def __init__(self, repo: Any, settings: Any) -> None:
        self._repo = repo
        self._settings = settings

    @property
    def settings(self) -> Any:
        return self._settings

    async def resolve(self, key: str) -> Resolved:
        spec = spec_for(key)
        if env_is_explicit(key, self._settings):
            return Resolved(key, getattr(self._settings, key), SOURCE_ENV)
        stored = await self._get_stored(key)
        if stored is not None:
            try:
                return Resolved(key, spec.parse(stored), SOURCE_DB)
            except SettingError:
                # A stored value the current build rejects (a type tightened, a
                # row written by hand) must not take the process down: fall back
                # to the default and say which row to fix.
                logger.warning(
                    "Stored value for %s is invalid (%r); using the code default",
                    key,
                    stored,
                )
        return Resolved(key, getattr(self._settings, key), SOURCE_DEFAULT)

    async def value(self, key: str) -> Any:
        return (await self.resolve(key)).value

    async def set(self, key: str, raw: Any) -> Resolved:
        spec = spec_for(key)
        if env_is_explicit(key, self._settings):
            raise EnvOverrideError(key, spec.env_var)
        value = spec.parse(raw)
        # An emptied string setting reads back as "no stored value", so saving a
        # blank field is the same act as clearing it - which is what an operator
        # emptying the box means.
        await self._repo.set_setting(DB_KEY_PREFIX + key, str(value))
        return Resolved(key, value, SOURCE_DB)

    async def clear(self, key: str) -> Resolved:
        spec = spec_for(key)
        if env_is_explicit(key, self._settings):
            raise EnvOverrideError(key, spec.env_var)
        await self._repo.set_setting(DB_KEY_PREFIX + key, "")
        await self._delete_stored(key)
        return await self.resolve(key)

    async def report(self) -> list[dict[str, Any]]:
        """Every registry setting with its value, where that value came from, and
        whether changing it takes effect now."""
        entries: list[dict[str, Any]] = []
        for key, spec in REGISTRY.items():
            resolved = await self.resolve(key)
            entries.append(
                {
                    "key": key,
                    "value": resolved.value,
                    "source": resolved.source,
                    "type": spec.type_,
                    "hot_reloadable": spec.hot_reloadable,
                    "description": spec.description,
                    "env_var": spec.env_var,
                    "editable": resolved.source != SOURCE_ENV,
                    # Whether this key can be tried against the live cluster
                    # before it is saved - what puts a "Test" button on the
                    # field and nowhere else (#553 C3).
                    "probeable": spec.probe is not None,
                }
            )
        return entries

    async def _get_stored(self, key: str) -> str | None:
        try:
            row = await self._repo.get_setting(DB_KEY_PREFIX + key)
        except Exception:  # pragma: no cover - a DB hiccup must not break a read path
            logger.debug("Could not read stored setting %s", key, exc_info=True)
            return None
        if not row:
            return None
        value = row.get("value")
        if value is None or value == "":
            # An emptied row means "back to the default", not "the empty string":
            # DELETE writes it when the repository has no row deletion.
            return None
        return str(value)

    async def _delete_stored(self, key: str) -> None:
        db = getattr(self._repo, "db", None)
        if db is None:  # pragma: no cover - repository always carries a db
            return
        try:
            await db.execute("DELETE FROM settings WHERE key = ?", (DB_KEY_PREFIX + key,))
            await db.conn.commit()
        except Exception:  # pragma: no cover - the emptied row above already suffices
            logger.debug("Could not delete stored setting %s", key, exc_info=True)


def resolver_from_state(state: Any) -> SettingsResolver | None:
    """The resolver for a running app, or None when there is nothing to resolve
    against (a CLI process, a test app built without a repository)."""
    resolver = getattr(state, "settings_resolver", None)
    if isinstance(resolver, SettingsResolver):
        return resolver
    repo = getattr(state, "repo", None)
    settings = getattr(state, "settings", None)
    if repo is None or settings is None:
        return None
    return SettingsResolver(repo, settings)


# ── Use-time resolution for consumers that hold no repository ────────────────
# KB embedding and the webhook senders are leaf functions reached from request
# handlers, executors and the CLI alike; threading a repository through all of
# them would be a large refactor for one lookup. They ask the process-wide
# resolver instead, which the app binds at startup. Unbound (CLI, unit tests)
# they fall back to the plain Settings value, which is the pre-C2 behaviour.

_bound_resolver: SettingsResolver | None = None


def bind_resolver(resolver: SettingsResolver | None) -> None:
    global _bound_resolver
    _bound_resolver = resolver


def bound_resolver() -> SettingsResolver | None:
    return _bound_resolver


async def effective(key: str, settings: Any) -> Any:
    """The value a consumer should act on right now.

    ``settings`` is the caller's own Settings object, used when no resolver is
    bound and as the env/default half of the precedence when one is.
    """
    resolver = _bound_resolver
    if resolver is None:
        return getattr(settings, key)
    return await resolver.value(key)


class EffectiveSettings:
    """Read-only overlay: registry keys resolved, everything else passed through.

    Lets code that takes a whole ``Settings`` object - the self-check builders -
    describe the values actually in force without every builder learning about
    the resolver. Snapshot, not a live view: it is built per report.
    """

    def __init__(self, settings: Any, overrides: dict[str, Any]) -> None:
        self._settings = settings
        self._overrides = overrides

    def __getattr__(self, name: str) -> Any:
        if name in self._overrides:
            return self._overrides[name]
        return getattr(self._settings, name)


async def effective_settings(state: Any, settings: Any) -> Any:
    """``settings`` with every registry key resolved through the precedence.

    Falls back to the plain object when there is no repository to resolve
    against - a report is never worth failing a boot over.
    """
    resolver = resolver_from_state(state)
    if resolver is None:
        return settings
    overrides: dict[str, Any] = {}
    for key in REGISTRY:
        try:
            overrides[key] = await resolver.value(key)
        except Exception:  # pragma: no cover - defensive: diagnostics never fail hard
            logger.debug("Could not resolve %s for the report", key, exc_info=True)
    return EffectiveSettings(settings, overrides)


async def resolve_interval(
    resolver: SettingsResolver | None,
    key: str,
    fallback: float,
) -> float:
    """Interval for a scheduler cycle, in seconds, resolved at cycle time."""
    if resolver is None:
        return fallback
    try:
        return float(await resolver.value(key))
    except Exception:  # pragma: no cover - a bad row must not stop the loop
        logger.warning("Could not resolve interval %s; keeping %.0fs", key, fallback)
        return fallback


IntervalSource = float | Callable[[], Awaitable[float]]


# ── Cluster-checked writes (#553 C3) ─────────────────────────────────────────
# A setting with a probe is never stored on its parse alone. The probe runs
# against the LIVE cluster at write time, and only an `ok` verdict reaches the
# repository - so a provisioning default the cluster refutes cannot be saved,
# and one nobody could check cannot be saved either. Both refusals live here
# rather than in the router, so every surface that writes settings (the admin
# API today, the C4 MCP setters next) refuses identically.


class ProbeRefusedError(RuntimeError):
    """The cluster refused a value, or could not be asked about it."""

    def __init__(self, key: str, result: ProbeResult) -> None:
        self.key = key
        self.result = result
        super().__init__(result.detail)


def _proxmox_from(state: Any) -> Any:
    proxmox = getattr(state, "proxmox", None)
    if proxmox is not None:
        return proxmox
    # `app.state.proxmox` is only bound when the adapter existed at boot; the
    # provision service always carries the live client after a reload rebinds it.
    service = getattr(state, "provision_service", None)
    return getattr(service, "proxmox", None)


async def probe_context(state: Any, resolver: SettingsResolver | None = None) -> ProbeContext:
    """What the probes need to know about the rest of this instance.

    The node and the bridge already IN FORCE, not the ones being written: a
    bridge is per-node and a VLAN tag is per-bridge, so the probe for one of
    them can only answer with the others' current values in hand.
    """
    from .provision.probes import ProbeContext

    resolver = resolver or resolver_from_state(state)
    node = ""
    bridge = ""
    guest_subnet = ""
    if resolver is not None:
        try:
            node = str(await resolver.value("provision_default_node") or "")
            bridge = str(await resolver.value("provision_default_bridge") or "")
            # A gateway and a DHCP range are only checkable against a subnet, so
            # the one in force rides along exactly as the node does for bridges.
            guest_subnet = str(await resolver.value("guest_network_subnet") or "")
        except Exception:  # pragma: no cover - a probe never fails on context
            logger.debug("Could not resolve the probe context", exc_info=True)
    return ProbeContext(
        proxmox=_proxmox_from(state),
        node=node,
        bridge=bridge,
        guest_subnet=guest_subnet,
    )


async def run_probe(state: Any, key: str, raw: Any) -> ProbeResult | None:
    """Ask the cluster about a candidate value WITHOUT saving anything.

    None when the spec has no probe - there is nothing to ask, and saying "ok"
    would imply a check that never happened.
    """
    spec = spec_for(key)
    if spec.probe is None:
        return None
    value = spec.parse(raw)
    ctx = await probe_context(state)
    return await spec.probe(value, ctx)


async def checked_set(
    state: Any,
    resolver: SettingsResolver,
    key: str,
    raw: Any,
) -> tuple[Resolved, ProbeResult | None]:
    """Save a setting only once the cluster has confirmed it.

    Raises EnvOverrideError (the environment decides this key), SettingError
    (the type rejects the value) or ProbeRefusedError (the cluster does) - and in
    every one of those cases nothing is written.
    """
    spec = spec_for(key)
    if env_is_explicit(key, resolver.settings):
        # Checked before the probe: an env-locked key is refused whatever the
        # cluster thinks, and there is no reason to make the operator wait for
        # a round trip to learn it.
        raise EnvOverrideError(key, spec.env_var)
    result = await run_probe(state, key, raw)
    if result is not None and not result.ok:
        raise ProbeRefusedError(key, result)
    return await resolver.set(key, raw), result
