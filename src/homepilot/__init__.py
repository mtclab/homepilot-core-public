"""HomePilot — AI-first, artifact-backed, MCP-native homelab platform."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _version
from pathlib import Path


def _read_pyproject_version() -> str:
    pyproject = Path(__file__).resolve().parent.parent.parent / "pyproject.toml"
    for line in pyproject.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("version"):
            return stripped.split("=", 1)[1].strip().strip('"').strip("'")
    return "2.2.5"


try:
    __version__: str = _version("homepilot")
except PackageNotFoundError:
    __version__ = _read_pyproject_version()
