"""MCP tool arguments are checked against the tool's own inputSchema (#696).

The mcp 2.x low-level ``Server`` does no input validation: whatever JSON a
caller sends reaches the handler as-is. Handlers index ``arguments["x"]`` and
call string methods on what they get, so a missing argument became a
``KeyError`` and a wrong-typed one a ``TypeError``/``AttributeError`` - and the
transport turned every one of those into "Internal server error". The 2026-09
audit counted 46 such escapes across 36 tools. The published schema already
says what each tool accepts; this module makes the dispatcher hold callers to
it before any handler runs, and phrases the refusal so a model can correct
the call (it names the argument and what was wrong).

It also defines which exceptions are DOMAIN answers - "Proxmox not configured",
"Secret 'x' not found", "Artifact ID already exists" - that the dispatcher
returns as an ``is_error`` result instead of letting them fall through to the
transport's generic handler. Anything not listed is a genuine defect and still
escapes (and is logged by the transport), which is what the registry-walking
fuzz gate in tests/test_mcp_input_fuzz_gate.py looks for.
"""

from __future__ import annotations

import re
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from .tools.host_param import DEPRECATED_HOST_PARAM, HOST_PARAM

_validators: dict[str, Draft202012Validator] = {}


def _validator(name: str, schema: dict[str, Any]) -> Draft202012Validator:
    cached = _validators.get(name)
    if cached is None or cached.schema is not schema:
        Draft202012Validator.check_schema(schema)
        cached = Draft202012Validator(schema)
        _validators[name] = cached
    return cached


def _describe(error: ValidationError, props: dict[str, Any]) -> str:
    where = ".".join(str(p) for p in error.absolute_path)
    if error.validator == "required" and not where:
        # "`approval_code` is required - <what it is>": the schema's own
        # description tells the caller what to supply, not just that it is gone.
        missing = re.match(r"'(.+)' is a required property", str(error.message))
        if missing:
            key = missing.group(1)
            about = (props.get(key) or {}).get("description")
            return f"`{key}` is required" + (f" - {about}" if about else "")
    # jsonschema's own message quotes the rejected instance ("'tskey-...' is
    # not of type 'integer'"), and these arguments include credentials (a
    # tailnet auth key, an approval code). The refusal names the argument and
    # the constraint it broke - from the SCHEMA, never the value sent (#697).
    reason = _constraint_text(error)
    return f"`{where}`: {reason}" if where else reason


def _constraint_text(error: ValidationError) -> str:
    """What the schema demanded, phrased without the instance (#697)."""
    kind, want = error.validator, error.validator_value
    if kind == "type":
        types = want if isinstance(want, list) else [want]
        return "must be of type " + " or ".join(str(t) for t in types)
    if kind == "enum":
        return f"must be one of {want}"
    if kind == "const":
        return f"must be {want!r}"
    if kind == "pattern":
        return f"does not match the pattern {want!r}"
    if kind == "format":
        return f"is not a valid {want}"
    if kind in ("minLength", "maxLength"):
        return f"must be {'at least' if kind == 'minLength' else 'at most'} {want} characters"
    if kind in ("minItems", "maxItems"):
        return f"must have {'at least' if kind == 'minItems' else 'at most'} {want} items"
    if kind in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"):
        op = {"minimum": ">=", "maximum": "<=", "exclusiveMinimum": ">", "exclusiveMaximum": "<"}
        return f"must be {op[kind]} {want}"
    if kind in ("required", "additionalProperties", "dependentRequired"):
        # These messages name PROPERTY keys from the call, not their values.
        return str(error.message)
    return f"fails the schema's {kind!r} check"


def validate_arguments(name: str, schema: dict[str, Any], arguments: Any) -> None:
    """Raise ``ValueError`` naming every argument that breaks ``schema``.

    ``hostname`` is the deprecated alias of ``host`` (#608): the handler accepts
    either, but only ``host`` is in ``required``, so the alias is mirrored onto
    ``host`` for the check alone. The handler still receives what was sent.
    """
    if not isinstance(arguments, dict):
        raise ValueError(
            f"Invalid arguments for '{name}': arguments must be a JSON object, "
            f"got {type(arguments).__name__}"
        )
    props = schema.get("properties") or {}
    view = arguments
    if (
        HOST_PARAM in props
        and DEPRECATED_HOST_PARAM in props
        and arguments.get(HOST_PARAM) is None
        and arguments.get(DEPRECATED_HOST_PARAM) is not None
    ):
        view = {**arguments, HOST_PARAM: arguments[DEPRECATED_HOST_PARAM]}

    errors = sorted(
        _validator(name, schema).iter_errors(view),
        key=lambda e: [str(p) for p in e.absolute_path],
    )
    if errors:
        detail = "; ".join(_describe(e, props) for e in errors[:5])
        raise ValueError(f"Invalid arguments for '{name}': {detail}")


def domain_error_types() -> tuple[type[BaseException], ...]:
    """Exceptions that carry an answer for the caller, not a crash.

    Imported lazily: several of these modules import the MCP server's
    neighbours, and this list is only needed once a call has failed.
    """
    from homepilot.adapters.agent import AgentAdapterError
    from homepilot.adapters.proxmox import ProxmoxError
    from homepilot.agent_hub.server import AgentCommandError
    from homepilot.artifacts.models import LifecycleError
    from homepilot.inventory.router import InventoryError
    from homepilot.vault.manager import VaultError

    return (
        ValueError,
        RuntimeError,
        AgentAdapterError,
        AgentCommandError,
        LifecycleError,
        InventoryError,
        ProxmoxError,
        VaultError,
    )


# RuntimeError subclasses that are interpreter faults, never a domain answer.
_NOT_DOMAIN: tuple[type[BaseException], ...] = (RecursionError, NotImplementedError)


def is_domain_error(exc: BaseException) -> bool:
    return isinstance(exc, domain_error_types()) and not isinstance(exc, _NOT_DOMAIN)


def domain_error_text(exc: BaseException) -> str:
    """What the caller reads. ``InventoryError`` keeps its text in ``detail``."""
    detail = getattr(exc, "detail", None)
    if isinstance(detail, str) and detail:
        return detail
    return str(exc) or type(exc).__name__
