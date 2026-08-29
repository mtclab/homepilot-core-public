from __future__ import annotations

import ast
from typing import Any

from jinja2 import StrictUndefined, TemplateSyntaxError
from jinja2.exceptions import SecurityError, UndefinedError
from jinja2.sandbox import ImmutableSandboxedEnvironment


class _AnySecret(dict):  # type: ignore[type-arg]
    """A stand-in for the vault at VALIDATION time (#505).

    The validator renders a body under StrictUndefined to catch typos, with a
    context that fakes each variable. `vault` was faked as `{"key": ""}`, so any
    real reference - `{{ vault.appdb.password }}` - raised "no attribute
    'appdb'" and the propose was REFUSED. Vault references were therefore
    unusable in a body no matter which executor resolved them.

    Secret names are not knowable at propose time (the vault may be locked, and
    the secret may be created later), so this answers for any name. It checks
    that the reference is well-FORMED, which is all a template validator can
    honestly check. A missing secret is caught at execute time, where it is a
    refusal rather than a guess.
    """

    def __getitem__(self, key: object) -> _AnySecret:
        return _AnySecret()

    def __getattr__(self, name: str) -> _AnySecret:
        return _AnySecret()

    def __str__(self) -> str:
        return ""


def _safe_context(fm: dict[str, Any] | None = None) -> dict[str, Any]:
    """The context a body is rendered against at PROPOSE time.

    It must be the same one the executor renders against at APPLY time, or the
    validator answers a different question from the one that matters. It was not:
    `target` was faked as the empty STRING, so ARTIFACT_SPEC D2's own canonical
    `{{ target.node }}` raised "'str object' has no attribute 'node'" and the
    propose was REFUSED - for every `proxmox-api-sequence` and `http-sequence`
    written the way the spec documents. Reproduced live on dev 3.6.14 with §13's
    worked Example 1, which could not be proposed at all.

    `artifact` and `now` were missing outright, and are also D2 names.

    So: the real target, from this artifact's own frontmatter. A body that names
    `{{ target.node }}` on a `cluster` target now fails here, which is what §11.7
    asks for ("paths must NOT contain `{{ target.node }}`" for cluster
    artifacts) - and a typo like `{{ target.nodee }}` fails too, which the old
    blanket-refusal could never distinguish from a correct reference.
    """
    from homepilot.executor.jinja_utils import interpolation_context

    ctx = interpolation_context((fm or {}).get("target"), fm)
    # Not part of D2's interpolation context, but long-standing names the
    # validator has always accepted; left in so this change refuses nothing it
    # used to allow.
    ctx.update({"host": "", "inventory": [], "vault": _AnySecret(), "fact": ""})
    return ctx


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


def validate_jinja2_template(template_str: str, fm: dict[str, Any] | None = None) -> list[str]:
    env = ImmutableSandboxedEnvironment(undefined=StrictUndefined)
    try:
        tmpl = env.from_string(template_str)
    except TemplateSyntaxError as exc:
        return [f"Jinja2 syntax error: {exc}"]
    try:
        tmpl.render(**_safe_context(fm))
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


def _body_skip_if_expressions(fm: dict[str, Any], body: str) -> list[tuple[str, str]]:
    """Every `precheck.skip_if` in this body, as (step id, expression).

    This is where `skip_if` actually lives - §5.2 / §5.3 put it inside each step's
    `precheck`. Nothing validated them; the propose-time check looked only at a
    frontmatter key the spec does not define.
    """
    import re

    import yaml

    fences = {
        "proxmox-api-sequence": ("proxmox-api-spec", "proxmox-api-rollback"),
        "http-sequence": ("http-spec", "http-rollback"),
    }.get(str(fm.get("kind", "")))
    if not fences:
        return []

    found: list[tuple[str, str]] = []
    for fence in fences:
        m = re.search(rf"```yaml\s+{re.escape(fence)}\s*\n(.*?)```", body, re.DOTALL)
        if not m:
            continue
        try:
            parsed = yaml.safe_load(m.group(1).strip())
        except yaml.YAMLError:
            continue
        if not isinstance(parsed, dict):
            continue
        steps = parsed.get("steps")
        if not isinstance(steps, list):
            continue
        for step in steps:
            if not isinstance(step, dict):
                continue
            precheck = step.get("precheck")
            if isinstance(precheck, dict) and precheck.get("skip_if"):
                found.append((str(step.get("id", "?")), str(precheck["skip_if"])))
    return found


def validate_artifact_expressions(fm: dict[str, Any], body: str) -> list[str]:
    from homepilot.executor.skip_if import validate_skip_if_expression

    errors: list[str] = []
    if "skip_if" in fm:
        errors.extend(validate_skip_if(fm["skip_if"]))
    for step_id, expr in _body_skip_if_expressions(fm, body):
        errors.extend(f"step '{step_id}': {e}" for e in validate_skip_if_expression(expr))
    if "{{" in body or "{%" in body:
        errors.extend(validate_jinja2_template(body, fm))
    return errors
