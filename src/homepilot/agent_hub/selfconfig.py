"""Material the agent hub generates for itself on first boot (ADR-004 S3).

The hub's fail-closed transport check refuses a plaintext non-loopback bind. That
check is correct and stays; what must not survive is the operator decision needed
to satisfy it. This module supplies the missing material - a self-signed server
certificate - so a default install satisfies the check on its own merits rather
than by weakening it or by setting the insecure override.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import ipaddress
import logging
import os
import socket
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

logger = logging.getLogger(__name__)

# Where generated hub material lives, relative to data_dir.
HUB_DIR_NAME = "hub"
CERT_FILENAME = "hub-cert.pem"
KEY_FILENAME = "hub-key.pem"

# Certificate lifetime. Agents pin this certificate by fingerprint, so every
# rotation is a fleet-wide re-enrolment: a short lifetime would turn expiry into
# a scheduled outage. Regeneration happens on expiry or absence, never on a SAN
# change, so an already-pinned hub keeps its identity.
CERT_VALIDITY_DAYS = 3650
# Backdate slightly so a hub whose clock is a little ahead of an agent's does not
# present a not-yet-valid certificate.
CERT_BACKDATE_MINUTES = 5

_COMMON_NAME = "homepilot-agent-hub"
# Reserved-for-documentation address (RFC 5737). Connecting a UDP socket to it
# selects a route without sending a packet, which is how the primary outbound
# interface address is discovered.
_ROUTE_PROBE_ADDR = "192.0.2.1"


def primary_local_address() -> str:
    """This machine's primary non-loopback IP, or ``""`` when undiscoverable.

    Feeds the certificate's SAN list so the generated certificate covers the
    address an agent most likely dials. Advertise-host resolution itself stays
    in ``router._advertised_hub`` - this is not a second copy of it."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((_ROUTE_PROBE_ADDR, 9))
        addr = str(sock.getsockname()[0])
    except OSError:
        return ""
    finally:
        sock.close()
    if not addr or ipaddress.ip_address(addr).is_loopback:
        return ""
    return addr


def _local_hostnames() -> list[str]:
    names = ["localhost"]
    for candidate in (socket.gethostname(), socket.getfqdn()):
        name = (candidate or "").strip()
        if name and name not in names:
            names.append(name)
    return names


def _local_ip_addresses() -> list[str]:
    addrs = ["127.0.0.1", "::1"]
    primary = primary_local_address()
    if primary and primary not in addrs:
        addrs.append(primary)
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None)
    except OSError:
        infos = []
    for info in infos:
        addr = str(info[4][0]).split("%", 1)[0]
        try:
            ipaddress.ip_address(addr)
        except ValueError:
            continue
        if addr not in addrs:
            addrs.append(addr)
    return addrs


def _san_entries(extra_hosts: tuple[str, ...]) -> list[x509.GeneralName]:
    """SAN list covering every address an agent might dial this hub on.

    Name verification is not what an agent relies on (it pins the certificate),
    but a broad SAN set keeps the certificate usable by anything that does
    verify by name - browsers, openssl s_client, the CA-file path."""
    hostnames = _local_hostnames()
    ips = _local_ip_addresses()
    for raw in extra_hosts:
        host = (raw or "").strip().strip("[]")
        if not host:
            continue
        host = host.removeprefix("http://").removeprefix("https://").split("/", 1)[0]
        try:
            ipaddress.ip_address(host)
        except ValueError:
            if host not in hostnames:
                hostnames.append(host)
        else:
            if host not in ips:
                ips.append(host)
    names: list[x509.GeneralName] = [x509.DNSName(h) for h in hostnames]
    names.extend(x509.IPAddress(ipaddress.ip_address(i)) for i in ips)
    return names


def certificate_fingerprint(cert_path: Path) -> str:
    """Lowercase hex sha256 of the certificate's DER encoding.

    This is the value an agent pins, so it must be computed over the same bytes
    the agent sees on the wire (DER, not the PEM armour)."""
    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    return hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()


