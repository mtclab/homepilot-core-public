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


def test_bootstrap_requires_a_reachable_backend() -> None:
    """The resolved impl needs the running backend: with none reachable it errors
    (exit 1) instead of printing an unusable token.

    This test used to assert the string "Agent hub not enabled", which was the
    shape of the #430 defect rather than a requirement - the registry it read is
    only ever set inside the FastAPI lifespan, so a standalone `hp` process could
    never print anything else. The command now goes through the API, so what has
    to hold is: no backend, no token, and a message that says which of the two
    things is missing.

    Revert-check: restore the shadowed stub as the resolved command and this
    fails - it would mint a token and exit 0 without any hub."""
    with patch("homepilot.cli.main._mint_token_via_api", return_value=(None, None)):
        result = runner.invoke(app, ["agent", "bootstrap"])
    assert result.exit_code == 1, result.output
    assert "not reachable" in result.output.lower()
    assert "hp_" not in result.output, "a token was printed without a hub to honour it"
