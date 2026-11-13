"""Review A+ "updates available", tested on the operator's outcome.

Not "the checker returned ok". Through the real repository, the real checker,
the real ``emit_event`` (its events-webhook POST - what the n8n flow receives),
the real notifier (an ntfy channel) and the real routes, with GitHub, the
webhook endpoint and the ntfy server all stood in for at the HTTP TRANSPORT
layer (``httpx.MockTransport``) - nothing here reaches the real GitHub API:

* a newer release produces exactly ONE Overview line's worth of state (the
  summary's ``updates`` block) and exactly ONE delivered notification on each
  channel - across a month of daily checks, ETag revalidations and a restart;
  a later release is announced once more; upgrading makes the line go away;
* offline produces one self-check line and one log warning - not an error per
  cycle, not a failing reconciler, not an exception;
* GitHub's rate limit is waited out, not hammered; the switch turns it all off.

Teeth (each proven by reverting the fix, recorded in the PR):
* drop the ``notified_version`` guard in ``UpdateChecker._announce_once`` ->
  the once-per-version tests go red (an event every cycle);
* drop ``If-None-Match`` from ``_fetch`` -> the ETag test goes red;
* drop the rate-limit wait in ``run`` -> the rate-limit test goes red;
* make a failed fetch raise / return success=False -> the offline test goes red;
* drop ``_update_check_subsystem`` from ``build_subsystems`` -> the self-check
  tests go red;
* treat a suffixed tag as a release -> the pre-release test goes red.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from homepilot import app_settings
from homepilot.app_settings import SettingError, SettingsResolver
from homepilot.auth.deps import require_token
from homepilot.config import get_settings
from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.db.repository import Repository
from homepilot.notifications import Notifier
from homepilot.selfcheck import selfcheck_report
from homepilot.updates import checker as checker_mod
from homepilot.updates import releases
from homepilot.updates.checker import UpdateChecker

pytestmark = pytest.mark.asyncio

# Example values only (RFC 2606): never a real ntfy server, topic or webhook.
TOPIC_URL = "https://ntfy.example.com/hp-update-topic-1c2d"
WEBHOOK_URL = "https://hooks.example.com/hp-events"
RUNNING = "3.7.0"
DAY = 86400.0
T0 = 1_800_000_000.0

NOTES_WITH_ACTIONS = """## 3.8.0 - 2026-12-01

A summary paragraph that is not an instruction.

**Upgrade:** one schema migration runs in one start, with the usual
pre-migration backup taken first. Re-import the n8n flow if you use it.

### Something new

Words about a feature.

### Upgrade notes

Set `HP_EXAMPLE` before you start the new image.

## Docker

