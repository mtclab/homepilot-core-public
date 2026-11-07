"""The ntfy adapter: one HTTP POST that makes a phone buzz.

ntfy (https://ntfy.sh, self-hostable) takes a message on a TOPIC. An operator
gives HomePilot the topic URL they would subscribe to in the app, e.g.
``https://ntfy.example.com/CHANGE_ME_matrix_room_alias``. That URL is a capability -
anyone holding it can read and post the topic - so it is stored in the vault,
never in the database, and is never logged or returned whole (see service.py).

We publish with ntfy's JSON form (POST to the server root, topic in the body)
rather than the header form, because the header form needs every header value
in Latin-1 and an alert rule's name is operator text that may not be.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit

# ntfy's own topic rule: 1-64 of letters, digits, '-' and '_'.
_TOPIC_RE = re.compile(r"^[-_A-Za-z0-9]{1,64}$")
MAX_URL_LENGTH = 2048
MAX_TOKEN_LENGTH = 512

# ntfy priorities: 3 = default, 4 = high (sound + heads-up on Android).
PRIORITY_FIRING = 4
PRIORITY_RESOLVED = 3
PRIORITY_TEST = 3


def split_topic_url(raw: str) -> tuple[str, str]:
    """``(publish_url, topic)`` for a topic URL, or ValueError naming the problem.

    The publish URL is the topic URL with the topic removed, so a server that
    lives under a path prefix (``https://example.com/ntfy/<topic>``) works too.
    Messages never quote the URL: it is the credential."""
    value = (raw or "").strip()
    if not value or len(value) > MAX_URL_LENGTH:
        raise ValueError(f"the topic URL must be 1-{MAX_URL_LENGTH} characters")
    if any(ch.isspace() for ch in value):
        raise ValueError("the topic URL must not contain whitespace")
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https"):
        raise ValueError("the topic URL must start with http:// or https://")
    if not parts.hostname:
        raise ValueError("the topic URL must name a host")
    if parts.query or parts.fragment:
        raise ValueError("the topic URL must not carry a query or fragment - just server/topic")
    if parts.username or parts.password:
        raise ValueError(
            "put an ntfy access token in the token field, not user:password in the URL"
        )
    path = parts.path.rstrip("/")
    prefix, _, topic = path.rpartition("/")
    if not _TOPIC_RE.match(topic):
        raise ValueError(
            "the topic URL must end in a topic name (letters, digits, '-' or '_', "
            "at most 64), e.g. https://ntfy.example.com/CHANGE_ME_matrix_room_alias"
        )
    publish = urlunsplit((parts.scheme, parts.netloc, prefix + "/", "", ""))
    return publish, topic


def build_message(
    *, topic: str, title: str, message: str, priority: int, tags: list[str]
) -> dict[str, Any]:
    """The JSON body ntfy's publish endpoint takes."""
    return {
        "topic": topic,
        "title": title,
        "message": message,
        "priority": int(priority),
        "tags": list(tags),
    }


def headers_for(token: str | None) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers
