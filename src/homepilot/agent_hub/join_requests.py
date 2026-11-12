"""Who is knocking, and which invitations are still out (lane A3, review 05/3).

Two things an operator could not see before this module:

* **A refused agent.** The hub turned it away - most often because no enrolment
  window was open - and said so in the host's journal and the audit trail.
  Nowhere else: ``record_agent_error`` writes nothing for a hostname the fleet
  has never seen (the agents table is the credential store, and a stranger must
  not grow it), so the host an operator had just installed was precisely the
  one no page mentioned. An agent retried every 30 s for days that way.
* **A minted bootstrap token nobody used.** "I generated the one-liner, did the
  host ever come?" had no answer short of reading the agents list and guessing.

The knocks live in ``enrolment_attempts``: one row per claimed hostname + peer
address, updated per refusal, bounded here (row cap + age-out) so a
token-guessing flood cannot grow it. A row holds the refusal's stable code and
operator sentence - never anything the peer presented as a credential. A host
that joins clears its rows (``AgentHubServer._clear_attempts``).

``payload`` is the one answer behind ``GET /agents/enrolment-requests`` and the
``get_enrolment_requests`` MCP tool, so the console and the assistant cannot
disagree about who is waiting.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Any

from .enrolment_window import payload as window_payload
from .server import DENY_WINDOW_CLOSED, VALID_TOKEN_REFUSALS

# At most this many knocking (hostname, peer) rows are kept. A homelab has tens
# of hosts; fifty distinct knockers is already a flood, and the eviction order
# keeps genuine agents waiting for a window over wrong-token rows.
MAX_ATTEMPT_ROWS = 50
# A knock nobody answered for a week is history, not a request - the audit log
# keeps it.
ATTEMPT_MAX_AGE = timedelta(days=7)
# Pending bootstrap tokens listed at most. Each is a hash row that disappears on
# use or on the next startup prune after expiry.
MAX_TOKENS_LISTED = 50

# Field bounds: every one of these comes from an unauthenticated peer.
_MAX_HOSTNAME = 253
_MAX_AGENT_ID = 128
_MAX_REASON = 500
_MAX_PEER = 64

_TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def _utcnow() -> datetime:
    """The clock, overridable by tests (the enrolment window does the same)."""
    return datetime.now(UTC)


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime(_TS_FORMAT)


def _clean(value: Any, limit: int) -> str:
    text = value if isinstance(value, str) else ""
    # Control characters have no business in a hostname or an id, and they are
    # what turns a stored string into a forged line in a log or a terminal.
    text = "".join(ch for ch in text if ch.isprintable())
    return text.strip()[:limit]


async def record_attempt(
    repo: Any,
    *,
    hostname: Any,
    peer: str,
    code: str,
    reason: str,
    agent_id: Any = None,
) -> None:
    """Upsert one refused register. There is deliberately no token parameter."""
    moment = _utcnow()
    await repo.upsert_enrolment_attempt(
        hostname=_clean(hostname, _MAX_HOSTNAME),
        peer=_clean(peer, _MAX_PEER) or "?",
        reason_code=code,
        reason=_clean(reason, _MAX_REASON),
        agent_id=_clean(agent_id, _MAX_AGENT_ID) or None,
        at=_stamp(moment),
        stale_before=_stamp(moment - ATTEMPT_MAX_AGE),
        max_rows=MAX_ATTEMPT_ROWS,
        keep_codes=tuple(sorted(VALID_TOKEN_REFUSALS)),
    )


def install_started_at() -> str:
    """The moment an install began, in the stamp format the knocks are kept in."""
    return _stamp(_utcnow())


async def refusal_since(repo: Any, hostname: str, since: str) -> dict[str, Any] | None:
    """The newest refusal ``hostname`` received at or after ``since``, or None.

    An install's verify step asks this when its agent never connected: a host
    that knocked and was turned away after the installer ran presented a
    credential other than the bootstrap token the install handed it (#815).
    """
    if not hostname:
        return None
    for row in await repo.list_enrolment_attempts(since):
        if row.get("hostname") == hostname:
            return dict(row)
    return None


def _iso(epoch: float | None) -> str | None:
    if epoch is None:
        return None
    return _stamp(datetime.fromtimestamp(float(epoch), UTC))


async def pending_tokens(repo: Any) -> list[dict[str, Any]]:
    """Minted-but-unused bootstrap tokens, by a short hash id - never the token."""
    rows = await repo.list_bootstrap_tokens(MAX_TOKENS_LISTED)
    now = time.time()
    out = []
    for row in rows:
        expires = float(row["expires_at"])
        remaining = int(expires - now)
        out.append(
            {
                # A prefix of the sha256 of the token: enough to tell two
                # pending tokens apart, useless for recovering either.
                "id": str(row["token_hash"])[:12],
                "minted_by": row.get("minted_by"),
                "created_at": _iso(row.get("created_at")),
                "expires_at": _iso(expires),
                "expired": remaining <= 0,
                "seconds_remaining": max(0, remaining),
            }
        )
    return out


async def install_host_id(repo: Any, hostname: str) -> str | None:
    """The inventory host a knocking hostname can be re-installed on from HomePilot.

    A host that knocks with a credential the hub refuses - most often one left by
    an earlier install (#815) - is fixed by running the zero-touch install again,
    which moves that credential aside and enrols with a fresh bootstrap token. That
    needs a live Proxmox QEMU guest HomePilot knows the node and VMID of; whether
    its guest agent answers is checked when the install is asked for.
    """
    get_host = getattr(repo, "get_host_by_hostname", None)
    if not hostname or get_host is None:
        return None
    host = await get_host(hostname)
    if (
        not host
        or host.get("host_type") != "qemu"
        or not host.get("node")
        or host.get("proxmox_id") is None
        or host.get("absent_since")
    ):
        return None
    return str(host["id"])


async def payload(repo: Any) -> dict[str, Any]:
    """Everything the Hosts page's "wants to join" panel shows, in one read."""
    stale_before = _stamp(_utcnow() - ATTEMPT_MAX_AGE)
    attempts = []
    for row in await repo.list_enrolment_attempts(stale_before):
        attempts.append(
            {
                **row,
                # Holds a genuine token and is only waiting for a window: the
                # "Open enrolment window" button lets it in on its next retry.
                "wants_to_join": row["reason_code"] == DENY_WINDOW_CLOSED,
                # Set when "Install again" can re-enrol it with no window (#815).
                "install_host_id": await install_host_id(repo, row["hostname"]),
            }
        )
    tokens = await pending_tokens(repo)
    window = await window_payload(repo)
    return {
        "attempts": attempts,
        "pending_tokens": tokens,
        "window": window,
        # Counts a later Overview line (lane A1 / Phase D attention) can read
        # without re-deriving them.
        "summary": {
            "wants_to_join": sum(1 for a in attempts if a["wants_to_join"]),
            "refused": sum(1 for a in attempts if not a["wants_to_join"]),
            "pending_tokens": sum(1 for t in tokens if not t["expired"]),
            "expired_unused_tokens": sum(1 for t in tokens if t["expired"]),
        },
    }
