from pathlib import Path

import homepilot


def _read_pyproject_version() -> str:
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    for line in pyproject.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("version"):
            return stripped.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError("version not found in pyproject.toml")


def test_version_matches_pyproject():
    assert homepilot.__version__ == _read_pyproject_version()


def test_version_is_string():
    assert isinstance(homepilot.__version__, str)
    assert homepilot.__version__


def _read_lock_homepilot_version() -> str:
    # The editable root package's own pin in uv.lock. It drifts when a release
    # bumps pyproject but nobody re-runs `uv sync`, and the Docker image installs
    # from the lock - so a stale entry is exactly the "artifact CI builds differs
    # from what the gate saw" trap. This asserts the two never diverge.
    lock = Path(__file__).resolve().parent.parent / "uv.lock"
    lines = lock.read_text().splitlines()
    for i, line in enumerate(lines):
        if line.strip() == 'name = "homepilot"':
            for follow in lines[i + 1 : i + 4]:
                stripped = follow.strip()
                if stripped.startswith("version"):
                    return stripped.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError("homepilot package not found in uv.lock")


def test_lock_version_matches_pyproject():
    assert _read_lock_homepilot_version() == _read_pyproject_version()
