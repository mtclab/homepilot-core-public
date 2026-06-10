from __future__ import annotations

from typing import Any


def _topological_sort(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    step_map: dict[str, dict[str, Any]] = {}
    deps: dict[str, list[str]] = {}
    for step in steps:
        sid = step.get("id", "")
        step_map[sid] = step
        deps[sid] = step.get("depends_on") or []

    visited: set[str] = set()
    order: list[str] = []
    visiting: set[str] = set()

    def visit(node: str) -> None:
        if node in visited:
            return
        if node in visiting:
            raise ValueError(f"Circular dependency detected involving: {node}")
        visiting.add(node)
        for dep in deps.get(node, []):
            visit(dep)
        visiting.discard(node)
        visited.add(node)
        order.append(node)

    for sid in step_map:
        visit(sid)

    return [step_map[sid] for sid in order if sid in step_map]
