"""How a host is built (#818): storage, what changed, the as-built export.

The control plane's half of the host description, without a Go toolchain (the
real-binary journey is tests/test_host_description_journey_818.py):

* migration 43, rehearsed on a database at 42 holding a host;
* the latest/previous shift, so "what changed" is a comparison of two rows;
* a failed attempt is recorded and KEEPS the stored description;
* an agent too old to describe its host is refused with the reason, at once;
* a description of an unknown schema is refused, not stored;
* the export goes through the KB's record path and SUPERSEDES the host's
  previous as-built note: one current recipe per host in the KB;
* forgetting a host removes its description.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from homepilot.adapters.agent import AgentAdapter, AgentTooOldError
from homepilot.artifacts.lifecycle import ArtifactLifecycle
from homepilot.artifacts.store import ArtifactStore
from homepilot.db.connection import Database
from homepilot.db.migrations import MIGRATIONS, run_migrations
from homepilot.db.repository import Repository
from homepilot.inventory.asbuilt import render_asbuilt
from homepilot.inventory.description import (
    DescriptionError,
    HostDescriptionService,
    changes_between,
)
from homepilot.kb.service import KBService


def description(**overrides: Any) -> dict[str, Any]:
    """A host description shaped exactly like the agent's (agent/go/describe.go)."""
    doc: dict[str, Any] = {
        "schema": "hp.host-description/1",
        "collected_at": "2026-11-18T12:00:00Z",
        "agent": {"version": "3.14.0", "privileged": False, "run_as": "hp-agent"},
        "host": {"hostname": "proxy-1", "os": "Ubuntu 24.04.1 LTS", "kernel": "6.8.0"},
        "listening": {
            "status": "partial",
            "reason": "the owning process of 1 socket(s) is not visible to the agent user",
            "sockets": [
                {
                    "proto": "tcp",
                    "address": "0.0.0.0",
                    "port": 443,
                    "user": "root",
                    "process": "nginx",
                    "unit": "nginx.service",
                },
                {
                    "proto": "udp",
                    "address": "127.0.0.53",
                    "port": 53,
                    "user": "systemd-resolve",
                    "owner": "not visible to the agent",
                },
            ],
        },
        "units": {
            "status": "ok",
            "units": [
                {
                    "name": "nginx.service",
                    "type": "service",
                    "active": "active",
                    "sub": "running",
                    "enabled": "enabled",
                    "programs": ["/usr/sbin/nginx"],
                    "unit_file": "/lib/systemd/system/nginx.service",
                },
                {
                    "name": "app.service",
                    "type": "service",
                    "active": "active",
                    "enabled": "enabled",
                    "programs": ["/opt/app/bin/app"],
                    "unit_file": "/etc/systemd/system/app.service",
                    "custom": True,
                },
                {
                    "name": "certbot.timer",
                    "type": "timer",
                    "active": "active",
                    "enabled": "enabled",
                },
            ],
        },
        "docker": {
            "status": "ok",
            "containers": [
                {
                    "name": "app-web-1",
                    "image": "nginx:1.27",
                    "digest": "sha256:aaaa",
                    "state": "running",
                    "restart_count": 2,
                    "restart_policy": "unless-stopped",
                    "ports": [
                        {"ip": "0.0.0.0", "container_port": 80, "host_port": 8080, "proto": "tcp"}
                    ],
                    "mounts": [
                        {
                            "type": "bind",
                            "source": "/opt/app/html",
                            "destination": "/html",
                            "rw": False,
                        }
                    ],
                    "compose_project": "app",
                    "compose_service": "web",
                }
            ],
            "compose_projects": [
                {
                    "name": "app",
                    "working_dir": "/opt/app",
                    "config_files": ["/opt/app/docker-compose.yml"],
                    "services": ["web"],
                }
            ],
        },
        "nginx": {
            "status": "ok",
            "config": "/etc/nginx/nginx.conf",
            "files": [
                {"file": "/etc/nginx/nginx.conf", "includes": ["/etc/nginx/sites-enabled/estate"]},
                {"file": "/etc/nginx/sites-enabled/estate", "includes": []},
            ],
            "servers": [
                {
                    "context": "http",
                    "file": "/etc/nginx/sites-enabled/estate",
                    "server_names": ["example.com"],
                    "listen": ["443 ssl"],
                    "tls": {
                        "certificate": "/etc/ssl/certs/example.pem",
                        "certificate_key": "present",
                        "client_certificate": "/etc/nginx/client-ca.pem",
                        "verify_client": "on",
                    },
                    "locations": [{"match": "/", "proxy_pass": "http://app_backend"}],
                }
            ],
            "upstreams": [
                {
                    "name": "app_backend",
                    "file": "/etc/nginx/sites-enabled/estate",
                    "servers": ["10.0.0.10:8080"],
                }
            ],
        },
        "certificates": {
            "status": "ok",
            "certificates": [
                {
                    "path": "/etc/ssl/certs/example.pem",
                    "source": "nginx",
                    "status": "ok",
                    "subject": "example.com",
                    "names": ["example.com"],
                    "not_after": "2026-12-18T12:00:00Z",
                    "days_left": 30,
                }
            ],
            "letsencrypt": [
                {
                    "name": "example.org",
                    "domains": ["example.org"],
                    "authenticator": "webroot",
                    "installer": "nginx",
                }
            ],
        },
        "firewall": {
            "ufw": {
                "status": "ok",
                "enabled": True,
                "rules_status": "ok",
                "rules": [
                    {
                        "action": "allow",
                        "proto": "tcp",
                        "port": "443",
                        "from": "0.0.0.0/0",
                        "direction": "in",
                    }
                ],
            },
            "nftables": {
                "status": "not_visible",
                "reason": "not visible to the agent",
                "tables": [],
            },
        },
        "updates": {"status": "ok", "pending": 7, "security": 3, "reboot_required": True},
        "time": {"status": "ok", "ntp_enabled": True, "synchronized": True, "timezone": "UTC"},
        "redactions": 0,
        "duration_ms": 12,
    }
    doc.update(overrides)
    return doc


