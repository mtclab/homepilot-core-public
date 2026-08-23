from __future__ import annotations

import asyncio
import contextlib
import hmac
import ipaddress
import json
import logging
import secrets
import signal
import ssl
import struct
import time
import uuid
from dataclasses import dataclass
from typing import Any, cast

from ..metrics.repository import MAX_METRIC_NAME_LEN, METRIC_NAME_RE
from .audit import ActionType
from .registry import AgentRegistry
from .replay import (
    NONCE_MIN_HEX_LEN,
    REPLAY_TS_WINDOW,
    NonceCache,
    ReplayError,
    ReplaySession,
)
from .tokens import BootstrapTokenStore, hash_token

logger = logging.getLogger(__name__)

# Agent-hub wire-protocol version. The Go agent sends ``"v": PROTOCOL_VERSION`` in
# the register frame and the hub echoes it (plus ``features``) in register_ack. A
# register frame WITHOUT ``v`` is an older agent and is still accepted via the
# shared/bootstrap path — the version is advisory, never a hard requirement.
PROTOCOL_VERSION = 1
# Capabilities this hub advertises to agents in register_ack. "replay-v1" is the
# app-layer replay protection negotiated per connection (see .replay).
HUB_FEATURES = ("per-agent-creds", "replay-v1")

# Per-agent credential length: 32 bytes -> 64 hex chars (secrets.token_hex(32)).
AGENT_CREDENTIAL_BYTES = 32

HEADER_LEN = 4
MAX_MESSAGE_SIZE = 1_048_576
# An oversize frame must be fully consumed to keep the stream framed. Cap how
# many bytes we are willing to drain to resync — beyond this a frame is treated
# as hostile/unrecoverable and the connection is dropped instead.
MAX_OVERSIZE_DRAIN = 16 * MAX_MESSAGE_SIZE

# Auth-flood protection (per-peer, in-memory).
MAX_AUTH_FAILURES = 5  # consecutive failed registrations before a temp ban
AUTH_BAN_SECONDS = 60.0  # how long a banned peer host stays blocked
MAX_PREAUTH_CONNECTIONS = 50  # global cap on concurrent un-authenticated sockets
PREAUTH_READ_TIMEOUT = 10  # seconds to send a valid handshake before disconnect

# Extra grace added to a caller's requested timeout when waiting for the agent
# to return a result, so the transport wait outlives the on-host deadline.
RESULT_WAIT_MARGIN = 5

# ── Metric ingest bounds (ADR-004 S5) ────────────────────────────────────────
# One frame is bounded independently of MAX_MESSAGE_SIZE: an agent flushing a
# long backlog sends several frames, and a single frame must never be able to
# commit an unbounded batch. The agent's own cap is 500.
MAX_SAMPLES_PER_FRAME = 2000
# A sample may be this far in the past (a long-buffered backlog) or this far in
# the future (clock skew). Anything outside is rejected rather than stored: a
# point dated next year would sit in the table until it fell out of retention,
# and one dated 1970 pollutes every window query in between.
MAX_SAMPLE_AGE_SECONDS = 7 * 86400
MAX_SAMPLE_SKEW_SECONDS = 300

_LOOPBACK_HOST_NAMES = {"localhost", "localhost.localdomain"}


@dataclass
class AuthResult:
    """Outcome of authenticating a register frame.

    ``kind`` is one of ``"per-agent"`` (a stored, non-revoked per-agent
    credential), ``"shared"`` (the fleet-wide shared token — enrollment only), or
    ``"bootstrap"`` (a consumed one-time enrollment token). ``agent_id`` is set
    only for ``"per-agent"`` — the id the credential is bound to."""

    kind: str
    agent_id: str | None = None


class ProtocolError(Exception):
    """A received frame is well-formed on the wire but violates the protocol
    (not a JSON object / undecodable). A single bad frame is a per-message error,
    not a reason to drop the connection."""


class OversizeMessageError(Exception):
    """A frame's declared length exceeds ``MAX_MESSAGE_SIZE``. Carries the length
    so the reader can drain the body and keep the connection framed."""

    def __init__(self, length: int) -> None:
        super().__init__(f"Message too large: {length} bytes (max {MAX_MESSAGE_SIZE})")
        self.length = length


def _is_loopback_bind(host: str) -> bool:
    """True when a bind address is loopback-only (safe for plaintext dev use).

    ``0.0.0.0`` / ``::`` bind every interface and are therefore NOT loopback —
    plaintext there is exposed on the management network."""
    h = (host or "").strip().strip("[]").lower()
    if not h:
        return False
    if h in _LOOPBACK_HOST_NAMES:
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def _encode(msg: dict[str, Any]) -> bytes:
    payload = json.dumps(msg).encode("utf-8")
    return struct.pack("!I", len(payload)) + payload


async def _read_exact(reader: asyncio.StreamReader, n: int) -> bytes:
    data = b""
    while len(data) < n:
        chunk = await reader.read(n - len(data))
        if not chunk:
            raise ConnectionError("agent disconnected")
        data += chunk
    return data


async def _read_message(reader: asyncio.StreamReader) -> dict[str, Any]:
    hdr = await _read_exact(reader, HEADER_LEN)
    length = struct.unpack("!I", hdr)[0]
    if length > MAX_MESSAGE_SIZE:
        # Do NOT read the body here — signal the caller with the length so it can
        # decide whether to drain-and-resync or drop the connection.
        raise OversizeMessageError(length)
    body = await _read_exact(reader, length)
    try:
        obj = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"undecodable JSON frame: {exc}") from exc
    if not isinstance(obj, dict):
        raise ProtocolError(f"frame must be a JSON object, got {type(obj).__name__}")
    return cast(dict[str, Any], obj)


