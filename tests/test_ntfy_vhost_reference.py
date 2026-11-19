"""Lint the committed ntfy vhost reference (deploy/ntfy/nginx-ntfy.conf, #822).

A phone keeps a subscription open for hours: through nginx that only works
with the websocket upgrade headers passed, buffering off, and read timeouts
longer than ntfy's 45 s keepalive (docs/config.md "Behind a proxy",
keepalive-interval; docs/spikes/ntfy-bundle.md). nginx's own defaults break
each of those, so a copy-pasted "simplification" fails quietly: the phone
shows "connected" and never buzzes. And the admin API (phone logins) is
HomePilot's alone, over the compose network - never through this vhost.

Text lint, like tests/test_guest_vhost_reference.py: no nginx binary exists in
this project's toolchain. It proves the reference is not quietly wrong; a live
walk with a real phone proves a deployment.

Teeth: drop the Upgrade header, turn buffering back on, set
proxy_read_timeout 30s, delete the admin-path block (nginx tries regex locations
before falling back to `location /`, so its presence with a 404 is the check), or
add a client-certificate check the ntfy app cannot satisfy, and a test here fails.
"""

from __future__ import annotations

import re
from pathlib import Path

CONF = Path(__file__).resolve().parents[1] / "deploy" / "ntfy" / "nginx-ntfy.conf"
KEEPALIVE_SECONDS = 45  # ntfy keepalive-interval default (docs/config.md)


def _code() -> str:
    return "\n".join(line.split("#", 1)[0] for line in CONF.read_text().splitlines())


def _proxy_block() -> str:
    code = _code()
    start = code.index("location / {")
    return code[start : code.index("}", start)]


def _seconds(value: str) -> int:
    m = re.fullmatch(r"(\d+)([sm]?)", value)
    assert m, value
    return int(m.group(1)) * (60 if m.group(2) == "m" else 1)


def test_websockets_and_streams_pass() -> None:
    block = _proxy_block()
    assert "proxy_http_version 1.1;" in block
    assert re.search(r"proxy_set_header\s+Upgrade\s+\$http_upgrade;", block)
    assert re.search(r'proxy_set_header\s+Connection\s+"upgrade";', block)
    assert re.search(r"proxy_buffering\s+off;", block)


def test_timeouts_outlast_the_keepalive() -> None:
    block = _proxy_block()
    for directive in ("proxy_read_timeout", "proxy_send_timeout"):
        m = re.search(rf"{directive}\s+(\S+);", block)
        assert m, f"{directive} missing: nginx's 60 s default leaves no margin"
        assert _seconds(m.group(1)) > KEEPALIVE_SECONDS * 2, m.group(0)


def test_the_client_address_reaches_ntfy() -> None:
    assert re.search(
        r"proxy_set_header\s+X-Forwarded-For\s+\$proxy_add_x_forwarded_for;", _proxy_block()
    ), "ntfy runs behind-proxy: without this every phone is one visitor"


def test_the_admin_api_never_leaves_the_control_plane() -> None:
    code = _code()
    m = re.search(r"location\s+~\s+\^/v1/\(([^)]*)\)[^{]*\{\s*return\s+404;\s*\}", code)
    assert m, "the admin paths are not refused"
    assert {"users", "tiers"} <= set(m.group(1).split("|"))
    assert re.search(r"location\s+=\s+/metrics\s*\{\s*return\s+404;\s*\}", code)


def test_no_client_certificate_the_app_cannot_present() -> None:
    assert "ssl_verify_client" not in _code()


def test_one_upstream_only() -> None:
    assert len(re.findall(r"\bproxy_pass\b", _code())) == 1
