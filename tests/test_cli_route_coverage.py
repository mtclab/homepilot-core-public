"""Gate: every route of the API is usable from `hp` (#remote-cli).

Owner, 2026-11-14: "all the functionality that can be done through the API
should be doable through the CLI". A route passes when

* it has a CURATED command (``CURATED``: the everyday operator commands, with
  readable tables and meaningful exit codes), or
* the generated layer offers it: it is in the server's OpenAPI document with an
  operationId and not under a tag the layer hides - computed with the layer's
  OWN ``operations()``, over the REAL app's document, so this gate and
  `hp api` cannot disagree about what is reachable.

A route that is neither must appear in ``EXCLUDED`` with the reason an operator
cannot or must not call it with an API token. Teeth, both ways:

* ``test_every_route_is_reachable_or_excluded`` fails naming a new route that
  is hidden from the document (``include_in_schema=False``) or sits under a
  hidden tag, with no exclusion - ``test_the_gate_has_teeth`` proves it on a
  synthetic app;
* ``test_no_stale_entries`` fails on an exclusion or curated entry for a route
  that no longer exists, and ``test_every_curated_command_exists`` on a curated
  entry naming a command `hp` does not have.

This is the CLI's twin of tests/test_mcp_read_parity.py's parity gate.
"""

from __future__ import annotations

import re
from typing import Any

import typer
from fastapi import APIRouter, FastAPI

from homepilot.cli.api_layer import HIDDEN_TAGS, operations

# (method, path template) -> the curated `hp` command that drives it.
CURATED: dict[tuple[str, str], str] = {
    ("GET", "/dashboard/summary"): "hp status",
    ("GET", "/inventory"): "hp hosts list",
    ("DELETE", "/inventory/{host_id}"): "hp hosts forget",
    ("GET", "/inventory/{host_id}/power"): "hp power",
    ("POST", "/inventory/{host_id}/power"): "hp power",
    ("GET", "/agents/"): "hp agents list",
    ("GET", "/agents/upgrades"): "hp agents upgrade",
    ("GET", "/agents/install/{host_id}"): "hp agents install",
    ("POST", "/agents/install"): "hp agents install",
    ("GET", "/agents/token"): "hp agents token",
    ("POST", "/agents/bootstrap"): "hp agents bootstrap",
    ("GET", "/agents/enrolment-window"): "hp agents enrolment-window",
    ("POST", "/agents/enrolment-window"): "hp agents enrolment-window",
    ("DELETE", "/agents/enrolment-window"): "hp agents enrolment-window",
    ("POST", "/agents/{agent_id}/revoke"): "hp agents revoke",
    ("DELETE", "/agents/{agent_id}"): "hp agents forget",
    ("GET", "/artifacts"): "hp changes list",
    ("GET", "/artifacts/{artifact_id}"): "hp changes show",
    ("POST", "/artifacts"): "hp changes propose",
    ("POST", "/artifacts/{artifact_id}/approve"): "hp changes approve",
    ("POST", "/artifacts/{artifact_id}/approve-and-apply"): "hp changes approve-and-apply",
    ("POST", "/artifacts/{artifact_id}/reject"): "hp changes reject",
    ("POST", "/artifacts/{artifact_id}/withdraw"): "hp changes withdraw",
    ("POST", "/artifacts/{artifact_id}/apply"): "hp changes apply",
    ("POST", "/artifacts/{artifact_id}/replay"): "hp changes replay",
    ("DELETE", "/artifacts/{artifact_id}"): "hp changes revoke",
    ("GET", "/artifacts/drift"): "hp drift",
    ("GET", "/tasks"): "hp tasks list",
    ("GET", "/tasks/{task_id}"): "hp tasks follow",
    ("POST", "/tasks/{task_id}/cancel"): "hp tasks cancel",
    ("GET", "/kb"): "hp kb list",
    ("GET", "/kb/{doc_id}"): "hp kb show",
    ("GET", "/kb/search"): "hp kb search",
    ("POST", "/kb/notes"): "hp kb add",
    ("GET", "/monitoring/alerts"): "hp alerts",
    ("GET", "/admin/notifications/channels"): "hp channels list",
    ("POST", "/admin/notifications/channels/{channel_id}/test"): "hp channels test",
    ("GET", "/admin/guests/friends"): "hp guests list",
    ("GET", "/admin/guests"): "hp quota list",
    ("POST", "/admin/guests/quota"): "hp quota set",
    ("POST", "/admin/guests/invites"): "hp invite create",
    ("POST", "/admin/guests/invites/{prefix}/revoke"): "hp invite revoke",
    ("GET", "/admin/webhooks"): "hp webhook list",
    ("POST", "/admin/webhooks"): "hp webhook add",
    ("DELETE", "/admin/webhooks/{webhook_id}"): "hp webhook delete",
    ("POST", "/admin/webhooks/{webhook_id}/test"): "hp webhook test",
    ("GET", "/auth/me"): "hp whoami",
    ("GET", "/health"): "hp whoami",
}

_PORTAL = (
    "the invite portal a friend redeems a link on: authenticated by their mTLS client "
    "certificate at the proxy, so an API token is refused (tag hidden by the CLI)"
)
_GUEST = (
    "the guest portal - a friend's own view of their machines: mTLS client certificate "
    "at the proxy, no API token (tag hidden by the CLI)"
)
_CLAIM = (
    "the first-run claim: made once, from a browser on the instance's network, with "
    "the claim code, before any token exists (tag hidden by the CLI)"
)

