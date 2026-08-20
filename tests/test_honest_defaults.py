"""Standing gate: no shipped default may name a host a stock install lacks.

ADR-004 corollary 3 - "an optional service either works out of the box or is off
and says so; never point a default at a host that does not exist". Two defaults
violated it (epic #458 S6): ``HP_EMBEDDING_SERVICE_URL`` pointed at ``llm-embed``,
which lives only in the docker-compose.agent.yml overlay behind the gpu/cpu
profiles, and ``.env.example`` shipped ``HP_EVENTS_WEBHOOK_URL`` pointing at the
n8n container, which sits behind the optional ``agents`` profile.

This forbids the CLASS, not those two lines: any ``Settings`` default or
``.env.example`` HP_* value that is a dialable address must be empty, or listed
below with a written reason.

Teeth: restore either default (in ``config.py`` or in ``.env.example``) and this
fails, naming the offender.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlsplit

from homepilot.config import Settings

REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_EXAMPLE = REPO_ROOT / ".env.example"
STOCK_COMPOSE = REPO_ROOT / "docker-compose.yml"

# Fields whose non-empty default is an address but NOT something HomePilot dials.
# Every entry needs a reason.
_NOT_A_DIAL_TARGET = {
    # An allowlist of BROWSER origins compared against the Origin header. Nothing
    # here is ever connected to, so an origin that does not exist costs nothing.
    "cors_origins",
}

_ENV_NOT_A_DIAL_TARGET = {
    "HP_CORS_ORIGINS",  # same as cors_origins above
}

_ENV_ASSIGN_RE = re.compile(r"^\s*(HP_[A-Z0-9_]+)\s*=\s*([^#\s]*)")


def _dialable_host(value: str) -> str | None:
    """The host a value tells HomePilot to connect to, or None if it is not an
    address at all (a path, a name, a number, a header)."""
    value = value.strip()
    if not value:
        return None
    parts = urlsplit(value)
    if parts.scheme in ("http", "https", "ws", "wss") and parts.netloc:
        return parts.hostname
    return None


def _services_in_stock_compose() -> set[str]:
    """Service names started by a bare ``docker compose up -d`` - i.e. those
    without a ``profiles:`` key, which gates a service out of the default run."""
    text = STOCK_COMPOSE.read_text()
    services: set[str] = set()
    current: str | None = None
    in_services = False
    for line in text.splitlines():
        if re.match(r"^services:\s*$", line):
            in_services = True
            continue
        if in_services and re.match(r"^\S", line):
            in_services = False
        if not in_services:
            continue
        m = re.match(r"^  ([A-Za-z0-9_.-]+):\s*$", line)
        if m:
            current = m.group(1)
            services.add(current)
            continue
        if current and re.match(r"^    profiles:", line):
            services.discard(current)
    return services


def test_stock_compose_scan_finds_the_backend() -> None:
    """Guard the scanner itself, so a parsing failure cannot neuter the gates."""
    services = _services_in_stock_compose()
    assert "backend" in services
    assert "n8n" not in services, "n8n is profile-gated; the scanner must exclude it"


def test_no_settings_default_dials_a_host() -> None:
    offenders: list[str] = []
    for name, field in Settings.model_fields.items():
        if name in _NOT_A_DIAL_TARGET:
            continue
        default = field.default
        if not isinstance(default, str):
            continue
        host = _dialable_host(default)
        if host is not None:
            offenders.append(f"{name}={default!r} (dials {host})")
    assert not offenders, (
        "Settings defaults that dial a host (ADR-004 corollary 3 - a default must "
        f"never point at a host a stock install does not run): {offenders}. "
        "Default it to empty and let the startup self-check state the consequence."
    )


def test_no_env_example_default_dials_a_host_that_is_not_shipped() -> None:
    stock_services = _services_in_stock_compose()
    offenders: list[str] = []
    for line in ENV_EXAMPLE.read_text().splitlines():
        m = _ENV_ASSIGN_RE.match(line)
        if not m:
            continue
        name, value = m.group(1), m.group(2)
        if name in _ENV_NOT_A_DIAL_TARGET:
            continue
        host = _dialable_host(value)
        if host is None:
            continue
        if host in stock_services:
            continue  # a container the default `docker compose up -d` actually starts
        offenders.append(f"{name}={value!r} (dials {host})")
    assert not offenders, (
        ".env.example ships values pointing at hosts a stock `docker compose up -d` "
        f"does not run: {offenders}. Leave them blank and document how to set them."
    )


def _env_example_assignments() -> dict[str, str]:
    """Live assignments only. A commented example line is documentation of how to
    set a variable, not a shipped value, so it is not an offender."""
    values: dict[str, str] = {}
    for line in ENV_EXAMPLE.read_text().splitlines():
        m = _ENV_ASSIGN_RE.match(line)
        if m:
            values[m.group(1)] = m.group(2)
    return values


def test_the_two_known_offenders_stay_fixed() -> None:
    """Direct guard on the specific regressions, in case the scan is loosened."""
    assert Settings.model_fields["embedding_service_url"].default == ""
    assert Settings.model_fields["embedding_fallback_url"].default == ""
    assignments = _env_example_assignments()
    for name in (
        "HP_EVENTS_WEBHOOK_URL",
        "HP_EMBEDDING_SERVICE_URL",
        "HP_EMBEDDING_FALLBACK_URL",
    ):
        assert name in assignments, f"{name} vanished from .env.example"
        assert assignments[name] == "", (
            f".env.example ships {name}={assignments[name]!r}; it must be blank so a stock "
            "install does not point at a host it does not run"
        )