async def _drain_oversize(reader: asyncio.StreamReader, length: int) -> bytes | None:
    """Consume an oversize frame body to keep the stream framed.

    Returns the drained bytes (so a ``request_id`` can be recovered) when within
    ``MAX_OVERSIZE_DRAIN``; returns ``None`` when the declared length is too large
    to safely consume — the caller should then drop the connection."""
    if length > MAX_OVERSIZE_DRAIN:
        return None
    return await _read_exact(reader, length)


def parse_metric_sample(item: Any, now: float) -> tuple[str, int, float] | None:
    """Validate one ``{metric, value, clock}`` entry into a storable row.

    Returns ``None`` for anything malformed. The hub is the trust boundary for
    metric names: they become storage keys and alert-rule join keys, so a frame
    can only create series the agent's own vocabulary allows."""
    if not isinstance(item, dict):
        return None
    metric = item.get("metric")
    if not isinstance(metric, str) or len(metric) > MAX_METRIC_NAME_LEN:
        return None
    if not METRIC_NAME_RE.match(metric):
        return None
    value = item.get("value")
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    value = float(value)
    if value != value or value in (float("inf"), float("-inf")):  # NaN / +-inf
        return None
    clock = item.get("clock")
    if isinstance(clock, bool) or not isinstance(clock, int):
        return None
    if clock < now - MAX_SAMPLE_AGE_SECONDS or clock > now + MAX_SAMPLE_SKEW_SECONDS:
        return None
    return metric, clock, value


class AgentCommandError(Exception):
    """An agent executed the request but returned an application-level error
    (e.g. path not in allowlist, file not found). Distinct from a transport
    failure (ConnectionError/TimeoutError)."""


