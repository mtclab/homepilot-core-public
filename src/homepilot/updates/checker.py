"""Is a newer HomePilot released? (review A+, "updates available")

Nothing told an operator that a release existed: upgrades happened when someone
remembered to look at the releases page. This reconciler looks for them - once
shortly after boot and then on ``update_check_interval_seconds`` (a day by
default) - at the public mirror's latest release, and when that is newer than
the version running here it:

* keeps the release in its state row, which the Overview, ``GET
  /dashboard/updates`` and the ``get_update_status`` MCP tool all read - so the
  "X.Y.Z is available" line exists for exactly as long as it is true, and goes
  the moment this install runs that version; and
* emits ONE ``update_available`` event per new version (SSE, the events
  webhook / n8n flow, and the ntfy channels). Which version was announced is
  persisted, so a daily check - or a restart - never announces it again.

It only ever TELLS. Nothing here downloads, pulls or upgrades anything.

Network etiquette, because every install asks the same public, unauthenticated
API (60 requests an hour per address): a conditional request with the stored
ETag (a 304 does not count against the budget), a short timeout, and when GitHub
says the budget is spent the checker waits for the reset it names rather than
asking again. An install with no internet records the failure on its state row
- one self-check line says so - logs it once, retries hourly at most, and never
raises: an offline install is a supported install, and an operator who wants
silence switches the check off (``HP_UPDATE_CHECK_ENABLED=0``).

The loop's result is ``success=True`` even when GitHub could not be reached:
the LOOP is healthy, and the outcome has its own self-check subsystem. Marking
the cycle failed would add a second, louder "scheduled reconciler failing"
line to an offline install for the same fact.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx

from ..app_settings import effective, resolve_interval
from ..config import get_settings
from ..events import emit_event
from ..notifications.service import EVENT_UPDATE_AVAILABLE
from ..reconciler.base import Reconciler, ReconcilerResult
from . import releases

logger = logging.getLogger(__name__)

# Settings-table key holding the checker's state. One WRITER (this class), any
# reader (update_status). The prefix-free name keeps it out of the operator
# settings namespace (`setting:`), like the archive push's outcome keys.
STATE_KEY = "update_check_state"

REQUEST_TIMEOUT_SECONDS = 10.0
# The check runs this soon after boot, so a restarted install knows within
# minutes - late enough that the network and the rest of the app are up.
STARTUP_DELAY_SECONDS = 120.0
# The floor on the interval, whatever is configured: every install shares one
# unauthenticated budget per public address.
MIN_INTERVAL_SECONDS = 3600.0
# After a failed check, the next one comes sooner than a day - but no sooner
# than this, so an offline install asks at most hourly.
RETRY_AFTER_FAILURE_SECONDS = 3600.0
# While the check is switched off the loop only re-reads the switch, so turning
# it back on takes effect within minutes rather than after a day.
DISABLED_POLL_SECONDS = 300.0

STATE_OFF = "off"
STATE_NOT_CHECKED = "not_checked"
STATE_OK = "ok"
STATE_FAILING = "failing"


def _parse_stamp(value: Any) -> float | None:
    if not value:
        return None
    try:
        return datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).timestamp()
    except ValueError:
        return None


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _describe(exc: BaseException) -> str:
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}"[:300] if text else type(exc).__name__


def running_version() -> str:
    from homepilot import __version__

    return str(__version__)


async def load_state(repo: Any) -> dict[str, Any]:
    """The stored state, or an empty one. Never raises on a damaged row."""
    try:
        row = await repo.get_setting(STATE_KEY)
    except Exception:  # pragma: no cover - a DB hiccup must not break a read path
        logger.debug("could not read %s", STATE_KEY, exc_info=True)
        return {}
    if not row or not row.get("value"):
        return {}
    try:
        data = json.loads(str(row["value"]))
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


async def _enabled() -> bool:
    return bool(int(await effective("update_check_enabled", get_settings()) or 0))


async def update_status(repo: Any, version: str | None = None) -> dict[str, Any]:
    """What every surface says about updates: one function, so the Overview, the
    API, the MCP tool and the self-check cannot disagree.

    ``update_available`` is computed against the version running NOW, not
    stored: after an upgrade the line is gone on the next read, without waiting
    for the next check."""
    current = version or running_version()
    enabled = await _enabled()
    data = await load_state(repo)
    latest = data.get("latest") if isinstance(data.get("latest"), dict) else None
    newer = releases.is_newer(latest.get("version"), current) if latest else None
    if not enabled:
        state = STATE_OFF
    elif not data.get("checked_at"):
        state = STATE_NOT_CHECKED
    elif data.get("last_error"):
        state = STATE_FAILING
    else:
        state = STATE_OK
    interval = await effective("update_check_interval_seconds", get_settings())
    return {
        "enabled": enabled,
        "state": state,
        "running_version": current,
        "update_available": bool(enabled and newer is True),
        "latest": latest,
        "checked_at": data.get("checked_at"),
        "last_ok_at": data.get("last_ok_at"),
        "last_error": data.get("last_error") or None,
        "consecutive_failures": int(data.get("failures") or 0),
        "rate_limited_until": data.get("rate_limited_until"),
        "notified_version": data.get("notified_version"),
        "notified_at": data.get("notified_at"),
        "interval_seconds": max(int(interval or 0), int(MIN_INTERVAL_SECONDS)),
        "releases_url": releases.RELEASES_PAGE_URL,
    }


def describe_update(release: dict[str, Any], current: str) -> str:
    """The one sentence every channel carries, plus the operator actions."""
    text = (
        f"HomePilot {release['version']} is available - you run {current}. "
        f"Release notes: {release['url']}"
    )
    actions = list(release.get("operator_actions") or [])
    if actions:
        text += "\nOperator actions for this release:\n" + "\n".join(f"- {a}" for a in actions)
    else:
        text += "\nRead the release notes for any operator action before upgrading."
    return text + "\nNothing is upgraded automatically."


class UpdateChecker(Reconciler):
    """Checks the public mirror's latest release and announces a newer one once.

    ``transport`` is an ``httpx`` transport, injectable so a test stands in for
    GitHub at the HTTP layer; ``now`` and ``version`` likewise."""

    def __init__(
        self,
        repo: Any,
        *,
        resolver: Any = None,
        notifier: Any = None,
        transport: httpx.AsyncBaseTransport | None = None,
        now: Callable[[], float] = time.time,
        version: str | None = None,
        url: str = releases.RELEASES_API_URL,
    ) -> None:
        self._repo = repo
        self._resolver = resolver
        self._notifier = notifier
        self._transport = transport
        self._now = now
        self._version = version
        self._url = url
        # What the next wait should be shortened to, decided by the last cycle:
        # retry sooner after a failure, wait out a rate limit, or poll the
        # switch while the check is off. In memory: after a restart the first
        # cycle runs on the startup delay anyway.
        self._next_wait: float | None = None

    @property
    def version(self) -> str:
        return self._version or running_version()

    async def interval(self) -> float:
        """Seconds until the next cycle; the scheduler re-asks while waiting."""
        configured = await resolve_interval(
            self._resolver,
            "update_check_interval_seconds",
            float(get_settings().update_check_interval_seconds),
        )
        base = max(float(configured), MIN_INTERVAL_SECONDS)
        if self._next_wait is None:
            return base
        return min(base, self._next_wait)

    async def _save(self, data: dict[str, Any]) -> None:
        await self._repo.set_setting(STATE_KEY, json.dumps(data, sort_keys=True))

    async def run(self) -> ReconcilerResult:
        if not await _enabled():
            self._next_wait = DISABLED_POLL_SECONDS
            return ReconcilerResult(
                name="update_check", success=True, details={"skipped": "the check is off"}
            )
        data = await load_state(self._repo)
        now = self._now()
        limited_until = _parse_stamp(data.get("rate_limited_until"))
        if limited_until is not None and limited_until > now:
            # GitHub named when the budget resets; asking before then only
            # spends what another install on this address needs.
            self._next_wait = max(limited_until - now, 60.0)
            return ReconcilerResult(
                name="update_check",
                success=True,
                details={"skipped": f"rate limited until {data['rate_limited_until']}"},
            )

        outcome = await self._fetch(data.get("etag"))
        data["checked_at"] = _iso(now)
        if outcome["error"] is not None:
            return await self._record_failure(data, outcome, now)

        was_failing = bool(data.get("last_error"))
        data["last_ok_at"] = _iso(now)
        data["last_error"] = None
        data["failures"] = 0
        data["rate_limited_until"] = None
        if outcome["status"] == 200:
            data["etag"] = outcome["etag"]
            data["latest"] = outcome["latest"]
        elif outcome["status"] == 404:
            # No release published at all: nothing to offer, nothing wrong.
            data["etag"] = None
            data["latest"] = None
        # 304: unchanged since the stored ETag; keep the stored release.
        if was_failing:
            logger.info("update check: the release check works again")
        self._next_wait = None

        latest = data.get("latest")
        announced = await self._announce_once(data, latest)
        await self._save(data)
        if announced is not None:
            await self._emit(announced)
        return ReconcilerResult(
            name="update_check",
            success=True,
            details={
                "http": outcome["status"],
                "latest": latest.get("version") if latest else None,
                "running": self.version,
                "announced": announced["version"] if announced else None,
            },
        )

    async def _record_failure(
        self, data: dict[str, Any], outcome: dict[str, Any], now: float
    ) -> ReconcilerResult:
        first = not data.get("last_error")
        data["last_error"] = outcome["error"]
        data["failures"] = int(data.get("failures") or 0) + 1
        retry_at = outcome.get("retry_at")
        data["rate_limited_until"] = _iso(retry_at) if retry_at else None
        await self._save(data)
        wait = RETRY_AFTER_FAILURE_SECONDS
        if retry_at:
            wait = max(retry_at - now, 60.0)
        self._next_wait = wait
        # Once per outage, not once per cycle: an offline install is told by
        # its self-check line, and a log that repeats it hourly buries the rest.
        if first:
            logger.warning(
                "update check: could not read the latest HomePilot release (%s); "
                "will retry, and the self-check reports it until it works",
                outcome["error"],
            )
        else:
            logger.debug("update check still failing: %s", outcome["error"])
        return ReconcilerResult(
            name="update_check",
            success=True,
            details={"checked": False, "error": outcome["error"], "failures": data["failures"]},
        )

    async def _fetch(self, etag: str | None) -> dict[str, Any]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": f"HomePilot/{self.version} (update check)",
        }
        if etag:
            headers["If-None-Match"] = etag
        result: dict[str, Any] = {"status": None, "error": None, "etag": None, "latest": None}
        try:
            async with httpx.AsyncClient(
                timeout=REQUEST_TIMEOUT_SECONDS, transport=self._transport
            ) as client:
                resp = await client.get(self._url, headers=headers)
        except (httpx.HTTPError, OSError) as exc:
            result["error"] = f"GitHub could not be reached: {_describe(exc)}"
            return result
        result["status"] = resp.status_code
        if resp.status_code == 304:
            return result
        if resp.status_code == 404:
            return result
        if resp.status_code in (403, 429):
            retry_at = self._rate_limit_reset(resp)
            if retry_at is not None or resp.status_code == 429:
                result["retry_at"] = retry_at or (self._now() + RETRY_AFTER_FAILURE_SECONDS)
                result["error"] = (
                    f"GitHub's rate limit for this address is spent (HTTP {resp.status_code}); "
                    f"the next check waits until {_iso(result['retry_at'])}"
                )
                return result
        if resp.status_code != 200:
            result["error"] = f"GitHub answered HTTP {resp.status_code}"
            return result
        try:
            body = resp.json()
        except ValueError:
            result["error"] = "GitHub's answer was not JSON"
            return result
        latest = releases.parse_latest(body)
        if latest is None:
            # `/releases/latest` never returns a draft or a pre-release, so
            # this is an answer of a shape we do not understand - say so rather
            # than read it as "no update".
            result["error"] = "GitHub's answer did not name a final X.Y.Z release"
            return result
        result["latest"] = latest
        result["etag"] = resp.headers.get("etag")
        return result

    def _rate_limit_reset(self, resp: httpx.Response) -> float | None:
        retry_after = resp.headers.get("retry-after")
        if retry_after and retry_after.strip().isdigit():
            return self._now() + float(retry_after.strip())
        if resp.headers.get("x-ratelimit-remaining") == "0":
            reset = resp.headers.get("x-ratelimit-reset", "")
            if reset.strip().isdigit():
                return float(reset.strip())
        return None

    async def _announce_once(
        self, data: dict[str, Any], latest: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        """The release to announce now, or None. Marks it announced in ``data``
        BEFORE anything is sent: one event per version is the promise, and a
        crash mid-send must not turn into a second one tomorrow."""
        if not latest or releases.is_newer(latest.get("version"), self.version) is not True:
            return None
        notified = releases.release_key(data.get("notified_version"))
        candidate = releases.release_key(latest.get("version"))
        if notified is not None and candidate is not None and candidate <= notified:
            return None
        data["notified_version"] = latest["version"]
        data["notified_at"] = _iso(self._now())
        return dict(latest)

    async def _emit(self, release: dict[str, Any]) -> None:
        payload: dict[str, Any] = {
            "version": release["version"],
            "tag": release["tag"],
            "running_version": self.version,
            "url": release["url"],
            "published_at": release.get("published_at"),
            "operator_actions": list(release.get("operator_actions") or []),
            "message": describe_update(release, self.version),
        }
        logger.info(
            "update available: HomePilot %s (this install runs %s)",
            release["version"],
            self.version,
        )
        try:
            await emit_event("update_available", payload, repo=self._repo)
        except Exception:
            logger.warning("could not emit update_available", exc_info=True)
        if self._notifier is not None:
            try:
                await self._notifier.dispatch(EVENT_UPDATE_AVAILABLE, payload)
            except Exception:
                logger.warning(
                    "could not hand update_available to the notification channels",
                    exc_info=True,
                )


__all__ = [
    "EVENT_UPDATE_AVAILABLE",
    "STARTUP_DELAY_SECONDS",
    "STATE_KEY",
    "UpdateChecker",
    "describe_update",
    "load_state",
    "running_version",
    "update_status",
]
