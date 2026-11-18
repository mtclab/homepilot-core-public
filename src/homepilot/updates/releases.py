"""What a GitHub release says, read conservatively (review A+, "updates available").

Pure functions, no I/O: the checker hands this module the JSON GitHub returned
for the public mirror's latest release, and gets back either a release this
install may be told about or ``None``.

Three judgments live here, each one-sided on purpose:

* **Version comparison.** A release counts only when its tag is a plain
  ``X.Y.Z`` (``v`` prefix allowed). Anything with a suffix - ``3.8.0-rc1``,
  ``3.8.0b2`` - is a pre-release and is never offered, whatever GitHub's own
  ``prerelease`` flag says. The RUNNING version may carry a suffix (a dev build);
  a pre-release suffix sorts it BEFORE the plain release of the same numbers,
  so a ``3.8.0rc1`` install is told about ``3.8.0``. An unparseable running
  version compares as unknown, never as "up to date" and never as "behind".
* **The release-notes link** is only ever an https URL on github.com; anything
  else in the answer falls back to the releases page we asked about.
* **Operator actions** are lifted from the release notes when they are written
  the way the changelog writes them (a paragraph led by ``**Upgrade:**`` or
  ``**Operator action...:**``, or a section headed "Upgrade notes" / "Required
  reading before upgrading"). When none can be found the list is empty - the
  line still links to the notes; it never invents an instruction.
"""

from __future__ import annotations

import re
from typing import Any

from ..agent_hub.dist import PUBLIC_RELEASES

# The public mirror is where releases are published (the same address the agent
# installer's fallback names); the private repository never is.
RELEASES_PAGE_URL = PUBLIC_RELEASES
RELEASES_API_URL = "https://api.github.com/repos/mtclab/homepilot-core-public/releases/latest"

_RELEASE_TAG_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
_RUNNING_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(.*)$")

# Bounds on what is carried out of an answer we do not control.
MAX_ACTIONS = 5
MAX_ACTION_CHARS = 500
MAX_NAME_CHARS = 120

VersionKey = tuple[int, int, int, int]


def release_key(tag: str | None) -> VersionKey | None:
    """Sort key of a RELEASE tag, or None when it is not a plain X.Y.Z."""
    match = _RELEASE_TAG_RE.match(str(tag or "").strip())
    if match is None:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)), 1)


def running_key(version: str | None) -> VersionKey | None:
    """Sort key of the RUNNING version, or None when it cannot be read.

    ``+local`` build metadata does not change what a version is; any other
    suffix (``rc1``, ``-dev``, ``.dev0``) marks a build from before that release.
    """
    match = _RUNNING_RE.match(str(version or "").strip())
    if match is None:
        return None
    suffix = match.group(4)
    final = 1 if suffix == "" or suffix.startswith("+") else 0
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)), final)


def plain_version(tag: str) -> str:
    """``v3.8.0`` -> ``3.8.0``, for a tag ``release_key`` accepted."""
    return str(tag).strip().lstrip("v")


def is_newer(release_version: str | None, running_version: str | None) -> bool | None:
    """Is the release newer than what runs here? None when either is unreadable."""
    rel = release_key(release_version)
    run = running_key(running_version)
    if rel is None or run is None:
        return None
    return rel > run


def _clean_text(value: str, limit: int) -> str:
    """One line of plain text: markdown emphasis and control characters out."""
    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", value)
    text = text.replace("**", "").replace("__", "")
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[: limit - 3].rstrip() + "..."
    return text


# A bold LABEL that ends in ":" or "." - `**Upgrade:**`, `**Operator action, agents
# profile only:**`, a label spanning lines - not any bold word that happens to
# start a paragraph.
_ACTION_LEAD_RE = re.compile(r"^\*\*\s*(upgrade|operator action)\b[^*]*[:.]\s*\*\*", re.IGNORECASE)
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
# Which headings hold operator instructions. A heading IS an instruction heading
# when, once leading emoji/punctuation are dropped, it
#   * starts with "Operator action(s)", "Upgrade note(s)", "Upgrade
#     instructions", "Upgrading" or "Before you upgrade" (a qualifier may follow:
#     "Operator actions (agents profile)", "Upgrade notes - read first"), or
#   * says "before upgrading" / "before you upgrade" anywhere ("Required reading
#     before upgrading").
# Not a heading that merely contains the word: "Upgrade agent keeps the host's
# grant (#841)" is a feature, and lifting it told operators to do a changelog
# entry (3.14 release check).
_ACTION_HEADING_RE = re.compile(
    r"^(operator actions?|upgrade notes?|upgrade instructions|upgrading|before you upgrade)"
    r"(?![\w-])"
    r"|\bbefore (you )?upgrad(e|ing)\b",
    re.IGNORECASE,
)
_HEADING_LEAD_RE = re.compile(r"^[^\w]+")
_LIST_ITEM_RE = re.compile(r"^([-*+]|\d+[.)])\s+(.*)$")
_NOTHING_TO_DO_RE = re.compile(r"^(-\s*)?none\.?$", re.IGNORECASE)


