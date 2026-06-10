from __future__ import annotations

import ast
from typing import Any

from jinja2 import StrictUndefined, TemplateSyntaxError
from jinja2.exceptions import SecurityError, UndefinedError
from jinja2.sandbox import ImmutableSandboxedEnvironment

_SAFE_CONTEXT: dict[str, Any] = {
    "target": "",
    "host": "",
    "inventory": [],
    "vault": {"key": ""},
    "fact": "",
}

_ALLOWLIST_VARS: set[str] = {
    "target",
    "host",
    "inventory",
    "status",
    "kind",
    "fact",
    "environment",
    "vars",
}

_ALLOWLIST_BUILTINS: set[str] = {"len", "str", "int", "bool", "float"}


def validate_jinja2_template(template_str: str) -> list[str]:
    env = ImmutableSandboxedEnvironment(undefined=StrictUndefined)
    try:
        tmpl = env.from_string(template_str)
    except TemplateSyntaxError as exc:
        return [f"Jinja2 syntax error: {exc}"]
    try:
        tmpl.render(**_SAFE_CONTEXT)
    except UndefinedError as exc:
        return [f"Jinja2 undefined variable: {exc}"]
    except SecurityError as exc:
        return [f"Jinja2 security error: {exc}"]
    except Exception as exc:
        return [f"Jinja2 render error: {exc}"]
    return []


def validate_skip_if(expression: str) -> list[str]:
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        return [f"skip_if syntax error: {exc}"]

    errors: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import | ast.ImportFrom):
            errors.append("skip_if: imports are not allowed")
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                if node.func.id not in _ALLOWLIST_BUILTINS:
                    errors.append(f"skip_if: function call '{node.func.id}' is not allowed")
            else:
                errors.append("skip_if: only allowlisted builtins can be called")
        elif isinstance(node, ast.Attribute):
            errors.append(f"skip_if: attribute access '.{node.attr}' is not allowed")
        elif isinstance(node, ast.Subscript):
            if not isinstance(node.slice, ast.Constant) or not isinstance(node.slice.value, str):
                errors.append("skip_if: only simple string-key subscripts are allowed")
        elif isinstance(node, ast.comprehension):
            errors.append("skip_if: comprehensions are not allowed")
        elif isinstance(node, ast.Lambda):
            errors.append("skip_if: lambda expressions are not allowed")
        elif isinstance(node, ast.NamedExpr):
            errors.append("skip_if: walrus operator is not allowed")
        elif isinstance(node, ast.Name):
            if node.id.startswith("__"):
                errors.append(f"skip_if: dunder name '{node.id}' is not allowed")
            elif (
                node.id not in _ALLOWLIST_VARS
                and node.id not in _ALLOWLIST_BUILTINS
                and node.id not in {"True", "False", "None"}
            ):
                errors.append(f"skip_if: variable '{node.id}' is not allowlisted")

    return errors


def validate_artifact_expressions(fm: dict[str, Any], body: str) -> list[str]:
    errors: list[str] = []
    if "skip_if" in fm:
        errors.extend(validate_skip_if(fm["skip_if"]))
    if "{{" in body or "{%" in body:
        errors.extend(validate_jinja2_template(body))
    return errors
