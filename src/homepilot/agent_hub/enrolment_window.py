"""The operator-opened enrolment window (#537).

The shared fleet token is a permanent enrolment credential: anyone holding it
could mint per-agent credentials for ANY hostname the hub had never seen, for
ever. The hijack guard covers hostnames that are already enrolled, so the
standing exposure was a leaked token growing the fleet with machines nobody
added.

The rule this module implements - enforced in ``server._verify_auth`` - is:

  a shared-token enrolment of a hostname the install has never seen is accepted
  only when either

    (a) the install has NO agents at all yet, so the zero-touch first rollout
        still needs exactly zero operator input (the whole point of #458), or
    (b) an operator has an enrolment window OPEN right now.

Everything else is untouched on purpose:

  * a per-agent credential (every steady-state reconnect) never reaches this
    check at all - it authenticates earlier;
  * a one-shot bootstrap token still enrols regardless of the window: that is
    the sanctioned "add one host later" path, and it is already single-use,
    expiring and operator-minted;
  * a hostname the install already knows still re-enrols with the shared token,
    which is the documented recovery path for a host whose credential was
    revoked or lost.

The window is a single persisted EXPIRY TIMESTAMP in the settings table, and it
is compared at authentication time. There is deliberately no background job to
close it: an expiry that is only enforced by a timer is an expiry that survives
a restart of the thing holding the timer.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

# One settings key. One WRITER (this module), any reader.
WINDOW_KEY = "agent_enrolment_window_expires_at"

DEFAULT_WINDOW_MINUTES = 15
# A day is the outer edge of "an operator is doing a rollout right now". Longer
# than that is not a window, it is the old always-open behaviour with extra
# steps.
MAX_WINDOW_MINUTES = 24 * 60

_TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def _utcnow() -> datetime:
    """The clock, as one overridable function.

    Tests fake time by monkeypatching this attribute rather than sleeping - an
    expiry gate proven by a real 15-minute wait is a gate nobody runs.
    """
    return datetime.now(UTC)


def _format(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime(_TS_FORMAT)


def _parse(value: str) -> datetime | None:
    """Parse a stored expiry, or ``None`` when it is missing/unreadable.

    Unreadable means CLOSED (fail closed): a corrupted value must never be read
    as "open for ever".
    """
    text = (value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        logger.warning("enrolment window expiry is unreadable (%r); treating it as closed", text)
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def normalise_minutes(minutes: int) -> int:
    """Clamp a requested window length into ``1..MAX_WINDOW_MINUTES``."""
    return max(1, min(int(minutes), MAX_WINDOW_MINUTES))


async def status(repo: Any) -> dict[str, Any]:
    """The window's live state: is it open, and until when."""
    if repo is None:
        return {"open": False, "expires_at": None, "seconds_remaining": 0}
    row = await repo.get_setting(WINDOW_KEY)
    expires = _parse(str(row.get("value") or "")) if row else None
    if expires is None:
        return {"open": False, "expires_at": None, "seconds_remaining": 0}
    remaining = (expires - _utcnow()).total_seconds()
    if remaining <= 0:
        return {"open": False, "expires_at": _format(expires), "seconds_remaining": 0}
    return {
        "open": True,
        "expires_at": _format(expires),
        "seconds_remaining": int(remaining),
    }


async def is_open(repo: Any) -> bool:
    """True only while an unexpired window is stored."""
    return bool((await status(repo))["open"])


async def open_window(repo: Any, minutes: int = DEFAULT_WINDOW_MINUTES) -> dict[str, Any]:
    """Open (or extend) the window for ``minutes`` from now.

    Extending is deliberately "from now", not "add to the remaining time": an
    operator who presses it twice means "I need another N minutes", not "give me
    an ever-growing window I have to reason about".
    """
    span = normalise_minutes(minutes)
    expires = _utcnow() + timedelta(minutes=span)
    await repo.set_setting(WINDOW_KEY, _format(expires))
    return {"open": True, "expires_at": _format(expires), "minutes": span}


async def close_window(repo: Any) -> dict[str, Any]:
    """Close the window now. Idempotent - closing a closed window is fine."""
    await repo.set_setting(WINDOW_KEY, "")
    return {"open": False, "expires_at": None}
