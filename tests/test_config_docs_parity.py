"""Config <-> docs parity gate (issue #394).

These assertions forbid the whole class of config/documentation drift that the
audit found: dead ``Settings`` fields, inert ``HP_*`` names in ``.env.example``
(names nothing reads), and a Docker image tag in the compose files that has
fallen behind the packaged version.

Teeth (proven by reverting the fix):
  * Reintroduce a removed dead field (e.g. ``auto_approve_nonmutating``) -> its
    ``HP_*`` env is neither documented nor read anywhere -> ``test_every_settable_field_is_documented`` fails.
  * Reintroduce an inert ``.env.example`` line (e.g. ``HP_DAEMON_HOST``) -> it
    resolves to no field/alias/reader -> ``test_env_example_has_no_inert_names`` fails.
  * Bump a compose ``HP_IMAGE_TAG`` default off the packaged version ->
    ``test_compose_image_tag_matches_version`` fails.
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

from homepilot.config import Settings

REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_EXAMPLE = REPO_ROOT / ".env.example"
PYPROJECT = REPO_ROOT / "pyproject.toml"
COMPOSE_FILES = (
    REPO_ROOT / "docker-compose.yml",
    REPO_ROOT / "deploy" / "control-plane" / "docker-compose.yml",
)
SRC = REPO_ROOT / "src"

# ``HP_*`` names consumed outside Settings and outside Python src: the compose
# files read these directly for the host-port mapping and image selection.
EXTERNAL_ENV = {"HP_IMAGE_TAG", "HP_DAEMON_PORT"}

# Secret / vault-managed fields intentionally NOT required as a plain value in
# .env.example (they live in the encrypted vault or are file-mounted). They may
# still appear as commented override hints.
SECRET_FIELDS = {
    "secret_key",
    "secret_key_file",
    "admin_secret",
    "vault_passphrase",
    "vault_passphrase_file",
    "events_webhook_secret",
    "n8n_api_key",
}

_ENV_ASSIGN_RE = re.compile(r"^\s*#?\s*(HP_[A-Z0-9_]+)\s*=")
_OS_ENVIRON_RE = re.compile(r"""os\.environ(?:\.get)?[(\[]\s*["'](HP_[A-Z0-9_]+)["']""")
_IMAGE_TAG_RE = re.compile(r"HP_IMAGE_TAG:-([^}\s]+)\}")


def _field_env_names(name: str) -> list[str]:
    """All env names that set ``name`` (honouring validation_alias)."""
    field = Settings.model_fields[name]
    alias = field.validation_alias
    if alias is None:
        return ["HP_" + name.upper()]
    if hasattr(alias, "choices"):  # AliasChoices
        return [str(c) for c in alias.choices]
    return [str(alias)]


def _all_field_envs() -> set[str]:
    envs: set[str] = set()
    for name in Settings.model_fields:
        envs.update(_field_env_names(name))
    return envs


def _env_example_names() -> set[str]:
    names: set[str] = set()
    for line in ENV_EXAMPLE.read_text().splitlines():
        m = _ENV_ASSIGN_RE.match(line)
        if m:
            names.add(m.group(1))
    return names


def _os_environ_reads() -> set[str]:
    reads: set[str] = set()
    for path in SRC.rglob("*.py"):
        reads.update(_OS_ENVIRON_RE.findall(path.read_text()))
    return reads


def _pyproject_version() -> str:
    data = tomllib.loads(PYPROJECT.read_text())
    return str(data["project"]["version"])


def test_env_example_has_no_inert_names() -> None:
    """Every HP_* name in .env.example must resolve to a real Settings field,
    an alias, an env read in src/, or a known compose var. No dead entries."""
    valid = _all_field_envs() | _os_environ_reads() | EXTERNAL_ENV
    documented = _env_example_names()
    inert = sorted(documented - valid)
    assert not inert, (
        f".env.example lists HP_* names that nothing reads (inert drift): {inert}. "
        "Remove them or wire them to a Settings field / env read."
    )


def test_every_settable_field_is_documented() -> None:
    """Every non-secret settable field must be documented in .env.example or be
    read directly from the environment. Forbids undocumented (often dead) fields."""
    env_names = _env_example_names()
    env_reads = _os_environ_reads()
    undocumented: list[str] = []
    for name in Settings.model_fields:
        if name in SECRET_FIELDS:
            continue
        candidates = set(_field_env_names(name))
        if candidates & env_names or candidates & env_reads:
            continue
        undocumented.append(name)
    assert not undocumented, (
        f"Settings fields with no .env.example entry and no env read: {undocumented}. "
        "Document them in .env.example, or remove them if they are dead."
    )


def test_compose_image_tag_matches_version() -> None:
    """Both compose files default HP_IMAGE_TAG to the packaged version."""
    version = _pyproject_version()
    for compose in COMPOSE_FILES:
        text = compose.read_text()
        tags = _IMAGE_TAG_RE.findall(text)
        assert tags, f"no HP_IMAGE_TAG default found in {compose}"
        for tag in tags:
            assert tag == version, (
                f"{compose} defaults HP_IMAGE_TAG to {tag!r}, but pyproject version is {version!r}"
            )


def test_removed_dead_fields_stay_removed() -> None:
    """Direct guard: the fields deleted in #394 must not creep back."""
    dead = {
        "rate_limit_backend",
        "agent_hub_heartbeat_interval",
        "auto_apply_on_approve",
        "auto_approve_nonmutating",
        "daemon_host",
    }
    present = dead & set(Settings.model_fields)
    assert not present, f"dead Settings fields reintroduced: {sorted(present)}"


# Guard the module's own machinery so a silent scan failure can't neuter teeth.
def test_parity_inputs_are_nonempty() -> None:
    assert ENV_EXAMPLE.exists() and _env_example_names()
    assert _all_field_envs()
    assert _os_environ_reads()
    assert sys.version_info >= (3, 11)
