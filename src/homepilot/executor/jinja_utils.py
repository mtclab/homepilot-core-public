from __future__ import annotations

import logging
from typing import Any

from jinja2 import BaseLoader, TemplateError, Undefined
from jinja2.sandbox import SandboxedEnvironment

logger = logging.getLogger(__name__)


class SilentUndefined(Undefined):
    def __str__(self) -> str:
        return ""

    def __repr__(self) -> str:
        return f"<Undefined:{self._undefined_name}>"


def _interpolate(template_str: str, context: dict[str, Any]) -> str:
    env = SandboxedEnvironment(loader=BaseLoader(), undefined=SilentUndefined)
    try:
        tpl = env.from_string(template_str)
        return tpl.render(context)
    except (TemplateError, ValueError, TypeError) as exc:
        logger.debug("Template interpolation failed, returning raw string: %s", exc)
        return template_str


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
