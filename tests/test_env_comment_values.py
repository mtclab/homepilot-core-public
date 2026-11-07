"""An env-template comment must never become a setting (#773).

Docker Compose's `env_file` reads `KEY=   # note` as the VALUE `# note` - the
inline-comment rule only applies after a real value. `.env.example` shipped 33
empty keys written that way, and on a production install
`HP_VAULT_PASSPHRASE` was the template's comment text: the vault (Proxmox write
token, admin secret, hub token) was encrypted with a string anyone who has seen
the repo can read.

Two gates, one per half of the defect:

* the TEMPLATES: no shipped env template has `KEY=<whitespace>#`, commented-out
  assignments included (uncommenting one would plant the same value);
* the CONFIG: whatever an env file says, the backend refuses to start while an
  `HP_*` value starts with `#`, before anything is derived from it, and the
  refusal names the key without echoing the value.

Teeth: restore one `HP_VAULT_PASSPHRASE=            # Override: ...` line in
`.env.example` and the template gate fails; delete the
`self._refuse_comment_values()` call in `Settings.model_post_init` and every
refusal test here fails (Settings then loads the comment as the passphrase).
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from homepilot.config import ConfigError, Settings

_REPO_ROOT = Path(__file__).resolve().parent.parent

# Directories that hold no shipped file (dependencies, build output, VCS).
_SKIP_DIRS = frozenset(
    {".git", ".venv", "venv", "node_modules", "dist", "build", "__pycache__", ".worktrees"}
)

# The templates this repo ships today. Discovery below must find at least
# these, so a broken walk cannot pass the gate by finding nothing.
_KNOWN_TEMPLATES = frozenset(
    {".env.example", ".env.example.agent", "deploy/control-plane/.env.example"}
)

# `KEY=` followed by nothing but whitespace and then `#`, on an active line or
# a commented-out one (`# KEY=   # note`).
_COMMENT_AS_VALUE = re.compile(r"^\s*(?:#\s*)?[A-Za-z_][A-Za-z0-9_]*=[ \t]*#")

# What the old template actually shipped for the secret-bearing keys.
_SHIPPED_COMMENTS = {
    "HP_VAULT_PASSPHRASE": "# Override: an existing passphrase",
    "HP_MCP_TOKEN": "# Optional. Bearer token for MCP transport",
    "HP_AGENT_HUB_AUTH_TOKEN": "# Shared secret for agent auth",
    "HP_PORTAL_PROXY_SECRET": "# Shared secret the proxy sets in X-Hp-Portal-Secret",
    "HP_ENV": "# Set to `production` to refuse an auto-generated vault passphrase",
    "HP_TRUSTED_PROXIES": "# Comma-separated proxy IPs trusted for X-Forwarded-For",
}


def _is_env_template(name: str) -> bool:
    lowered = name.lower()
    if lowered.endswith((".env.example", ".env.sample", ".env.template")):
        return True
    return lowered.startswith(".env") and any(
        marker in lowered for marker in ("example", "sample", "template")
    )


def _shipped_env_templates() -> list[Path]:
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(_REPO_ROOT):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for filename in filenames:
            if _is_env_template(filename):
                found.append(Path(dirpath) / filename)
    return sorted(found)


def _settings(tmp_path: Path, **kwargs: object) -> Settings:
    data_dir = tmp_path / "hp"
    return Settings(
        data_dir=str(data_dir),
        artifacts_dir=str(data_dir / "artifacts"),
        **kwargs,  # type: ignore[arg-type]
    )


def _refusal(exc: BaseException) -> ConfigError:
    """The ConfigError behind a refusal, whether pydantic wrapped it or not."""
    if isinstance(exc, ConfigError):
        return exc
    assert isinstance(exc, ValidationError), f"unexpected {type(exc).__name__}: {exc}"
    causes = [err.get("ctx", {}).get("error") for err in exc.errors()]
    configs = [c for c in causes if isinstance(c, ConfigError)]
    assert configs, f"refused, but not by the config guard: {exc}"
    return configs[0]


# ── The templates ────────────────────────────────────────────────────────────


def test_discovery_finds_every_known_template() -> None:
    found = {p.relative_to(_REPO_ROOT).as_posix() for p in _shipped_env_templates()}
    missing = _KNOWN_TEMPLATES - found
    assert not missing, f"env-template discovery missed {sorted(missing)}"


def test_no_shipped_env_template_has_a_comment_after_an_empty_value() -> None:
    bad: list[str] = []
    for template in _shipped_env_templates():
        rel = template.relative_to(_REPO_ROOT).as_posix()
        for number, line in enumerate(template.read_text(encoding="utf-8").splitlines(), 1):
            if _COMMENT_AS_VALUE.match(line):
                bad.append(f"{rel}:{number}: {line.strip()}")
    assert not bad, (
        "Docker Compose reads `KEY=   # note` as the value `# note` (#773). Put "
        "the comment on the line above and leave the value empty:\n" + "\n".join(bad)
    )


def test_the_gate_pattern_matches_the_shape_that_shipped() -> None:
    """The regex has to catch exactly what reached production, and not a
    comment that follows a real value (Compose strips that one)."""
    assert _COMMENT_AS_VALUE.match("HP_VAULT_PASSPHRASE=            # Override: x")
    assert _COMMENT_AS_VALUE.match("# HP_VAULT_PASSPHRASE_FILE=    # Docker secrets")
    assert _COMMENT_AS_VALUE.match("N8N_ENCRYPTION_KEY=#x")
    assert not _COMMENT_AS_VALUE.match("HP_LOG_LEVEL=info               # debug | info")
    assert not _COMMENT_AS_VALUE.match("HP_VAULT_PASSPHRASE=")
    assert not _COMMENT_AS_VALUE.match("# Override: an existing passphrase")


# ── The config guard ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("key", sorted(_SHIPPED_COMMENTS))
def test_a_comment_valued_hp_variable_is_refused_by_name(tmp_path: Path, key: str) -> None:
    value = _SHIPPED_COMMENTS[key]
    with patch.dict(os.environ, {key: value}), pytest.raises(Exception) as caught:
        _settings(tmp_path)
    refusal = _refusal(caught.value)
    assert key in str(refusal)
    # Never the value: for the passphrase it IS the secret.
    assert value not in str(caught.value)
    assert value.lstrip("# ") not in str(caught.value)


def test_refused_before_anything_is_derived(tmp_path: Path) -> None:
    """HP_VAULT_AUTO_INIT is read straight from os.environ, not through a
    field. A comment there used to mean "auto-init on"; it must refuse before
    a passphrase is generated and written."""
    with (
        patch.dict(
            os.environ,
            {"HP_VAULT_AUTO_INIT": "# Set 0 to refuse to generate one (vault stays disabled)"},
        ),
        pytest.raises(Exception) as caught,
    ):
        _settings(tmp_path, vault_passphrase="")
    assert "HP_VAULT_AUTO_INIT" in str(_refusal(caught.value))
    assert not (tmp_path / "hp" / ".vault_passphrase").exists()


def test_a_comment_valued_field_from_a_dotenv_file_is_refused(tmp_path: Path) -> None:
    env_file = tmp_path / "planted.env"
    env_file.write_text('HP_AGENT_HUB_AUTH_TOKEN="# Shared secret for agent auth"\n')
    with pytest.raises(Exception) as caught:
        _settings(tmp_path, _env_file=str(env_file))
    assert "HP_AGENT_HUB_AUTH_TOKEN" in str(_refusal(caught.value))
    assert "Shared secret for agent auth" not in str(caught.value)


def test_a_comment_valued_field_passed_directly_is_refused(tmp_path: Path) -> None:
    with pytest.raises(Exception) as caught:
        _settings(tmp_path, portal_proxy_secret="   # leading whitespace too")
    assert "HP_PORTAL_PROXY_SECRET" in str(_refusal(caught.value))


def test_a_hash_inside_a_value_is_not_a_comment(tmp_path: Path) -> None:
    with patch.dict(os.environ, {"HP_PORTAL_PROXY_SECRET": "abc#def"}):
        settings = _settings(tmp_path)
    assert settings.portal_proxy_secret == "abc#def"


def test_other_services_variables_are_named_not_refused(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """N8N_/SEARXNG_ keys belong to other containers: the backend cannot
    refuse them, but it names them (names only) because they are the same
    defect - a secret that is public text."""
    with (
        patch.dict(os.environ, {"N8N_ENCRYPTION_KEY": "# openssl rand -hex 32"}),
        caplog.at_level(logging.WARNING, logger="homepilot.config"),
    ):
        _settings(tmp_path)
    warned = [r.getMessage() for r in caplog.records if "N8N_ENCRYPTION_KEY" in r.getMessage()]
    assert warned
    assert not any("openssl" in message for message in warned)
