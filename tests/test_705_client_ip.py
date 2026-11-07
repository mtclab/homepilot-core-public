"""One trusted-proxy-aware client address, everywhere it is decided (#705).

The shipped proxy config appends the address it saw to whatever the client sent
(`X-Forwarded-For $proxy_add_x_forwarded_for`), so the FIRST hop is the one the
client wrote. Both `claim/trust._forwarded_client` and `main._get_client_ip`
took that first hop:

    claim_source('<proxy>', {'x-forwarded-for': '10.0.0.5, 203.0.113.9'}, [proxy])
      -> (True, '10.0.0.5')

a remote client posing as local, which claims an unclaimed instance without the
code once HP_TRUSTED_PROXIES is set; and the per-IP rate limits keyed on a value
the client chooses. Three more places (the SSE cap, the token-create limit, the
portal limiters) used the raw peer, so behind the proxy every operator shared
one bucket.

The rule now: walk X-Forwarded-For right to left, skipping hops that are
themselves trusted proxies; the first untrusted hop is the client. Only when the
peer is a trusted proxy - otherwise the headers are the client's own words.
"""

from __future__ import annotations

import asyncio
import ipaddress
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from homepilot.claim.trust import claim_source

PROXY = "10.0.0.2"
NETS = [ipaddress.ip_network("10.0.0.2/32")]
PROXY_LAN = [ipaddress.ip_network("10.0.0.0/8")]


class TestClaimTrustTakesTheHopTheProxyAdded:
    def test_a_spoofed_local_first_hop_is_not_local(self):
        """The exact repro from the report."""
        is_local, client = claim_source(PROXY, {"x-forwarded-for": "10.0.0.5, 203.0.113.9"}, NETS)
        assert (is_local, client) == (False, "203.0.113.9")

    def test_a_real_local_client_through_the_proxy_is_local(self):
        is_local, client = claim_source(PROXY, {"x-forwarded-for": "192.168.1.20"}, NETS)
        assert (is_local, client) == (True, "192.168.1.20")

    def test_a_chain_of_trusted_proxies_is_skipped(self):
        is_local, client = claim_source(
            "10.0.0.2", {"x-forwarded-for": "10.9.9.9, 203.0.113.9, 10.0.0.3"}, PROXY_LAN
        )
        assert (is_local, client) == (False, "203.0.113.9")

    def test_x_real_ip_from_the_trusted_proxy_is_used_when_there_is_no_xff(self):
        is_local, client = claim_source(PROXY, {"x-real-ip": "203.0.113.9"}, NETS)
        assert (is_local, client) == (False, "203.0.113.9")

    def test_an_unparseable_hop_fails_closed(self):
        is_local, _ = claim_source(PROXY, {"x-forwarded-for": "10.0.0.5, bogus"}, NETS)
        assert is_local is False

    def test_headers_from_an_untrusted_peer_are_still_refused(self):
        is_local, client = claim_source("203.0.113.50", {"x-forwarded-for": "127.0.0.1"}, NETS)
        assert (is_local, client) == (False, "203.0.113.50")


def _request(peer: str, headers: dict[str, str], trusted: list[Any]) -> Any:
    return SimpleNamespace(
        client=SimpleNamespace(host=peer),
        headers=headers,
        app=SimpleNamespace(state=SimpleNamespace(trusted_proxy_networks=trusted)),
        is_disconnected=lambda: asyncio.sleep(0, result=True),
    )


class TestRateLimitKeyIsNotClientChosen:
    def test_rotating_the_first_hop_does_not_change_the_key(self):
        from homepilot.main import _get_client_ip

        keys = {
            _get_client_ip(
                _request(PROXY, {"x-forwarded-for": f"198.51.100.{i}, 203.0.113.9"}, NETS),
                NETS,
            )
            for i in range(20)
        }
        assert keys == {"203.0.113.9"}, "the rate-limit key is chosen by the client"


class TestSseCapIsPerRealClient:
    async def test_clients_behind_the_proxy_do_not_share_one_bucket(self):
        from homepilot.artifacts import router as artifacts_router

        artifacts_router._sse_conn_count.clear()
        responses = []
        try:
            # Four different operators behind the one proxy, one tab each.
            for i in range(4):
                req = _request(PROXY, {"x-forwarded-for": f"203.0.113.{i + 1}"}, NETS)
                responses.append(await artifacts_router.sse_stream(req))  # type: ignore[arg-type]
        except HTTPException as exc:  # pragma: no cover - the defect
            pytest.fail(f"a different operator behind the proxy was refused: {exc.detail}")
        finally:
            artifacts_router._sse_conn_count.clear()
            from homepilot.sse import bus

            bus._subscribers.clear()
        assert len(responses) == 4

    async def test_one_client_is_still_capped(self):
        from homepilot.artifacts import router as artifacts_router

        artifacts_router._sse_conn_count.clear()
        try:
            req = _request(PROXY, {"x-forwarded-for": "203.0.113.7"}, NETS)
            for _ in range(artifacts_router._SSE_MAX_CONN_PER_IP):
                await artifacts_router.sse_stream(req)  # type: ignore[arg-type]
            with pytest.raises(HTTPException) as exc:
                await artifacts_router.sse_stream(req)  # type: ignore[arg-type]
            assert exc.value.status_code == 429
        finally:
            artifacts_router._sse_conn_count.clear()
            from homepilot.sse import bus

            bus._subscribers.clear()

    async def test_the_cap_is_taken_atomically(self):
        """Check and increment in one critical section: a burst of concurrent
        opens from one client admits exactly the cap, never more."""
        from homepilot.artifacts import router as artifacts_router

        artifacts_router._sse_conn_count.clear()
        req = _request(PROXY, {"x-forwarded-for": "203.0.113.8"}, NETS)
        try:
            results = await asyncio.gather(
                *(artifacts_router.sse_stream(req) for _ in range(10)),  # type: ignore[arg-type]
                return_exceptions=True,
            )
            admitted = [r for r in results if not isinstance(r, BaseException)]
            assert len(admitted) == artifacts_router._SSE_MAX_CONN_PER_IP
            assert artifacts_router._sse_conn_count["203.0.113.8"] == len(admitted)
        finally:
            artifacts_router._sse_conn_count.clear()
            from homepilot.sse import bus

            bus._subscribers.clear()


class TestTokenCreateLimitIsPerRealClient:
    async def test_one_client_exhausting_the_limit_does_not_lock_out_another(self):
        from fastapi import FastAPI
        from httpx import ASGITransport, AsyncClient

        from homepilot.auth import router as auth_router

        auth_router._token_create_attempts.clear()
        app = FastAPI()
        app.include_router(auth_router.router, prefix="/auth")
        app.state.trusted_proxy_networks = NETS
        app.state.repo = SimpleNamespace()  # never reached: no credential is sent
        transport = ASGITransport(app=app, client=(PROXY, 4242))
        try:
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                for _ in range(auth_router._TOKEN_CREATE_RATE_LIMIT):
                    r = await client.post(
                        "/auth/tokens",
                        json={"label": "x", "scope": "read_only"},
                        headers={"x-forwarded-for": "203.0.113.66"},
                    )
                    assert r.status_code == 403
                other = await client.post(
                    "/auth/tokens",
                    json={"label": "x", "scope": "read_only"},
                    headers={"x-forwarded-for": "203.0.113.77"},
                )
                assert other.status_code != 429, "a second operator was rate-limited"
                again = await client.post(
                    "/auth/tokens",
                    json={"label": "x", "scope": "read_only"},
                    headers={"x-forwarded-for": "203.0.113.66"},
                )
                assert again.status_code == 429
        finally:
            auth_router._token_create_attempts.clear()


class TestThereIsOneImplementation:
    def test_no_route_reads_the_raw_peer_for_a_client_address(self):
        """Every client-address decision goes through homepilot.client_ip. The
        raw peer is legitimately read only where the question is 'is the PEER a
        trusted proxy' (the portal/guest proxy check) or inside the resolver."""
        from pathlib import Path

        src = Path(__file__).resolve().parents[1] / "src" / "homepilot"
        allowed = {
            "client_ip.py",  # the resolver itself
            "portal/router.py:_client_cn",  # peer IS the proxy being checked
            "guest/router.py:_client_cn",
            "claim/router.py:_source",  # the peer handed to claim_source, which resolves it
            "auth/router.py:_cookie_secure",  # asks whether the PEER is a trusted proxy (#685)
        }
        offenders = []
        for path in src.rglob("*.py"):
            rel = str(path.relative_to(src))
            text = path.read_text(encoding="utf-8")
            if "request.client.host" not in text or rel in allowed:
                continue
            for lineno, line in enumerate(text.splitlines(), 1):
                if "request.client.host" in line:
                    # Which function is this in?
                    head = text.splitlines()[:lineno]
                    fn = next(
                        (
                            h.strip().split("(")[0].replace("def ", "").replace("async ", "")
                            for h in reversed(head)
                            if h.lstrip().startswith(("def ", "async def "))
                        ),
                        "?",
                    )
                    if f"{rel}:{fn}" not in allowed:
                        offenders.append(f"{rel}:{lineno} ({fn})")
        assert not offenders, f"raw peer used as a client address: {offenders}"
