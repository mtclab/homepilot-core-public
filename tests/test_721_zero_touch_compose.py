"""#721: the README's `docker compose up -d` must come up on a fresh host.

The root compose bind-mounts ``./data/hp``; Docker creates a missing bind
source as root:root and the image runs as ``homepilot``, so the backend could
not create its database and crash-looped (docs/bugs-found.md Bug 8). The image
smoke hid it: it ``chmod 777``-ed its data dir and passed the vault passphrase
and MCP token in the environment, and judged "up" from one /health probe. The
in-process zero-touch gate never touches compose at all.

This gate cannot run Docker (the unit gate has none); it pins the SHAPE that
makes the shipped path work and the smoke that proves it on a Docker host:

- the backend's data dir is either a named volume or handed to the image's
  user by a one-shot step the backend waits on, before the backend starts;
- ``.env`` is optional, because the README no longer asks for one;
- the smoke runs the shipped compose with none of the props that hid the bug,
  and judges "up" over time (healthy, then zero restarts across a window).

Teeth: drop ``hp-data-init`` (or its ``service_completed_successfully``
dependency), make ``.env`` required again, or put the chmod / env secrets back
into the smoke, and this fails.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
ROOT_COMPOSE = REPO / "docker-compose.yml"
DEPLOY_COMPOSE = REPO / "deploy" / "control-plane" / "docker-compose.yml"
SMOKE = REPO / "scripts" / "smoke-image.sh"
DATA_TARGET = "/home/homepilot/.hp"


def _compose(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def _data_source(service: dict) -> str | None:
    for vol in service.get("volumes", []):
        source, _, rest = str(vol).partition(":")
        if rest.split(":")[0] == DATA_TARGET:
            return source
    return None


def _is_bind(source: str) -> bool:
    return source.startswith((".", "/", "~", "$"))


def _env_file_optional(service: dict) -> bool:
    entries = service.get("env_file")
    if entries is None:
        return True
    if isinstance(entries, str):
        entries = [entries]
    return all(isinstance(e, dict) and e.get("required") is False for e in entries)


class TestTheDataDirIsTheBackendsOnAFreshHost:
    def _assert_owned(self, compose: dict) -> None:
        services = compose["services"]
        backend = services["backend"]
        source = _data_source(backend)
        assert source is not None, "the backend mounts no data dir"
        if not _is_bind(source):
            return  # a named volume inherits the image's ownership
        init = services.get("hp-data-init")
        assert init is not None, (
            f"the backend bind-mounts {source}, which Docker creates root:root on a "
            "fresh host, and nothing hands it to the image's user (#721)"
        )
        assert _data_source(init) == source, "the ownership step fixes a different dir"
        assert str(init.get("user", "")).split(":")[0] in ("0", "root")
        command = " ".join(init.get("entrypoint") or []) + " " + str(init.get("command") or "")
        assert "chown" in command and "homepilot" in command, (
            "the ownership step must chown to the image's user by NAME, not a guessed uid"
        )
        assert init.get("image") == backend.get("image"), (
            "the ownership step must use the backend's own image (its user's uid)"
        )
        deps = backend.get("depends_on") or {}
        assert isinstance(deps, dict), "depends_on must carry a condition"
        assert deps.get("hp-data-init", {}).get("condition") == "service_completed_successfully", (
            "the backend must wait for the ownership step to FINISH, not merely start"
        )

    def test_root_compose(self) -> None:
        self._assert_owned(_compose(ROOT_COMPOSE))

    def test_control_plane_compose(self) -> None:
        self._assert_owned(_compose(DEPLOY_COMPOSE))


class TestNoEnvFileIsRequired:
    def test_root_compose(self) -> None:
        assert _env_file_optional(_compose(ROOT_COMPOSE)["services"]["backend"])

    def test_control_plane_compose(self) -> None:
        assert _env_file_optional(_compose(DEPLOY_COMPOSE)["services"]["backend"])

    def test_the_readme_quick_start_does_not_copy_an_env_file(self) -> None:
        readme = (REPO / "README.md").read_text()
        quick = readme.split("## Quick Start", 1)[1].split("\n## ", 1)[0]
        block = quick.split("```bash", 1)[1].split("```", 1)[0]
        assert "cp .env.example .env" not in block


class TestTheSmokeRunsTheShippedPath:
    text = SMOKE.read_text()
    # Comments say what used to be done; only commands count.
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))

    def test_it_uses_the_shipped_compose_not_a_hand_rolled_run(self) -> None:
        assert "docker compose" in self.code
        assert re.search(r'-f "\$REPO/docker-compose\.yml"', self.code)
        assert "docker run -d" not in self.code

    def test_it_hides_nothing_the_operator_is_not_given(self) -> None:
        assert not re.search(r"\bchmod\b", self.code), "a chmod hides the ownership bug"
        assert not re.search(r"\bchown\b", self.code), "a chown hides the ownership bug"
        for var in ("HP_VAULT_PASSPHRASE", "HP_MCP_TOKEN", "HP_SECRET_KEY"):
            assert var not in self.code, f"the smoke supplies {var}, which a real install is not"

    def test_the_override_changes_only_names_tags_and_build_context(self) -> None:
        override = self.code.split("<<YAML", 1)[1].split("\nYAML", 1)[0]
        for forbidden in ("volumes", "env_file", "environment", "user:", "entrypoint"):
            assert forbidden not in override, f"the smoke override replaces {forbidden}"

    def test_up_is_judged_over_time(self) -> None:
        assert "running/healthy" in self.code, "it must wait for compose's own healthy"
        assert "RestartCount" in self.code, "a crash-loop shows as restarts"
        assert "WATCH_S" in self.code

    def test_it_claims_and_opens_mcp_with_the_claimed_token(self) -> None:
        assert "/claim" in self.code
        assert "Authorization: Bearer $TOKEN" in self.code
