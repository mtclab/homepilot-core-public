"""Tests for SSRF guard — private IP blocking, scheme validation, DNS rebinding prevention, allowlist."""

from __future__ import annotations

import ipaddress
from unittest.mock import AsyncMock

import httpx
import pytest

from homepilot.mcp.tools.ssrf_guard import SSRFError, _is_private_ip, _PinnedTransport, validate_url


def _mock_dns_response(ips: list[str]) -> httpx.Response:
    answers = [{"data": ip, "type": 1} for ip in ips]
    return httpx.Response(
        200,
        json={"Status": 0, "Answer": answers},
        request=httpx.Request("GET", "https://dns.google/resolve"),
    )


class TestIsPrivateIP:
    @pytest.mark.parametrize(
        "ip_str",
        [
            "10.0.0.1",
            "10.255.255.255",
            "172.16.0.1",
            "172.31.255.255",
            "192.168.0.1",
            "192.168.255.255",
            "127.0.0.1",
            "169.254.169.254",
            "169.254.0.1",
        ],
    )
    def test_private_ipv4(self, ip_str: str) -> None:
        assert _is_private_ip(ipaddress.ip_address(ip_str))

    @pytest.mark.parametrize("ip_str", ["::1", "fc00::1", "fdff:ffff::1"])
    def test_private_ipv6(self, ip_str: str) -> None:
        assert _is_private_ip(ipaddress.ip_address(ip_str))

    @pytest.mark.parametrize("ip_str", ["8.8.8.8", "1.1.1.1", "93.184.216.34"])
    def test_public_ips_ok(self, ip_str: str) -> None:
        assert not _is_private_ip(ipaddress.ip_address(ip_str))

    def test_documentation_range_blocked(self) -> None:
        # 203.0.113.0/24 (TEST-NET-3, RFC 5737) is not a routable destination;
        # the property-based catch-all now treats it as forbidden (#387).
        assert _is_private_ip(ipaddress.ip_address("203.0.113.1"))

    @pytest.mark.parametrize(
        "ip_str",
        [
            "fe80::1",  # link-local
            "::",  # unspecified
            "64:ff9b::a9fe:a9fe",  # NAT64-embedded 169.254.169.254
            "2002:a9fe:a9fe::1",  # 6to4-embedded 169.254.169.254
        ],
    )
    def test_ipv6_special_ranges_blocked(self, ip_str: str) -> None:
        # Regression for #387: these IPv6 forms bypassed the old network list
        # (only ::1/128, fc00::/7, ff00::/8 were present).
        assert _is_private_ip(ipaddress.ip_address(ip_str))

    def test_cloud_metadata_blocked(self) -> None:
        assert _is_private_ip(ipaddress.ip_address("169.254.169.254"))


class TestSchemeValidation:
    @pytest.mark.asyncio
    async def test_file_scheme_blocked(self) -> None:
        with pytest.raises(SSRFError, match="scheme 'file' not allowed"):
            await validate_url("file:///etc/passwd")

    @pytest.mark.asyncio
    async def test_ftp_scheme_blocked(self) -> None:
        with pytest.raises(SSRFError, match="scheme 'ftp' not allowed"):
            await validate_url("ftp://evil.com/file")

    @pytest.mark.asyncio
    async def test_gopher_scheme_blocked(self) -> None:
        with pytest.raises(SSRFError, match="scheme 'gopher' not allowed"):
            await validate_url("gopher://evil.com")


class TestPrivateIPBlocking:
    @pytest.mark.asyncio
    async def test_localhost_blocked(self) -> None:
        client = AsyncMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(return_value=_mock_dns_response(["127.0.0.1"]))
        client.aclose = AsyncMock()
        with pytest.raises(SSRFError, match="private/forbidden"):
            await validate_url("https://localhost/api", resolver=client)

    @pytest.mark.asyncio
    async def test_internal_ip_blocked(self) -> None:
        client = AsyncMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(return_value=_mock_dns_response(["10.0.0.1"]))
        client.aclose = AsyncMock()
        with pytest.raises(SSRFError, match="private/forbidden"):
            await validate_url("https://internal.corp/api", resolver=client)

    @pytest.mark.asyncio
    async def test_cloud_metadata_blocked(self) -> None:
        client = AsyncMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(return_value=_mock_dns_response(["169.254.169.254"]))
        client.aclose = AsyncMock()
        with pytest.raises(SSRFError, match="private/forbidden"):
            await validate_url("https://metadata.google/api", resolver=client)

    @pytest.mark.asyncio
    async def test_192_168_blocked(self) -> None:
        client = AsyncMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(return_value=_mock_dns_response(["192.168.1.1"]))
        client.aclose = AsyncMock()
        with pytest.raises(SSRFError, match="private/forbidden"):
            await validate_url("https://home.router/api", resolver=client)


