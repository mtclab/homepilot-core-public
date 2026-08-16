"""App-layer replay protection for the Agent Hub (#362 slice 3).

Defense-in-depth for the ``HP_HUB_ALLOW_INSECURE`` (plaintext / untrusted-TLS)
deployment mode: TLS already covers the encrypted channel, but on an insecure
transport a captured frame could be replayed. This module supplies the pieces
the hub and the Go agent use to make that useless:

* a **canonical serialization** both languages reproduce byte-for-byte, so an
  HMAC computed on one side verifies on the other;
* a per-frame **HMAC-SHA256** keyed by the connection's per-agent token;
* a per-connection, per-direction **monotonic sequence** so a replayed,
  reordered, or dropped frame is detected;
* a register-frame **nonce + timestamp** freshness cache so the auth frame
  itself cannot be replayed.

The canonical form is compact, sorted-key JSON with the ``mac`` field removed:
``{"a":1,"b":"x"}`` — no spaces, keys sorted, non-ASCII emitted as raw UTF-8,
and only ``\\ " \\n \\r \\t`` given short escapes (every other control byte as
``\\u00xx``). This mirrors Go's ``json.Encoder`` with ``SetEscapeHTML(false)``
exactly (crucially, ``<`` ``>`` ``&`` stay raw — they are pervasive in shell
commands), so the two implementations agree bit-for-bit. See
``tests/test_agent_hub_replay.py`` and ``agent/go/replay_test.go`` for the
pinned cross-language test vector.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
from typing import Any

# How far a register frame's timestamp may drift from the hub's clock (seconds).
REPLAY_TS_WINDOW = 300
# A nonce must be at least 16 random bytes -> 32 hex chars.
NONCE_MIN_HEX_LEN = 32


class ReplayError(Exception):
    """A frame on a replay-protected connection failed its sequence or MAC check.

    Raised by :meth:`ReplaySession.verify`; the hub treats it as fatal and closes
    the connection (fail-closed)."""


def _canon_str(s: str) -> str:
    """Serialize a JSON string exactly like Go's encoder with HTML escaping off."""
    out = ['"']
    for ch in s:
        o = ord(ch)
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif o < 0x20:
            out.append(f"\\u{o:04x}")
        else:
            # Raw, including non-ASCII UTF-8 and the HTML chars < > &. Go's
            # SetEscapeHTML(false) leaves these untouched too.
            out.append(ch)
    out.append('"')
    return "".join(out)


def _canon(value: Any) -> str:
    """Canonical JSON for the value types used in hub frames.

    Matches Go's ``json.Marshal`` (with ``SetEscapeHTML(false)``): sorted object
    keys, compact separators, integer-valued numbers without a decimal point."""
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return _canon_str(value)
    if isinstance(value, int):  # bool already handled above
        return str(value)
    if isinstance(value, float):
        # Go marshals whole-valued float64 without a decimal point ("45", not
        # "45.0"); non-integral floats use the shortest round-trip repr, which
        # Go and CPython agree on for float64.
        if value.is_integer():
            return str(int(value))
        return repr(value)
    if isinstance(value, dict):
        parts = []
        for k in sorted(value):
            if not isinstance(k, str):
                raise TypeError(f"non-string JSON key: {k!r}")
            parts.append(_canon_str(k) + ":" + _canon(value[k]))
        return "{" + ",".join(parts) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_canon(v) for v in value) + "]"
    raise TypeError(f"uncanonicalizable value of type {type(value).__name__}")


def canonical_bytes(frame: dict[str, Any]) -> bytes:
    """Canonical UTF-8 serialization of ``frame`` with the ``mac`` field removed."""
    stripped = {k: v for k, v in frame.items() if k != "mac"}
    return _canon(stripped).encode("utf-8")


def compute_mac(key: bytes, frame: dict[str, Any]) -> str:
    """Hex HMAC-SHA256 over :func:`canonical_bytes` of ``frame``, keyed by ``key``."""
    return hmac.new(key, canonical_bytes(frame), hashlib.sha256).hexdigest()


def verify_mac(key: bytes, frame: dict[str, Any], mac: str) -> bool:
    """Constant-time check that ``mac`` is the HMAC of ``frame`` under ``key``."""
    return hmac.compare_digest(compute_mac(key, frame), mac)


class ReplaySession:
    """Per-connection replay state: a MAC key plus per-direction sequence counters.

    Outbound frames are stamped with a monotonic ``seq`` (starting at 1) and a
    ``mac``; inbound frames are checked against the expected next ``seq`` and
    their ``mac``. ``lock`` serializes the increment+write of outbound frames so
    concurrent senders (the connection loop and command dispatch) cannot reorder
    sequence numbers on the wire."""

    def __init__(self, key: bytes) -> None:
        self.key = key
        self._send_seq = 0
        self._recv_expected = 1
        self.lock = asyncio.Lock()

    def stamp(self, frame: dict[str, Any]) -> dict[str, Any]:
        """Return a copy of ``frame`` with the next ``seq`` and its ``mac`` added.

        Must be called while holding :attr:`lock` and immediately followed by the
        write, so the on-wire order matches the assigned sequence."""
        self._send_seq += 1
        out = dict(frame)
        out.pop("mac", None)
        out["seq"] = self._send_seq
        out["mac"] = compute_mac(self.key, out)
        return out

    def verify(self, frame: dict[str, Any]) -> None:
        """Check an inbound frame's ``seq`` and ``mac``; raise :class:`ReplayError`
        on any failure. Advances the expected sequence only on success."""
        seq = frame.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool) or seq != self._recv_expected:
            raise ReplayError(f"bad seq: expected {self._recv_expected}, got {seq!r}")
        mac = frame.get("mac")
        if not isinstance(mac, str) or not verify_mac(self.key, frame, mac):
            raise ReplayError("bad mac")
        self._recv_expected += 1


class NonceCache:
    """Per-agent seen-nonce set with time-based pruning for register freshness.

    A ``(agent_id, nonce)`` pair may be recorded once per window; a repeat within
    the window is a replayed register frame and is refused."""

    def __init__(self, window: float = REPLAY_TS_WINDOW) -> None:
        self._window = window
        self._seen: dict[str, dict[str, float]] = {}

    def check_and_record(self, agent_id: str, nonce: str, now: float) -> bool:
        """Record ``(agent_id, nonce)`` and return ``True`` when it is fresh;
        return ``False`` (recording nothing new) when it was already seen in the
        window."""
        self._prune(now)
        bucket = self._seen.setdefault(agent_id, {})
        if bucket.get(nonce, 0.0) > now:
            return False
        bucket[nonce] = now + self._window
        return True

    def _prune(self, now: float) -> None:
        for aid in list(self._seen):
            bucket = self._seen[aid]
            for nonce in list(bucket):
                if bucket[nonce] <= now:
                    del bucket[nonce]
            if not bucket:
                del self._seen[aid]
