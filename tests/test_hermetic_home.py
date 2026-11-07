"""The suite never reads the developer's own HomePilot configuration (#759).

A workstation's `~/.hp/.env` carries a LIVE `HP_ADMIN_SECRET`. Test runs loaded
it: `Settings` reads that file by default, so the live secret sat in the suite's
settings where any test echoing them could print it.

Teeth: make `hermetic.isolate()` a no-op (or drop its call from conftest) and
`test_home_is_a_per_run_sandbox` fails on every machine, and
`test_a_planted_home_config_and_env_never_reach_settings` fails because the
planted canary secret comes back out of `Settings()`.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

from homepilot.config import Settings

from . import hermetic

REPO_ROOT = Path(__file__).resolve().parents[1]
CANARY = "canary-admin-secret-from-a-real-home"  # pragma: allowlist secret - test canary


def test_home_is_a_per_run_sandbox() -> None:
    sandbox = hermetic.ACTIVE
    assert sandbox is not None, "conftest did not isolate HOME"
    home = Path.home().resolve()
    assert home == sandbox.home.resolve()
    assert home.name.startswith(hermetic.SANDBOX_PREFIX)
    assert home != sandbox.real_home.resolve()


def test_no_homepilot_variable_survived_isolation() -> None:
    sandbox = hermetic.ACTIVE
    assert sandbox is not None
    assert sandbox.hp_env_after <= hermetic.KEEP, sorted(sandbox.hp_env_after - hermetic.KEEP)


def test_settings_reads_no_dotenv_outside_the_sandbox() -> None:
    sandbox = hermetic.ACTIVE
    assert sandbox is not None
    files = Settings.model_config.get("env_file") or ()
    for f in [files] if isinstance(files, str) else files:
        assert sandbox.home.resolve() in Path(f).resolve().parents, f


def test_a_planted_home_config_and_env_never_reach_settings(tmp_path: Path) -> None:
    """End to end in a fresh interpreter: a home whose `.hp/.env` holds an admin
    secret, and the same secret exported in the environment - the shape of a
    real workstation. After isolation, Settings must see neither."""
    real_home = tmp_path / "workstation-home"
    (real_home / ".hp").mkdir(parents=True)
    (real_home / ".hp" / ".env").write_text(f"HP_ADMIN_SECRET={CANARY}\n", encoding="utf-8")
    workdir = tmp_path / "cwd"
    workdir.mkdir()

    script = textwrap.dedent(
        """
        import sys
        from tests import hermetic

        sandbox = hermetic.isolate()
        hermetic.assert_no_foreign_env_files(sandbox)
        from homepilot.config import Settings

        print("ADMIN_SECRET=" + Settings().admin_secret)
        hermetic.remove(sandbox)
        """
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("HP_") and k != "PYTHONPATH"}
    env["HOME"] = str(real_home)
    env["HP_ADMIN_SECRET"] = CANARY
    env["PYTHONPATH"] = os.pathsep.join([str(REPO_ROOT), str(REPO_ROOT / "src")])
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(workdir),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert "ADMIN_SECRET=\n" in proc.stdout, "isolation did not reset the admin secret"
    assert CANARY not in proc.stdout + proc.stderr, "the workstation's secret reached Settings"
    leftovers = [p.name for p in real_home.iterdir() if p.name.startswith(hermetic.SANDBOX_PREFIX)]
    assert not leftovers, f"the sandbox was not removed: {leftovers}"


def test_a_dotenv_in_the_working_directory_refuses_the_run(tmp_path: Path) -> None:
    """A deployment `.env` in the checkout would be read by every Settings()."""
    home = tmp_path / "home"
    home.mkdir()
    workdir = tmp_path / "checkout"
    workdir.mkdir()
    (workdir / ".env").write_text(f"HP_ADMIN_SECRET={CANARY}\n", encoding="utf-8")
    script = textwrap.dedent(
        """
        from tests import hermetic

        sandbox = hermetic.isolate()
        try:
            hermetic.assert_no_foreign_env_files(sandbox)
        except hermetic.ForeignConfigError:
            print("REFUSED")
        finally:
            hermetic.remove(sandbox)
        """
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("HP_") and k != "PYTHONPATH"}
    env["HOME"] = str(home)
    env["PYTHONPATH"] = os.pathsep.join([str(REPO_ROOT), str(REPO_ROOT / "src")])
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(workdir),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert "REFUSED" in proc.stdout, "a .env in the working directory was accepted"