docker pull example
"""


def _release(tag: str = "v3.8.0", *, body: str = NOTES_WITH_ACTIONS, **over: Any) -> dict:
    return {
        "tag_name": tag,
        "name": tag,
        "html_url": f"https://github.com/mtclab/homepilot-core-public/releases/tag/{tag}",
        "published_at": "2026-12-01T10:00:00Z",
        "draft": False,
        "prerelease": False,
        "body": body,
        **over,
    }


class GitHubStub:
    """The releases/latest endpoint, at the transport layer, honouring ETags."""

    def __init__(self, release: dict | None = None) -> None:
        self.release = release
        self.requests: list[httpx.Request] = []
        self.offline = False
        self.rate_limit_reset: float | None = None

    @property
    def etag(self) -> str:
        return f'"etag-{self.release["tag_name"]}"' if self.release else '"none"'

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.offline:
            raise httpx.ConnectError("[Errno -3] Temporary failure in name resolution")
        if self.rate_limit_reset is not None:
            return httpx.Response(
                403,
                headers={
                    "x-ratelimit-remaining": "0",
                    "x-ratelimit-reset": str(int(self.rate_limit_reset)),
                },
                json={"message": "API rate limit exceeded"},
            )
        if self.release is None:
            return httpx.Response(404, json={"message": "Not Found"})
        if request.headers.get("if-none-match") == self.etag:
            return httpx.Response(304, headers={"etag": self.etag})
        return httpx.Response(200, headers={"etag": self.etag}, json=self.release)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


class Endpoint:
    """An HTTP receiver (ntfy server or the n8n webhook) that records bodies."""

    def __init__(self) -> None:
        self.bodies: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


class FakeVault:
    def __init__(self) -> None:
        self.secrets: dict[str, dict[str, Any]] = {}

    async def store_secret(self, name: str, value: dict[str, Any]) -> None:
        self.secrets[name] = dict(value)

    async def get_secret(self, name: str) -> dict[str, Any]:
        return dict(self.secrets[name])

    async def delete_secret(self, name: str) -> bool:
        return self.secrets.pop(name, None) is not None


class Clock:
    def __init__(self) -> None:
        self.t = T0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
async def db(tmp_path: Path):
    database = Database(str(tmp_path / "hp.db"))
    await database.connect()
    await run_migrations(database)
    yield database
    await database.close()


@pytest.fixture
def repo(db) -> Repository:
    return Repository(db)


@pytest.fixture
async def resolver(repo):
    """The operator settings, bound process-wide as the app binds them."""
    bound = SettingsResolver(repo, get_settings())
    app_settings.bind_resolver(bound)
    await bound.set("events_webhook_url", WEBHOOK_URL)
    yield bound
    app_settings.bind_resolver(None)


@pytest.fixture(autouse=True)
def running(monkeypatch):
    monkeypatch.setattr(checker_mod, "running_version", lambda: RUNNING)


@pytest.fixture
def webhook(monkeypatch) -> Endpoint:
    """The events webhook (the n8n flow's trigger), at emit_event's transport."""
    endpoint = Endpoint()
    real = httpx.AsyncClient

    def client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = endpoint.transport
        return real(*args, **kwargs)

    monkeypatch.setattr(
        "homepilot.events.httpx",
        SimpleNamespace(
            AsyncClient=client,
            HTTPError=httpx.HTTPError,
            TimeoutException=httpx.TimeoutException,
        ),
    )
    return endpoint


@pytest.fixture
def ntfy() -> Endpoint:
    return Endpoint()


@pytest.fixture
async def notifier(db, ntfy) -> Notifier:
    n = Notifier(db, FakeVault(), transport=ntfy.transport, retry_delays=())
    await n.create_channel(kind="ntfy", name="phone", url=TOPIC_URL, token=None)
    return n


@pytest.fixture
def github() -> GitHubStub:
    return GitHubStub(_release())


@pytest.fixture
def clock() -> Clock:
    return Clock()


def _checker(repo, resolver, notifier, github, clock) -> UpdateChecker:
    return UpdateChecker(
        repo, resolver=resolver, notifier=notifier, transport=github.transport, now=clock
    )


async def _summary(repo) -> dict[str, Any]:
    """GET /dashboard/summary through the real route - what the Overview reads."""
    from homepilot.dashboard.router import router

    app = FastAPI()
    app.include_router(router)
    app.state.repo = repo
    app.dependency_overrides[require_token] = lambda: {"scope": "*", "role": "admin"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://hp"
    ) as client:
        resp = await client.get("/dashboard/summary")
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _selfcheck_entry(repo) -> dict[str, Any]:
    from homepilot.app_settings import effective_settings

    # As app.state carries them: the repository and the bound resolver.
    state = SimpleNamespace(repo=repo, settings_resolver=app_settings.bound_resolver())
    settings = await effective_settings(state, get_settings())
    report = await selfcheck_report(state, settings)
    return {e["name"]: e for e in report["subsystems"]}["update_check"]


def _update_events(endpoint: Endpoint) -> list[dict[str, Any]]:
    return [b for b in endpoint.bodies if b.get("event") == "update_available"]


async def _days(checker: UpdateChecker, clock: Clock, n: int, notifier: Notifier) -> None:
    for _ in range(n):
        result = await checker.run()
        assert result.success, result.details
        clock.t += DAY
    await notifier.drain()


# ── The journey ─────────────────────────────────────────────────────────────


async def test_a_newer_release_is_one_line_and_one_notification_across_a_month(
    repo, resolver, notifier, ntfy, webhook, github, clock
) -> None:
    checker = _checker(repo, resolver, notifier, github, clock)
    await _days(checker, clock, 30, notifier)

    # The Overview's line: one block, saying the version, the running one, the
    # notes and the operator actions the notes carried.
    updates = (await _summary(repo))["updates"]
    assert updates["update_available"] is True
    assert updates["running_version"] == RUNNING
    assert updates["latest"]["version"] == "3.8.0"
    assert updates["latest"]["url"].endswith("/releases/tag/v3.8.0")
    actions = updates["latest"]["operator_actions"]
    assert len(actions) == 2
    assert actions[0].startswith("Upgrade: one schema migration")
    assert "HP_EXAMPLE" in actions[1]

    # Exactly one notification, on each path, in thirty daily checks.
    assert len(ntfy.bodies) == 1
    phone = ntfy.bodies[0]
    assert phone["title"] == "HomePilot 3.8.0 is available"
    assert "you run 3.7.0" in phone["message"]
    assert "Re-import the n8n flow" in phone["message"]
    assert phone["click"].endswith("/releases/tag/v3.8.0")
    events = _update_events(webhook)
    assert len(events) == 1
    assert events[0]["version"] == "3.8.0" and events[0]["running_version"] == RUNNING
    assert events[0]["operator_actions"] == actions

    # A restart (a new checker over the same database) does not announce it again.
    restarted = _checker(repo, resolver, notifier, github, clock)
    await _days(restarted, clock, 3, notifier)
    assert len(ntfy.bodies) == 1 and len(_update_events(webhook)) == 1

    # A later release is new news: announced once more, and only once.
    github.release = _release("v3.8.1", body="Bug fixes only.")
    await _days(restarted, clock, 5, notifier)
    assert [b["title"] for b in ntfy.bodies] == [
        "HomePilot 3.8.0 is available",
        "HomePilot 3.8.1 is available",
    ]
    assert [e["version"] for e in _update_events(webhook)] == ["3.8.0", "3.8.1"]
    later = (await _summary(repo))["updates"]
    assert later["latest"]["version"] == "3.8.1"
    # No instruction is invented when the notes carry none.
    assert later["latest"]["operator_actions"] == []
    assert "Read the release notes" in ntfy.bodies[1]["message"]


async def test_after_upgrading_the_line_is_gone_and_nothing_is_announced(
    repo, resolver, notifier, ntfy, webhook, github, clock, monkeypatch
) -> None:
    checker = _checker(repo, resolver, notifier, github, clock)
    await _days(checker, clock, 1, notifier)
    assert (await _summary(repo))["updates"]["update_available"] is True

    monkeypatch.setattr(checker_mod, "running_version", lambda: "3.8.0")
    # Gone on the next read - not after the next check.
    assert (await _summary(repo))["updates"]["update_available"] is False
    await _days(checker, clock, 3, notifier)
    assert len(ntfy.bodies) == 1
    entry = await _selfcheck_entry(repo)
    assert entry["state"] == "ok"
    assert "3.8.0 is the newest released HomePilot" in entry["consequence"]


async def test_the_running_release_is_not_news(
    repo, resolver, notifier, ntfy, webhook, github, clock
) -> None:
    github.release = _release(f"v{RUNNING}")
    checker = _checker(repo, resolver, notifier, github, clock)
    await _days(checker, clock, 3, notifier)
    assert (await _summary(repo))["updates"]["update_available"] is False
    assert ntfy.bodies == [] and _update_events(webhook) == []


async def test_revalidation_carries_the_etag(repo, resolver, notifier, github, clock) -> None:
    checker = _checker(repo, resolver, notifier, github, clock)
    await _days(checker, clock, 3, notifier)
    assert len(github.requests) == 3
    assert "if-none-match" not in github.requests[0].headers
    assert github.requests[1].headers["if-none-match"] == github.etag
    assert github.requests[2].headers["if-none-match"] == github.etag
    assert github.requests[0].headers["user-agent"].startswith("HomePilot/")
    # A 304 keeps the stored release rather than forgetting it.
    assert (await _summary(repo))["updates"]["latest"]["version"] == "3.8.0"


async def test_a_pre_release_is_never_offered(
    repo, resolver, notifier, ntfy, webhook, github, clock
) -> None:
    for release in (_release("v3.9.0-rc1"), _release("v3.9.0", prerelease=True)):
        github.release = release
        checker = _checker(repo, resolver, notifier, github, clock)
        await _days(checker, clock, 1, notifier)
        updates = (await _summary(repo))["updates"]
        assert updates["update_available"] is False
        assert updates["latest"] is None
    assert ntfy.bodies == [] and _update_events(webhook) == []


# ── Offline, rate limits, the switch ────────────────────────────────────────


async def test_offline_is_one_selfcheck_line_not_an_error_storm(
    repo, resolver, notifier, ntfy, webhook, github, clock, caplog
) -> None:
    github.offline = True
    checker = _checker(repo, resolver, notifier, github, clock)
    caplog.set_level(logging.DEBUG, logger="homepilot.updates.checker")
    for _ in range(24):
        result = await checker.run()
        # The LOOP is healthy - otherwise the reconcilers line would repeat the
        # same fact louder.
        assert result.success is True
        clock.t += await checker.interval()
    await notifier.drain()

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1, [r.getMessage() for r in warnings]
    # Hourly at most while failing, never faster.
    assert await checker.interval() == checker_mod.RETRY_AFTER_FAILURE_SECONDS
    assert ntfy.bodies == [] and _update_events(webhook) == []

    updates = (await _summary(repo))["updates"]
    assert updates["state"] == "failing"
    assert updates["consecutive_failures"] == 24
    assert updates["update_available"] is False
    assert "could not be reached" in updates["last_error"]

    entry = await _selfcheck_entry(repo)
    assert entry["state"] == "unknown"
    assert "24 time(s) in a row" in entry["consequence"]
    assert "HP_UPDATE_CHECK_ENABLED=0" in entry["consequence"]

    # Back online: the line clears and the release is announced once.
    github.offline = False
    await _days(checker, clock, 2, notifier)
    assert (await _selfcheck_entry(repo))["state"] == "ok"
    assert len(ntfy.bodies) == 1
    assert (await _summary(repo))["updates"]["consecutive_failures"] == 0


async def test_a_spent_rate_limit_is_waited_out(
    repo, resolver, notifier, ntfy, github, clock
) -> None:
    github.rate_limit_reset = clock.t + 1800
    checker = _checker(repo, resolver, notifier, github, clock)
    await checker.run()
    assert len(github.requests) == 1
    assert 1700 <= await checker.interval() <= 1800

    # Asked again before the reset: no request at all.
    clock.t += 600
    await checker.run()
    assert len(github.requests) == 1

    # After the reset it asks, and the release arrives.
    github.rate_limit_reset = None
    clock.t += 1300
    await checker.run()
    await notifier.drain()
    assert len(github.requests) == 2
    assert len(ntfy.bodies) == 1


async def test_switched_off_it_asks_nothing_and_says_so(
    repo, resolver, notifier, ntfy, webhook, github, clock
) -> None:
    await resolver.set("update_check_enabled", 0)
    checker = _checker(repo, resolver, notifier, github, clock)
    await _days(checker, clock, 5, notifier)
    assert github.requests == []
    assert ntfy.bodies == [] and _update_events(webhook) == []
    updates = (await _summary(repo))["updates"]
    assert updates["enabled"] is False and updates["state"] == "off"
    entry = await _selfcheck_entry(repo)
    assert entry["state"] == "off"
    assert "never told" in entry["consequence"]
    # Switched back on, it checks within minutes, not after a day.
    assert await checker.interval() == checker_mod.DISABLED_POLL_SECONDS
    await resolver.set("update_check_enabled", 1)
    await _days(checker, clock, 1, notifier)
    assert len(github.requests) == 1 and len(ntfy.bodies) == 1


async def test_the_env_var_switches_it_off(monkeypatch, repo) -> None:
    monkeypatch.setenv("HP_UPDATE_CHECK_ENABLED", "0")
    get_settings.cache_clear()
    try:
        bound = SettingsResolver(repo, get_settings())
        app_settings.bind_resolver(bound)
        try:
            assert (await checker_mod.update_status(repo))["state"] == "off"
            with pytest.raises(app_settings.EnvOverrideError):
                await bound.set("update_check_enabled", 1)
        finally:
            app_settings.bind_resolver(None)
    finally:
        monkeypatch.delenv("HP_UPDATE_CHECK_ENABLED")
        get_settings.cache_clear()


async def test_the_interval_has_a_floor(repo, resolver) -> None:
    with pytest.raises(SettingError):
        await resolver.set("update_check_interval_seconds", 60)
    await resolver.set("update_check_interval_seconds", 7200)
    assert await UpdateChecker(repo, resolver=resolver).interval() == 7200
    assert checker_mod.MIN_INTERVAL_SECONDS == 3600


async def test_not_checked_yet_is_not_a_warning(repo, resolver) -> None:
    """A fresh boot must not put a line on the Overview for two minutes."""
    entry = await _selfcheck_entry(repo)
    assert entry["state"] == "ok"
    assert "No release check has run yet" in entry["consequence"]
    assert (await _summary(repo))["updates"]["state"] == "not_checked"


# ── The release judgments, as functions ─────────────────────────────────────


@pytest.mark.parametrize(
    "release,running,newer",
    [
        ("v3.8.0", "3.7.0", True),
        ("3.7.1", "3.7.0", True),
        ("v4.0.0", "3.99.99", True),
        ("v3.10.0", "3.9.0", True),  # numeric, not string, comparison
        ("v3.7.0", "3.7.0", False),
        ("v3.6.25", "3.7.0", False),
        ("v3.8.0", "3.8.0rc1", True),  # a pre-release build is behind its release
        ("v3.8.0", "3.8.0+local", False),  # build metadata is the same version
        ("v3.8.0-rc1", "3.7.0", None),  # a pre-release tag is never a release
        ("latest", "3.7.0", None),
        ("v3.8.0", "unknown", None),
    ],
)
async def test_version_comparison(release, running, newer) -> None:
    assert releases.is_newer(release, running) is newer


async def test_operator_actions_follow_the_changelog_conventions() -> None:
    body = (
        "Intro.\n\n**Operator action, `agents` profile only:** before upgrading, set "
        "both values.\n\n### REQUIRED READING BEFORE UPGRADING\n\nBack up first.\n\n"
        "- and keep the key\n\n## Next\n\n**Upgrade** is a word here, not a label.\n\n"
        "**Operator action - check every install whose `.env` came from an older\n"
        "template.** Run the grep.\n"
    )
    actions = releases.operator_actions(body)
    assert actions == [
        "Operator action, `agents` profile only: before upgrading, set both values.",
        "Back up first. - and keep the key",
        "Operator action - check every install whose `.env` came from an older "
        "template. Run the grep.",
    ]
    assert releases.operator_actions(None) == []
    assert releases.operator_actions("Just notes.\n\n## Docker\n") == []


async def test_an_operator_actions_section_that_says_none_is_no_action() -> None:
    """A release with nothing to do says so under its heading ("None.", as the
    CHANGELOG convention asks); the notice must not tell the operator to do
    "None."."""
    for nothing in ("None.", "None", "- None.", "none."):
        body = f"## 3.12.0\n\n### Operator actions\n\n{nothing}\n\n### Guests\n\nNotes.\n"
        assert releases.operator_actions(body) == [], nothing
    # A real action that merely starts with the word is still an action.
    body = "### Operator actions\n\nNone of the old tokens work: mint new ones.\n"
    assert releases.operator_actions(body) == ["None of the old tokens work: mint new ones."]


async def test_a_notes_link_off_github_is_not_followed() -> None:
    parsed = releases.parse_latest(_release(html_url="https://evil.example.com/x"))
    assert parsed is not None
    assert parsed["url"] == releases.RELEASES_PAGE_URL
