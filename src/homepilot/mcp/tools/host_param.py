"""One name for "which machine" across the MCP tool surface (#608).

The tool surface grew two names for the same thing. An assistant that had just
run ``exec_on_host(host=...)`` would call ``get_host_metrics(host=...)`` on the
next turn and get a KeyError, because that one wanted ``hostname`` - the kind of
inconsistency that costs a turn every time and teaches nothing.

**The standard is ``host``**, chosen by counting the surface rather than by
taste: at the time of the fix five tools took ``host``
(``read_file_on_guest``, ``exec_on_guest_readonly``, ``check_host_reachable``,
``exec_on_host``, ``write_file_on_host``) against four taking ``hostname``
(``add_host``, ``get_agent``, ``get_host_metrics``,
``get_host_metrics_series``). The majority name wins, so the smaller half moved.

``hostname`` stays accepted everywhere as a DEPRECATED ALIAS: a caller written
against the old surface - or a cached tool list - keeps working. Every schema
declares the alias and says it is deprecated, and tools that answer with a dict
add a ``warning`` naming the rename, so the deprecation is visible at runtime
and not only in the schema a caller may never re-read.

Deliberately NOT renamed: OUTPUT fields. ``get_host_metrics`` answers
``{"hostname": ...}`` because its HTTP route does, and payload shape is a
different contract from parameter naming - #608 is about what a caller passes.
"""

from __future__ import annotations

from typing import Any

#: The one parameter name every host-addressed tool takes.
HOST_PARAM = "host"

#: Accepted, deprecated, never the name in `required`.
DEPRECATED_HOST_PARAM = "hostname"

_ALIAS_NOTE = (
    "DEPRECATED alias for `host`, accepted so callers written against the older "
    "tool surface keep working. Pass `host` instead."
)

HOST_ALIAS_WARNING = (
    "`hostname` is a deprecated alias for `host` and may be removed; pass `host` instead."
)


def host_properties(description: str) -> dict[str, Any]:
    """The `host` property plus its deprecated `hostname` twin, for an inputSchema.

    Spread into a tool's ``properties``. Only ``host`` belongs in ``required``:
    the alias is a leniency of the handler, not a second way to be correct.
    """
    return {
        HOST_PARAM: {"type": "string", "description": description},
        DEPRECATED_HOST_PARAM: {
            "type": "string",
            "description": f"{_ALIAS_NOTE} ({description})",
        },
    }


def host_arg(arguments: dict[str, Any], *, required: bool = True) -> tuple[str, str | None]:
    """Read the host a call addresses, from either name.

    Returns ``(host, warning)`` - the warning is non-None exactly when the
    caller used the deprecated alias, so a dict-returning handler can pass it
    back. ``host`` wins if a caller somehow sends both: it is the standard, and
    silently preferring the deprecated name would make the migration a lie.
    """
    value = str(arguments.get(HOST_PARAM) or "").strip()
    if value:
        return value, None
    alias = str(arguments.get(DEPRECATED_HOST_PARAM) or "").strip()
    if alias:
        return alias, HOST_ALIAS_WARNING
    if required:
        raise ValueError(f"`{HOST_PARAM}` is required")
    return "", None


def with_host_warning(result: dict[str, Any], warning: str | None) -> dict[str, Any]:
    """Attach the deprecation warning to a dict result, if there is one."""
    if warning:
        result["warning"] = warning
    return result
