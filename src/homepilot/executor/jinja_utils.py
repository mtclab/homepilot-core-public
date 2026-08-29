from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from jinja2 import BaseLoader, StrictUndefined, TemplateError
from jinja2.sandbox import SandboxedEnvironment

logger = logging.getLogger(__name__)


class InterpolationError(Exception):
    """A spec value referred to something the apply context does not have.

    The executor used to render with a ``SilentUndefined``, so
    ``{{ target.vmid }}`` against a target with no vmid became the empty string
    and the call went out at ``/nodes/pve/lxc//status/current``; and a template
    error returned the RAW string, so ``{{ ... }}`` was sent to Proxmox verbatim.
    Both are the #642 shape in its most direct form: a value that was never
    resolved, and then a mutating call anyway.

    Refusing here is safe because the propose-time validator renders the same
    body against the SAME context (``homepilot.artifacts.validators``), so a body
    that would raise at apply is refused before a human ever reviews it.
    """


def interpolation_context(
    target: dict[str, Any] | None, frontmatter: dict[str, Any] | None = None
) -> dict[str, Any]:
    """The names ARTIFACT_SPEC D2 says a spec body may interpolate.

    ONE definition, shared by the executors and by the propose-time validator, so
    the two cannot disagree about what an artifact is allowed to say. They did:
    the validator faked ``target`` as a STRING, which made D2's own canonical
    ``{{ target.node }}`` impossible to propose at all (proved live on 3.6.14).
    """
    fm = frontmatter or {}
    return {
        "target": dict(target or {}),
        "artifact": {
            "id": fm.get("id", ""),
            "intent": fm.get("intent", ""),
        },
        "now": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }


def _env() -> SandboxedEnvironment:
    return SandboxedEnvironment(loader=BaseLoader(), undefined=StrictUndefined)


def _interpolate(template_str: str, context: dict[str, Any]) -> str:
    env = _env()
    try:
        tpl = env.from_string(template_str)
        return tpl.render(context)
    except (TemplateError, ValueError, TypeError) as exc:
        raise InterpolationError(f"{template_str!r}: {exc}") from None


def _interpolate_obj(obj: Any, context: dict[str, Any]) -> Any:
    if isinstance(obj, str):
        return _interpolate(obj, context)
    if isinstance(obj, dict):
        return {k: _interpolate_obj(v, context) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_interpolate_obj(v, context) for v in obj]
    return obj


def _eval_skip_if(expression: str, response: Any, target: dict[str, Any]) -> bool:
    from homepilot.executor.skip_if import safe_eval_skip_if

    return safe_eval_skip_if(expression, response, target)
