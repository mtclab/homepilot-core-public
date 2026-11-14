"""Gate: routes that destroy carry ``x-hp-destructive`` (#remote-cli).

The generated `hp api` layer asks before every DELETE and before every
operation the server marks ``x-hp-destructive`` (set from
``homepilot.common.OPENAPI_DESTRUCTIVE`` in the routers). A POST that revokes,
rejects, withdraws, cancels or powers a machine off is as final as a DELETE, so
a route of that shape without the mark would run with no question asked.

Teeth: drop ``openapi_extra=OPENAPI_DESTRUCTIVE`` from any of them and
``test_every_destroying_post_is_marked`` fails naming it.
"""

from __future__ import annotations

from homepilot.cli.api_layer import DESTRUCTIVE_EXTENSION, operations
from homepilot.common import OPENAPI_DESTRUCTIVE

_FINAL_VERBS = ("revoke", "reject", "withdraw", "cancel", "power", "forget", "destroy")

# The marked set, named, so a mark that disappears (or appears) is a diff here.
EXPECTED_MARKED = {
    ("POST", "/agents/{agent_id}/revoke"),
    ("POST", "/admin/guests/invites/{prefix}/revoke"),
    ("POST", "/artifacts/{artifact_id}/reject"),
    ("POST", "/artifacts/{artifact_id}/withdraw"),
    ("POST", "/inventory/{host_id}/power"),
    ("POST", "/tasks/{task_id}/cancel"),
    ("POST", "/guest/vms/{host_id}/power"),
}


def _spec() -> dict:
    from homepilot.main import app

    return app.openapi()


def _marked(spec: dict) -> set[tuple[str, str]]:
    return {
        (method.upper(), path)
        for path, methods in spec["paths"].items()
        for method, op in methods.items()
        if op.get(DESTRUCTIVE_EXTENSION)
    }


def test_the_mark_is_the_extension_the_cli_reads() -> None:
    assert OPENAPI_DESTRUCTIVE == {DESTRUCTIVE_EXTENSION: True}


def test_every_destroying_post_is_marked() -> None:
    spec = _spec()
    marked = _marked(spec)
    shaped = {
        (method.upper(), path)
        for path, methods in spec["paths"].items()
        for method in methods
        if method in ("post", "put", "patch")
        and path.rstrip("/").rsplit("/", 1)[-1] in _FINAL_VERBS
    }
    unmarked = sorted(shaped - marked)
    assert not unmarked, f"destroying route(s) without x-hp-destructive: {unmarked}"


def test_the_marked_set_is_exactly_the_expected_one() -> None:
    assert _marked(_spec()) == EXPECTED_MARKED


def test_the_cli_treats_marked_and_delete_operations_as_destructive() -> None:
    spec = _spec()
    by_route = {(op.method, op.path): op for op in operations(spec)}
    for key in EXPECTED_MARKED - {("POST", "/guest/vms/{host_id}/power")}:
        assert by_route[key].destructive, key
    deletes = [op for op in by_route.values() if op.method == "DELETE"]
    assert deletes and all(op.destructive for op in deletes)
    reads = [op for op in by_route.values() if op.method == "GET"]
    assert not any(op.destructive for op in reads)
