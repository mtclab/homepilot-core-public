"""Safe expression evaluator for artifact skip_if conditions.

Replaces eval() with a restricted AST-based evaluator that only allows
comparison and boolean operations on response/target attributes.
No function calls, no imports, no attribute access beyond dict keys.

Allowed skip_if context (per ARTIFACT_SPEC.md §5.2/§5.3 and D2):
  response.status_code  — int HTTP status of the precheck response
  response.headers      — dict of response headers (auth/cookie headers excluded)
  response.json         — pre-parsed JSON body (dict/list/None); walk with subscripts
  target                — artifact target dict; use subscript access: target["host"]

An expression that CANNOT BE DECIDED against the response is not the same answer
as "do not skip" (#642). Every failure used to collapse into ``False``, and
``False`` is the branch that RUNS the mutating step - so a precheck whose binding
was wrong, or whose expression referred to a name that is not there, silently
became permission to act. Those cases now raise :class:`SkipIfUndecided`, and the
executors refuse to run the step behind an undecided guard.

A missing dict KEY is different and still answers ``None``: "the response has no
``count`` field" is a fact about the target's state, which is exactly what a
precheck is for. A missing ATTRIBUTE is a fact about the BINDING being wrong, and
no artifact should mutate on the strength of one.
"""

from __future__ import annotations

import ast
import operator
from collections.abc import Callable
from typing import Any

_BoolOpFunc = Callable[[Any, Any], Any]

_ALLOWED_BOOL_OPS: dict[type, _BoolOpFunc] = {
    ast.And: lambda a, b: a and b,
    ast.Or: lambda a, b: a or b,
}

_ALLOWED_COMPARE_OPS: dict[type, _BoolOpFunc] = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.In: lambda a, b: a in b,
    ast.NotIn: lambda a, b: a not in b,
    ast.Is: operator.is_,
    ast.IsNot: operator.is_not,
}

_ALLOWED_NAMES = {"True": True, "False": False, "None": None}

_EXCLUDED_RESPONSE_HEADERS = frozenset(
    {"authorization", "cookie", "set-cookie", "set-cookie2", "x-auth-token"}
)


class _ResponseProxy:
    """Sanitised view of an HTTP precheck response for skip_if evaluation.

    __slots__ prevents arbitrary attribute injection. Only exposes:
      status_code — HTTP status code (int)
      headers     — response headers dict, sensitive keys excluded
      json        — pre-parsed JSON body (dict/list/None), walk with subscripts
    """

    __slots__ = ("headers", "json", "status_code")

    def __init__(
        self,
        status_code: int,
        headers: dict[str, str],
        json_body: Any,
    ) -> None:
        self.status_code = status_code
        self.headers = headers
        self.json = json_body


class _PveResponseProxy(_ResponseProxy):
    """The Proxmox flavour of the same view, and the envelope it wraps.

    ``ProxmoxClient.call`` hands back the parsed ``{"data": ...}`` envelope and
    nothing else, so ``proxmox-api-sequence`` used to bind that raw dict as
    ``response``. ARTIFACT_SPEC §5.2 and D2 define the binding as
    ``response.status_code`` / ``response.json``, and a dict has neither: every
    precheck written to the spec evaluated to False and the mutating step ran.
    Proved live on dev 3.6.14 - the spec's own worked example never skipped.

    So the documented names are bound properly here. Subscripting ``response``
    still reaches the envelope, because artifacts already in the store were
    written against the shape the executor really had (``response["data"]["…"]``)
    and silently changing what an APPLIED artifact's precheck means would be its
    own dishonesty.
    """

    __slots__ = ("envelope",)

    def __init__(self, status_code: int, envelope: Any) -> None:
        super().__init__(status_code, {}, envelope)
        self.envelope = envelope

    def __getitem__(self, key: Any) -> Any:
        return self.envelope[key]


