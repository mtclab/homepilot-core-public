"""`hp agent list|token|bootstrap` reach the hub through the API (#430).

These three commands read ``app_state.get_agent_registry()`` - a global that is
only ever set inside the FastAPI lifespan. A standalone ``hp`` process therefore
ALWAYS printed "Agent hub not enabled" and exited 1, while the README presented
them as the enrolment path. The hub lives in the backend process, so the only
honest way for a separate process to ask it anything is over the API.

The gates assert what an operator gets: real fleet data on the terminal when the
backend is up, and a message naming the actual problem when it is not.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import AsyncMock, patch

from typer.testing import CliRunner

from homepilot.cli.main import app

runner = CliRunner()

CLI_SOURCE = Path(__file__).resolve().parents[1] / "src" / "homepilot" / "cli" / "main.py"

FLEET = [
    {
        "agent_id": "aaaaaaaa-1111",
        "hostname": "web01",
        "system_info": {"os": "Linux", "arch": "amd64", "agent_version": "v2.8.0"},
        "connected": True,
        "last_heartbeat": "2026-08-21T10:00:00Z",
        "last_error": None,
    },
    {
        "agent_id": "bbbbbbbb-2222",
        "hostname": "db01",
        "system_info": {"os": "Linux", "arch": "arm64"},
        "connected": False,
        "last_heartbeat": "2026-08-01T09:00:00Z",
        "last_error": "credential revoked by an operator",
    },
]


def _plain(output: str) -> str:
    """Rich wraps table cells; collapse whitespace so assertions are about text."""
    return re.sub(r"\s+", " ", output)


class TestTheFleetIsVisibleFromTheTerminal:
    def test_list_shows_the_fleet_the_api_reports(self) -> None:
        with patch("homepilot.cli.main._backend_api", AsyncMock(return_value=FLEET)):
            result = runner.invoke(app, ["agent", "list"])

        assert result.exit_code == 0, result.output
        out = _plain(result.output)
        assert "web01" in out and "db01" in out, (
            "the command reached the API and still showed no fleet"
        )

    def test_list_shows_which_binary_each_host_runs(self) -> None:
        """The reason #430 asks for a version at all: after the 2.6.0 regression
        the only way to find the hosts on the broken binary was to SSH each one."""
        with patch("homepilot.cli.main._backend_api", AsyncMock(return_value=FLEET)):
            result = runner.invoke(app, ["agent", "list"])

        out = _plain(result.output)
        assert "v2.8.0" in out
        assert "unknown" in out, (
            "an agent that predates the version stamp must be shown as unknown, not "
            "blank or invented"
        )

    def test_list_shows_why_a_disconnected_agent_is_gone(self) -> None:
        with patch("homepilot.cli.main._backend_api", AsyncMock(return_value=FLEET)):
            result = runner.invoke(app, ["agent", "list"])

        assert "revoked" in _plain(result.output), (
            "a revoked agent is still just a disconnected row with no reason"
        )


class TestItNamesTheRealProblem:
    def test_no_backend_says_so_instead_of_blaming_the_hub(self) -> None:
        with patch("homepilot.cli.main._mint_token_via_api", return_value=(None, None)):
            result = runner.invoke(app, ["agent", "list"])

        assert result.exit_code == 1
        out = result.output.lower()
        assert "not reachable" in out
        assert "not enabled" not in out, (
            "the command still blames a disabled hub for an unreachable backend - "
            "the exact #430 misdirection"
        )

    def test_a_refusal_from_the_backend_is_surfaced_verbatim(self) -> None:
        with patch(
            "homepilot.cli.main._mint_token_via_api",
            return_value=(None, "HP_ADMIN_SECRET is not configured"),
        ):
            result = runner.invoke(app, ["agent", "token"])

        assert result.exit_code == 1
        assert "HP_ADMIN_SECRET" in result.output


class TestTheseCommandsNoLongerReadALifespanGlobal:
    def test_the_source_does_not_reach_for_the_in_process_registry(self) -> None:
        """A behavioural test alone cannot catch a re-introduction: a fallback to
        `get_agent_registry()` would simply never fire in a test that stubs the
        API. The defect IS the import, so the source is what has to be checked.
        """
        src = CLI_SOURCE.read_text(encoding="utf-8")
        # Slice out the agent command block: other commands may legitimately use
        # in-process state.
        start = src.index("# ── Agent management ")
        block = src[start:]
        assert "import get_agent_registry" not in block, (
            "an agent CLI command reads app_state.get_agent_registry() again - that "
            "global is only set inside the FastAPI lifespan, so the command can only "
            "ever fail from a standalone `hp` process (#430)"
        )
