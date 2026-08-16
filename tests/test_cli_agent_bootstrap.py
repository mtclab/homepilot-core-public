"""`hp agent bootstrap` must resolve to the working, hub-backed implementation.

There used to be TWO ``@agent_app.command("bootstrap")`` definitions in
``cli/main.py``. Click keeps the later one (``agent_bootstrap_cmd``); the earlier
shadowed ``agent_bootstrap`` was dead code AND broken — it minted a token via
``generate_bootstrap_token()`` without inserting it into the store, so that token
could never authenticate, and it printed misleading guidance. The shadowed
definition was deleted (#381); these tests lock in that the surviving command is
the hub-backed one.
"""

from __future__ import annotations

from unittest.mock import patch

from typer.testing import CliRunner

from homepilot.cli.main import app

runner = CliRunner()


def test_bootstrap_help_is_the_one_time_impl() -> None:
    """Revert-check: re-add a shadowing ``@agent_app.command("bootstrap")`` stub
    after the working one and its help ("Generate a bootstrap token …") wins,
    losing 'one-time' -> this fails."""
    result = runner.invoke(app, ["agent", "bootstrap", "--help"])
    assert result.exit_code == 0, result.output
    assert "one-time" in result.output.lower()


def test_bootstrap_requires_running_hub() -> None:
    """The resolved impl needs a running hub: with the hub disabled it errors
    (exit 1, "Agent hub not enabled") instead of printing an unusable token.

    Revert-check: restore the shadowed stub as the resolved command and this
    fails — it would mint a token and exit 0 without any hub."""
    with patch("homepilot.app_state.get_agent_registry", return_value=None):
        result = runner.invoke(app, ["agent", "bootstrap"])
    assert result.exit_code == 1, result.output
    assert "not enabled" in result.output.lower()
