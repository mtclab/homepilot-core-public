"""#688: http_call_read reaches the LAN services it advertises, privately.

Before the fix the SSRF guard resolved every hostname through a public
DNS-over-HTTPS service: each internal name went to a third party, LAN-only
names did not resolve, an IP-literal URL was refused as "DNS resolution
failed", and a registered LAN service was refused as "private".

These gates pin the corrected contract:

- names resolve through the system resolver and never through an HTTP client;
- an IP-literal URL is accepted as itself;
- the operator-registered host may resolve into the LAN ranges, any other host
  may not, and loopback / link-local / cloud metadata stay refused for every
  host, registered or not;
- the caller's path cannot move the request off the registered host.
"""

from __future__ import annotations

import contextlib
import socket
from typing import Any, ClassVar
from unittest.mock import AsyncMock

import httpx
import pytest

from homepilot.mcp.tools import ssrf_guard, system_tools
from homepilot.mcp.tools.ssrf_guard import SSRFError, validate_url


@pytest.fixture
def no_http_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any HTTP request other than the pinned call itself is a resolver leak."""
    real_get = httpx.AsyncClient.get

    async def _leak(self: httpx.AsyncClient, url: Any, *a: Any, **kw: Any) -> httpx.Response:
        if isinstance(self._transport, _RecordingPinned):
            return await real_get(self, url, *a, **kw)
        raise AssertionError(f"the SSRF guard made an HTTP request while resolving: {url}")

    monkeypatch.setattr(httpx.AsyncClient, "get", _leak)


@pytest.fixture
def system_dns(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
    """Answer the SYSTEM resolver from a table; record every name asked."""
    table: dict[str, list[str]] = {}
    asked: list[str] = []
    real = socket.getaddrinfo

    def _getaddrinfo(host: Any, port: Any, *args: Any, **kwargs: Any) -> list[Any]:
        name = host.decode() if isinstance(host, bytes) else host
        if name in table:
            asked.append(name)
            out = []
            for ip in table[name]:
                fam = socket.AF_INET6 if ":" in ip else socket.AF_INET
                addr = (ip, port or 0, 0, 0) if fam == socket.AF_INET6 else (ip, port or 0)
                out.append((fam, socket.SOCK_STREAM, 6, "", addr))
            return out
        return real(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo)
    table["__asked__"] = asked  # type: ignore[assignment]
    return table


class TestSystemResolver:
    async def test_a_name_is_resolved_by_the_system_resolver(
        self, no_http_dns: None, system_dns: dict[str, list[str]]
    ) -> None:
        system_dns["svc.example.com"] = ["93.184.216.34"]
        _, ips = await validate_url("https://svc.example.com/api")
        assert ips == ["93.184.216.34"]
        assert system_dns["__asked__"] == ["svc.example.com"]

    async def test_an_ip_literal_is_accepted_as_itself(self, no_http_dns: None) -> None:
        _, ips = await validate_url("https://93.184.216.34:8443/api")
        assert ips == ["93.184.216.34"]

    async def test_an_unresolvable_name_is_refused(self) -> None:
        with pytest.raises(SSRFError, match="DNS resolution failed"):
            await validate_url("https://nothing-here.invalid/api")


class TestRegisteredHostMayBeOnTheLan:
    @pytest.mark.parametrize(
        "ip", ["10.0.0.5", "172.16.4.4", "192.168.1.20", "100.64.0.9", "fd00::5"]
    )
    async def test_the_registered_host_reaches_its_lan_address(self, ip: str) -> None:
        resolver = AsyncMock(return_value=[ip])
        _, ips = await validate_url(
            "https://svc.example.com/api", resolver=resolver, private_host="svc.example.com"
        )
        assert ips == [ip]

    async def test_a_registered_lan_ip_literal_is_accepted(self, no_http_dns: None) -> None:
        _, ips = await validate_url("http://10.0.0.5:8080/api", private_host="10.0.0.5")
        assert ips == ["10.0.0.5"]

    @pytest.mark.parametrize(
        "ip",
        ["127.0.0.1", "::1", "169.254.169.254", "169.254.1.1", "fe80::1", "0.0.0.0", "224.0.0.1"],
    )
    async def test_loopback_and_metadata_stay_refused_for_the_registered_host(
        self, ip: str
    ) -> None:
        resolver = AsyncMock(return_value=[ip])
        with pytest.raises(SSRFError, match="private/forbidden"):
            await validate_url(
                "https://svc.example.com/api", resolver=resolver, private_host="svc.example.com"
            )

    async def test_another_host_on_the_lan_is_still_refused(self) -> None:
        resolver = AsyncMock(return_value=["10.0.0.5"])
        with pytest.raises(SSRFError, match="private/forbidden"):
            await validate_url(
                "https://other.example.com/api", resolver=resolver, private_host="svc.example.com"
            )

    async def test_no_registered_host_means_no_lan(self) -> None:
        resolver = AsyncMock(return_value=["10.0.0.5"])
        with pytest.raises(SSRFError, match="private/forbidden"):
            await validate_url("https://svc.example.com/api", resolver=resolver)


class _RecordingPinned(httpx.AsyncBaseTransport):
    """Stands in for _PinnedTransport: records the pinned IPs, answers 200."""

    seen: ClassVar[list[tuple[list[str], str]]] = []

    def __init__(self, resolved_ips: list[str], port: int | None = None) -> None:
        self._ips = resolved_ips

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        _RecordingPinned.seen.append((self._ips, str(request.url)))
        return httpx.Response(200, json={"ok": True}, request=request)


def _ctx(url: str) -> dict[str, Any]:
    vault = AsyncMock()
    vault.get_secret = AsyncMock(return_value={"url": url, "token": "t"})
    return {"vault": vault}


class TestHttpCallReadJourney:
    @pytest.fixture(autouse=True)
    def _transport(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _RecordingPinned.seen = []
        monkeypatch.setattr(system_tools, "_PinnedTransport", _RecordingPinned)
        monkeypatch.delenv("HP_ALLOWED_HTTP_DOMAINS", raising=False)

    async def test_a_registered_lan_service_answers(
        self, no_http_dns: None, system_dns: dict[str, list[str]]
    ) -> None:
        system_dns["traefik.example.com"] = ["10.0.0.7"]
        out = await system_tools.handle_http_call_read(
            {"name": "traefik", "path": "/api/overview"}, _ctx("https://traefik.example.com")
        )
        assert '"ok": true' in out[0].text
        assert _RecordingPinned.seen == [(["10.0.0.7"], "https://traefik.example.com/api/overview")]

    async def test_a_registered_ip_literal_service_answers(self, no_http_dns: None) -> None:
        out = await system_tools.handle_http_call_read(
            {"name": "svc", "path": "/health"}, _ctx("http://10.0.0.7:8080")
        )
        assert '"ok": true' in out[0].text
        assert _RecordingPinned.seen[0][0] == ["10.0.0.7"]

    async def test_a_registered_name_pointing_at_loopback_is_refused(
        self, system_dns: dict[str, list[str]]
    ) -> None:
        system_dns["svc.example.com"] = ["127.0.0.1"]
        with pytest.raises(ValueError, match="SSRF protection"):
            await system_tools.handle_http_call_read(
                {"name": "svc", "path": "/api"}, _ctx("https://svc.example.com")
            )
        assert _RecordingPinned.seen == []

    @pytest.mark.parametrize("path", ["@evil.example.net/x", "\\\\evil.example.net/x"])
    async def test_the_path_cannot_leave_the_registered_host(self, path: str) -> None:
        # Whatever the path holds, the request goes to the registered host or
        # nowhere: either the host check refuses it or it stays on svc.
        with contextlib.suppress(ValueError):
            await system_tools.handle_http_call_read(
                {"name": "svc", "path": path}, _ctx("http://10.0.0.7:8080")
            )
        for _ips, url in _RecordingPinned.seen:
            assert httpx.URL(url).host == "10.0.0.7"


def test_the_guard_has_no_public_resolver() -> None:
    """The DoH endpoint must not come back (it leaked every internal name)."""
    import inspect

    src = inspect.getsource(ssrf_guard)
    assert "dns.google" not in src
    assert "/resolve?name=" not in src