class HubCertificateError(Exception):
    """The hub's certificate material could not be READ, as opposed to being
    absent or corrupt.

    Regeneration re-pins the entire fleet: every agent verifies this hub by the
    sha256 of the certificate it was handed at enrolment, so a new certificate
    means every managed host refuses the handshake until it is re-enrolled by
    hand. That is not an outcome to reach from a read that did not happen (#642),
    so "I could not look" is raised rather than answered."""


def _present(path: Path) -> bool:
    """Whether ``path`` exists, distinguishing "no" from "could not tell".

    ``Path.exists()`` swallows every OSError into ``False`` - an EACCES on the
    parent directory, an EIO, a dead NFS mount all read as "the file is not
    there", which here means "generate a new one"."""
    try:
        path.stat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise HubCertificateError(
            f"could not determine whether the hub certificate material at {path} exists "
            f"({exc}). Refusing to generate a replacement: a new certificate re-pins "
            "every enrolled agent. Fix the permissions or the volume and restart."
        ) from exc
    return True


def _usable(cert_path: Path, key_path: Path) -> bool:
    """True when both files exist and the certificate is still within validity.

    An *unparseable* certificate counts as absent - the bytes are there and they
    are not a certificate, so regenerating is the only way forward. An
    *unreadable* one does not: raising is the fail-safe direction, because the
    alternative is to re-pin the whole fleet on the strength of an errno."""
    if not _present(cert_path) or not _present(key_path):
        return False
    try:
        raw = cert_path.read_bytes()
    except OSError as exc:
        raise HubCertificateError(
            f"could not read the hub certificate at {cert_path} ({exc}). Refusing to "
            "generate a replacement: a new certificate re-pins every enrolled agent, "
            "which would strand the fleet. Fix the permissions or the volume and restart."
        ) from exc
    try:
        cert = x509.load_pem_x509_certificate(raw)
    except ValueError:
        logger.warning(
            "hub certificate at %s is not a parseable certificate - regenerating", cert_path
        )
        return False
    now = _dt.datetime.now(_dt.UTC)
    if cert.not_valid_after_utc <= now:
        logger.warning(
            "hub certificate at %s expired at %s - regenerating",
            cert_path,
            cert.not_valid_after_utc.isoformat(),
        )
        return False
    return True


def _write_private(path: Path, data: bytes) -> None:
    """Create/replace ``path`` with 0600 from the moment it exists.

    ``os.open`` with the mode carries the permission through creation, so the
    key is never briefly world-readable the way write-then-chmod leaves it."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    path.chmod(0o600)


def ensure_hub_certificate(data_dir: Path, extra_hosts: tuple[str, ...] = ()) -> tuple[Path, Path]:
    """Return the hub's (cert, key) paths, generating them once if needed.

    Generation happens only when the pair is absent or the certificate has
    expired, so the fingerprint agents pinned at enrolment stays valid across
    restarts."""
    hub_dir = Path(data_dir) / HUB_DIR_NAME
    cert_path = hub_dir / CERT_FILENAME
    key_path = hub_dir / KEY_FILENAME

    if _usable(cert_path, key_path):
        return cert_path, key_path

    hub_dir.mkdir(parents=True, exist_ok=True)
    hub_dir.chmod(0o700)

    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, _COMMON_NAME)])
    now = _dt.datetime.now(_dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _dt.timedelta(minutes=CERT_BACKDATE_MINUTES))
        .not_valid_after(now + _dt.timedelta(days=CERT_VALIDITY_DAYS))
        .add_extension(x509.SubjectAlternativeName(_san_entries(extra_hosts)), critical=False)
        # CA:TRUE so the certificate can anchor itself: an agent handed this one
        # file as its trust root gets a complete chain without a separate CA.
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )

    _write_private(
        key_path,
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ),
    )
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    cert_path.chmod(0o644)
    logger.info(
        "generated self-signed agent-hub certificate at %s (sha256:%s, valid until %s)",
        cert_path,
        certificate_fingerprint(cert_path),
        cert.not_valid_after_utc.date().isoformat(),
    )
    return cert_path, key_path
