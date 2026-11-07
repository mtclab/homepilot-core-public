"""Keep the developer's own HomePilot configuration out of the test run (#759).

`Settings` reads dotenv files from `~/.hp/.env`, `$HP_DATA_DIR/.env` and `./.env`
(config.py `_env_files`), and every `HP_*` environment variable. On a real
workstation `~/.hp/.env` holds a LIVE `HP_ADMIN_SECRET`, so a bare test run
loaded it into the suite - where any test that echoes settings could print it,
and where it silently changed what the tests were testing (several CLI tests
had grown `"HP_ADMIN_SECRET": ""` overrides to stop it authenticating them).

`isolate()` runs from tests/conftest.py BEFORE anything imports `homepilot`:

  * HOME becomes a fresh directory made for this run. It sits UNDER the real
    home on purpose: `create_app_state` refuses an artifacts dir in /tmp, and
    the agent journeys rely on /home being an agent read prefix - both already
    hold for every `tempfile.mkdtemp(dir=Path.home())` in the suite.
  * every `HP_*` variable is dropped except the harness's own knobs (KEEP).
  * toolchain locations that default to $HOME (Go, Playwright) are pinned to
    the real home first, so the sandbox costs no rebuilds or downloads.

`assert_no_foreign_env_files()` then fails the run if `Settings` would still
read any dotenv outside the sandbox - e.g. a deployment `.env` left in the
checkout's root, which the working directory candidate would pick up.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import MutableMapping
from dataclasses import dataclass, field
from pathlib import Path

SANDBOX_PREFIX = ".hp-test-home-"

# HP_* variables that configure the TEST HARNESS, not HomePilot: the Go
# toolchain and gate switches (#726) and the live-instance e2e suite's target.
KEEP = frozenset(
    {
        "HP_GO_BIN",
        "HP_GATE",
        "HP_GATE_ALLOW_NO_GO",
        "HP_TEST_URL",
        "HP_TEST_TOKEN",
        "HP_TEST_ADMIN_SECRET",
    }
)


@dataclass
class Sandbox:
    home: Path
    real_home: Path
    dropped: list[str] = field(default_factory=list)
    hp_env_after: frozenset[str] = frozenset()


def isolate(environ: MutableMapping[str, str] | None = None) -> Sandbox:
    env = os.environ if environ is None else environ
    real_home = Path(env.get("HOME") or os.path.expanduser("~"))

    # Toolchains that would otherwise follow HOME into an empty sandbox.
    env.setdefault("GOPATH", str(real_home / "go"))
    go_env = real_home / ".config" / "go" / "env"
    if go_env.exists():
        env.setdefault("GOENV", str(go_env))
    browsers = real_home / ".cache" / "ms-playwright"
    if browsers.exists():
        env.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(browsers))

    home = Path(tempfile.mkdtemp(dir=str(real_home), prefix=SANDBOX_PREFIX))
    env["HOME"] = str(home)

    dropped = sorted(k for k in env if k.startswith("HP_") and k not in KEEP)
    for key in dropped:
        del env[key]
    return Sandbox(
        home=home,
        real_home=real_home,
        dropped=dropped,
        hp_env_after=frozenset(k for k in env if k.startswith("HP_")),
    )


# The sandbox tests/conftest.py created for this run, for the gate tests to
# inspect (tests/test_hermetic_home.py).
ACTIVE: Sandbox | None = None


class ForeignConfigError(Exception):
    """Settings would read configuration from outside the test sandbox."""


def assert_no_foreign_env_files(sandbox: Sandbox) -> None:
    """Fail when `Settings` would load any dotenv outside the sandbox."""
    from homepilot.config import Settings

    files = Settings.model_config.get("env_file") or ()
    if isinstance(files, str | os.PathLike):
        files = (files,)
    foreign = [
        str(Path(f).resolve())
        for f in files
        if sandbox.home.resolve() not in Path(f).resolve().parents
    ]
    if foreign:
        raise ForeignConfigError(
            "The test run would load HomePilot settings from outside its sandbox "
            f"(#759): {foreign}. A test suite must not read a real deployment's "
            "configuration or secrets. Move that .env out of the working "
            "directory (deployment values do not belong in the checkout) and "
            "run the suite again."
        )


def remove(sandbox: Sandbox) -> None:
    """Delete exactly the sandbox this run created, and nothing else."""
    home = sandbox.home
    if home.name.startswith(SANDBOX_PREFIX) and home.parent == sandbox.real_home:
        shutil.rmtree(home, ignore_errors=True)