class AgentHubServer:
    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 8443,
        auth_token: str | None = None,
        registry: AgentRegistry | None = None,
        ssl_context: ssl.SSLContext | None = None,
        token_store: BootstrapTokenStore | None = None,
        allow_insecure: bool = False,
        cert_fingerprint: str = "",
    ) -> None:
        self.host = host
        self.port = port
        self.auth_token = auth_token
        self.registry = registry or AgentRegistry()
        self._ssl_context = ssl_context
        self._token_store = token_store or BootstrapTokenStore()
        self.allow_insecure = allow_insecure
        # sha256 of the DER form of the certificate this hub serves. Handed out
        # at enrollment so an agent can pin THIS hub rather than trusting a
        # self-signed certificate on sight. Empty when TLS is off.
        self.cert_fingerprint = cert_fingerprint
        self._server: asyncio.Server | None = None
        # Auth-flood protection state (in-memory, per remote host).
        self._auth_failures: dict[str, int] = {}
        self._auth_bans: dict[str, float] = {}
        self._preauth_count = 0
        # Register-frame freshness state for replay-protected connections.
        self._nonce_cache = NonceCache()
        # Live connection handlers, by conn_id (#496). Python 3.12's
        # Server.wait_closed() waits for every handler to RETURN, so a shutdown
        # has to hang up on live connections itself rather than wait them out -
        # a registered agent sits in a 300s read, and an idle one that never
        # sends again would hold the wait for the full timeout.
        self._connections: dict[str, tuple[asyncio.Task[Any], asyncio.StreamWriter]] = {}

    @property
    def tls_enabled(self) -> bool:
        return self._ssl_context is not None

    def check_transport_security(self) -> None:
        """Fail closed on an exposed plaintext transport.

        A non-loopback bind with no TLS carries authentication credentials and
        privileged command/file traffic in the clear. Refuse to start unless the
        operator has explicitly opted into an insecure transport (a trusted,
        isolated network only), which we log loudly. Loopback binds are always
        allowed for local dev."""
        if self._ssl_context is not None:
            return
        if _is_loopback_bind(self.host):
            return
        if self.allow_insecure:
            logger.warning(
                "SECURITY: Agent Hub is running WITHOUT TLS on non-loopback bind "
                "%s:%s because the insecure override (HP_HUB_ALLOW_INSECURE / "
                "agent_hub.allow_insecure) is set. Authentication tokens and "
                "privileged command/file traffic are exposed in PLAINTEXT and can "
                "be captured, replayed, or altered by anyone on the path. Use only "
                "on a trusted, isolated network — never in production.",
                self.host,
                self.port,
            )
            return
        raise RuntimeError(
            f"Agent Hub refusing to start: bind {self.host}:{self.port} is not "
            "loopback and TLS is not configured. Plaintext on a routable interface "
            "exposes fleet credentials and command traffic. Configure TLS "
            "(HP_AGENT_HUB_TLS=1 with cert/key) or, for a trusted isolated network "
            "only, set HP_HUB_ALLOW_INSECURE=1 to override the fail-closed check."
        )

    @property
    def _repo(self) -> Any:
        """The persistence Repository (if any), used for per-agent credentials.

        Lives on the registry; ``None`` in unit tests that build a bare registry,
        in which case per-agent credentials are unavailable and enrollment falls
        back to the shared/bootstrap paths with no minted token."""
        return getattr(self.registry, "_repo", None)

    def _register_ack(
        self, agent_id: str, request_id: str, minted_token: str | None = None
    ) -> dict[str, Any]:
        """Build a register_ack. Carries the protocol version + advertised
        features, and — only when a fresh per-agent token was just minted — hands
        that token back so the agent persists it as its durable credential.

        The shared fleet token is NEVER handed back: per-agent credentials
        replace it as the steady-state per-connection secret. A register whose
        auth was already ``"per-agent"`` mints nothing, so ``minted_token`` is
        ``None`` and the agent keeps the credential it presented."""
        ack: dict[str, Any] = {
            "action": "register_ack",
            "agent_id": agent_id,
            "request_id": request_id,
            "v": PROTOCOL_VERSION,
            "features": list(HUB_FEATURES),
        }
        if minted_token:
            ack["auth_token"] = minted_token
        return ack

    async def _mint_agent_credential(self, agent_id: str, hostname: str) -> str | None:
        """Mint a fresh per-agent token, persist its sha256 hash bound to
        ``agent_id``/``hostname``, and return the raw token for one-time handback.

        Returns ``None`` when persistence is unavailable (no repo) — the agent
        then keeps using its shared/bootstrap enrollment credential."""
        repo = self._repo
        if repo is None:
            return None
        token = secrets.token_hex(AGENT_CREDENTIAL_BYTES)
        await repo.set_agent_credential(agent_id, hostname, hash_token(token))
        logger.info("minted per-agent credential for %s (host=%s)", agent_id, hostname)
        return token

    async def _rebind_by_hostname(
        self, token: str, claimed_agent_id: str, claimed_hostname: str
    ) -> AuthResult | None:
        """Recognise a host that came back under a NEW ``agent_id``.

        An agent that loses its persisted identity (or is reinstalled) still holds
        the per-agent token it was issued. Without this the token authenticates
        against nothing, the agent retries, and the peer is banned after
        ``MAX_AUTH_FAILURES`` — a permanent lockout for a host that holds a
        perfectly valid credential.

        Acceptance still requires POSSESSION: the token must match the hash of a
        non-revoked credential issued to this exact hostname. On a match the
        credential is rebound to the newly claimed id (the hash is unchanged, so
        the agent keeps using the same token). Returns ``None`` — i.e. falls
        through to the shared/bootstrap paths — when nothing matches.
        """
        repo = self._repo
        if repo is None or not claimed_hostname or not claimed_agent_id:
            return None
        presented = hash_token(token)
        for row in await repo.get_agent_credentials_by_hostname(claimed_hostname):
            stored = row.get("credential_hash")
            prior_id = row.get("agent_id")
            if not stored or prior_id == claimed_agent_id:
                continue
            if not hmac.compare_digest(presented, stored):
                continue
            if await repo.rebind_agent_credential(prior_id, claimed_agent_id):
                logger.warning(
                    "rebinding per-agent credential on host %s: %s -> %s "
                    "(agent returned with a new agent_id)",
                    claimed_hostname,
                    prior_id,
                    claimed_agent_id,
                )
                return AuthResult(kind="per-agent", agent_id=claimed_agent_id)
        return None

    async def _verify_auth(
        self, request: dict[str, Any], deny: list[str] | None = None
    ) -> AuthResult | None:
        """Authenticate a register frame. Returns an :class:`AuthResult` or
        ``None`` on failure.

        ``deny`` is an out-parameter: on failure the SPECIFIC reason is appended
        to it. It is a list rather than a richer return type because the reason
        is diagnostic, not part of the auth decision - every caller still branches
        on ``None``. Without it the four distinct refusals here collapse into one
        "invalid auth_token" on the wire, which is exactly the hole #430 names:
        a revoked agent, a stolen token and a typo are indistinguishable.

        Precedence:
          1. ``"per-agent"`` — the presented token's sha256 hash matches the
             stored, non-revoked credential for the frame's claimed ``agent_id``
             AND the claimed ``hostname`` matches the one the credential was
             issued to. A per-agent token presented with a different
             ``hostname`` than it was issued to is rejected outright. A REVOKED
             credential whose token does match is rejected outright (``None``).
          1b. ``"per-agent"`` (rebind) — no credential matches the claimed
             ``agent_id``, but the token matches a non-revoked credential issued
             to the SAME ``hostname``. The host proves possession of a credential
             issued to it, so it is accepted and the credential is REBOUND to the
             newly claimed ``agent_id``. This is what lets a host whose id changed
             (e.g. an agent that lost its persisted id) reconnect instead of
             bricking; the token still has to match a credential issued to that
             exact hostname, so nothing is accepted on hostname alone.
          2. ``"shared"`` — the fleet-wide shared token (enrollment only).
          3. ``"bootstrap"`` — a consumed one-time enrollment token.
        """

        def _deny(reason: str) -> None:
            if deny is not None:
                deny.append(reason)

        token = request.get("auth_token", "")
        if not token:
            _deny("register carried no auth token")
            return None

        claimed_agent_id = request.get("agent_id", "")
        claimed_hostname = request.get("hostname", "")
        repo = self._repo
        if repo is not None and claimed_agent_id:
            row = await repo.get_agent_credential(claimed_agent_id)
            stored = row.get("credential_hash") if row else None
            if stored and hmac.compare_digest(hash_token(token), stored):
                # The token IS this agent's per-agent credential.
                if row.get("revoked_at"):
                    logger.warning("rejecting revoked credential for agent %s", claimed_agent_id)
                    _deny("credential revoked by an operator; re-enrol this host to restore it")
                    return None
                # Bind check: the credential is tied to the hostname it was issued
                # to. A token replayed from a different host is rejected.
                bound_host = row.get("hostname")
                if bound_host and claimed_hostname and bound_host != claimed_hostname:
                    logger.warning(
                        "rejecting per-agent credential for %s: bound to host %s, "
                        "presented from %s",
                        claimed_agent_id,
                        bound_host,
                        claimed_hostname,
                    )
                    _deny(
                        f"credential is bound to host {bound_host} but was presented "
                        f"from {claimed_hostname}"
                    )
                    return None
                return AuthResult(kind="per-agent", agent_id=claimed_agent_id)

            # Reaching here means no credential is stored under the claimed id (or
            # the stored one does not match this token) — the host may have come
            # back with a different agent_id. Accept it only if the token matches
            # a live credential issued to the SAME hostname, then rebind that
            # credential to the id now being claimed.
            rebound = await self._rebind_by_hostname(token, claimed_agent_id, claimed_hostname)
            if rebound is not None:
                return rebound

        if self.auth_token and hmac.compare_digest(token, self.auth_token):
            return AuthResult(kind="shared")
        if await self._token_store.consume(token):
            return AuthResult(kind="bootstrap")
        _deny("auth token matched no per-agent credential, the shared token or a bootstrap token")
        return None

    async def _record_rejection(
        self,
        *,
        reason: str,
        peer_host: str,
        agent_id: str = "",
        hostname: str | None = None,
    ) -> None:
        """Make a refusal visible instead of only logging it (#430).

        Every rejection path used to be `logger.warning` and nothing else, so an
        operator looking at the UI could not tell a revoked agent from a banned
        peer from a powered-off box - all three are a grey dot. A refusal now
        lands in two places with different jobs: the AUDIT LOG keeps the durable,
        append-only history of who was turned away and why, and the agent row
        keeps the LAST reason, which is the one the fleet list has to show.

        Best-effort by construction: a hub that cannot write the reason down must
        still refuse the connection, so a storage failure is logged, not raised.
        """
        logger.warning(
            "agent rejected from %s (agent=%s host=%s): %s",
            peer_host,
            agent_id or "?",
            hostname or "?",
            reason,
        )
        try:
            self.registry.audit_log.log(
                agent_id=agent_id,
                action="register_rejected",
                command_or_path=f"peer={peer_host}: {reason}",
                result="blocked",
                hostname=hostname,
                caller="system",
            )
        except Exception as exc:  # pragma: no cover - audit must never break auth
            logger.warning("could not audit the rejection: %s", exc)
        if self._repo is None:
            return
        try:
            await self._repo.record_agent_error(agent_id, reason, hostname)
        except Exception as exc:
            logger.warning("could not record the rejection reason: %s", exc)

    def _record_auth_failure(self, peer_host: str, loop: asyncio.AbstractEventLoop) -> None:
        """Track consecutive failed registrations from a peer and temp-ban after
        ``MAX_AUTH_FAILURES`` to blunt a token-guessing flood."""
        n = self._auth_failures.get(peer_host, 0) + 1
        self._auth_failures[peer_host] = n
        if n >= MAX_AUTH_FAILURES:
            self._auth_bans[peer_host] = loop.time() + AUTH_BAN_SECONDS
            self._auth_failures.pop(peer_host, None)
            logger.warning(
                "peer %s temporarily banned for %.0fs after %d failed registrations",
                peer_host,
                AUTH_BAN_SECONDS,
                n,
            )

    async def _emit(
        self,
        writer: asyncio.StreamWriter,
        session: ReplaySession | None,
        msg: dict[str, Any],
    ) -> None:
        """Write one outbound frame, stamping ``seq``+``mac`` on a replay-protected
        connection. The stamp+write is done under the session lock so concurrent
        senders (the connection loop and command dispatch) cannot interleave and
        reorder sequence numbers on the wire."""
        if session is None:
            writer.write(_encode(msg))
            await writer.drain()
            return
        async with session.lock:
            framed = session.stamp(msg)
            writer.write(_encode(framed))
            await writer.drain()

    def _check_register_freshness(self, agent_id: str, frame: dict[str, Any]) -> str | None:
        """Validate a replay-protected register frame's ``nonce``+``ts``.

        Returns ``None`` when the frame is fresh (and records the nonce), or a
        short reason string when it must be rejected: a missing/short nonce, a
        missing/invalid ts, a timestamp outside ``REPLAY_TS_WINDOW``, or a nonce
        already seen for this agent within the window (a replayed register)."""
        nonce = frame.get("nonce")
        ts = frame.get("ts")
        if not isinstance(nonce, str) or len(nonce) < NONCE_MIN_HEX_LEN:
            return "missing or short nonce"
        if not isinstance(ts, int) or isinstance(ts, bool):
            return "missing or invalid ts"
        now = time.time()
        if abs(now - ts) > REPLAY_TS_WINDOW:
            return "stale timestamp"
        if not self._nonce_cache.check_and_record(agent_id, nonce, now):
            return "duplicate nonce"
        return None

    async def _handle_oversize(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        agent_id: str,
        exc: OversizeMessageError,
    ) -> bool:
        """Handle a single oversize frame as a per-request error.

        Drains the body to keep the stream framed, routes a clean "response too
        large" error to any waiting caller, and acks the agent. Returns ``True`` if
        the connection may continue, ``False`` if the frame is too large to safely
        resync and the connection must be dropped."""
        body = await _drain_oversize(reader, exc.length)
        if body is None:
            logger.warning(
                "oversize frame (%d bytes) from %s exceeds drain cap; dropping connection",
                exc.length,
                agent_id,
            )
            return False
        request_id = ""
        try:
            parsed = json.loads(body)
            if isinstance(parsed, dict):
                request_id = str(parsed.get("request_id", ""))
        except (ValueError, UnicodeDecodeError):
            request_id = ""
        err = {
            "error": "response too large",
            "detail": f"message of {exc.length} bytes exceeds max {MAX_MESSAGE_SIZE}",
            "request_id": request_id,
        }
        # Route the failure to the caller waiting on this request (if any) so it
        # gets a clean error instead of timing out, then ack the agent.
        if request_id:
            self.registry.store_command_result(agent_id, err)
        writer.write(_encode(err))
        await writer.drain()
        logger.warning(
            "oversize frame (%d bytes) from %s returned as per-request error",
            exc.length,
            agent_id,
        )
        return True

    async def _handle_agent(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = writer.get_extra_info("peername")
        peer_host = peer[0] if isinstance(peer, tuple) and peer else "?"
        agent_id: str | None = None
        conn_id = str(uuid.uuid4())
        registered = False
        # Why this connection ended, in the operator's words (#430). A plain
        # socket drop and a fail-closed protocol kill look identical from the UI
        # otherwise, and the second one is the one worth waking up for.
        disconnect_reason = "connection closed"
        loop = asyncio.get_running_loop()

        # Tracked so a shutdown can hang up on this connection (#496). Registered
        # BEFORE the auth-flood guards below, which can return early - an
        # untracked handler is one wait_closed() can still wait on.
        handler = asyncio.current_task()
        if handler is not None:
            self._connections[conn_id] = (handler, writer)

        # Auth-flood guards, evaluated before we read or trust anything.
        ban_until = self._auth_bans.get(peer_host)
        if ban_until is not None and loop.time() < ban_until:
            # No handshake has been read, so the only identity is the peer: match
            # the reason onto whatever agent the hub knows at this address by
            # hostname only if one is claimed later. Here it is audit-only.
            await self._record_rejection(
                reason=(
                    f"peer temporarily banned after {MAX_AUTH_FAILURES} failed "
                    f"registrations; retry in {AUTH_BAN_SECONDS:.0f}s"
                ),
                peer_host=peer_host,
            )
            try:
                writer.close()
                await writer.wait_closed()
            finally:
                # In a finally: a peer that resets while banned makes
                # wait_closed() raise, and an entry left behind here would leak
                # for the life of the process - under exactly the flood this
                # guard exists for.
                self._connections.pop(conn_id, None)
            return
        if self._preauth_count >= MAX_PREAUTH_CONNECTIONS:
            logger.warning(
                "pre-auth connection cap (%d) reached; rejecting %s",
                MAX_PREAUTH_CONNECTIONS,
                peer_host,
            )
            try:
                writer.close()
                await writer.wait_closed()
            finally:
                self._connections.pop(conn_id, None)
            return

        self._preauth_count += 1
        preauth_counted = True

        def _release_preauth() -> None:
            nonlocal preauth_counted
            if preauth_counted:
                self._preauth_count -= 1
                preauth_counted = False

        try:
            try:
                handshake = await asyncio.wait_for(
                    _read_message(reader), timeout=PREAUTH_READ_TIMEOUT
                )
            except (TimeoutError, ConnectionError, ProtocolError, OversizeMessageError):
                return

            deny: list[str] = []
            auth_result = await self._verify_auth(handshake, deny)
            if auth_result is None:
                self._record_auth_failure(peer_host, loop)
                _release_preauth()
                await self._record_rejection(
                    reason=deny[0] if deny else "invalid auth token",
                    peer_host=peer_host,
                    agent_id=str(handshake.get("agent_id", "")),
                    hostname=str(handshake.get("hostname", "")) or None,
                )
                writer.write(
                    _encode(
                        {
                            "error": "invalid auth_token",
                            "request_id": handshake.get("request_id", ""),
                        }
                    )
                )
                await writer.drain()
                writer.close()
                await writer.wait_closed()
                return

            # Authenticated: reset the failure streak and leave the pre-auth pool.
            self._auth_failures.pop(peer_host, None)
            _release_preauth()

            action = handshake.get("action", "")
            if action != "register":
                writer.write(
                    _encode(
                        {
                            "error": "must register first",
                            "request_id": handshake.get("request_id", ""),
                        }
                    )
                )
                await writer.drain()
                writer.close()
                await writer.wait_closed()
                return

            hostname = handshake.get("hostname", "unknown")
            agent_id = handshake.get("agent_id", str(uuid.uuid4()))
            system_info = handshake.get("system_info", {})

            # Replay protection is enabled for a connection ONLY when the agent
            # asked for it (register carried "replay": 1) AND it authenticated
            # with its per-agent token (the shared secret used as the MAC key).
            # Enrollment (shared/bootstrap) connections have no per-agent key yet
            # and are exempt; they get one in the ack and reconnect protected.
            replay_enabled = handshake.get("replay") == 1 and auth_result.kind == "per-agent"
            if replay_enabled:
                reason = self._check_register_freshness(agent_id, handshake)
                if reason is not None:
                    await self._record_rejection(
                        reason=f"register frame rejected as replayed or stale: {reason}",
                        peer_host=peer_host,
                        agent_id=agent_id,
                        hostname=hostname,
                    )
                    writer.write(
                        _encode(
                            {
                                "error": f"register rejected: {reason}",
                                "request_id": handshake.get("request_id", ""),
                            }
                        )
                    )
                    await writer.drain()
                    writer.close()
                    await writer.wait_closed()
                    return

            accepted = self.registry.register(
                agent_id=agent_id,
                hostname=hostname,
                system_info=system_info,
                writer=writer,
                conn_id=conn_id,
            )
            if not accepted:
                await self._record_rejection(
                    reason=(
                        "identity already claimed by a live connection - another agent "
                        "holds this hostname or agent id"
                    ),
                    peer_host=peer_host,
                    agent_id=agent_id,
                    hostname=hostname,
                )
                writer.write(
                    _encode(
                        {
                            "error": "identity already claimed by a live connection",
                            "request_id": handshake.get("request_id", ""),
                        }
                    )
                )
                await writer.drain()
                writer.close()
                await writer.wait_closed()
                return
            registered = True
            logger.info("agent registered: %s (host=%s)", agent_id, hostname)
            # Lifecycle audit: record the connection event so the durable trail
            # covers register/disconnect, not just commands (#381). The hub itself
            # is the actor, so the caller is "system".
            self.registry.audit_log.log(
                agent_id=agent_id,
                action="register",
                command_or_path="",
                result="success",
                hostname=hostname,
                caller="system",
            )

            # Mint a fresh per-agent credential for an enrollment (shared/bootstrap)
            # connection so the agent stops using the shared/bootstrap token and
            # reconnects with its own durable, revocable credential. A connection
            # that already authenticated per-agent keeps its existing credential.
            minted_token: str | None = None
            if auth_result.kind in ("shared", "bootstrap"):
                minted_token = await self._mint_agent_credential(agent_id, hostname)

            # Establish the per-connection replay session (if negotiated) BEFORE the
            # ack so every subsequent frame in both directions is protected. It is
            # stored on the agent record so command dispatch (send_command et al.,
            # running in the caller's coroutine) stamps outbound frames too.
            replay_session: ReplaySession | None = None
            if replay_enabled:
                replay_session = ReplaySession(handshake["auth_token"].encode("utf-8"))
                agent_rec = self.registry.get(agent_id)
                if agent_rec is not None:
                    agent_rec.replay = replay_session

            # The register_ack is the handshake reply and is NOT sequence/MAC
            # framed — replay framing starts on the frames AFTER it.
            writer.write(
                _encode(
                    self._register_ack(
                        agent_id, handshake.get("request_id", ""), minted_token=minted_token
                    )
                )
            )
            await writer.drain()

            while True:
                try:
                    msg = await asyncio.wait_for(_read_message(reader), timeout=300)
                except TimeoutError:
                    disconnect_reason = "no frame for 300s; hub closed the idle connection"
                    break
                except ConnectionError as exc:
                    disconnect_reason = f"transport error: {exc}" if str(exc) else "connection lost"
                    break
                except OversizeMessageError as exc:
                    if replay_session is not None:
                        # On a replay-protected connection an unverifiable oversize
                        # frame is fail-closed (a bad/hostile frame cannot be MAC'd).
                        logger.warning(
                            "oversize frame on replay-protected connection %s; closing",
                            agent_id,
                        )
                        disconnect_reason = (
                            "oversize frame on a replay-protected connection; closed fail-closed"
                        )
                        break
                    # One oversize frame is a per-request failure, not a transport
                    # death — drain it, report an error, keep the agent connected.
                    if not await self._handle_oversize(reader, writer, agent_id, exc):
                        break
                    continue
                except ProtocolError as exc:
                    if replay_session is not None:
                        logger.warning(
                            "protocol error on replay-protected connection %s (%s); closing",
                            agent_id,
                            exc,
                        )
                        disconnect_reason = f"protocol error on a protected connection: {exc}"
                        break
                    writer.write(_encode({"error": f"protocol error: {exc}", "request_id": ""}))
                    await writer.drain()
                    continue

                # Replay check: verify seq+mac on every inbound frame (heartbeats
                # included) before acting on it. Any failure is fail-closed.
                if replay_session is not None:
                    try:
                        replay_session.verify(msg)
                    except ReplayError as exc:
                        logger.warning(
                            "replay check failed on connection %s (%s); closing",
                            agent_id,
                            exc,
                        )
                        disconnect_reason = f"replay check failed: {exc}"
                        break

                request_id = msg.get("request_id", "")
                msg_action = msg.get("action", "")

                if msg_action == "heartbeat":
                    self.registry.update_heartbeat(agent_id)
                    ack = {"action": "heartbeat_ack", "request_id": request_id}
                    await self._emit(writer, replay_session, ack)
                elif msg_action == "command_result":
                    self.registry.store_command_result(agent_id, msg)
                    ack = {"action": "result_ack", "request_id": request_id}
                    await self._emit(writer, replay_session, ack)
                elif msg_action == "metrics":
                    stored, rejected = await self._ingest_metrics(agent_id, msg)
                    # The ack is what lets the agent free its buffer, so it is
                    # sent only AFTER the samples are committed. An agent that
                    # gets no ack re-sends rather than losing the batch.
                    metrics_ack: dict[str, Any] = {
                        "action": "metrics_ack",
                        "request_id": request_id,
                        "accepted": stored,
                        "rejected": rejected,
                    }
                    await self._emit(writer, replay_session, metrics_ack)
                else:
                    await self._emit(
                        writer,
                        replay_session,
                        {"error": f"unknown action: {msg_action}", "request_id": request_id},
                    )

        except Exception as exc:
            logger.exception("error handling agent %s", agent_id or peer)
            disconnect_reason = f"hub error while handling this agent: {exc}"
        finally:
            self._connections.pop(conn_id, None)
            _release_preauth()
            if registered and agent_id:
                # Lifecycle audit before eviction so the durable trail records the
                # disconnect (#381). Captured while the record still exists.
                disc_agent = self.registry.get(agent_id)
                self.registry.audit_log.log(
                    agent_id=agent_id,
                    action="disconnect",
                    command_or_path="",
                    result="success",
                    hostname=disc_agent.hostname if disc_agent else None,
                    caller="system",
                )
                # Compare-and-delete: only evict if THIS connection still owns the
                # registration (a reconnect may have replaced us already).
                self.registry.unregister(agent_id, conn_id, reason=disconnect_reason)
                logger.info("agent disconnected: %s (%s)", agent_id, disconnect_reason)
            writer.close()
            await writer.wait_closed()

    async def _ingest_metrics(self, agent_id: str, msg: dict[str, Any]) -> tuple[int, int]:
        """Store one metrics frame and refresh this agent's live state.

        The metrics frame is the agent's ONLY state channel (ADR-004 S5): it
        carries the fresh values AND doubles as the periodic touch that keeps the
        persisted ``last_heartbeat`` moving. The former ``report_state`` action
        was a second, dead channel - no agent ever sent it, which is why the
        Agents view showed an empty state and a frozen heartbeat (#430).

        Returns ``(accepted, rejected)``. A frame with unusable entries is never
        a reason to fail the whole batch: the good samples are stored and the bad
        ones are counted back to the agent."""
        agent = self.registry.get(agent_id)
        hostname = agent.hostname if agent else ""
        raw = msg.get("samples")
        if not isinstance(raw, list):
            logger.warning("metrics frame from %s carried no sample list", agent_id)
            return 0, 0

        rejected = max(0, len(raw) - MAX_SAMPLES_PER_FRAME)
        rows: list[tuple[str, int, float]] = []
        for item in raw[:MAX_SAMPLES_PER_FRAME]:
            parsed = parse_metric_sample(item, time.time())
            if parsed is None:
                rejected += 1
                continue
            rows.append(parsed)

        if rejected:
            logger.warning(
                "metrics frame from %s (host=%s): %d of %d samples rejected as malformed "
                "or out of range",
                agent_id,
                hostname,
                rejected,
                len(raw),
            )

        metrics_repo = getattr(self.registry, "metrics_repo", None)
        if metrics_repo is not None and rows and hostname:
            await metrics_repo.insert_samples(hostname, agent_id, rows)

        # Newest value per metric becomes the agent's live state, so the Agents
        # view shows real numbers instead of an empty object.
        if rows:
            latest: dict[str, Any] = {}
            newest_ts: dict[str, int] = {}
            for metric, ts, value in rows:
                if ts >= newest_ts.get(metric, -1):
                    newest_ts[metric] = ts
                    latest[metric] = value
            self.registry.update_state(agent_id, latest)
        return len(rows), rejected

    async def start(self) -> None:
        self.check_transport_security()
        if self._ssl_context:
            self._server = await asyncio.start_server(
                self._handle_agent, self.host, self.port, ssl=self._ssl_context
            )
            logger.info("agent hub listening on %s:%s (TLS)", self.host, self.port)
        else:
            self._server = await asyncio.start_server(self._handle_agent, self.host, self.port)
            logger.info("agent hub listening on %s:%s", self.host, self.port)

    def is_listening(self) -> bool:
        """Whether the hub is actually accepting connections.

        A constructed AgentHubServer is not a serving one - start() binds the
        socket in a background task - so the startup self-check must ask the
        socket, not the object's existence."""
        return self._server is not None and self._server.is_serving()

    async def serve_forever(self) -> None:
        await self.start()
        stop = asyncio.Event()

        def _signal_handler() -> None:
            stop.set()

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _signal_handler)
        await stop.wait()
        await self.stop()

    # A shutdown that cannot complete is a shutdown that never happens (#496).
    # Sized so the WHOLE of stop() - hang-up wait, cancellation unwind, then
    # wait_closed - fits inside the budget main.lifespan allows it. If stop()
    # can outlast its caller, the caller's guard cancels it midway and
    # registry.drain() below never runs, which is the #496 precondition again.
    _STOP_TIMEOUT_SECONDS = 3.0

    async def stop(self, timeout: float | None = None) -> None:
        """Stop listening, hang up on every live agent, and never wait forever.

        Since Python 3.12 ``Server.wait_closed()`` returns only once every
        connection HANDLER has finished, not merely when the listening socket is
        shut. Our handler sits in a 300s read waiting for the agent's next frame,
        so a plain ``close() + wait_closed()`` waits up to five minutes per
        connected agent - and forever if a peer never hangs up at all. That is
        the deadlock that wedged a fixture teardown and took `make gate` with it,
        and in production it is a backend that ignores SIGTERM until Docker kills
        it. So: close the listener, hang up on the live connections ourselves,
        and bound what is left.
        """
        limit = self._STOP_TIMEOUT_SECONDS if timeout is None else timeout
        # Claimed up front. stop() now awaits between the close and the
        # wait_closed, so a second concurrent caller (serve_forever's signal
        # handler racing the lifespan shutdown) would otherwise find
        # self._server set to None mid-flight and raise AttributeError - out of
        # a lifespan `finally`, which would skip every later shutdown step.
        server = self._server
        self._server = None
        if server is not None:
            server.close()
            await self._close_live_connections(limit)
            try:
                await asyncio.wait_for(server.wait_closed(), limit)
            except TimeoutError:
                logger.warning(
                    "agent hub: %d connection handler(s) still running after %.0fs; "
                    "abandoning the wait rather than blocking the shutdown",
                    len(self._connections),
                    limit,
                )
            logger.info("agent hub stopped")
        # Let the registry's fire-and-forget writes finish before whoever called
        # us closes the database under them (#496). Bounded; a write that cannot
        # finish must not stop a shutdown.
        await self.registry.drain()

    async def _close_live_connections(self, timeout: float) -> None:
        """Hang up on every tracked connection, then cancel what does not finish.

        Closing the writer is the polite half: the handler's next read raises and
        it runs its own disconnect path (audit entry, unregister, reason). The
        cancel is for a handler that is blocked somewhere a closed socket does
        not reach.
        """
        live = list(self._connections.values())
        if not live:
            return
        logger.info("agent hub: hanging up on %d live agent connection(s)", len(live))
        for _task, writer in live:
            with contextlib.suppress(Exception):
                writer.close()
        tasks = [t for t, _w in live if not t.done()]
        if not tasks:
            return
        # Two thirds to wait, one third to unwind: the whole of this call has to
        # fit in `timeout`, because stop() spends the rest on wait_closed().
        _done, pending = await asyncio.wait(tasks, timeout=timeout * 2 / 3)
        for task in pending:
            task.cancel()
        if pending:
            # Give the cancellations a turn to unwind so wait_closed() is not
            # left waiting on a handler that is already on its way out.
            with contextlib.suppress(Exception):
                await asyncio.wait(pending, timeout=timeout / 3)

    def _finalize_result(
        self,
        agent_id: str,
        action: str,
        command_or_path: str,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        """Audit by real outcome and raise if the agent reported an error.

        The agent replies with an ``error`` key when the request failed on the
        host (path not in allowlist, file not found, …). Without this check the
        caller would treat a rejected write as success."""
        error = result.get("error")
        agent = self.registry.get(agent_id)
        self.registry.audit_log.log(
            agent_id=agent_id,
            action=action,  # type: ignore[arg-type]
            command_or_path=command_or_path,
            result="error" if error is not None else "success",
            exit_code=result.get("exit_code"),
            hostname=agent.hostname if agent else None,
        )
        if error is not None:
            raise AgentCommandError(str(error))
        return result

    async def send_command(self, agent_id: str, command: str, timeout: int = 30) -> dict[str, Any]:
        agent = self.registry.get(agent_id)
        if not agent or not agent.writer:
            self.registry.audit_log.log(
                agent_id=agent_id,
                action="exec",
                command_or_path=command,
                result="error",
                exit_code=None,
                hostname=agent.hostname if agent else None,
            )
            raise ConnectionError(f"agent {agent_id} not connected")

        request_id = str(uuid.uuid4())
        msg = {
            "action": "exec",
            "command": command,
            "timeout": timeout,
            "request_id": request_id,
        }
        await self._emit(agent.writer, agent.replay, msg)

        result = await asyncio.wait_for(
            self.registry.wait_for_result(agent_id, request_id),
            timeout=timeout + RESULT_WAIT_MARGIN,
        )
        return self._finalize_result(agent_id, "exec", command, result)

    async def send_action(
        self,
        agent_id: str,
        action: ActionType,
        params: dict[str, Any],
        timeout: int = 30,
    ) -> dict[str, Any]:
        """Dispatch a native provisioning action (#397 phase-B1) to an agent and
        await its structured result.

        Reuses the same request/future plumbing as send_command / send_write_file:
        the outbound frame is stamped via ``_emit`` (replay framing preserved), the
        wait honours the caller timeout plus ``RESULT_WAIT_MARGIN``, and the result
        is routed through ``_finalize_result`` so an agent-reported error surfaces
        as :class:`AgentCommandError` instead of a silent success. ``params`` are
        merged into the frame (e.g. ``{"name": ...}`` / ``{"path", "content",
        "mode"}``)."""
        target = str(params.get("name") or params.get("path") or "")
        agent = self.registry.get(agent_id)
        if not agent or not agent.writer:
            self.registry.audit_log.log(
                agent_id=agent_id,
                action=action,
                command_or_path=target,
                result="error",
                exit_code=None,
                hostname=agent.hostname if agent else None,
            )
            raise ConnectionError(f"agent {agent_id} not connected")

        request_id = str(uuid.uuid4())
        msg = {"action": action, "request_id": request_id, **params}
        await self._emit(agent.writer, agent.replay, msg)

        result = await asyncio.wait_for(
            self.registry.wait_for_result(agent_id, request_id),
            timeout=timeout + RESULT_WAIT_MARGIN,
        )
        return self._finalize_result(agent_id, action, target, result)

    async def send_read_file(self, agent_id: str, path: str, timeout: int = 30) -> dict[str, Any]:
        agent = self.registry.get(agent_id)
        if not agent or not agent.writer:
            self.registry.audit_log.log(
                agent_id=agent_id,
                action="read_file",
                command_or_path=path,
                result="error",
                exit_code=None,
                hostname=agent.hostname if agent else None,
            )
            raise ConnectionError(f"agent {agent_id} not connected")

        request_id = str(uuid.uuid4())
        msg = {"action": "read_file", "path": path, "request_id": request_id}
        await self._emit(agent.writer, agent.replay, msg)

        result = await asyncio.wait_for(
            self.registry.wait_for_result(agent_id, request_id),
            timeout=timeout + RESULT_WAIT_MARGIN,
        )
        return self._finalize_result(agent_id, "read_file", path, result)

    async def send_write_file(
        self, agent_id: str, path: str, content: str, timeout: int = 30
    ) -> dict[str, Any]:
        agent = self.registry.get(agent_id)
        if not agent or not agent.writer:
            self.registry.audit_log.log(
                agent_id=agent_id,
                action="write_file",
                command_or_path=path,
                result="error",
                exit_code=None,
                hostname=agent.hostname if agent else None,
            )
            raise ConnectionError(f"agent {agent_id} not connected")

        request_id = str(uuid.uuid4())
        msg = {"action": "write_file", "path": path, "content": content, "request_id": request_id}
        await self._emit(agent.writer, agent.replay, msg)

        result = await asyncio.wait_for(
            self.registry.wait_for_result(agent_id, request_id),
            timeout=timeout + RESULT_WAIT_MARGIN,
        )
        return self._finalize_result(agent_id, "write_file", path, result)