def make_pve_response_proxy(status_code: int, envelope: Any) -> _PveResponseProxy:
    """Bind a Proxmox API answer under the names ARTIFACT_SPEC D2 documents."""
    return _PveResponseProxy(status_code, envelope)


def make_response_proxy(response: Any) -> _ResponseProxy:
    """Wrap an httpx.Response (or similar) in a sanitised _ResponseProxy."""
    if isinstance(response, _ResponseProxy):
        return response

    try:
        status_code = int(getattr(response, "status_code", 0))
    except (ValueError, TypeError):
        status_code = 0

    try:
        raw_headers = getattr(response, "headers", {})
        safe_headers: dict[str, str] = {
            k.lower(): str(v)
            for k, v in (raw_headers.items() if hasattr(raw_headers, "items") else {}.items())
            if k.lower() not in _EXCLUDED_RESPONSE_HEADERS
        }
    except (AttributeError, KeyError, TypeError, ValueError):
        safe_headers = {}

    try:
        json_method = getattr(response, "json", None)
        json_body = json_method() if callable(json_method) else None
    except (AttributeError, TypeError, ValueError):
        json_body = None

    return _ResponseProxy(status_code, safe_headers, json_body)


class UnsafeExpressionError(Exception):
    pass


class SkipIfUndecided(Exception):  # noqa: N818 - it is not an error, it is an answer
    """The precheck expression could not be decided against this response.

    Raised instead of answering ``False``, because ``False`` means "the target is
    NOT in the desired state, run the mutating step" - and an undecided guard
    must never say that (#642).
    """


def _eval_node(node: ast.AST, context: dict[str, Any]) -> Any:
    if isinstance(node, ast.Expression):
        return _eval_node(node.body, context)

    if isinstance(node, ast.Constant):
        return node.value

    if isinstance(node, ast.Name):
        if node.id in _ALLOWED_NAMES:
            return _ALLOWED_NAMES[node.id]
        if node.id in context:
            return context[node.id]
        raise UnsafeExpressionError(f"unknown name: {node.id}")

    if isinstance(node, ast.Subscript):
        value = _eval_node(node.value, context)
        if isinstance(node.slice, ast.Constant):
            key = node.slice.value
        elif isinstance(node.slice, ast.Name):
            key = _eval_node(node.slice, context)
        else:
            raise UnsafeExpressionError("complex subscript not allowed")
        try:
            return value[key]
        except (KeyError, IndexError, TypeError):
            return None

    if isinstance(node, ast.Attribute):
        if node.attr.startswith("__") and node.attr.endswith("__"):
            raise UnsafeExpressionError(f"dunder attribute access not allowed: {node.attr}")
        if node.attr.startswith("_"):
            raise UnsafeExpressionError(f"private attribute access not allowed: {node.attr}")
        value = _eval_node(node.value, context)
        try:
            return getattr(value, node.attr)
        except AttributeError:
            # NOT None. A name that is not bound on this response is a broken
            # precheck, and answering None here made it compare False, which is
            # the branch that mutates.
            raise SkipIfUndecided(
                f"the precheck response has no '{node.attr}'; this expression could not be decided"
            ) from None

    if isinstance(node, ast.BoolOp):
        op_func = _ALLOWED_BOOL_OPS.get(type(node.op))
        if op_func is None:
            raise UnsafeExpressionError(f"bool op not allowed: {type(node.op).__name__}")
        values = [_eval_node(v, context) for v in node.values]
        result = values[0]
        for v in values[1:]:
            result = op_func(result, v)
        return result

    if isinstance(node, ast.UnaryOp):
        if isinstance(node.op, ast.Not):
            return not _eval_node(node.operand, context)
        raise UnsafeExpressionError(f"unary op not allowed: {type(node.op).__name__}")

    if isinstance(node, ast.Compare):
        left = _eval_node(node.left, context)
        for op, comparator in zip(node.ops, node.comparators, strict=True):
            right = _eval_node(comparator, context)
            op_func = _ALLOWED_COMPARE_OPS.get(type(op))
            if op_func is None:
                raise UnsafeExpressionError(f"compare op not allowed: {type(op).__name__}")
            if not op_func(left, right):
                return False
            left = right
        return True

    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Name) and func.id in ("any", "all") and len(node.args) == 1:
            raise UnsafeExpressionError("any/all with generators not supported")
        raise UnsafeExpressionError(f"function call not allowed: {ast.dump(node)}")

    if isinstance(node, ast.List):
        return [_eval_node(elt, context) for elt in node.elts]

    if isinstance(node, ast.Tuple):
        return tuple(_eval_node(elt, context) for elt in node.elts)

    if isinstance(node, ast.Set):
        return {_eval_node(elt, context) for elt in node.elts}

    if isinstance(node, ast.Dict):
        result_dict: dict[Any, Any] = {}
        for k, v in zip(node.keys, node.values, strict=True):
            if k is None:
                raise UnsafeExpressionError("dict unpacking (**) not allowed")
            result_dict[_eval_node(k, context)] = _eval_node(v, context)
        return result_dict

    raise UnsafeExpressionError(f"node type not allowed: {type(node).__name__}")