EXCLUDED: dict[tuple[str, str], str] = {
    ("GET", "/"): "redirects a browser to the web UI",
    ("GET", "/metrics"): "Prometheus exposition for a scraper, not an operator command",
    ("GET", "/ui/{path:path}"): "the web UI's static files",
    ("GET", "/ui/_app/{path:path}"): "the web UI's static build assets",
    ("GET", "/invite/{token}"): _PORTAL,
    ("POST", "/invite/{token}"): _PORTAL,
    ("GET", "/invite/{token}/status"): _PORTAL,
    ("POST", "/invite/{token}/tailnet-join"): _PORTAL,
    ("GET", "/guest/"): _GUEST,
    ("GET", "/guest/vms"): _GUEST,
    ("GET", "/guest/vms/{host_id}"): _GUEST,
    ("GET", "/guest/quota"): _GUEST,
    ("POST", "/guest/vms/{host_id}/power"): _GUEST,
    ("GET", "/claim/status"): _CLAIM,
    ("POST", "/claim"): _CLAIM,
}

# Registered only when the web UI is built (web/dist): present on a release,
# absent in a checkout that has not run `npm run build`. Not stale either way.
_CONDITIONAL = {("GET", "/ui/{path:path}"), ("GET", "/ui/_app/{path:path}")}


def _template(path: str) -> str:
    """The OpenAPI form of a path: Starlette's `{key:path}` is `{key}` there."""
    return re.sub(r"\{(\w+):[^}]+\}", r"{\1}", path)


def _routes(app: FastAPI) -> set[tuple[str, str]]:
    from homepilot.main import _walk_api_routes

    return {
        (method, path)
        for path, route, _deps in _walk_api_routes(list(app.routes))
        for method in route.methods - {"HEAD", "OPTIONS"}
    }


def uncovered(app: FastAPI, curated: dict[Any, str], excluded: dict[Any, str]) -> list[str]:
    reachable = {(op.method, op.path) for op in operations(app.openapi())}
    return sorted(
        f"{method} {path}"
        for method, path in _routes(app)
        if (method, _template(path)) not in reachable
        and (method, path) not in curated
        and (method, path) not in excluded
    )


def _real_app() -> FastAPI:
    from homepilot.main import app

    return app


def test_the_route_walk_finds_the_api() -> None:
    """Guard the guard: an empty walk would make the gate vacuous."""
    routes = _routes(_real_app())
    assert len(routes) > 100, routes
    assert ("DELETE", "/inventory/{host_id}") in routes


def test_every_route_is_reachable_or_excluded() -> None:
    missing = uncovered(_real_app(), CURATED, EXCLUDED)
    assert not missing, (
        f"route(s) `hp` cannot reach: {missing}. Keep them in the OpenAPI document "
        "(the generated `hp api` layer then offers them), add a curated command, or add "
        "an EXCLUDED entry saying why an operator must not have them."
    )


def test_no_stale_entries() -> None:
    live = _routes(_real_app())
    stale = sorted((set(CURATED) | set(EXCLUDED)) - live - _CONDITIONAL)
    assert not stale, f"coverage map names route(s) the API no longer has: {stale}"


def test_every_exclusion_is_really_unreachable() -> None:
    """An exclusion for a route the layer offers is a reason nobody needs."""
    reachable = {(op.method, op.path) for op in operations(_real_app().openapi())}
    needless = sorted(k for k in EXCLUDED if (k[0], _template(k[1])) in reachable)
    assert not needless, f"excluded route(s) the generated layer reaches anyway: {needless}"


def _commands() -> set[str]:
    from homepilot.cli.main import app

    found: set[str] = set()

    def walk(cmd: Any, prefix: str) -> None:
        found.add(prefix)
        # Typer vendors its own click, so its groups are not `click.Group`.
        for name, sub in (getattr(cmd, "commands", None) or {}).items():
            walk(sub, f"{prefix} {name}")

    walk(typer.main.get_command(app), "hp")
    return found


def test_every_curated_command_exists() -> None:
    commands = _commands()
    absent = sorted({c for c in CURATED.values() if c not in commands})
    assert not absent, f"CURATED names command(s) `hp` does not have: {absent}"


def test_hidden_tags_are_the_token_less_portals() -> None:
    assert set(HIDDEN_TAGS) == {"guest", "invite", "claim"}


def test_the_gate_has_teeth() -> None:
    """A route kept out of the document, and one under a hidden tag, both fail
    the gate until they are excluded with a reason."""
    router = APIRouter()

    @router.get("/visible")
    async def visible() -> dict[str, str]:
        return {}

    @router.get("/secret-shape", include_in_schema=False)
    async def hidden() -> dict[str, str]:
        return {}

    @router.post("/portal-thing", tags=["guest"])
    async def portal() -> dict[str, str]:
        return {}

    synthetic = FastAPI()
    synthetic.include_router(router)
    assert uncovered(synthetic, {}, {}) == ["GET /secret-shape", "POST /portal-thing"]
    assert uncovered(synthetic, {("GET", "/secret-shape"): "x"}, {}) == ["POST /portal-thing"]
    assert uncovered(synthetic, {}, {("POST", "/portal-thing"): "x"}) == ["GET /secret-shape"]