class TestDNSRebinding:
    @pytest.mark.asyncio
    async def test_private_ip_in_second_answer_blocked(self) -> None:
        client = AsyncMock(spec=httpx.AsyncClient)
        mixed = [
            {"data": "8.8.8.8", "type": 1},
            {"data": "10.0.0.1", "type": 1},
        ]
        client.get = AsyncMock(
            return_value=httpx.Response(
                200,
                json={"Status": 0, "Answer": mixed},
                request=httpx.Request("GET", "https://dns.google/resolve"),
            )
        )
        client.aclose = AsyncMock()
        with pytest.raises(SSRFError, match="private/forbidden"):
            await validate_url("https://rebinding.evil/api", resolver=client)

    @pytest.mark.asyncio
    async def test_no_dns_answer_raises(self) -> None:
        client = AsyncMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(
            return_value=httpx.Response(
                200,
                json={"Status": 0, "Answer": []},
                request=httpx.Request("GET", "https://dns.google/resolve"),
            )
        )
        client.aclose = AsyncMock()
        with pytest.raises(SSRFError, match="DNS resolution failed"):
            await validate_url("https://nonexistent.invalid/api", resolver=client)


class TestDomainAllowlist:
    @pytest.mark.asyncio
    async def test_allowed_domain_passes(self) -> None:
        client = AsyncMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(return_value=_mock_dns_response(["1.2.3.4"]))
        client.aclose = AsyncMock()
        url, resolved_ips = await validate_url(
            "https://api.authentik.local/health",
            allowed_domains=["authentik.local"],
            resolver=client,
        )
        assert url == "https://api.authentik.local/health"
        assert resolved_ips == ["1.2.3.4"]

    @pytest.mark.asyncio
    async def test_subdomain_passes(self) -> None:
        client = AsyncMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(return_value=_mock_dns_response(["1.2.3.4"]))
        client.aclose = AsyncMock()
        url, resolved_ips = await validate_url(
            "https://api.authentik.local/health",
            allowed_domains=["authentik.local"],
            resolver=client,
        )
        assert "authentik.local" in url
        assert resolved_ips == ["1.2.3.4"]

    @pytest.mark.asyncio
    async def test_blocked_domain_not_in_allowlist(self) -> None:
        client = AsyncMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(return_value=_mock_dns_response(["1.2.3.4"]))
        client.aclose = AsyncMock()
        with pytest.raises(SSRFError, match="not in allowed domains"):
            await validate_url(
                "https://evil.com/api",
                allowed_domains=["authentik.local"],
                resolver=client,
            )

    @pytest.mark.asyncio
    async def test_no_allowlist_allows_any_domain(self) -> None:
        client = AsyncMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(return_value=_mock_dns_response(["1.2.3.4"]))
        client.aclose = AsyncMock()
        url, resolved_ips = await validate_url("https://any.public/api", resolver=client)
        assert url == "https://any.public/api"
        assert resolved_ips == ["1.2.3.4"]


class TestNoHostname:
    @pytest.mark.asyncio
    async def test_empty_hostname_raises(self) -> None:
        with pytest.raises(SSRFError, match="no hostname"):
            await validate_url("https:///api")


class TestPinnedTransport:
    def test_empty_resolved_ips_raises(self) -> None:
        with pytest.raises(ValueError, match="resolved_ips must not be empty"):
            _PinnedTransport(resolved_ips=[], port=443)

    def test_constructor_accepts_valid_ips(self) -> None:
        transport = _PinnedTransport(resolved_ips=["1.2.3.4"], port=443)
        assert transport._resolved_ips == ["1.2.3.4"]
        assert transport._port == 443
