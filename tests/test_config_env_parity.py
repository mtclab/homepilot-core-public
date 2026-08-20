"""Parity gate between Settings, .env.example, and docker-compose.yml (#394).

The repo's ``Settings`` class, ``.env.example``, and
``deploy/control-plane/docker-compose.yml`` have drifted apart. This test is a
standing gate that fails whenever they drift again.

There are THREE classes of ``HP_*`` variable in this repo, and all three are
legitimate:

(A) pydantic ``Settings`` fields - resolved by reflection over
    ``Settings.model_fields`` honouring ``validation_alias``.
(B) DIRECT ``os.environ`` reads in ``src/`` that never go through ``Settings``.
    Real examples verified in this repo: ``HP_MCP_TOKEN`` (src/homepilot/main.py:268),
    ``HP_RATE_LIMIT`` (main.py:41), ``HP_ADMIN_SECRET``, ``HP_TRUSTED_PROXIES``,
    ``HP_DATA_DIR``, ``HP_VAULT_PASSPHRASE``, and ~12 more.
(C) DEPLOYMENT-only variables consumed by docker-compose, never by Python.
    Verified examples: ``HP_IMAGE_TAG`` (compose image tag) and ``HP_DAEMON_PORT``
    (the host port mapped to the container's fixed :8000 - ``.env.example``
    documents this explicitly).

How env names resolve for class (A):
``Settings`` is a pydantic-settings ``BaseSettings`` with
``model_config = {"env_prefix": "HP_", ...}``.
- Default: field ``foo_bar`` -> env var ``HP_FOO_BAR`` (prefix + uppercased name).
- If a field sets ``validation_alias="HP_PORT"`` (a plain string), the REAL env var
  is exactly ``HP_PORT``. The prefix is NOT applied again.
- If a field sets ``validation_alias=AliasChoices("HP_A", "HP_B")``, then ALL of
  those names are real accepted env vars.

Assertions:
1. Every resolved env name from class (A) appears in ``.env.example`` (as a
   ``NAME=`` line, possibly commented out with a leading ``#``). Exception: if a
   field's ``AliasChoices`` lists several names, the test passes when AT LEAST ONE
   of that field's names is documented - documenting every alias of the same field
   is not required.
2. Every ``HP_*`` name assigned in ``.env.example`` must be in class (A) UNION
   class (B) UNION class (C). Anything else is genuinely inert.
3. The default value of ``HP_IMAGE_TAG`` in
   ``deploy/control-plane/docker-compose.yml`` (written as
   ``${HP_IMAGE_TAG:-<tag>}``) equals the ``version`` in ``pyproject.toml``.
   If more than one compose file under ``deploy/`` sets an image tag, assert all
   of them match.

Teeth: add a new field to ``Settings`` without documenting it in ``.env.example``,
add an inert ``HP_*`` entry to ``.env.example`` that doesn't map to any real
Settings field, direct os.environ read, or deployment-only variable, or bump the
version in ``pyproject.toml`` without updating the default image tag in
``docker-compose.yml``, and this test fails. Additionally, if an environment
variable required by Settings or read directly via os.environ is not documented
in ``docs/deployment.md``, this test fails.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from pydantic import AliasChoices

from homepilot.config import Settings

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ENV_EXAMPLE = _REPO_ROOT / ".env.example"
_PYPROJECT = _REPO_ROOT / "pyproject.toml"
_DEPLOY_DIR = _REPO_ROOT / "deploy"
_SRC_DIR = _REPO_ROOT / "src"
_DEPLOYMENT_MD = _REPO_ROOT / "docs" / "deployment.md"

# DEPLOYMENT-only variables consumed by docker-compose and never read by Python.
# Any addition here must be justified in a comment.
_DEPLOYMENT_ONLY: frozenset[str] = frozenset(
    {
        "HP_IMAGE_TAG",  # compose image tag
        "HP_DAEMON_PORT",  # host port mapped to container's fixed :8000
    }
)

# Variables exempt from the docs/deployment.md documentation requirement.
# Anything added here needs a written justification.
_DEPLOYMENT_DOC_EXEMPT: frozenset[str] = frozenset()


def _settings_env_names() -> set[str]:
    """Resolve all real env var names accepted by Settings via reflection."""
    env_names: set[str] = set()
    for field_name, field_info in Settings.model_fields.items():
        alias = field_info.validation_alias
        if alias is None:
            # Default: prefix + uppercased field name
            env_names.add(f"HP_{field_name.upper()}")
        elif isinstance(alias, str):
            # Plain string alias: the real env var is exactly this string
            env_names.add(alias)
        elif isinstance(alias, AliasChoices):
            # AliasChoices: all choices are real accepted env vars
            for choice in alias.choices:
                if isinstance(choice, str):
                    env_names.add(choice)
    return env_names


def _direct_env_names() -> set[str]:
    """Scan every *.py under src/ and collect HP_* names from os.environ access."""
    if not _SRC_DIR.exists():
        raise AssertionError(f"src/ directory not found at {_SRC_DIR}")

    # Match os.environ.get("HP_X"...) or os.environ["HP_X"], single or double quotes
    pattern = re.compile(r"os\.environ(?:\.get\(|\[)['\"](HP_\w+)['\"]")
    names: set[str] = set()
    for py_file in _SRC_DIR.rglob("*.py"):
        content = py_file.read_text(encoding="utf-8")
        for match in pattern.finditer(content):
            names.add(match.group(1))
    return names


def _parse_env_example() -> set[str]:
    """Parse .env.example and return all HP_* names that are assigned (NAME=...)."""
    if not _ENV_EXAMPLE.exists():
        raise AssertionError(f".env.example not found at {_ENV_EXAMPLE}")

    names: set[str] = set()
    for line in _ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        # Strip leading whitespace and optional comment marker
        stripped = line.strip()
        if stripped.startswith("#"):
            stripped = stripped.lstrip("#").strip()

        # Match NAME=... pattern
        m = re.match(r"^(HP_\w+)=.*", stripped)
        if m:
            names.add(m.group(1))
    return names


def _get_project_version() -> str:
    """Read version from pyproject.toml."""
    if not _PYPROJECT.exists():
        raise AssertionError(f"pyproject.toml not found at {_PYPROJECT}")

    with open(_PYPROJECT, "rb") as f:
        data = tomllib.load(f)

    version = data.get("project", {}).get("version")
    if not version:
        raise AssertionError("version not found in pyproject.toml [project] section")
    return version


def _find_compose_files() -> list[Path]:
    """Find all docker-compose.yml files under deploy/."""
    if not _DEPLOY_DIR.exists():
        raise AssertionError(f"deploy/ directory not found at {_DEPLOY_DIR}")

    return sorted(_DEPLOY_DIR.rglob("docker-compose.yml"))


def _extract_image_tag_default(compose_path: Path) -> str | None:
    """Extract the default value of HP_IMAGE_TAG from a compose file."""
    content = compose_path.read_text(encoding="utf-8")
    # Match ${HP_IMAGE_TAG:-<tag>} pattern
    m = re.search(r"\$\{HP_IMAGE_TAG:-([^}]+)\}", content)
    if m:
        return m.group(1)
    return None


def test_every_settings_field_is_documented_in_env_example() -> None:
    """Every resolved env name from Settings appears in .env.example."""
    if not _ENV_EXAMPLE.exists():
        raise AssertionError(f".env.example not found at {_ENV_EXAMPLE}")

    documented_names = _parse_env_example()
    missing: list[str] = []

    for field_name, field_info in Settings.model_fields.items():
        alias = field_info.validation_alias
        if alias is None:
            # Default: prefix + uppercased field name
            env_name = f"HP_{field_name.upper()}"
            if env_name not in documented_names:
                missing.append(env_name)
        elif isinstance(alias, str):
            # Plain string alias: the real env var is exactly this string
            if alias not in documented_names:
                missing.append(alias)
        elif isinstance(alias, AliasChoices):
            # AliasChoices: at least one choice must be documented
            choices = [c for c in alias.choices if isinstance(c, str)]
            if not any(c in documented_names for c in choices):
                missing.extend(choices)

    assert not missing, f"env vars in Settings but absent from .env.example: {sorted(set(missing))}"


def test_no_inert_entries_in_env_example() -> None:
    """Every HP_* name assigned in .env.example must be in Settings, direct os.environ, or deployment-only."""
    if not _ENV_EXAMPLE.exists():
        raise AssertionError(f".env.example not found at {_ENV_EXAMPLE}")

    settings_names = _settings_env_names()
    direct_names = _direct_env_names()
    documented_names = _parse_env_example()

    valid_names = settings_names | direct_names | _DEPLOYMENT_ONLY
    inert = documented_names - valid_names
    assert not inert, (
        f"HP_* entries in .env.example that don't map to any Settings field, "
        f"direct os.environ read, or deployment-only variable: {sorted(inert)}"
    )


def test_compose_image_tag_matches_project_version() -> None:
    """The default value of HP_IMAGE_TAG in compose files matches pyproject.toml version."""
    if not _PYPROJECT.exists():
        raise AssertionError(f"pyproject.toml not found at {_PYPROJECT}")

    project_version = _get_project_version()
    compose_files = _find_compose_files()

    if not compose_files:
        raise AssertionError(f"No docker-compose.yml files found under {_DEPLOY_DIR}")

    mismatches: list[tuple[str, str]] = []
    for compose_path in compose_files:
        tag = _extract_image_tag_default(compose_path)
        if tag is not None and tag != project_version:
            mismatches.append((str(compose_path.relative_to(_REPO_ROOT)), tag))

    assert not mismatches, (
        f"compose image tag defaults don't match pyproject.toml version {project_version}: "
        f"{mismatches}"
    )


def test_env_vars_documented_in_deployment_md() -> None:
    """Every env var from Settings and direct os.environ reads is documented in docs/deployment.md."""
    if not _DEPLOYMENT_MD.exists():
        raise AssertionError(f"docs/deployment.md not found at {_DEPLOYMENT_MD}")

    content = _DEPLOYMENT_MD.read_text(encoding="utf-8")
    required_names = _settings_env_names() | _direct_env_names()
    missing = {name for name in required_names if name not in content} - _DEPLOYMENT_DOC_EXEMPT
    assert not missing, f"env vars undocumented in docs/deployment.md: {sorted(missing)}"
