"""What a webhook can subscribe to, and what a webhook endpoint may be (#700).

`webhook_configs.event_types` used to be stored as typed: a typo
(`artifact_aplied`) was accepted, listed and never matched an event, so the
endpoint silently heard nothing. The catalogue is the set of names
``emit_event`` is actually called with; a gate test holds it equal to the
source, so a new event cannot ship without becoming subscribable and a renamed
one cannot leave a dead name behind.
"""

from __future__ import annotations

from collections.abc import Iterable
from urllib.parse import urlsplit

# Every event the platform emits. Held equal to the `emit_event(...)` call sites
# by tests/test_webhooks_700.py.
EVENT_TYPES: frozenset[str] = frozenset(
    {
        "artifact_proposed",
        "artifact_approved",
        "artifact_rejected",
        "artifact_applied",
        "artifact_failed",
        "artifact_superseded",
        "artifact_revoked",
        "artifact_withdrawn",
        "artifact_drifted",
        "alert_firing",
        "alert_resolved",
        "update_available",
    }
)
# Subscribes to everything, including events added later.
WILDCARD = "*"
# The one event only `test` sends; never subscribable.
TEST_EVENT = "webhook_test"

MAX_URL_LENGTH = 2048


def validate_event_types(raw: Iterable[str]) -> list[str]:
    """The cleaned, de-duplicated list, or ValueError naming every unknown name."""
    cleaned = sorted({str(e).strip() for e in raw if str(e).strip()})
    if not cleaned:
        raise ValueError("at least one event type is required (or '*' for every event)")
    unknown = [e for e in cleaned if e != WILDCARD and e not in EVENT_TYPES]
    if unknown:
        raise ValueError(
            f"unknown event type(s) {', '.join(unknown)}; known: "
            f"{', '.join(sorted(EVENT_TYPES))}, or '*' for every event"
        )
    if WILDCARD in cleaned:
        return [WILDCARD]
    return cleaned


def validate_webhook_url(raw: str) -> str:
    """An absolute http(s) URL with a host, or ValueError.

    The message never quotes the URL: endpoints routinely carry their own
    credential in the path or query, and a refusal is logged and echoed.
    """
    value = (raw or "").strip()
    if not value or len(value) > MAX_URL_LENGTH:
        raise ValueError(f"the url must be 1-{MAX_URL_LENGTH} characters")
    if any(ch.isspace() for ch in value):
        raise ValueError("the url must not contain whitespace")
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https"):
        raise ValueError("the url must start with http:// or https://")
    if not parts.hostname:
        raise ValueError("the url must name a host")
    return value
