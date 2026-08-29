"""Check a tool's answer against the outputSchema it advertises (#628).

MCP clients validate `structured_content` against the tool's declared
`outputSchema` and REFUSE the whole answer when it does not match. Nothing in
this repo did that check, so `get_task_result` shipped promising
`artifact_id: string` while every provision, tailnet-join and template-build
task carries NULL - and the single tool that reports an outcome answered every
one of them with an error instead of an outcome.

Deliberately small and dependency-free: `type` (including union lists),
`required`, and nothing else. The schemas on this surface are flat, and a
checker with its own dependency would be one more thing that can go stale.
"""

from __future__ import annotations

from typing import Any

_PY_TYPES: dict[str, tuple[type, ...] | None] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "object": (dict,),
    "array": (list,),
    "null": None,
}


def _matches(value: Any, declared: Any) -> bool:
    names = declared if isinstance(declared, list) else [declared]
    for name in names:
        if name == "null":
            if value is None:
                return True
            continue
        expected = _PY_TYPES.get(str(name))
        if expected is None:
            continue
        # bool is an int in Python and never an acceptable integer here.
        if isinstance(value, bool) and name != "boolean":
            continue
        if isinstance(value, expected):
            return True
    return False


def assert_matches_output_schema(schema: dict[str, Any], result: dict[str, Any]) -> None:
    """Raise AssertionError unless `result` satisfies `schema`'s types and required set."""
    for name in schema.get("required", []):
        assert name in result, f"the handler omitted required field {name!r}"
    properties = schema.get("properties") or {}
    for name, declared in properties.items():
        if name not in result:
            continue
        if "type" not in declared:
            continue
        assert _matches(result[name], declared["type"]), (
            f"{name}={result[name]!r} does not match the declared type "
            f"{declared['type']!r}; an MCP client validating structured content "
            "refuses the whole answer when this happens"
        )
