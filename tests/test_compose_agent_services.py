"""Static gates on the shipped compose files' agent services.

These read the compose YAML the operator runs; nothing here starts a container.

#775 - n8n. The service set `N8N_BASIC_AUTH_*`, which n8n removed in 1.0 and the
pinned 2.35.x ignores, so a production instance ran with no owner account: the
first visitor to its UI could claim it, and every credential stored in n8n with
it. The owner is now managed from the environment
(`N8N_INSTANCE_OWNER_MANAGED_BY_ENV`, verified in n8n@2.35.3: the owner is set
from `N8N_INSTANCE_OWNER_EMAIL`/`_PASSWORD_HASH` at every start, and start-up
fails while either is empty), and the UI is never published beyond loopback.

Teeth: put `N8N_BASIC_AUTH_ACTIVE` back, drop the managed-owner variable, give
the owner a default, or publish `5678:5678` on n8n and a test here fails.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.tree import shipped

_REPO_ROOT = Path(__file__).resolve().parent.parent
_COMPOSE_FILES = shipped(
    "docker-compose.yml",
    "docker-compose.agent.yml",
    "deploy/control-plane/docker-compose.yml",
)
_MAIN_COMPOSE = _REPO_ROOT / "docker-compose.yml"
_ENV_TEMPLATES = shipped(".env.example", ".env.example.agent")
_LOOPBACK = frozenset({"127.0.0.1", "::1", "[::1]", "localhost"})


def _compose(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"{path} is not a compose mapping"
    return data


def _service(name: str, path: Path = _MAIN_COMPOSE) -> dict[str, Any]:
    services = _compose(path).get("services") or {}
    assert name in services, f"{path.name} has no `{name}` service"
    service = services[name]
    assert isinstance(service, dict)
    return service


def _environment(service: dict[str, Any]) -> dict[str, str]:
    env = service.get("environment") or {}
    if isinstance(env, list):
        pairs = (item.split("=", 1) for item in env)
        return {p[0]: (p[1] if len(p) > 1 else "") for p in pairs}
    return {str(k): "" if v is None else str(v) for k, v in env.items()}


def _published_host_ips(service: dict[str, Any]) -> list[str]:
    """The host address each published port binds; "" means all interfaces."""
    ips: list[str] = []
    for port in service.get("ports") or []:
        if isinstance(port, dict):
            ips.append(str(port.get("host_ip") or ""))
            continue
        spec = str(port).split("/", 1)[0]
        if spec.startswith("["):  # [::1]:5678:5678
            ips.append(spec[: spec.index("]") + 1])
            continue
        parts = spec.split(":")
        ips.append(parts[0] if len(parts) == 3 else "")
    return ips


def _template_keys(path: Path) -> set[str]:
    keys: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=", line.strip())
        if match:
            keys.add(match.group(1))
    return keys


# ── #775: n8n is never up without an owner ───────────────────────────────────


def test_n8n_carries_no_dead_basic_auth() -> None:
    """Variables n8n ignores read as protection that does not exist."""
    dead = [k for k in _environment(_service("n8n")) if k.startswith("N8N_BASIC_AUTH")]
    assert not dead, f"n8n ignores {dead} since 1.0; they protect nothing"


@pytest.mark.parametrize("template", _ENV_TEMPLATES, ids=lambda p: p.name)
def test_env_templates_carry_no_dead_basic_auth(template: Path) -> None:
    dead = sorted(k for k in _template_keys(template) if k.startswith("N8N_BASIC_AUTH"))
    assert not dead, f"{template.name} still offers {dead}, which n8n ignores"


def test_n8n_owner_is_managed_from_the_environment() -> None:
    env = _environment(_service("n8n"))
    assert env.get("N8N_INSTANCE_OWNER_MANAGED_BY_ENV") == "true", (
        "without N8N_INSTANCE_OWNER_MANAGED_BY_ENV=true a fresh n8n has no owner "
        "and the first visitor claims it"
    )


@pytest.mark.parametrize("key", ["N8N_INSTANCE_OWNER_EMAIL", "N8N_INSTANCE_OWNER_PASSWORD_HASH"])
def test_n8n_owner_comes_from_env_with_no_default(key: str) -> None:
    """A default owner would be a credential published in this repo. Empty
    must stay empty, so n8n refuses to start rather than run unowned."""
    value = _environment(_service("n8n")).get(key)
    assert value is not None, f"n8n does not receive {key}"
    assert re.fullmatch(rf"\$\{{{key}(:?-)?\}}", value), (
        f"{key} must be passed through from .env with no default, got {value!r}"
    )


@pytest.mark.parametrize("template", _ENV_TEMPLATES, ids=lambda p: p.name)
def test_env_templates_offer_the_owner_variables(template: Path) -> None:
    missing = {"N8N_INSTANCE_OWNER_EMAIL", "N8N_INSTANCE_OWNER_PASSWORD_HASH"} - _template_keys(
        template
    )
    assert not missing, f"{template.name} does not offer {sorted(missing)}"


@pytest.mark.parametrize("path", _COMPOSE_FILES, ids=lambda p: p.relative_to(_REPO_ROOT).as_posix())
def test_n8n_is_never_published_beyond_loopback(path: Path) -> None:
    services = _compose(path).get("services") or {}
    if "n8n" not in services:
        return
    exposed = [ip for ip in _published_host_ips(services["n8n"]) if ip not in _LOOPBACK]
    assert not exposed, (
        f"{path.name} publishes n8n on {exposed or 'all interfaces'}; bind it to "
        "127.0.0.1 and reach it over an SSH tunnel"
    )


def test_the_port_parser_sees_an_all_interfaces_binding() -> None:
    """The loopback gate is only as good as its parser."""
    assert _published_host_ips({"ports": ["5678:5678"]}) == [""]
    assert _published_host_ips({"ports": ["0.0.0.0:5678:5678"]}) == ["0.0.0.0"]
    assert _published_host_ips({"ports": ["127.0.0.1:5678:5678"]}) == ["127.0.0.1"]
    assert _published_host_ips({"ports": ["[::1]:5678:5678/tcp"]}) == ["[::1]"]
    assert _published_host_ips({"ports": [{"target": 5678, "published": 5678}]}) == [""]


# ── #776: piper starts ───────────────────────────────────────────────────────
#
# rhasspy/wyoming-piper 2.4.2 exits with "--voice is required for the piper
# backend" unless --voice is on its command line, and reads no PIPER_*
# environment (verified in the v2.4.2 __main__.py: argparse only). The compose
# file set the voice as environment, so piper crash-looped on prod. Its
# entrypoint passes `--data-dir /data`, and port 10200 speaks Wyoming (JSON
# lines over TCP), not HTTP - the old urllib healthcheck could never pass.
#
# Teeth: put the voice back into `environment:` (or drop it from `command:`),
# mount voices anywhere but /data, or restore an HTTP healthcheck on 10200 and
# a test here fails.


def _piper_command() -> list[str]:
    command = _service("piper").get("command")
    assert isinstance(command, list), (
        f"piper needs its settings as command-line arguments (exec-form list); got {command!r}"
    )
    return [str(arg) for arg in command]


def test_piper_is_given_a_voice_on_its_command_line() -> None:
    command = _piper_command()
    assert "--voice" in command, "wyoming-piper 2.4.2 refuses to start without --voice"
    following = command[command.index("--voice") + 1 :]
    voice = following[0] if following else ""
    assert voice and not voice.startswith("-"), "--voice has no value"
    interpolated = re.fullmatch(r"\$\{(\w+)(:?-)(.*)\}", voice)
    if interpolated:
        assert interpolated.group(3), (
            f"--voice {voice} is empty whenever {interpolated.group(1)} is unset"
        )


def test_piper_voice_stays_configurable_from_env() -> None:
    command = _piper_command()
    assert command[command.index("--voice") + 1].startswith("${PIPER_VOICE"), (
        "the voice should come from PIPER_VOICE in .env, with a default"
    )


def test_piper_carries_no_ignored_environment() -> None:
    dead = [k for k in _environment(_service("piper")) if k.startswith("PIPER_")]
    assert not dead, f"wyoming-piper reads no environment; {dead} configure nothing"


def test_piper_voices_persist_in_its_data_dir() -> None:
    targets = [
        str(v).split(":")[1] for v in _service("piper").get("volumes") or [] if ":" in str(v)
    ]
    assert "/data" in targets, (
        f"the image downloads voices into --data-dir /data; mounted {targets} instead"
    )


def test_piper_healthcheck_does_not_speak_http_to_wyoming() -> None:
    test = _service("piper").get("healthcheck", {}).get("test")
    text = " ".join(test) if isinstance(test, list) else str(test)
    assert "10200" in text, "piper's healthcheck does not probe its Wyoming port"
    assert "urllib" not in text and "http://" not in text, (
        "port 10200 speaks the Wyoming protocol, not HTTP; an HTTP probe never passes"
    )
    assert "describe" in text
