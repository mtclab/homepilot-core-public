"""Fail any test that opens a network connection off this machine (#840 review).

Thirty-six CLI tests ran under ``patch.dict(os.environ, ..., clear=True)``,
which dropped the conftest's XDG isolation, so ``hp`` read the developer's REAL
``~/.config/homepilot/client.toml`` and ran in remote mode against the server
it named - production, on the box that found it. They happened to stop at a
local-only refusal before any request; the next such test need not. The config
path is now pinned by the conftest; this is the backstop for every other way a
test could reach a real host.

``socket.socket.connect`` / ``connect_ex`` - the call every client in this
process ends in (httpx, asyncio's ``sock_connect`` and ``create_connection``,
``socket.create_connection``, ssl) - refuses an IPv4/IPv6 address that is not
loopback (or unspecified, which Linux routes to loopback) on a STREAM socket -
a connection. A datagram socket's connect() sends nothing (the hub's route
probe uses one to learn its own address) and is not judged. The attempt raises
``ConnectionRefusedError`` at once, so nothing waits on a timeout, AND it is
recorded against the running test, which then FAILS naming the address -
because a client may well catch the error and carry on. In-process transports
(``httpx.ASGITransport``, ``MockTransport``, respx) never open a socket and are
untouched; so are child processes, which isolate themselves.

There is no allowlist, not even for the documentation ranges (192.0.2.0/24 and
friends): a test that wants "unreachable" uses a loopback port nobody listens
on, which fails just as fast and leaves the machine.
"""

from __future__ import annotations

import ipaddress
import socket
import threading
from collections.abc import Callable, Generator
from typing import Any

import pytest

_violations: list[str] = []
_lock = threading.Lock()
_original: dict[str, Callable[..., Any]] = {}


class OffBoxConnectionRefusedError(ConnectionRefusedError):
    """Raised instead of connecting anywhere but this machine."""


def off_box(family: int, address: Any) -> str | None:
    """The host an INET connect would reach when it is not this machine, else None."""
    if family not in (socket.AF_INET, socket.AF_INET6):
        return None
    if not isinstance(address, tuple) or not address:
        return None
    host = str(address[0])
    try:
        ip = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        # connect() also takes a name and resolves it itself. Only the loopback
        # names are this machine; any other name is treated as off-box.
        name = host.lower().rstrip(".")
        if name == "localhost" or name.endswith(".localhost"):
            return None
        return host
    if ip.is_loopback or ip.is_unspecified:
        return None
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None and (mapped.is_loopback or mapped.is_unspecified):
        return None
    return host


def _refuse(sock: socket.socket, address: Any) -> None:
    if sock.type != socket.SOCK_STREAM:
        return
    host = off_box(sock.family, address)
    if host is None:
        return
    message = (
        f"a test connected off this machine: {address!r} "
        "(tests/network_guard.py - tests talk to loopback only)"
    )
    with _lock:
        _violations.append(message)
    raise OffBoxConnectionRefusedError(message)


def _connect(self: socket.socket, address: Any) -> None:
    _refuse(self, address)
    _original["connect"](self, address)


def _connect_ex(self: socket.socket, address: Any) -> int:
    _refuse(self, address)
    return int(_original["connect_ex"](self, address))


def install() -> None:
    if _original:
        return
    _original["connect"] = socket.socket.connect
    _original["connect_ex"] = socket.socket.connect_ex
    socket.socket.connect = _connect  # type: ignore[method-assign]
    socket.socket.connect_ex = _connect_ex  # type: ignore[method-assign]


def uninstall() -> None:
    if not _original:
        return
    socket.socket.connect = _original.pop("connect")  # type: ignore[method-assign]
    socket.socket.connect_ex = _original.pop("connect_ex")  # type: ignore[method-assign]


def _take() -> list[str]:
    with _lock:
        taken = list(_violations)
        _violations.clear()
    return taken


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[Any]
) -> Generator[None, Any, None]:
    outcome = yield
    report: pytest.TestReport = outcome.get_result()
    found = _take()
    if not found:
        return
    report.outcome = "failed"
    report.longrepr = (
        f"Network guard: the test tried to leave this machine during {report.when}:\n"
        + "\n".join(found)
        + (f"\n\n(the test also failed on its own:)\n{report.longrepr}" if call.excinfo else "")
    )


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    stray = _take()
    if stray:
        session.config.stash[_STRAY_KEY] = stray
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


_STRAY_KEY = pytest.StashKey[list[str]]()


def pytest_terminal_summary(terminalreporter: Any, exitstatus: int, config: pytest.Config) -> None:
    for message in config.stash.get(_STRAY_KEY, []):
        terminalreporter.write_line(f"Network guard (outside any test): {message}", red=True)
