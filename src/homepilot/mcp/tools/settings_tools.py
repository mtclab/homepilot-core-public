"""Operator settings over MCP, at the admin tier (#553 C4).

C2 gave the settings a persistence layer with env > db > default precedence and
an HTTP surface; C3 put a live cluster probe in front of the provisioning half.
This module is the same four operations reachable by an assistant, so a wiring
it can READ is a wiring it can FIX - at the tier the API already reserves for
them (every ``/admin/settings/overrides`` route is ``require_scope("admin")``,
so all four tools sit in ``_ADMIN_TOOLS`` and the tier gate holds them there).

Nothing here re-implements the rules. ``checked_set``, ``run_probe``,
``SettingsResolver.report`` and ``SettingsResolver.clear`` are the SAME
functions the admin router calls, so a refusal an operator meets in the UI is
the refusal an assistant meets here, word for word - an env-locked key, a value
the type rejects, a value the cluster refutes, a cluster that could not be asked
at all. Any of those and nothing is written.

Secrets are not reachable through this module, and not by a filter someone has
to remember: the registry these tools walk contains only non-secret settings, so
there is no key here that names a token, a passphrase or a signing secret, and
an attempt to set one is refused as an unknown setting.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from homepilot.app_settings import (
    EnvOverrideError,
    ProbeRefusedError,
    checked_set,
    resolver_from_state,
    run_probe,
)

# Repeated in every description so the tier and the secret rule are visible to a
# model reading one tool in isolation, not only to one that read them all.
_ADMIN_NOTE = (
    "Admin tier: an MCP token with read_only or full scope is refused. Only "
    "NON-SECRET settings exist here - tokens, passphrases and signing secrets "
    "are not in the registry at all and stay operator-only in the web UI."
)

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "query_settings_overrides",
        "description": (
            "Every operator-editable setting of this install, with the value in "
            "force, where it came from ('env', 'db' or 'default'), whether "
            "changing it takes effect without a restart (hot_reloadable), "
            "whether it can be tried against the live cluster first (probeable), "
            "the environment variable that would override it, and what it is "
            "for. A setting whose source is 'env' cannot be written from here "
            "(editable false) - the environment already decides it. Reads "
            "nothing and writes nothing. " + _ADMIN_NOTE
        ),
        "inputSchema": {"type": "object", "properties": {}},
        "outputSchema": {
            "type": "object",
            "properties": {"settings": {"type": "array", "items": {"type": "object"}}},
            "required": ["settings"],
        },
    },
    {
        "name": "set_setting_override",
        "description": (
            "Persist one operator setting, exactly as the Settings UI does. The "
            "write is REFUSED, and nothing is stored, when: the key is not a "
            "known setting (every secret is refused this way, because no secret "
            "has one); the environment already sets it, in which case a stored "
            "value would silently contradict the environment at the next boot; "
            "the value is the wrong shape for the setting; or the setting has a "
            "cluster probe and the cluster either refutes the value or cannot be "
            "asked about it - saving a provisioning default nobody could check "
            "is the lie the probe exists to prevent. On success returns the "
            "stored value, its source, and whatever the cluster said while "
            "confirming it. Use query_settings_overrides for the key names. " + _ADMIN_NOTE
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "The setting's key, as reported by query_settings_overrides",
                },
                "value": {
                    "description": "The new value; a number for int settings, a string otherwise"
                },
            },
            "required": ["key", "value"],
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "status": {"type": "string"},
                "key": {"type": "string"},
                "value": {},
                "source": {"type": "string"},
                "probe": {"type": ["object", "null"]},
            },
            "required": ["status", "key", "value", "source"],
        },
    },
    {
        "name": "clear_setting_override",
        "description": (
            "Drop the stored value for one setting, so the install goes back to "
            "the code default (or to the environment's value, if one is set). "
            "Refused, storing nothing, for an unknown key - which is every "
            "secret - and for a key the environment already decides. Returns the "
            "value that is in force afterwards and where it now comes from. " + _ADMIN_NOTE
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "The setting's key"},
            },
            "required": ["key"],
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "status": {"type": "string"},
                "key": {"type": "string"},
                "value": {},
                "source": {"type": "string"},
            },
            "required": ["status", "key", "value", "source"],
        },
    },
    {
        "name": "probe_setting_override",
        "description": (
            "Ask the live Proxmox cluster about a candidate value WITHOUT saving "
            "anything - is this node real, is this bridge on it, is this vmid a "
            "template, can this bridge carry this VLAN tag. A refusal is the "
            "answer that was asked for, not a failure: 'ok' false with "
            "'reachable' true means the cluster refuted the value and 'detail' "
            "repeats what it said, while 'reachable' false means the cluster "
            "could not be asked at all and the answer says nothing about the "
            "value. A setting with no probe answers ok, saying there is nothing "
            "to check it against. An unknown key is refused - which is every "
            "secret. Writes nothing either way. " + _ADMIN_NOTE
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "The setting's key"},
                "value": {"description": "The candidate value to try"},
            },
            "required": ["key", "value"],
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "key": {"type": "string"},
                "ok": {"type": "boolean"},
                "reachable": {"type": "boolean"},
                "detail": {"type": "string"},
            },
            "required": ["key", "ok", "reachable", "detail"],
        },
    },
]


def _state(ctx: dict[str, Any]) -> Any:
    """The state object the settings machinery reads.

    ``resolver_from_state`` wants a repository and a Settings; ``probe_context``
    wants the live Proxmox client, which after a secrets reload lives on the
    provision service rather than on the state. The MCP process has no FastAPI
    app, so this stands one up over the tool context - taking each piece from the
    context first and from the real AppState second, so both transports resolve
    against the same objects instead of one of them raising.
    """
    app_state = ctx.get("app_state")

    def pick(name: str) -> Any:
        value = ctx.get(name)
        if value is not None:
            return value
        return getattr(app_state, name, None)

    return SimpleNamespace(
        settings=pick("settings"),
        repo=pick("repo"),
        proxmox=pick("proxmox"),
        provision_service=pick("provision_service"),
        # Not carried by either transport's context: the resolver is built from
        # the repo and the Settings below, which is the same precedence over the
        # same rows the HTTP app's resolver reads.
        settings_resolver=None,
    )


def _resolver_or_raise(state: Any) -> Any:
    resolver = resolver_from_state(state)
    if resolver is None:
        raise RuntimeError(
            "Operator settings are not available: this process has no database to "
            "resolve them against."
        )
    return resolver


def _key(arguments: dict[str, Any]) -> str:
    return str(arguments.get("key") or "").strip()


async def handle_query_settings_overrides(
    arguments: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    """The same report GET /admin/settings/overrides returns."""
    resolver = _resolver_or_raise(_state(ctx))
    return {"settings": await resolver.report()}


async def handle_set_setting_override(
    arguments: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    """PUT /admin/settings/overrides/{key} over MCP.

    The refusals are re-raised as ValueError because that is what the MCP
    dispatcher turns into a tool error the model can read; the MESSAGE is the
    one the API surfaces, so the two surfaces refuse in the same words.
    """
    state = _state(ctx)
    resolver = _resolver_or_raise(state)
    try:
        resolved, probe = await checked_set(
            state, resolver, _key(arguments), arguments.get("value")
        )
    except ProbeRefusedError as exc:
        raise ValueError(exc.result.detail) from exc
    except EnvOverrideError as exc:
        raise ValueError(str(exc)) from exc
    return {
        "status": "ok",
        "key": resolved.key,
        "value": resolved.value,
        "source": resolved.source,
        "probe": None if probe is None else {"ok": probe.ok, "detail": probe.detail},
    }


async def handle_clear_setting_override(
    arguments: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    """DELETE /admin/settings/overrides/{key} over MCP."""
    resolver = _resolver_or_raise(_state(ctx))
    try:
        resolved = await resolver.clear(_key(arguments))
    except EnvOverrideError as exc:
        raise ValueError(str(exc)) from exc
    return {
        "status": "ok",
        "key": resolved.key,
        "value": resolved.value,
        "source": resolved.source,
    }


async def handle_probe_setting_override(
    arguments: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    """POST /admin/settings/overrides/{key}/probe over MCP: ask, never save."""
    state = _state(ctx)
    _resolver_or_raise(state)
    key = _key(arguments)
    result = await run_probe(state, key, arguments.get("value"))
    if result is None:
        return {
            "key": key,
            "ok": True,
            "reachable": True,
            "detail": "This setting has no cluster probe: there is nothing to check it against.",
        }
    return {"key": key, "ok": result.ok, "reachable": result.reachable, "detail": result.detail}
