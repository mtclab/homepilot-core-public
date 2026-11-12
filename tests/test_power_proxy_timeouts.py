"""The reference vhosts wait as long as a power action is followed (3.10.0).

The operator's power route (3.9.0) and, since 3.10.0, the guest portal's answer
only once Proxmox has settled the action: a start followed up to
``START_STOP_WAIT_S``, a reboot up to its shutdown timeout plus the wait margin
plus the come-back wait. nginx's default ``proxy_read_timeout`` is 60 s, so a
reboot behind the committed reference vhost answered 504 while the reboot went
on - the operator (or the friend) read "error" for an action that was happening.

Teeth: drop ``proxy_read_timeout`` from either location block, or lower it under
the longest default follow, and the matching test fails.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from homepilot.inventory.power import (
    DEFAULT_SHUTDOWN_TIMEOUT_S,
    REBOOT_COMEBACK_S,
    START_STOP_WAIT_S,
    TASK_WAIT_MARGIN_S,
)
from tests.test_guest_vhost_reference import parse_locations

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"

# The longest a power request is held at the defaults the UI and portal use.
LONGEST_FOLLOW_S = max(
    START_STOP_WAIT_S,
    DEFAULT_SHUTDOWN_TIMEOUT_S + TASK_WAIT_MARGIN_S + REBOOT_COMEBACK_S,
)


def _seconds(value: str) -> float:
    m = re.fullmatch(r"(\d+)(ms|s|m|h)?", value.strip())
    assert m, f"unreadable nginx time {value!r}"
    n, unit = int(m.group(1)), m.group(2) or "s"
    return n * {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[unit]


def _read_timeout(conf: Path, location_match: str) -> float:
    (body,) = [b for match, b in parse_locations(conf.read_text()) if match == location_match]
    found = re.search(r"\bproxy_read_timeout\s+([^;]+);", body)
    assert found, f"{conf.name} `location {location_match}` has no proxy_read_timeout (60 s)"
    return _seconds(found.group(1))


@pytest.mark.parametrize(
    ("conf", "location_match"),
    [
        pytest.param(DEPLOY / "control-plane" / "nginx-hp-proxy.conf", "/", id="operator"),
        pytest.param(
            DEPLOY / "portal" / "nginx-guest-portal.conf", "~ ^/(guest|invite)(/|$)", id="portal"
        ),
    ],
)
def test_the_vhost_outwaits_the_longest_power_follow(conf: Path, location_match: str) -> None:
    assert _read_timeout(conf, location_match) > LONGEST_FOLLOW_S
