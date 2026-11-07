"""The deploy templates carry placeholders, never a real network's addresses (#704).

`deploy/control-plane/nginx-hp-proxy.conf` shipped one real LAN address in six
upstreams: an environment value committed to the tree, which the
no-environment-info rule forbids in every repository from its first commit.
The template now uses the same placeholder the public scrub already wrote
(``10.0.0.1``) and says, at its top, that every upstream must be set.

The gate: every IPv4 literal anywhere under ``deploy/`` is a loopback, the
unspecified address, the one placeholder, or an RFC 5737 documentation address.
Teeth: put any real-looking upstream back and this names the file and line.
"""

from __future__ import annotations

import ipaddress
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEPLOY = REPO_ROOT / "deploy"

_IPV4 = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.])")
_PLACEHOLDERS = {ipaddress.ip_address("10.0.0.1"), ipaddress.ip_address("0.0.0.0")}
_DOCUMENTATION = [
    ipaddress.ip_network(n) for n in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")
]


def _allowed(raw: str) -> bool:
    try:
        addr = ipaddress.ip_address(raw)
    except ValueError:
        return True  # not an address (a version string, say)
    if addr in _PLACEHOLDERS or addr.is_loopback:
        return True
    return any(addr in net for net in _DOCUMENTATION)


def test_no_deploy_file_carries_a_real_address():
    offenders: list[str] = []
    for path in sorted(p for p in DEPLOY.rglob("*") if p.is_file()):
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for raw in _IPV4.findall(line):
                if not _allowed(raw):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}")
    assert not offenders, "an environment address in a deploy template: " + ", ".join(offenders)


def test_the_proxy_template_says_its_upstreams_are_placeholders():
    text = (DEPLOY / "control-plane" / "nginx-hp-proxy.conf").read_text(encoding="utf-8")
    assert text.startswith("# ")
    assert "placeholder" in text.splitlines()[2].lower()
    assert "proxy_pass http://10.0.0.1:8000;" in text
