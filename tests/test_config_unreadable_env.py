"""An unreadable dotenv must not brick the boot (found live on prod 3.0.0).

A root-owned `.env` inside the data volume - written by a root `docker exec`
at some earlier point - made `Settings()` raise PermissionError at import
time, so the container crash-looped with a raw traceback. The file's settings
are the operator's to fix; everything configured through environment
variables must keep working, and the skip must be LOUD.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from homepilot.config import _env_files


@pytest.mark.skipif(os.getuid() == 0, reason="root can read anything")
def test_an_unreadable_env_file_is_skipped_loudly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    env = data_dir / ".env"
    env.write_text("HP_ALLOWED_HTTP_DOMAINS=example.com\n")
    env.chmod(0o000)
    monkeypatch.setenv("HP_DATA_DIR", str(data_dir))
    try:
        with caplog.at_level("ERROR"):
            files = _env_files()

        assert str(env) not in files, "an unreadable dotenv was handed to the parser"
        assert any("NOT READABLE" in r.message for r in caplog.records), (
            "the skip was silent - an operator gets no clue why their settings are ignored"
        )
    finally:
        env.chmod(0o600)


def test_a_readable_env_file_is_still_used(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / ".env").write_text("HP_ALLOWED_HTTP_DOMAINS=example.com\n")
    monkeypatch.setenv("HP_DATA_DIR", str(data_dir))

    assert str(data_dir / ".env") in _env_files()
