"""THE GATE for #708: a slow PVE never freezes HomePilot's event loop.

proxmox_mcp's coroutines call the synchronous proxmoxer/requests client inside
``async def`` - no ``to_thread`` - so every SDN/firewall read and write the
guest-network survey, the drift loop and the provision fence make froze the
whole backend (API, agent hub, reconcilers) for as long as PVE took to answer.
Measured in the audit: a 2 s answer stalled the loop 2.08 s.

Driven through the REAL library: a real ``MultiClient`` built by HomePilot's own
factory, the real ``safe_api_call`` / ``retry_with_backoff`` / module functions,
with only proxmoxer's HTTP object swapped for one that blocks the way a slow
cluster does. A heartbeat task measures how long the loop went unserved.

Teeth: make ``PveSdnGateway.__init__`` keep the raw client (drop the
``OffLoopClient`` wrap) -> both responsiveness tests fail with a gap of about
the full PVE delay.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

import pytest

from homepilot.adapters.pve_sdn import (
    PVE_LIBRARY_WORKERS,
    PveCredentials,
    PveSdnGateway,
    multi_client_from,
)

pytestmark = pytest.mark.asyncio

PVE_DELAY_S = 0.6
# Far below the delay, far above scheduler noise: a loop that was blocked for
# the PVE call shows a gap close to PVE_DELAY_S.
MAX_GAP_S = 0.25


class SlowPveResource:
    """proxmoxer's resource object, answering the way a slow cluster does: blocking."""

    def __init__(self, calls: list[tuple[str, str]]) -> None:
        self._calls = calls

    def __call__(self, *_segments: Any) -> SlowPveResource:
        return self

    def __getattr__(self, name: str) -> Any:
        if name in ("get", "put", "post", "delete"):

            def blocking(*_a: Any, **_kw: Any) -> Any:
                self._calls.append((name, threading.current_thread().name))
                time.sleep(PVE_DELAY_S)  # what requests does while PVE thinks
                return [{"node": "pve1", "status": "online"}] if name == "get" else None

            return blocking
        return self


def _gateway(calls: list[tuple[str, str]]) -> PveSdnGateway:
    creds = PveCredentials(
        base_url="https://pve.example.com:8006",
        token="monitor@pve!hp=00000000-0000-0000-0000-000000000000",
        write_token="admin@pve!hp=00000000-0000-0000-0000-000000000000",
        verify_ssl=False,
    )
    multi = multi_client_from(creds)
    endpoint = multi.clients[multi.default_endpoint]
    endpoint.monitor_client = SlowPveResource(calls)
    endpoint.admin_client = SlowPveResource(calls)
    return PveSdnGateway(multi, base_url=creds.base_url)


async def _max_loop_gap_while(work: Any) -> tuple[Any, float]:
    """Run ``work`` and return (its result, the longest the loop went unserved)."""
    gaps: list[float] = []
    done = asyncio.Event()

    async def heartbeat() -> None:
        last = time.monotonic()
        while not done.is_set():
            await asyncio.sleep(0.01)
            now = time.monotonic()
            gaps.append(now - last)
            last = now

    beat = asyncio.create_task(heartbeat())
    await asyncio.sleep(0.02)  # let the heartbeat start ticking
    try:
        result = await work
    finally:
        done.set()
        await beat
    return result, max(gaps)


class TestTheLoopStaysResponsive:
    async def test_a_slow_read_does_not_block_the_loop(self) -> None:
        calls: list[tuple[str, str]] = []
        gateway = _gateway(calls)
        nodes, gap = await _max_loop_gap_while(gateway.list_nodes())

        assert nodes == [{"node": "pve1", "status": "online"}], "the read did not complete"
        assert calls, "the slow PVE call was never made"
        assert gap < MAX_GAP_S, f"the event loop stalled {gap:.2f}s behind a PVE read"
        assert calls[0][1] != threading.main_thread().name

    async def test_a_slow_library_mutation_does_not_block_the_loop(self) -> None:
        """The library's module functions (here the per-VM fence's options write)."""
        calls: list[tuple[str, str]] = []
        gateway = _gateway(calls)
        said, gap = await _max_loop_gap_while(
            gateway.set_vm_firewall_options(node="pve1", vmid=100, enable=1)
        )

        assert calls and calls[-1][0] == "put", "the write never reached the cluster"
        assert "100" in said
        assert gap < MAX_GAP_S, f"the event loop stalled {gap:.2f}s behind a PVE write"

    async def test_the_pool_is_bounded(self) -> None:
        """A dead cluster ties up a fixed number of threads, never an unbounded set."""
        assert 1 <= PVE_LIBRARY_WORKERS <= 8
