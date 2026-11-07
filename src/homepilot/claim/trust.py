from __future__ import annotations

import ipaddress
from collections.abc import Mapping

from ..client_ip import forwarded_client, is_trusted_proxy

_Network = ipaddress.IPv4Network | ipaddress.IPv6Network
_Address = ipaddress.IPv4Address | ipaddress.IPv6Address

# What "on my own network" means, written out rather than left to
# ``ip_address().is_private`` - that property also covers documentation and
# benchmarking ranges, and its membership has changed between Python releases.
# A claim decision must not move because the interpreter did.
LOCAL_NETWORKS: tuple[_Network, ...] = tuple(
    ipaddress.ip_network(cidr)
    for cidr in (
        "127.0.0.0/8",  # loopback
        "10.0.0.0/8",  # RFC1918
        "172.16.0.0/12",  # RFC1918
        "192.168.0.0/16",  # RFC1918
        "100.64.0.0/10",  # CGNAT / shared address space
        "169.254.0.0/16",  # link-local
        "::1/128",  # loopback
        "fc00::/7",  # unique local
        "fe80::/10",  # link-local
    )
)

# Headers that mean "somebody in front of me rewrote the source address". Their
# PRESENCE matters even when we do not parse them: seeing one from a hop we do
# not trust is proof the peer address is not the real client.
_FORWARD_HEADERS = ("x-forwarded-for", "x-real-ip", "forwarded")


def _parse(address: str) -> _Address | None:
    try:
        return ipaddress.ip_address(address.strip())
    except ValueError:
        return None


def is_local_address(address: str) -> bool:
    parsed = _parse(address)
    if parsed is None:
        return False
    return any(parsed in network for network in LOCAL_NETWORKS)


def claim_source(
    peer: str | None,
    headers: Mapping[str, str],
    trusted_proxies: list[_Network] | tuple[_Network, ...] = (),
) -> tuple[bool, str]:
    """Decide whether a claim request came from this machine's own network.

    Returns (is_local, effective_client_address). The address is what gets rate
    limited and logged, so a proxy's own address never stands in for the caller.

    Fail closed, in three places, because getting this wrong hands a stranger an
    unclaimed instance:

    * A forwarding header from a peer that is NOT in HP_TRUSTED_PROXIES makes the
      source untrusted outright. A client can set X-Forwarded-For itself; if we
      merely ignored it we would judge the peer, and a public client behind any
      unlisted relay would be judged by the relay's private address.
    * A trusted proxy that forwarded NO client address is also untrusted: the
      real client cannot be evaluated, and the proxy's own address is not it.
    * Anything unparseable is untrusted - including a hop in the forwarded
      chain, since nothing to the left of it can be believed.

    RFC 7239's ``Forwarded`` is deliberately not parsed: a half-understood
    parse of a security decision is worse than declining to make it.
    """
    peer_address = peer or ""
    forwarded_header = next((h for h in _FORWARD_HEADERS if headers.get(h)), None)
    peer_is_trusted_proxy = is_trusted_proxy(peer_address, trusted_proxies)

    if forwarded_header is not None and not peer_is_trusted_proxy:
        return False, peer_address

    if peer_is_trusted_proxy:
        # The shared resolver (#705): X-Forwarded-For walked from the RIGHT,
        # skipping our own proxies. The first hop is the one the client wrote,
        # and taking it let a remote client claim to be local.
        client = forwarded_client(headers, trusted_proxies)
        if client is None:
            return False, peer_address
        return is_local_address(client), client

    return is_local_address(peer_address), peer_address
