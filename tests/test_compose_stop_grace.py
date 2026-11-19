"""The shipped compose files outwait the shutdown's bounded worst case (#812).

Since 3.10.0 SIGTERM really shuts HomePilot down: the lifespan's ``finally``
stops the agent hub, the reconcilers, drains seven services, retires the
blocking pools, checkpoints the WAL and closes the database - each step bounded
on its own. Normally that is about a second; with every step at its bound it is
``main.shutdown_worst_case_s()``. The compose files set no
``stop_grace_period``, so Docker's default 10 s SIGKILLed a shutdown that
legitimately needed longer, mid-drain - the state the clean shutdown exists to
prevent.

Teeth: drop ``stop_grace_period`` from either compose file, lower it under the
worst case, or add a step to the shutdown with a bound that is not in the
``SHUTDOWN_*`` table (a literal ``timeout=``), and a test here fails.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
import yaml

from homepilot import main

REPO = Path(__file__).resolve().parents[1]

COMPOSE_FILES = [
    pytest.param(REPO / "docker-compose.yml", id="docker-compose.yml"),
    pytest.param(REPO / "deploy" / "control-plane" / "docker-compose.yml", id="control-plane"),
]


def _seconds(value: str) -> float:
    """A compose duration (``150s``, ``2m30s``, ``1m``) in seconds."""
    parts = re.findall(r"(\d+(?:\.\d+)?)(ms|us|h|m|s)", value.strip())
    assert parts and "".join(n + u for n, u in parts) == value.strip(), (
        f"unreadable compose duration {value!r}"
    )
    scale = {"us": 1e-6, "ms": 1e-3, "s": 1, "m": 60, "h": 3600}
    return sum(float(n) * scale[u] for n, u in parts)


def test_the_worst_case_is_the_sum_of_the_shutdown_steps() -> None:
    # 20 hub + 10 reconcilers + 7 x 10 drains + 10 pools + 3 checkpoint
    # + 10 close + 3 watchdog join, at the time of writing.
    assert len(main.SHUTDOWN_DRAINABLE) == 7
    assert main.shutdown_worst_case_s() == pytest.approx(126.0)


@pytest.mark.parametrize("compose", COMPOSE_FILES)
def test_the_backend_grace_period_covers_the_worst_case_shutdown(compose: Path) -> None:
    services = yaml.safe_load(compose.read_text())["services"]
    backend = services["backend"]
    grace = backend.get("stop_grace_period")
    assert grace is not None, (
        f"{compose.name}: the backend has no stop_grace_period, so Docker's 10 s default "
        f"SIGKILLs a shutdown that may need {main.shutdown_worst_case_s():.0f} s"
    )
    assert _seconds(str(grace)) >= main.shutdown_worst_case_s(), (
        f"{compose.name}: stop_grace_period {grace} is shorter than the shutdown's "
        f"worst case {main.shutdown_worst_case_s():.0f} s"
    )


def _lifespan_finally() -> list[ast.stmt]:
    tree = ast.parse((REPO / "src" / "homepilot" / "main.py").read_text())
    (lifespan,) = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "lifespan"
    ]
    finals = [node.finalbody for node in ast.walk(lifespan) if isinstance(node, ast.Try)]
    (body,) = [f for f in finals if f]
    return body


def test_every_shutdown_bound_is_counted_in_the_worst_case() -> None:
    """A bound written as a literal (or a new constant) escapes the sum the
    compose grace period is held to; every bound must be a SHUTDOWN_* name."""
    counted = {
        "SHUTDOWN_HUB_STOP_S",
        "SHUTDOWN_RECONCILER_STOP_S",
        "SHUTDOWN_DRAIN_S",
        "SHUTDOWN_POOLS_S",
        "SHUTDOWN_DB_CLOSE_S",
    }
    seen: set[str] = set()
    for stmt in _lifespan_finally():
        for node in ast.walk(stmt):
            if not isinstance(node, ast.Call):
                continue
            bounds = [kw.value for kw in node.keywords if kw.arg == "timeout"]
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "wait_for" and len(node.args) > 1:
                bounds.append(node.args[1])
            for bound in bounds:
                assert isinstance(bound, ast.Name) and bound.id in counted, (
                    f"main.py:{node.lineno}: shutdown bound {ast.unparse(bound)} is not one "
                    "of the SHUTDOWN_* constants that shutdown_worst_case_s() sums"
                )
                seen.add(bound.id)
    assert seen == counted, f"bounds summed but not used in the shutdown: {counted - seen}"