@pytest.fixture
async def db(tmp_path: Path):
    database = Database(str(tmp_path / "hp.db"))
    await database.connect()
    await run_migrations(database)
    yield database
    await database.close()


@pytest.fixture
async def repo(db: Database) -> Repository:
    return Repository(db)


class _Hub:
    """The hub as the adapter sees it: a registry and send_action."""

    def __init__(self, hostname: str, capabilities: list[str], answers: list[Any]) -> None:
        self.calls = 0
        self._answers = answers
        agent = SimpleNamespace(
            agent_id="agent-1",
            hostname=hostname,
            system_info={"agent_version": "3.14.0", "capabilities": capabilities},
        )
        self.registry = SimpleNamespace(
            get_by_hostname=lambda name: agent if name == hostname else None,
            list_connected=lambda: [
                {"agent_id": "agent-1", "hostname": hostname, "system_info": agent.system_info}
            ],
        )

    async def send_action(
        self, agent_id: str, action: str, params: dict[str, Any], timeout: int = 30
    ):
        assert action == "describe_host" and params == {}, "describe_host takes no parameters"
        answer = self._answers[min(self.calls, len(self._answers) - 1)]
        self.calls += 1
        if isinstance(answer, Exception):
            raise answer
        return {"description": answer}


async def _service(repo: Repository, hub: _Hub) -> tuple[HostDescriptionService, dict[str, Any]]:
    host_id = await repo.create_host(hostname="proxy-1", host_type="qemu")
    svc = HostDescriptionService(repo, AgentAdapter(hub_server=hub))
    return svc, await svc.host(host_id=host_id)