_STATIC_ALLOWED_NAMES = frozenset({"response", "target", "True", "False", "None"})


def validate_skip_if_expression(expression: str) -> list[str]:
    """Would :func:`safe_eval_skip_if` be able to run this at all?

    Checked at PROPOSE, so a `skip_if` the evaluator will refuse is caught while
    it is still a draft rather than at apply - where it now means the step does
    not run. The old propose-time check validated `frontmatter["skip_if"]`, a key
    ARTIFACT_SPEC does not define, under an allowlist that forbade attribute
    access and did not bind `response` - so it could only ever have refused the
    spec's own documented form, and it never looked at the prechecks in the body
    where every real `skip_if` lives.

    Mirrors ``_eval_node``'s refusals; ``tests/test_precheck_truth.py`` gates the
    two against each other.
    """
    errors: list[str] = []
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        return [f"skip_if syntax error: {exc}"]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import | ast.ImportFrom):
            errors.append("skip_if: imports are not allowed")
        elif isinstance(node, ast.Call):
            errors.append("skip_if: function calls are not allowed")
        elif isinstance(node, ast.Lambda):
            errors.append("skip_if: lambda expressions are not allowed")
        elif isinstance(node, ast.comprehension):
            errors.append("skip_if: comprehensions are not allowed")
        elif isinstance(node, ast.NamedExpr):
            errors.append("skip_if: the walrus operator is not allowed")
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("_"):
                errors.append(f"skip_if: private attribute access '{node.attr}' is not allowed")
        elif isinstance(node, ast.Name) and node.id not in _STATIC_ALLOWED_NAMES:
            errors.append(
                f"skip_if: '{node.id}' is not bound; the context is "
                "response.status_code / response.headers / response.json and target"
            )
    return errors


def safe_eval_skip_if(expression: str, response: Any, target: dict[str, Any]) -> bool:
    """Decide a precheck expression, or raise :class:`SkipIfUndecided`.

    Every failure used to become ``False``. ``False`` runs the mutating step, so
    a syntax error, a refused construct, or a wrong binding was silently upgraded
    to "yes, apply it" - in the one guard whose entire job is to stop that (#642).
    """
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise SkipIfUndecided(f"skip_if is not a valid expression: {exc}") from None
    context: dict[str, Any] = {
        "response": response,
        "target": target,
    }
    try:
        result = _eval_node(tree, context)
    except SkipIfUndecided:
        raise
    except UnsafeExpressionError as exc:
        raise SkipIfUndecided(f"skip_if was refused by the evaluator: {exc}") from None
    except (ValueError, TypeError, AttributeError, KeyError) as exc:
        raise SkipIfUndecided(f"skip_if could not be evaluated: {exc}") from None
    return bool(result)
