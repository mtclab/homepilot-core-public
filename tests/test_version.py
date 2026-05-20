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
