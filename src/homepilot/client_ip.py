"""The one answer to "which address is this client?" (#705).

Every per-client decision - the first-run claim's "is this my own network", the
request rate limits, the SSE connection cap, the token-mint limit, the invite
portal's limiters - asks the same question and must get the same answer. There
used to be three implementations and two of them were wrong in the same way.

The rule:

* If the TCP peer is NOT a trusted proxy (HP_TRUSTED_PROXIES), the peer is the
  client. Forwarding headers are the client's own words and are not read.
* If the peer IS a trusted proxy, walk ``X-Forwarded-For`` from the RIGHT: each
  proxy appends the address it saw, so the rightmost entries are the ones our
  own proxies wrote. Skip every hop that is itself a trusted proxy; the first one
  that is not is the client. The LEFT end is whatever the client chose to send
  (the shipped nginx config uses ``$proxy_add_x_forwarded_for``, which appends
  to the client's header), so taking the first hop let a remote client name
  itself ``10.0.0.5``.
* No ``X-Forwarded-For``: ``X-Real-IP``, which the shipped config SETS (not
  appends) from ``$remote_addr``, so from a trusted peer it is the proxy's own
  observation.
* A hop that does not parse, or a trusted proxy that named no client at all,
  resolves to nothing (``None``): the caller decides whether that fails closed
  (the claim does) or falls back to the peer (a rate-limit key does).
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable, Mapping
from typing import Any

_Network = ipaddress.IPv4Network | ipaddress.IPv6Network
_Address = ipaddress.IPv4Address | ipaddress.IPv6Address


def _parse(address: str) -> _Address | None:
    try:
        return ipaddress.ip_address(address.strip())
    except ValueError:
        return None


def _trusted(address: _Address, trusted: Iterable[_Network]) -> bool:
    return any(address in network for network in trusted)


def is_trusted_proxy(peer: str | None, trusted: Iterable[_Network]) -> bool:
    parsed = _parse(peer or "")
    return parsed is not None and _trusted(parsed, trusted)


def forwarded_client(headers: Mapping[str, str], trusted: Iterable[_Network]) -> str | None:
    """The client address our trusted proxies assert, or None if they assert none.

    Only call this when the peer is a trusted proxy."""
    trusted = tuple(trusted)
    xff = headers.get("x-forwarded-for", "") or ""
    hops = [hop.strip() for hop in xff.split(",") if hop.strip()]
    if hops:
        leftmost: str | None = None
        for hop in reversed(hops):
            parsed = _parse(hop)
            if parsed is None:
                # A hop our own proxies would never write: someone else wrote
                # the header, and nothing to the left of it can be believed.
                return None
            if not _trusted(parsed, trusted):
                return str(parsed)
            leftmost = str(parsed)
        # Every hop is one of our proxies: the request started at a proxy.
        return leftmost
    real_ip = (headers.get("x-real-ip", "") or "").strip()
    if real_ip:
        parsed = _parse(real_ip)
        return str(parsed) if parsed is not None else None
    return None


def resolve_client_address(
    peer: str | None, headers: Mapping[str, str], trusted: Iterable[_Network]
) -> str | None:
    """The effective client address, or None when a trusted proxy named none."""
    trusted = tuple(trusted)
    if not is_trusted_proxy(peer, trusted):
        return peer or None
    return forwarded_client(headers, trusted)


def trusted_proxies_of(request: Any) -> tuple[_Network, ...]:
    """HP_TRUSTED_PROXIES as parsed once by the app lifespan (main.py)."""
    state = getattr(getattr(request, "app", None), "state", None)
    return tuple(getattr(state, "trusted_proxy_networks", None) or ())


def is_loopback_client(request: Any, trusted: Iterable[_Network] | None = None) -> bool:
    """Is the CLIENT (not merely the TCP peer) on this machine's loopback?

    The container's own healthcheck connects to localhost with no forwarding
    headers, so its peer is loopback and it is the client. A reverse proxy on
    the same host (``network_mode: host``, or a proxy in the same netns) ALSO
    connects from loopback - for a remote client. Reading the raw peer would
    hand every proxied request the healthcheck's exemptions (#687 vs #705), so
    the answer is the resolved client address, and a trusted proxy that named
    no client is NOT loopback (fail closed: an exemption is never a fallback)."""
    networks = tuple(trusted) if trusted is not None else trusted_proxies_of(request)
    peer = request.client.host if getattr(request, "client", None) else None
    resolved = _parse(resolve_client_address(peer, request.headers, networks) or "")
    return resolved is not None and resolved.is_loopback


def client_ip(request: Any, trusted: Iterable[_Network] | None = None) -> str:
    """The client address to key per-client limits on.

    Falls back to the peer when a trusted proxy named no usable client - a
    limit keyed on the proxy is conservative (it limits more, never less). Pass
    `trusted` for a surface with its own proxy list (the invite portal)."""
    networks = tuple(trusted) if trusted is not None else trusted_proxies_of(request)
    peer = request.client.host if getattr(request, "client", None) else None
    return resolve_client_address(peer, request.headers, networks) or peer or "unknown"
