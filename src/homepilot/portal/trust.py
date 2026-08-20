from __future__ import annotations

import hmac
import ipaddress
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# The header the reverse proxy sets to prove the request came THROUGH it. Its
# name is fixed because we define it (unlike the client-cert headers, whose
# names belong to the operator's existing vhost and are therefore configurable).
PROXY_SECRET_HEADER = "x-hp-portal-secret"

# The value nginx puts in $ssl_client_verify for a certificate that passed.
VERIFY_OK = "SUCCESS"


class PortalNotConfiguredError(Exception):
    """The portal is missing one of its three trust inputs; every route 503s."""


class PortalUntrustedError(Exception):
    """The request did not prove a client-certificate identity. Refuse it."""


@dataclass(frozen=True)
class PortalTrust:
    """The three layers, resolved once at request time.

    All three are REQUIRED. Any one alone is forgeable: the CN header alone is
    just a header, the source address alone does not prove the proxy ran the TLS
    handshake, and the shared secret alone can leak into a log or a mirror.
    """

    cn_header: str
    verify_header: str
    proxy_secret: str
    networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]


def _parse_networks(raw: str) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    nets: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        # strict=False so a bare host address ("10.0.0.5") is accepted as a /32.
        nets.append(ipaddress.ip_network(chunk, strict=False))
    return tuple(nets)


def load_trust(settings: Any) -> PortalTrust:
    """Resolve the trust config, or raise PortalNotConfiguredError naming what is missing.

    Fail closed: an operator who deploys the portal with a blank secret or no
    trusted proxy gets a 503 that says so, never an open portal.
    """
    missing: list[str] = []
    cn_header = (getattr(settings, "portal_cn_header", "") or "").strip().lower()
    verify_header = (getattr(settings, "portal_verify_header", "") or "").strip().lower()
    proxy_secret = getattr(settings, "portal_proxy_secret", "") or ""
    trusted_proxy = (getattr(settings, "portal_trusted_proxy", "") or "").strip()

    if not cn_header:
        missing.append("HP_PORTAL_CN_HEADER")
    if not verify_header:
        missing.append("HP_PORTAL_VERIFY_HEADER")
    if not proxy_secret:
        missing.append("HP_PORTAL_PROXY_SECRET")
    if not trusted_proxy:
        missing.append("HP_PORTAL_TRUSTED_PROXY")
    if missing:
        raise PortalNotConfiguredError(
            "The invite portal refuses client-certificate identities until the operator sets "
            + ", ".join(missing)
        )

    try:
        networks = _parse_networks(trusted_proxy)
    except ValueError as exc:
        raise PortalNotConfiguredError(
            f"HP_PORTAL_TRUSTED_PROXY is not a valid address or CIDR: {exc}"
        ) from exc
    if not networks:
        raise PortalNotConfiguredError(
            "The invite portal refuses client-certificate identities until the operator sets "
            "HP_PORTAL_TRUSTED_PROXY"
        )
    return PortalTrust(
        cn_header=cn_header,
        verify_header=verify_header,
        proxy_secret=proxy_secret,
        networks=networks,
    )


def _peer_is_trusted(peer: str | None, trust: PortalTrust) -> bool:
    if not peer:
        return False
    try:
        addr = ipaddress.ip_address(peer)
    except ValueError:
        return False
    return any(addr in net for net in trust.networks)


def _unescape_dn_value(value: str) -> str:
    """Undo RFC 4514 escaping: '\\,' -> ',', '\\2C' -> ','."""
    out: list[str] = []
    i = 0
    while i < len(value):
        ch = value[i]
        if ch != "\\" or i + 1 >= len(value):
            out.append(ch)
            i += 1
            continue
        nxt = value[i + 1 : i + 3]
        if len(nxt) == 2 and all(c in "0123456789abcdefABCDEF" for c in nxt):
            out.append(chr(int(nxt, 16)))
            i += 3
        else:
            out.append(value[i + 1])
            i += 2
    return "".join(out)


def _split_unescaped(text: str, separators: str) -> list[str]:
    """Split on separators that are neither backslash-escaped nor inside quotes."""
    parts: list[str] = []
    current: list[str] = []
    in_quotes = False
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text):
            current.append(ch)
            current.append(text[i + 1])
            i += 2
            continue
        if ch == '"':
            in_quotes = not in_quotes
            i += 1
            continue
        if ch in separators and not in_quotes:
            parts.append("".join(current))
            current = []
            i += 1
            continue
        current.append(ch)
        i += 1
    parts.append("".join(current))
    return parts


def extract_cn(dn: str) -> str | None:
    """Return the single CN of a distinguished name, or None.

    Handles both formats a proxy may hand us: RFC 4514 ("CN=friend,OU=lab,O=MTC",
    which nginx's $ssl_client_s_dn emits) and the legacy OpenSSL oneline form
    ("/C=FI/O=MTC/CN=friend", emitted by $ssl_client_s_dn_legacy and by older
    nginx builds). Multi-valued RDNs ("CN=a+OU=b") are split too.

    Returns None when there is no CN or MORE THAN ONE: a naive "take the first
    CN" would let a certificate carrying two CNs, or a value with an embedded
    escaped comma, decide which identity the portal sees.
    """
    dn = dn.strip()
    if not dn:
        return None
    # Legacy oneline form starts with the separator; commas are then ordinary
    # characters inside values and must NOT be treated as RDN separators. In the
    # RFC 4514 form ',' separates RDNs and '+' separates a multi-valued one.
    separators = "/" if dn.startswith("/") else ",+"
    found: list[str] = []
    for rdn in _split_unescaped(dn, separators):
        if not rdn.strip():
            continue
        pieces = _split_unescaped(rdn, "=")
        if len(pieces) < 2:
            continue
        attr_type = pieces[0].strip()
        value = "=".join(pieces[1:])
        if attr_type.upper() != "CN":
            continue
        found.append(_unescape_dn_value(value).strip())
    if len(found) != 1 or not found[0]:
        return None
    return found[0]


def assert_trusted_cn(
    peer: str | None,
    headers: dict[str, str] | Any,
    trust: PortalTrust,
) -> str:
    """Return the client-certificate CN, or raise PortalUntrustedError.

    Order matters only for cheapness, not for safety: every layer must pass.
    """
    if not _peer_is_trusted(peer, trust):
        logger.warning("portal: request from untrusted source %s refused", peer)
        raise PortalUntrustedError("source address is not a trusted proxy")

    presented = headers.get(PROXY_SECRET_HEADER) or ""
    if not hmac.compare_digest(presented.encode("utf-8"), trust.proxy_secret.encode("utf-8")):
        logger.warning("portal: request without the proxy shared secret refused")
        raise PortalUntrustedError("proxy shared secret missing or wrong")

    verify = (headers.get(trust.verify_header) or "").strip()
    if verify.upper() != VERIFY_OK:
        logger.warning("portal: client-cert verification header was %r, refused", verify)
        raise PortalUntrustedError("client certificate was not verified by the proxy")

    cn = extract_cn(headers.get(trust.cn_header) or "")
    if cn is None:
        logger.warning("portal: no single CN in the client-certificate subject, refused")
        raise PortalUntrustedError("client certificate carries no usable CN")
    return cn
