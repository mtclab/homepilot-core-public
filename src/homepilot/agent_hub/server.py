from __future__ import annotations

import asyncio
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
    ) -> None:
        self.host = host
        self.port = port
        self.auth_token = auth_token
        self.registry = registry or AgentRegistry()
        self._ssl_context = ssl_context
        self._token_store = token_store or BootstrapTokenStore()
        self.allow_insecure = allow_insecure
        self._server: asyncio.Server | None = None
        # Auth-flood protection state (in-memory, per remote host).
        self._auth_failures: dict[str, int] = {}
        self._auth_bans: dict[str, float] = {}
        self._preauth_count = 0
        # Register-frame freshness state for replay-protected connections.
        self._nonce_cache = NonceCache()

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

    async def _verify_auth(self, request: dict[str, Any]) -> AuthResult | None:
        """Authenticate a register frame. Returns an :class:`AuthResult` or
        ``None`` on failure.

        Precedence:
          1. ``"per-agent"`` — the presented token's sha256 hash matches the
             stored, non-revoked credential for the frame's claimed ``agent_id``
             AND the claimed ``hostname`` matches the one the credential was
             issued to. A per-agent token presented with a different
             ``agent_id``/``hostname`` will not match here and falls through (and,
             finding no other valid credential, is rejected). A REVOKED credential
             whose token does match is rejected outright (``None``).
          2. ``"shared"`` — the fleet-wide shared token (enrollment only).
          3. ``"bootstrap"`` — a consumed one-time enrollment token.
        """
        token = request.get("auth_token", "")
        if not token:
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
                    return None
                return AuthResult(kind="per-agent", agent_id=claimed_agent_id)

        if self.auth_token and hmac.compare_digest(token, self.auth_token):
            return AuthResult(kind="shared")
        if await self._token_store.consume(token):
            return AuthResult(kind="bootstrap")
        return None

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
        loop = asyncio.get_running_loop()

        # Auth-flood guards, evaluated before we read or trust anything.
        ban_until = self._auth_bans.get(peer_host)
        if ban_until is not None and loop.time() < ban_until:
            logger.warning("rejecting connection from banned peer %s", peer_host)
            writer.close()
            await writer.wait_closed()
            return
        if self._preauth_count >= MAX_PREAUTH_CONNECTIONS:
            logger.warning(
                "pre-auth connection cap (%d) reached; rejecting %s",
                MAX_PREAUTH_CONNECTIONS,
                peer_host,
            )
            writer.close()
            await writer.wait_closed()
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

            auth_result = await self._verify_auth(handshake)
            if auth_result is None:
                self._record_auth_failure(peer_host, loop)
                _release_preauth()
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
                    logger.warning(
                        "rejecting replayed/stale register from %s (agent %s): %s",
                        peer_host,
                        agent_id,
                        reason,
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
                logger.warning(
                    "register rejected for agent %s (host=%s) from %s",
                    agent_id,
                    hostname,
                    peer_host,
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
                except (TimeoutError, ConnectionError):
                    break
                except OversizeMessageError as exc:
                    if replay_session is not None:
                        # On a replay-protected connection an unverifiable oversize
                        # frame is fail-closed (a bad/hostile frame cannot be MAC'd).
                        logger.warning(
                            "oversize frame on replay-protected connection %s; closing",
                            agent_id,
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
                elif msg_action == "report_state":
                    self.registry.update_state(agent_id, msg.get("state", {}))
                    ack = {"action": "state_ack", "request_id": request_id}
                    await self._emit(writer, replay_session, ack)
                elif msg_action == "zabbix_push_result":
                    self.registry.store_command_result(agent_id, msg)
                    ack = {"action": "zabbix_ack", "request_id": request_id}
                    await self._emit(writer, replay_session, ack)
                else:
                    await self._emit(
                        writer,
                        replay_session,
                        {"error": f"unknown action: {msg_action}", "request_id": request_id},
                    )

        except Exception:
            logger.exception("error handling agent %s", agent_id or peer)
        finally:
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
                self.registry.unregister(agent_id, conn_id)
                logger.info("agent disconnected: %s", agent_id)
            writer.close()
            await writer.wait_closed()

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

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
            logger.info("agent hub stopped")

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

    async def send_zabbix_push(self, agent_id: str, timeout: int = 30) -> dict[str, Any]:
        agent = self.registry.get(agent_id)
        if not agent or not agent.writer:
            raise ConnectionError(f"agent {agent_id} not connected")

        request_id = str(uuid.uuid4())
        msg = {"action": "zabbix_push", "request_id": request_id}
        await self._emit(agent.writer, agent.replay, msg)

        result = await asyncio.wait_for(
            self.registry.wait_for_result(agent_id, request_id),
            timeout=timeout + RESULT_WAIT_MARGIN,
        )
        # Route through the outcome check so an agent-reported zabbix error
        # surfaces as a failure instead of a silent success.
        return self._finalize_result(agent_id, "zabbix_push", "", result)

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
