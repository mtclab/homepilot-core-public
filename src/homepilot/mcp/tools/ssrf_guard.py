"""SSRF protection — blocks private IPs, non-HTTP schemes, DNS rebinding.

Names are resolved with the SYSTEM resolver (#688). The guard used to ask a
public DNS-over-HTTPS service, which disclosed every internal hostname to a
third party, could not see LAN-only names at all, and refused IP-literal URLs
as "DNS resolution failed".

Private ranges are refused by default. A caller that targets an
operator-registered service (``http_call_read`` reads its base URL from the
vault) passes that service's host as ``private_host``: for that one host the
LAN ranges (RFC 1918, CGNAT, IPv6 ULA) are accepted, because reaching a LAN
service the operator registered is what the tool is for. Loopback, link-local,
cloud metadata, multicast, unspecified, reserved and IPv4-embedding IPv6 forms
stay refused for every host, registered or not.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from collections.abc import Awaitable, Callable
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

_ALLOWED_SCHEMES = {"http", "https"}

_PRIVATE_NETWORKS = [
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.0.0.0/29"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("198.18.0.0/15"),
    ipaddress.ip_network("224.0.0.0/4"),
    ipaddress.ip_network("240.0.0.0/4"),
    ipaddress.ip_network("::/128"),  # IPv6 unspecified
    ipaddress.ip_network("::1/128"),  # IPv6 loopback
    ipaddress.ip_network("fc00::/7"),  # IPv6 unique-local
    ipaddress.ip_network("fe80::/10"),  # IPv6 link-local
    ipaddress.ip_network("ff00::/8"),  # IPv6 multicast
    ipaddress.ip_network("64:ff9b::/96"),  # NAT64 (embeds IPv4, incl. metadata)
    ipaddress.ip_network("2002::/16"),  # 6to4 (embeds IPv4, incl. metadata)
]

_CLOUD_METADATA_IP = ipaddress.ip_address("169.254.169.254")

# The LAN ranges an operator-registered service host may resolve into. Anything
# private that is NOT in this list (loopback, link-local, metadata, multicast,
# NAT64/6to4 embeddings, ...) stays forbidden even for a registered host.
_REGISTERED_LAN_NETWORKS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("100.64.0.0/10"),  # CGNAT / tailnet addresses
    ipaddress.ip_network("fc00::/7"),  # IPv6 unique-local
]

Resolver = Callable[[str, int | None], Awaitable[list[str]]]

_DOMAIN_ALLOWLIST: list[str] | None = None


class SSRFError(ValueError):
    pass


def set_domain_allowlist(domains: list[str] | None) -> None:
    global _DOMAIN_ALLOWLIST
    _DOMAIN_ALLOWLIST = domains


def _is_private_ip(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return True if *addr* belongs to a private/forbidden network.

    0.0.0.0/8 is included specifically to block the use of the zero address as a
    destination — some stacks treat 0.0.0.0 as "any" and may route it to
    localhost, creating an SSRF vector.
    """
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    if addr == _CLOUD_METADATA_IP:
        return True
    # Property-based catch-all — covers IPv6 forms the explicit network list may
    # miss (link-local fe80::, unspecified ::, reserved ranges, etc.).
    if (
        addr.is_private
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_loopback
        or addr.is_unspecified
    ):
        return True
    return any(addr in net for net in _PRIVATE_NETWORKS)


def _is_registered_lan_ip(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True if *addr* is a LAN address a registered service host may use."""
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    if addr == _CLOUD_METADATA_IP:
        return False
    return any(addr in net for net in _REGISTERED_LAN_NETWORKS)


async def system_resolve(host: str, port: int | None) -> list[str]:
    """Resolve *host* with the system resolver (what every other client uses).

    IP literals come back unchanged. Order is preserved and duplicates dropped.
    """
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError) as exc:
        raise SSRFError(f"DNS resolution failed for '{host}'") from exc
    ips: list[str] = []
    for info in infos:
        ip = str(info[4][0])
        if ip not in ips:
            ips.append(ip)
    return ips


class _PinnedTransport(httpx.AsyncBaseTransport):
    """Custom transport that replaces the hostname with a pinned IP.

    Forces the HTTP connection to use the DNS-verified IP, preventing
    TOCTOU DNS rebinding attacks where a second DNS lookup could resolve
    to a different (private) IP.

    Only the first resolved IP is used for pinning because
    ``validate_url()`` already verifies that **all** resolved IPs are
    non-private.  Any remaining IPs in ``resolved_ips`` that also passed
    validation would be equally safe, so selecting the first one is
    deterministic and harmless.
    """

    def __init__(self, resolved_ips: list[str], port: int | None = None) -> None:
        if not resolved_ips:
            raise ValueError("resolved_ips must not be empty")
        self._resolved_ips = resolved_ips
        self._port = port
        self._inner = httpx.AsyncHTTPTransport()

    async def handle_async_request(
        self,
        request: httpx.Request,
    ) -> httpx.Response:
        # Use the first validated IP — safe because validate_url() checked
        # every resolved IP against _is_private_ip before returning them.
        pinned_ip = self._resolved_ips[0]
        parsed = httpx.URL(str(request.url))
        port = self._port or parsed.port
        original_host = parsed.host
        if parsed.host == pinned_ip:
            return await self._inner.handle_async_request(request)

        if ":" in pinned_ip:
            netloc = f"[{pinned_ip}]" if port is None else f"[{pinned_ip}]:{port}"
        else:
            netloc = pinned_ip if port is None else f"{pinned_ip}:{port}"

        new_url = parsed.copy_with(raw_netloc=netloc.encode())
        request.url = new_url
        request.headers["host"] = original_host
        request.extensions["sni_hostname"] = original_host
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


async def validate_url(
    url: str,
    *,
    allowed_domains: list[str] | None = None,
    resolver: Resolver | None = None,
    private_host: str | None = None,
) -> tuple[str, list[str]]:
    """Check *url* and return it with the IPs the request must be pinned to.

    ``private_host`` names the one operator-registered host whose LAN addresses
    are acceptable (see the module docstring). ``resolver`` is injectable for
    tests; the default is the system resolver.
    """
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise SSRFError(f"scheme '{parsed.scheme}' not allowed (only http/https)")
    host = parsed.hostname
    if not host:
        raise SSRFError("URL has no hostname")

    domain_list = allowed_domains if allowed_domains is not None else _DOMAIN_ALLOWLIST
    if domain_list is not None:
        normalized = host.lower().rstrip(".")
        allowed = any(
            normalized == d.lower().rstrip(".") or normalized.endswith("." + d.lower().rstrip("."))
            for d in domain_list
        )
        if not allowed:
            raise SSRFError(f"host '{host}' not in allowed domains")

    registered = private_host is not None and (
        host.lower().rstrip(".") == private_host.lower().rstrip(".")
    )
    try:
        port = parsed.port
    except ValueError as exc:
        raise SSRFError(f"invalid port in URL for '{host}'") from exc

    answers = await (resolver or system_resolve)(host, port)
    if not answers:
        raise SSRFError(f"DNS resolution failed for '{host}'")

    resolved_ips: list[str] = []
    for ip_str in answers:
        try:
            addr = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        if _is_private_ip(addr) and not (registered and _is_registered_lan_ip(addr)):
            raise SSRFError(f"resolved IP {addr} for '{host}' is private/forbidden")
        resolved_ips.append(ip_str)

    if not resolved_ips:
        raise SSRFError(f"no valid IP addresses resolved for '{host}'")

    return url, resolved_ips