def is_action_heading(text: str) -> bool:
    """Does this heading introduce operator instructions (see _ACTION_HEADING_RE)?"""
    return bool(_ACTION_HEADING_RE.search(_HEADING_LEAD_RE.sub("", text.strip())))


def _section_items(lines: list[str]) -> list[str]:
    """An instruction section's items: one per top-level list item, prose as one.

    A top-level list item (``- ``, ``* ``, ``1. `` at the start of the line) is
    one action, its wrapped and indented continuation lines joined to it. Text
    that is not in a list item - a section written as plain prose, over one
    paragraph or several - is ONE action. The 3.14 notes have five bullets;
    joining the whole section into one item cut it at 500 characters and lost
    the compose and n8n steps.
    """
    items: list[list[str]] = []
    prose: list[str] | None = None
    current: list[str] | None = None
    for raw in lines:
        if not raw.strip():
            current = None  # a blank line ends a list item; prose runs on
            continue
        item = _LIST_ITEM_RE.match(raw)
        if item and raw[:1] not in (" ", "\t"):
            current = [item.group(2).strip()]
            items.append(current)
        elif current is not None and (raw[:1] in (" ", "\t") or not item):
            current.append(raw.strip())
        else:
            if prose is None:
                prose = []
                items.append(prose)
            prose.append(raw.strip())
    return [" ".join(i) for i in items]


def _limit(actions: list[str]) -> list[str]:
    """At most MAX_ACTIONS, and when more were found the last one says so."""
    if len(actions) <= MAX_ACTIONS:
        return actions
    more = len(actions) - (MAX_ACTIONS - 1)
    return [
        *actions[: MAX_ACTIONS - 1],
        f"{more} more operator actions are in the release notes - read them before upgrading.",
    ]


def operator_actions(body: str | None) -> list[str]:
    """The upgrade instructions a release's notes carry, as plain-text items.

    Two sources, both CHANGELOG conventions (docs/CHANGELOG.md):

    * a section under an instruction heading (:func:`is_action_heading`), up to
      the next heading of the same or a higher level - each top-level list
      item one action, prose one action (:func:`_section_items`); a section
      that only says "None." is no action;
    * outside such sections, a paragraph led by a bold ``**Upgrade...:**`` or
      ``**Operator action...:**`` label.

    Each action is cut at MAX_ACTION_CHARS (ending "..." when cut); past
    MAX_ACTIONS the last item says how many more are in the notes.
    """
    if not body:
        return []
    lines = str(body).replace("\r\n", "\n").split("\n")
    actions: list[str] = []

    section_level = 0
    section: list[str] = []
    paragraph: list[str] = []

    def flush_section() -> None:
        for item in _section_items(section):
            text = _clean_text(item, MAX_ACTION_CHARS)
            # "### Operator actions" + "None." is how a release says there is
            # nothing to do; lifted, it became an update notice telling the
            # operator to do "None.".
            if text and not _NOTHING_TO_DO_RE.match(text):
                actions.append(text)
        section.clear()

    def flush_paragraph() -> None:
        if paragraph and _ACTION_LEAD_RE.match(" ".join(paragraph)):
            text = _clean_text(" ".join(paragraph), MAX_ACTION_CHARS)
            if text:
                actions.append(text)
        paragraph.clear()

    for raw in lines:
        heading = _HEADING_RE.match(raw.strip())
        if heading:
            level = len(heading.group(1))
            if section_level and level <= section_level:
                flush_section()
                section_level = 0
            flush_paragraph()
            if not section_level and is_action_heading(heading.group(2)):
                section_level = level
            continue
        if section_level:
            section.append(raw.rstrip())
            continue
        if raw.strip():
            paragraph.append(raw.strip())
        else:
            flush_paragraph()
    flush_section()
    flush_paragraph()
    return _limit(actions)


def _notes_url(value: Any) -> str:
    url = str(value or "").strip()
    if url.startswith("https://github.com/") and not any(ch.isspace() for ch in url):
        return url
    return RELEASES_PAGE_URL


def parse_latest(data: Any) -> dict[str, Any] | None:
    """The release an install may be told about, or None.

    None for anything that is not a published, final, plainly-versioned release:
    a draft, a pre-release (by flag OR by tag), or an answer of the wrong shape.
    """
    if not isinstance(data, dict):
        return None
    if data.get("draft") or data.get("prerelease"):
        return None
    tag = data.get("tag_name")
    if not isinstance(tag, str) or release_key(tag) is None:
        return None
    published = data.get("published_at")
    return {
        "version": plain_version(tag),
        "tag": tag.strip(),
        "name": _clean_text(str(data.get("name") or tag), MAX_NAME_CHARS),
        "url": _notes_url(data.get("html_url")),
        "published_at": published if isinstance(published, str) else None,
        "operator_actions": operator_actions(
            data.get("body") if isinstance(data.get("body"), str) else None
        ),
    }
