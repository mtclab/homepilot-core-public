"""SSRF protection — blocks private IPs, non-HTTP schemes, DNS rebinding."""

from __future__ import annotations

import ipaddress
import logging
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
    resolver: httpx.AsyncClient | None = None,
) -> tuple[str, list[str]]:
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

    client = resolver or httpx.AsyncClient(timeout=10.0)
    close_client = resolver is None
    resolved_ips: list[str] = []
    try:
        resp = await client.get(f"https://dns.google/resolve?name={host}&type=A")
        data = resp.json()
        answers = data.get("Answer", [])
        if not answers:
            resp = await client.get(f"https://dns.google/resolve?name={host}&type=AAAA")
            data = resp.json()
            answers = data.get("Answer", [])
        if not answers:
            raise SSRFError(f"DNS resolution failed for '{host}'")
        for ans in answers:
            ip_str = ans.get("data", "")
            if not ip_str:
                continue
            try:
                addr = ipaddress.ip_address(ip_str)
            except ValueError:
                continue
            if _is_private_ip(addr):
                raise SSRFError(f"resolved IP {addr} for '{host}' is private/forbidden")
            resolved_ips.append(ip_str)
    finally:
        if close_client:
            await client.aclose()

    if not resolved_ips:
        raise SSRFError(f"no valid IP addresses resolved for '{host}'")

    return url, resolved_ips
