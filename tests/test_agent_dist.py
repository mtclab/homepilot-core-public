"""HomePilot serves the agent payload itself, verified (#464).

The bug these gates forbid: both install paths sent the GUEST to GitHub - the
one-liner fetched `install-agent.sh` from `releases/latest/download`, and the
script then resolved a release through `api.github.com` and pulled the binary. An
isolated guest could not enrol at all, which is exactly the case the friend
portal's egress-limited VLAN creates, and nothing was verified beyond TLS: a
script piped to bash and a binary executed as root, with no checksum.

These assert the OUTCOME - the bytes a guest would receive, the digest that comes
with them, and that an unauthenticated caller gets nothing - rather than that a
route exists.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from homepilot.agent_hub import dist
from homepilot.auth.deps import require_token

AGENT_TOKEN = "hub-enrolment-token-for-tests"  # pragma: allowlist secret - test fixture


@pytest.fixture
def payload_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A stand-in for what the Dockerfile bakes into the image."""
    root = tmp_path / "agent-dist"
    root.mkdir()
    # An ELF-ish header, so a test can tell "the bytes arrived" from "something
    # arrived": the installer checks for ELF too.
    (root / "hp-agent-linux-amd64").write_bytes(b"\x7fELF" + b"amd64 agent payload")
    (root / "hp-agent-linux-arm64").write_bytes(b"\x7fELF" + b"arm64 agent payload")
    (root / "install-agent.sh").write_text("#!/usr/bin/env bash\necho installer\n")
    monkeypatch.setenv("HP_AGENT_DIST_DIR", str(root))
    return root


@pytest.fixture
def client(payload_dir: Path) -> TestClient:
    from homepilot.agent_hub.router import router as agents_router

    app = FastAPI()
    app.include_router(agents_router)

    class _Hub:
        auth_token = AGENT_TOKEN

    class _Registry:
        hub_server = _Hub()

    app.state.agent_registry = _Registry()
    app.state.repo = None
    # The manifest is an operator surface and stays admin-gated; the payload
    # routes are the ones a guest reaches with only the enrolment token.
    app.dependency_overrides[require_token] = lambda: {"scope": "*", "role": "admin"}
    return TestClient(app)


def _auth() -> dict[str, str]:
    return {"x-hp-agent-token": AGENT_TOKEN}


class TestTheGuestGetsTheRealBytes:
    def test_the_binary_is_served_with_a_matching_digest(self, client, payload_dir):
        """The whole point: a guest with no internet gets the agent, and can tell
        it received what the control plane meant to send."""
        resp = client.get("/agents/dist/hp-agent-linux-amd64", headers=_auth())

        assert resp.status_code == 200
        expected = (payload_dir / "hp-agent-linux-amd64").read_bytes()
        assert resp.content == expected, "the served bytes are not the image's binary"
        assert resp.headers["x-hp-sha256"] == hashlib.sha256(expected).hexdigest(), (
            "the advertised digest does not describe the bytes that were sent"
        )

    def test_the_installer_is_served_too(self, client, payload_dir):
        resp = client.get("/agents/dist/install-agent.sh", headers=_auth())
        assert resp.status_code == 200
        assert resp.text == (payload_dir / "install-agent.sh").read_text()
        assert resp.headers["x-hp-sha256"]

    def test_uname_spellings_resolve(self, client):
        """A guest reports `x86_64`/`aarch64`; the release files are named by
        GOARCH. Both spellings have to land on the same file."""
        by_goarch = client.get("/agents/dist/hp-agent-linux-amd64", headers=_auth())
        by_uname = client.get("/agents/dist/hp-agent-linux-x86_64", headers=_auth())
        assert by_uname.status_code == 200
        assert by_uname.content == by_goarch.content

    def test_an_unknown_architecture_is_refused_clearly(self, client):
        resp = client.get("/agents/dist/hp-agent-linux-mips", headers=_auth())
        assert resp.status_code == 404
        assert "mips" in resp.json()["detail"]


class TestItIsNotAnOpenDownload:
    """The payload is credential-gated. Not because the bytes are secret - they
    are on a public release - but because an unauthenticated binary endpoint on a
    control plane is a door nobody asked for."""

    def test_no_credential_is_refused(self, client):
        assert client.get("/agents/dist/hp-agent-linux-amd64").status_code == 401
        assert client.get("/agents/dist/install-agent.sh").status_code == 401

    def test_a_wrong_credential_is_refused(self, client):
        resp = client.get(
            "/agents/dist/hp-agent-linux-amd64",
            headers={"x-hp-agent-token": "not-the-hub-token"},
        )
        assert resp.status_code == 401

    def test_a_hub_with_no_token_does_not_become_open(self, client):
        """A misconfiguration must not turn the check into a formality."""
        client.app.state.agent_registry.hub_server.auth_token = ""
        try:
            resp = client.get("/agents/dist/hp-agent-linux-amd64", headers={"x-hp-agent-token": ""})
            assert resp.status_code == 401
        finally:
            client.app.state.agent_registry.hub_server.auth_token = AGENT_TOKEN


class TestTheManifestTellsTheTruth:
    def test_it_reports_digests_that_match_the_served_files(self, client, payload_dir):
        listed = client.get("/agents/dist", headers=_auth()).json()["artifacts"]
        entry = listed["hp-agent-linux-arm64"]
        assert entry["available"] is True
        assert entry["sha256"] == dist.sha256(payload_dir / "hp-agent-linux-arm64")
        assert entry["size_bytes"] == (payload_dir / "hp-agent-linux-arm64").stat().st_size

    def test_a_missing_artifact_says_so_rather_than_vanishing(self, client, payload_dir):
        """An operator debugging an enrolment needs to know WHICH artifact the
        image lacks, not infer it from a failed download."""
        (payload_dir / "hp-agent-linux-arm64").unlink()
        listed = client.get("/agents/dist", headers=_auth()).json()["artifacts"]
        assert listed["hp-agent-linux-arm64"]["available"] is False
        assert "not in this image" in listed["hp-agent-linux-arm64"]["reason"]


class TestNoPathEscape:
    def test_a_traversing_name_cannot_reach_outside_the_payload(self, payload_dir, tmp_path):
        """The arch comes from a URL. It is validated against a fixed table, but
        the resolver refuses an escape on its own merits too."""
        (tmp_path / "secret.txt").write_text("not yours")
        with pytest.raises(dist.DistUnavailableError):
            dist._resolve("../secret.txt")


class TestTheInstallerPrefersTheControlPlane:
    """The script is the other half: it must fetch from HomePilot when it knows
    where HomePilot is, and refuse a binary it cannot verify."""

    def _script(self) -> str:
        return (Path(__file__).resolve().parents[1] / "scripts" / "install-agent.sh").read_text()

    def test_it_fetches_the_binary_from_the_control_plane(self):
        script = self._script()
        assert "HP_API" in script
        assert "/agents/dist/hp-agent-linux-" in script, (
            "the installer no longer knows how to fetch from the control plane"
        )

    def test_it_refuses_a_binary_it_cannot_verify(self):
        """A missing digest must stop the install, not fall through to running an
        unverified binary as root."""
        script = self._script()
        assert "Refusing to install an unverified binary" in script
        assert "sha256sum" in script
        assert "Checksum mismatch" in script