class TestMigration43:
    async def test_a_database_at_42_with_a_host_gains_the_table_and_keeps_the_host(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        database = Database(str(tmp_path / "at42.db"))
        await database.connect()
        try:
            monkeypatch.delitem(MIGRATIONS, 43)
            await run_migrations(database)
            monkeypatch.undo()
            tables = {
                r["name"]
                for r in await database.fetchall(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            assert "host_descriptions" not in tables
            repo = Repository(database)
            host_id = await repo.create_host(hostname="proxy-1", host_type="qemu")
            before = await repo.get_host(host_id)

            await run_migrations(database)

            row = await database.fetchone("SELECT value FROM settings WHERE key = 'schema_version'")
            assert int(row["value"]) == 43
            columns = {
                r["name"] for r in await database.fetchall("PRAGMA table_info(host_descriptions)")
            }
            assert columns == {
                "host_id", "hostname", "schema", "collected_at", "received_at",
                "description_json", "previous_json", "previous_collected_at",
                "last_error", "last_error_at", "asbuilt_note_id", "asbuilt_exported_at",
            }  # fmt: skip
            assert await repo.get_host(host_id) == before
            assert await database.fetchall("SELECT * FROM host_descriptions") == []
            # The new table works on the migrated database.
            await repo.save_host_description(host_id, "proxy-1", "s", "t", "{}")
            assert (await repo.get_host_description(host_id))["description_json"] == "{}"
        finally:
            await database.close()


class TestCollectAndCompare:
    async def test_the_latest_becomes_previous_and_the_changes_say_what_moved(self, repo):
        first = description()
        second = copy.deepcopy(first)
        second["collected_at"] = "2026-11-19T12:00:00Z"
        second["units"]["units"].append(
            {
                "name": "backup.service",
                "type": "service",
                "active": "inactive",
                "enabled": "enabled",
            }
        )
        second["docker"]["containers"][0]["image"] = "nginx:1.28"
        hub = _Hub("proxy-1", ["describe_host"], [first, second])
        svc, host = await _service(repo, hub)

        view = await svc.collect(host)
        assert view["description"] == first
        assert view["previous_collected_at"] is None and view["changes"] == []
        assert view["agent"] == {"connected": True, "can_describe": True, "version": "3.14.0"}

        # Within the refresh window the stored one answers; the agent is not asked.
        assert (await svc.collect(host))["collected_at"] == first["collected_at"]
        assert hub.calls == 1

        view = await svc.collect(host, force=True)
        assert hub.calls == 2
        assert view["collected_at"] == "2026-11-19T12:00:00Z"
        assert view["previous_collected_at"] == "2026-11-18T12:00:00Z"
        changes = {c["part"]: c for c in view["changes"]}
        assert changes["units"]["added"] == ["backup.service (inactive, enabled)"]
        assert changes["containers"]["added"] == ["app-web-1 nginx:1.28@sha256:aaaa"]
        assert changes["containers"]["removed"] == ["app-web-1 nginx:1.27@sha256:aaaa"]
        assert set(changes) == {"units", "containers"}

    async def test_a_failure_is_recorded_and_the_stored_description_kept(self, repo):
        hub = _Hub("proxy-1", ["describe_host"], [description(), TimeoutError()])
        svc, host = await _service(repo, hub)
        await svc.collect(host)
        with pytest.raises(DescriptionError) as err:
            await svc.collect(host, force=True)
        assert err.value.status == 504
        view = await svc.view(host)
        assert view["description"] == description()
        assert "did not answer in time" in view["last_error"]
        # A later success clears the error.
        hub._answers.append(description(collected_at="2026-11-20T00:00:00Z"))
        view = await svc.collect(host, force=True)
        assert view["last_error"] is None

    async def test_an_agent_too_old_is_refused_at_once_with_the_reason(self, repo):
        hub = _Hub("proxy-1", [], [description()])
        svc, host = await _service(repo, hub)
        with pytest.raises(DescriptionError) as err:
            await svc.collect(host)
        assert err.value.status == 409
        assert "upgrade it" in err.value.detail and "3.14.0" in err.value.detail
        assert hub.calls == 0, "an agent without the capability must not be sent the frame"
        assert "upgrade it" in (await svc.view(host))["last_error"]
        with pytest.raises(AgentTooOldError):
            await AgentAdapter(hub_server=hub).describe_host("proxy-1")

    async def test_an_unknown_schema_is_refused_not_stored(self, repo):
        hub = _Hub("proxy-1", ["describe_host"], [description(schema="hp.host-description/99")])
        svc, host = await _service(repo, hub)
        with pytest.raises(DescriptionError) as err:
            await svc.collect(host)
        assert err.value.status == 502 and "hp.host-description/99" in err.value.detail
        assert (await svc.view(host))["description"] is None

    async def test_no_agent_connected(self, repo):
        hub = _Hub("another-host", ["describe_host"], [description()])
        svc, host = await _service(repo, hub)
        with pytest.raises(DescriptionError) as err:
            await svc.collect(host)
        assert err.value.status == 409 and "no agent connected for proxy-1" in err.value.detail
        assert (await svc.view(host))["agent"] == {"connected": False, "can_describe": False}

    async def test_forgetting_a_host_removes_its_description(self, repo):
        hub = _Hub("proxy-1", ["describe_host"], [description()])
        svc, host = await _service(repo, hub)
        await svc.collect(host)
        await repo.delete_host(host["id"])
        assert await repo.get_host_description(host["id"]) is None

    async def test_the_schedule_describes_capable_hosts_whose_description_is_old(self, repo):
        from homepilot.inventory.description import HostDescriptionReconciler

        hub = _Hub("proxy-1", ["describe_host"], [description()])
        svc, _host = await _service(repo, hub)
        result = await HostDescriptionReconciler(svc, max_age=86400).run()
        assert result.success and result.details["described"] == 1
        # Fresh now: the next cycle leaves it alone.
        result = await HostDescriptionReconciler(svc, max_age=86400).run()
        assert result.details["described"] == 0 and hub.calls == 1


class TestChanges:
    def test_nothing_to_compare_against(self):
        assert changes_between(None, description()) == []

    def test_identical_descriptions_have_no_changes(self):
        assert changes_between(description(), description()) == []


@pytest.fixture
async def kb(repo: Repository, tmp_path: Path):
    store_dir = tmp_path / "artifacts"
    store_dir.mkdir()
    store = ArtifactStore(store_dir)
    lifecycle = ArtifactLifecycle(store, repo)
    svc = KBService(repo=repo, store=store, lifecycle=lifecycle)
    lifecycle._kb_service = svc
    yield svc
    await svc.drain()


class TestAsBuiltExport:
    async def test_export_records_one_current_recipe_per_host(self, repo, kb):
        changed = description(collected_at="2026-11-19T12:00:00Z")
        changed["nginx"]["servers"][0]["server_names"] = ["example.com", "new.example.com"]
        hub = _Hub("proxy-1", ["describe_host"], [description(), changed])
        svc, host = await _service(repo, hub)

        with pytest.raises(DescriptionError) as err:
            await svc.export_asbuilt(host, kb)
        assert err.value.status == 409, "nothing to export before a description exists"

        await svc.collect(host)
        first = await svc.export_asbuilt(host, kb)
        assert first["indexed"] is True and first["superseded"] is None
        fm, body = kb.store.read(first["note_id"])
        assert fm["kind"] == "kb-note" and fm["note_kind"] == "as-built"
        assert fm["status"] == "applied"
        assert fm["intent"].startswith("As-built: proxy-1")
        assert body.startswith("# As-built: proxy-1")
        doc = await repo.get_doc_by_source(f"artifact:{first['note_id']}")
        assert doc["target"] == "proxy-1" and doc["kind"] == "as-built"

        # Unchanged description: the same note, nothing superseded.
        again = await svc.export_asbuilt(host, kb)
        assert again["note_id"] == first["note_id"] and again["unchanged"] is True

        await svc.collect(host, force=True)
        second = await svc.export_asbuilt(host, kb)
        assert second["note_id"] != first["note_id"]
        assert second["superseded"] == first["note_id"]
        old_fm, _ = kb.store.read(first["note_id"])
        assert old_fm["status"] == "superseded" and old_fm["superseded_by"] == second["note_id"]
        new_fm, _ = kb.store.read(second["note_id"])
        assert new_fm["supersedes"] == [first["note_id"]]
        # The KB holds ONE as-built note for this host: the old document is gone.
        assert await repo.get_doc_by_source(f"artifact:{first['note_id']}") is None
        hits = await kb.search("new.example.com", kind="as-built")
        assert [h.get("source") for h in hits if h.get("kind") == "as-built"] == [
            f"artifact:{second['note_id']}"
        ]
        view = await svc.view(host)
        assert view["as_built"]["note_id"] == second["note_id"]
        # Exporting the same description again - after a supersede, when the
        # record path would collide with the note it already wrote - is a no-op.
        third = await svc.export_asbuilt(host, kb)
        assert third["note_id"] == second["note_id"] and third["unchanged"] is True
        assert third["superseded"] is None


class TestTheRecipe:
    def test_the_note_answers_what_runs_how_exposed_where_config_lives_what_it_needs(self):
        md = render_asbuilt("proxy-1", description())
        for heading in (
            "## What runs",
            "## How it is exposed",
            "## Where its configuration lives",
            "## What it depends on",
            "## Not visible to the agent",
            "## Rebuild checklist",
        ):
            assert heading in md
        assert "| nginx.service | active/running | enabled | /usr/sbin/nginx |" in md
        assert "/etc/systemd/system/app.service (custom)" in md
        assert "compose project **app** in `/opt/app`" in md
        assert "0.0.0.0:8080->80/tcp" in md and "/opt/app/html:/html:ro" in md
        assert "client certificates on against `/etc/nginx/client-ca.pem`" in md
        assert "`location /` -> proxy to `http://app_backend`" in md
        assert "backends it forwards to: `10.0.0.10:8080`, `http://app_backend`" in md
        assert "expires 2026-12-18T12:00:00Z (30 days)" in md
        assert "certbot renews example.org via webroot (installer nginx)" in md
        assert "- nftables: not visible to the agent" in md
        assert "pending updates: 7 (3 security); a reboot is required" in md
        assert "`docker compose -f /opt/app/docker-compose.yml up -d`" in md
        assert "Recreate and enable the custom units: app.service." in md

    def test_host_data_cannot_break_a_table(self):
        doc = description()
        doc["units"]["units"][0]["programs"] = ["/usr/bin/a|b\nc"]
        md = render_asbuilt("proxy-1", doc)
        assert "/usr/bin/a\\|b c" in md

    def test_a_bare_host_renders(self):
        bare = {
            "schema": "hp.host-description/1",
            "collected_at": "2026-11-18T12:00:00Z",
            "nginx": {"status": "not_installed"},
            "docker": {"status": "not_installed"},
        }
        md = render_asbuilt("bare", bare)
        assert "No nginx on this host." in md and "No Docker on this host." in md
        json.dumps(md)

    def test_a_reason_that_names_its_subject_is_not_said_twice(self):
        """3.14 walk: the agent's docker reason stands alone ("docker: not
        readable - ..."), and after the renderer's label it read "Docker:
        docker: not readable". Every hidden section, as the note says it."""
        doc = description()
        doc["docker"] = {
            "status": "not_visible",
            "reason": "docker: not readable - the agent's user cannot open the docker socket",
        }
        doc["nginx"] = {"status": "error", "reason": "nginx: the command failed (exit status 1)"}
        doc["firewall"]["nftables"] = {"status": "error", "reason": "nftables: timed out"}
        md = render_asbuilt("proxy-1", doc)
        assert "Docker: not readable - the agent's user cannot open" in md
        assert "nginx: the command failed" in md and "nftables: timed out" in md
        # No label anywhere is followed by itself.
        doubled = re.findall(r"(?i)\b([a-z][a-z ]*?): \1:", md)
        assert not doubled, doubled
        # A reason that merely contains a colon is left whole.
        doc["docker"]["reason"] = "not visible to the agent: its user cannot read this"
        assert "Docker: not visible to the agent: its user" in render_asbuilt("p", doc)
