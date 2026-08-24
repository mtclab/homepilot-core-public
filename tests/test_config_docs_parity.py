"""Config <-> docs parity gate (issue #394).

These assertions forbid the whole class of config/documentation drift that the
audit found: dead ``Settings`` fields, inert ``HP_*`` names in ``.env.example``
or in the ``docs/deployment.md`` environment table (names nothing reads), fields
that exist but are documented nowhere, and image tags that fall behind the
packaged version.

The rule the gate encodes:

* every ``Settings`` field must be reachable from ``.env.example`` (at least one
  of its env names, aliases included) AND every one of its env names must appear
  in the ``docs/deployment.md`` environment table - that table is the reference,
  so an alias missing from it is undocumented;
* every ``HP_*`` name in ``.env.example`` or in that table must resolve back to a
  live field, to an ``os.environ`` read in ``src/``, or to ``EXTERNAL_ENV`` - the
  small allowlist of names consumed outside Python. ``EXTERNAL_ENV`` is itself
  asserted non-stale: each entry must still be found in the file that consumes
  it, so removing that consumer fails the gate rather than leaving a dead name
  documented forever;
* both compose files, ``.env.example`` and the table's ``HP_IMAGE_TAG`` row all
  default to the packaged version.

Teeth (proven by planting the defect):
  * Plant a dead ``HP_NOPE=1`` line in ``.env.example`` ->
    ``test_env_example_has_no_inert_names`` fails.
  * Delete a real field's ``.env.example`` line (e.g. ``HP_LOG_LEVEL``) ->
    ``test_every_field_is_documented`` fails.
  * Delete a field's ``docs/deployment.md`` row -> same test fails.
  * Plant a dead ``HP_NOPE`` row in the deployment table ->
    ``test_deployment_table_has_no_inert_names`` fails.
  * Bump a compose / ``.env.example`` / table ``HP_IMAGE_TAG`` off the packaged
    version -> ``test_image_tag_matches_version`` fails.
  * Point an ``EXTERNAL_ENV`` entry at a file that does not read it ->
    ``test_external_env_allowlist_is_not_stale`` fails.
  * Reintroduce a removed dead field -> ``test_removed_dead_fields_stay_removed``
    fails, and the field is undocumented so the documentation test fails too.
  * Reintroduce a "JWT signing" claim in an operator-facing file ->
    ``test_no_jwt_claims_in_operator_docs`` fails.
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

from homepilot.config import Settings

REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_EXAMPLE = REPO_ROOT / ".env.example"
DEPLOYMENT_DOC = REPO_ROOT / "docs" / "deployment.md"
PYPROJECT = REPO_ROOT / "pyproject.toml"
COMPOSE_FILES = (
    REPO_ROOT / "docker-compose.yml",
    REPO_ROOT / "deploy" / "control-plane" / "docker-compose.yml",
)
SRC = REPO_ROOT / "src"

# ``HP_*`` names consumed OUTSIDE the Python source: compose reads two of them
# for image selection and the host-port mapping, and the Go agent reads its own
# test-only TLS escape hatch. Each maps to the file that must still consume it -
# `test_external_env_allowlist_is_not_stale` re-checks that every session, so a
# removed consumer fails the gate instead of leaving a dead documented name.
EXTERNAL_ENV: dict[str, tuple[Path, ...]] = {
    "HP_IMAGE_TAG": COMPOSE_FILES,
    "HP_DAEMON_PORT": COMPOSE_FILES,
    "HP_AGENT_TLS_INSECURE": (REPO_ROOT / "agent" / "go" / "config.go",),
}

_ENV_ASSIGN_RE = re.compile(r"^\s*#?\s*(HP_[A-Z0-9_]+)\s*=")
_OS_ENVIRON_RE = re.compile(r"""os\.environ(?:\.get)?[(\[]\s*["'](HP_[A-Z0-9_]+)["']""")
_IMAGE_TAG_RE = re.compile(r"HP_IMAGE_TAG:-([^}\s]+)\}")
_TABLE_ROW_RE = re.compile(r"^\|\s*(.+?)\s*\|\s*(.*?)\s*\|")


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


def _deployment_table_rows() -> list[tuple[str, str]]:
    """(first cell, second cell) of every row under `## Environment Variables`."""
    lines = DEPLOYMENT_DOC.read_text().splitlines()
    start = lines.index("## Environment Variables")
    rows: list[tuple[str, str]] = []
    for line in lines[start + 1 :]:
        if line.startswith("## "):
            break
        m = _TABLE_ROW_RE.match(line)
        if m and not set(m.group(1)) <= set("-: "):
            rows.append((m.group(1), m.group(2)))
    return rows


def _deployment_table_names() -> set[str]:
    names: set[str] = set()
    for first, _ in _deployment_table_rows():
        names.update(re.findall(r"HP_[A-Z0-9_]+", first))
    return names


def _os_environ_reads() -> set[str]:
    reads: set[str] = set()
    for path in SRC.rglob("*.py"):
        reads.update(_OS_ENVIRON_RE.findall(path.read_text()))
    return reads


def _pyproject_version() -> str:
    data = tomllib.loads(PYPROJECT.read_text())
    return str(data["project"]["version"])


def _known_names() -> set[str]:
    return _all_field_envs() | _os_environ_reads() | set(EXTERNAL_ENV)


def test_env_example_has_no_inert_names() -> None:
    """Every HP_* name in .env.example must resolve to a real Settings field,
    an alias, an env read in src/, or an allowlisted external consumer."""
    inert = sorted(_env_example_names() - _known_names())
    assert not inert, (
        f".env.example lists HP_* names that nothing reads (inert drift): {inert}. "
        "Remove them or wire them to a Settings field / env read."
    )


def test_deployment_table_has_no_inert_names() -> None:
    """Same rule for the docs/deployment.md environment table."""
    inert = sorted(_deployment_table_names() - _known_names())
    assert not inert, (
        f"docs/deployment.md documents HP_* names that nothing reads: {inert}. "
        "Remove the rows or wire the names to a Settings field / env read."
    )


def test_every_field_is_documented() -> None:
    """Every field must be reachable from .env.example, and EVERY env name it
    answers to must appear in the deployment.md table. Forbids both undocumented
    (often dead) fields and undocumented aliases."""
    env_names = _env_example_names()
    table_names = _deployment_table_names()
    missing_from_env_example: list[str] = []
    missing_from_table: list[str] = []
    for name in Settings.model_fields:
        candidates = set(_field_env_names(name))
        if not candidates & env_names:
            missing_from_env_example.append(f"{name} ({'/'.join(sorted(candidates))})")
        for env in sorted(candidates - table_names):
            missing_from_table.append(f"{name} -> {env}")
    assert not missing_from_env_example, (
        f"Settings fields absent from .env.example: {missing_from_env_example}. "
        "Document them there, or remove them if they are dead."
    )
    assert not missing_from_table, (
        "Env names absent from the docs/deployment.md environment table: "
        f"{missing_from_table}. Add a row per name (aliases included)."
    )


def test_external_env_allowlist_is_not_stale() -> None:
    """Every allowlisted external name must still be read by the file that
    justifies it - a removed consumer must fail here, not linger documented."""
    stale: list[str] = []
    for name, consumers in EXTERNAL_ENV.items():
        if not any(c.exists() and name in c.read_text() for c in consumers):
            stale.append(name)
    assert not stale, (
        f"EXTERNAL_ENV allowlists names no listed consumer reads any more: {stale}. "
        "Drop them from the allowlist and from the docs."
    )


def test_image_tag_matches_version() -> None:
    """Both compose files, .env.example and the docs table default the image tag
    to the packaged version."""
    version = _pyproject_version()
    for compose in COMPOSE_FILES:
        tags = _IMAGE_TAG_RE.findall(compose.read_text())
        assert tags, f"no HP_IMAGE_TAG default found in {compose}"
        for tag in tags:
            assert tag == version, (
                f"{compose} defaults HP_IMAGE_TAG to {tag!r}, but pyproject version is {version!r}"
            )

    env_tags = [
        line.split("=", 1)[1].split("#")[0].strip()
        for line in ENV_EXAMPLE.read_text().splitlines()
        if line.startswith("HP_IMAGE_TAG=")
    ]
    assert env_tags == [version], (
        f".env.example sets HP_IMAGE_TAG to {env_tags}, but pyproject version is {version!r}"
    )

    doc_tags = [second for first, second in _deployment_table_rows() if "HP_IMAGE_TAG" in first]
    assert doc_tags, "docs/deployment.md has no HP_IMAGE_TAG row"
    for tag in doc_tags:
        assert tag.strip("`") == version, (
            f"docs/deployment.md documents HP_IMAGE_TAG default {tag!r}, "
            f"but pyproject version is {version!r}"
        )


def test_removed_dead_fields_stay_removed() -> None:
    """Direct guard: the fields deleted in #394 must not creep back.

    ``secret_key``/``secret_key_file`` were a security control that did not
    exist - a vault->file->auto-generate chain plus a production fail-closed
    gate for a value NOTHING in src/ ever read (the docs claimed JWT signing;
    there is no JWT). ``ssh_key_dir`` lost its last reader with the jumpserver
    removal (#327); ``<data_dir>/ssh`` stays as a fixed, operator-managed
    directory, not a configurable one.
    """
    dead = {
        "rate_limit_backend",
        "agent_hub_heartbeat_interval",
        "auto_apply_on_approve",
        "auto_approve_nonmutating",
        "daemon_host",
        "secret_key",
        "secret_key_file",
        "ssh_key_dir",
    }
    present = dead & set(Settings.model_fields)
    assert not present, f"dead Settings fields reintroduced: {sorted(present)}"


def test_no_jwt_claims_in_operator_docs() -> None:
    """HomePilot issues opaque API tokens (token_hex + SHA-256 + compare_digest).
    Nothing signs a JWT, so no operator-facing file may claim one is signed."""
    offenders: list[str] = []
    for rel in (".env.example", "README.md", "docs/deployment.md", "docs/vault.md"):
        if re.search(r"\bJWT\b", (REPO_ROOT / rel).read_text()):
            offenders.append(rel)
    for path in SRC.rglob("*.py"):
        if re.search(r"\bJWT\b", path.read_text()):
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert not offenders, (
        f"JWT signing is claimed in {offenders}, but HomePilot signs no JWT. "
        "Describe the real token scheme instead."
    )


# Guard the module's own machinery so a silent scan failure can't neuter teeth.
def test_parity_inputs_are_nonempty() -> None:
    assert ENV_EXAMPLE.exists() and _env_example_names()
    assert len(_deployment_table_names()) > 20
    assert _all_field_envs()
    assert _os_environ_reads()
    assert sys.version_info >= (3, 11)
