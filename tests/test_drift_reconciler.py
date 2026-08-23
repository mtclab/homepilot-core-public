from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from homepilot.reconciler import (
    DriftReconciler,
    verify_artifact,
)
from homepilot.reconciler.verify import DriftState


@pytest.fixture
async def real_db(tmp_path: Path):
    from homepilot.db.connection import Database
    from homepilot.db.migrations import run_migrations

    db = Database(str(tmp_path / "test.db"))
    await db.connect()
    await run_migrations(db)
    yield db
    await db.close()


@pytest.fixture
async def repo(real_db):
    from homepilot.db.repository import Repository

    return Repository(real_db)


@pytest.fixture
def mock_store():
    store = MagicMock()
    return store


@pytest.fixture
def mock_executor():
    executor = MagicMock()
    executor.ssh = AsyncMock()
    executor.proxmox = AsyncMock()
    executor.vault = MagicMock()
    executor.pve_nodes = []
    return executor


def _make_artifact_fm(
    artifact_id: str = "2025-01-01-test-art",
    kind: str = "ansible-playbook",
    status: str = "applied",
    host: str = "testhost",
    **extra: object,
) -> dict:
    fm = {
        "id": artifact_id,
        "kind": kind,
        "status": status,
        "intent": "test intent",
        "target": {"host": host, "kind": "vm", "node": "pve1", "vmid": 100},
        **extra,
    }
    return fm


class TestVerifyArtifactNonApplied:
    async def test_proposed_returns_not_applied(self, repo, mock_store):
        fm = _make_artifact_fm(status="proposed")
        mock_store.read.return_value = (fm, "body")
        result = await verify_artifact("art-1", repo, mock_store, executor=None)
        assert result.drifted is False
        assert result.details["reason"] == "not_applied"

    async def test_approved_returns_not_applied(self, repo, mock_store):
        fm = _make_artifact_fm(status="approved")
        mock_store.read.return_value = (fm, "body")
        result = await verify_artifact("art-1", repo, mock_store, executor=None)
        assert result.drifted is False
        assert result.details["reason"] == "not_applied"


class TestVerifyArtifactKbNote:
    async def test_kb_note_skipped(self, repo, mock_store):
        fm = _make_artifact_fm(kind="kb-note", status="applied")
        mock_store.read.return_value = (fm, "body")
        result = await verify_artifact("art-1", repo, mock_store, executor=None)
        assert result.drifted is False
        assert result.details["reason"] == "kb_note_skipped"


class TestVerifyArtifactNoExecutor:
    async def test_no_executor_returns_gracefully(self, repo, mock_store):
        fm = _make_artifact_fm(status="applied")
        mock_store.read.return_value = (fm, "body")
        result = await verify_artifact("art-1", repo, mock_store, executor=None)
        assert result.drifted is False
        assert result.details["reason"] == "no_executor"


class TestVerifyArtifactFileNotFound:
    async def test_file_not_found_propagates(self, repo, mock_store):
        mock_store.read.side_effect = FileNotFoundError("not found")
        with pytest.raises(FileNotFoundError):
            await verify_artifact("missing", repo, mock_store)


class TestVerifyShellScript:
    async def test_shell_script_unverifiable(self, repo, mock_store, mock_executor):
        fm = _make_artifact_fm(kind="shell-script", status="applied")
        mock_store.read.return_value = (fm, "body")
        result = await verify_artifact("art-1", repo, mock_store, mock_executor)
        assert result.drifted is False
        assert result.details["reason"] == "shell_script_unverifiable"


class TestVerifyAnsible:
    async def test_ansible_no_spec(self, repo, mock_store, mock_executor):
        fm = _make_artifact_fm(kind="ansible-playbook", status="applied")
        mock_store.read.return_value = (fm, "no spec here")
        result = await verify_artifact("art-1", repo, mock_store, mock_executor)
        assert result.drifted is False
        assert result.details["reason"] == "no_spec"

    async def test_ansible_no_host(self, repo, mock_store, mock_executor):
        fm = _make_artifact_fm(kind="ansible-playbook", status="applied")
        fm["target"] = {}
        body = "```yaml ansible-spec\n- hosts: all\n  tasks: []\n```"
        mock_store.read.return_value = (fm, body)
        result = await verify_artifact("art-1", repo, mock_store, mock_executor)
        assert result.drifted is False
        assert result.details["reason"] == "no_host"

    async def test_ansible_reports_unknown_rather_than_a_verdict(
        self, repo, mock_store, mock_executor
    ):
        """Ansible drift checking does not exist (#425).

        The two tests that stood here asserted `changed=0 -> not drifted` and
        `changed=2 -> drifted` by setting `mock_executor.ssh.exec`, MANUFACTURING
        the very attribute production does not have: `ArtifactExecutor` lost
        `.ssh` with the jump server, so the real call raised AttributeError into
        a handler that returned `drifted=False`. The tests were green and every
        applied ansible artifact reported "in spec" forever.

        The verifier now says it did not check, which is the only true answer
        until the playbook transport is rebuilt (#388).
        """
        fm = _make_artifact_fm(kind="ansible-playbook", status="applied")
        body = "```yaml ansible-spec\n- hosts: all\n  tasks: []\n```"
        mock_store.read.return_value = (fm, body)

        result = await verify_artifact("art-1", repo, mock_store, mock_executor)

        assert result.state is DriftState.UNKNOWN
        assert result.details["reason"] == "ansible_unverifiable"
        assert "not implemented" in result.verification_log

    async def test_ansible_forbidden_host(self, repo, mock_store, mock_executor):
        """A PVE hypervisor node must be refused as an ansible verify target (#388).

        This test previously did:

            mock_executor.ssh._validate_guest_only = MagicMock(
                side_effect=GuestHostError("forbidden"))

        which MANUFACTURED both the attribute and the method on a MagicMock. In
        production neither existed - the orchestrator exposes `.agent` /
        `.host_adapter`, and the method is `_check_guest_only` - so the guard
        raised AttributeError (uncaught) while this test stayed green because it
        had invented the thing that was missing. It also imported GuestHostError
        from adapters.ssh, a different class from the one the agent adapter
        raises, and that passed only because the mock raised exactly that class.

        It now drives the real code path: name a real PVE node as the target and
        assert the refusal, with nothing mocked into existence.

        Teeth: remove the `is_pve_node` guard in `_verify_ansible` and this fails.
        """
        fm = _make_artifact_fm(kind="ansible-playbook", status="applied", host="pve1")
        body = "```yaml ansible-spec\n- hosts: all\n  tasks: []\n```"
        mock_store.read.return_value = (fm, body)
        mock_executor.pve_nodes = ["pve1"]
        result = await verify_artifact("art-1", repo, mock_store, mock_executor)
        assert result.state is DriftState.UNKNOWN
        assert result.details["reason"] == "forbidden_host", (
            f"PVE node target was not refused: {result.details}"
        )

    async def test_ansible_guest_host_is_not_refused(self, repo, mock_store, mock_executor):
        """The guard must refuse ONLY hypervisor nodes, not ordinary guests."""
        fm = _make_artifact_fm(kind="ansible-playbook", status="applied", host="webvm01")
        body = "```yaml ansible-spec\n- hosts: all\n  tasks: []\n```"
        mock_store.read.return_value = (fm, body)
        mock_executor.pve_nodes = ["pve1"]
        result = await verify_artifact("art-1", repo, mock_store, mock_executor)
        assert result.details.get("reason") != "forbidden_host"


class TestVerifyProxmoxApi:
    async def test_proxmox_no_spec(self, repo, mock_store, mock_executor):
        fm = _make_artifact_fm(kind="proxmox-api-sequence", status="applied")
        mock_store.read.return_value = (fm, "no spec")
        result = await verify_artifact("art-1", repo, mock_store, mock_executor)
        assert result.drifted is False
        assert result.details["reason"] == "no_spec"

    async def test_proxmox_no_drift(self, repo, mock_store, mock_executor):
        fm = _make_artifact_fm(kind="proxmox-api-sequence", status="applied")
        body = (
            "```yaml proxmox-api-spec\n"
            "steps:\n"
            "  - id: step1\n"
            "    method: POST\n"
            "    path: /nodes/pve1/lxc/100/config\n"
            "    precheck:\n"
            "      method: GET\n"
            "      path: /nodes/pve1/lxc/100/config\n"
            "      skip_if: \"response['status'] == 'running'\"\n"
            "```\n"
        )
        mock_store.read.return_value = (fm, body)
        mock_executor.proxmox.call = AsyncMock(return_value={"status": "running", "data": {}})
        result = await verify_artifact("art-1", repo, mock_store, mock_executor)
        assert result.drifted is False

    async def test_proxmox_detected_drift(self, repo, mock_store, mock_executor):
        fm = _make_artifact_fm(kind="proxmox-api-sequence", status="applied")
        body = (
            "```yaml proxmox-api-spec\n"
            "steps:\n"
            "  - id: step1\n"
            "    method: POST\n"
            "    path: /nodes/pve1/lxc/100/config\n"
            "    precheck:\n"
            "      method: GET\n"
            "      path: /nodes/pve1/lxc/100/config\n"
            "      skip_if: \"response['status'] == 'running'\"\n"
            "```\n"
        )
        mock_store.read.return_value = (fm, body)
        mock_executor.proxmox.call = AsyncMock(return_value={"status": "stopped", "data": {}})
        result = await verify_artifact("art-1", repo, mock_store, mock_executor)
        assert result.drifted is True
        assert "step1" in result.details["drifted_steps"]

    async def test_proxmox_get_step_skipped(self, repo, mock_store, mock_executor):
        fm = _make_artifact_fm(kind="proxmox-api-sequence", status="applied")
        body = (
            "```yaml proxmox-api-spec\n"
            "steps:\n"
            "  - id: step1\n"
            "    method: GET\n"
            "    path: /nodes/pve1/status\n"
            "```\n"
        )
        mock_store.read.return_value = (fm, body)
        result = await verify_artifact("art-1", repo, mock_store, mock_executor)
        assert result.drifted is False
        assert "step1" in result.details["skipped_steps"]

    async def test_proxmox_no_precheck_skipped(self, repo, mock_store, mock_executor):
        fm = _make_artifact_fm(kind="proxmox-api-sequence", status="applied")
        body = (
            "```yaml proxmox-api-spec\n"
            "steps:\n"
            "  - id: step1\n"
            "    method: POST\n"
            "    path: /nodes/pve1/lxc/100/config\n"
            "```\n"
        )
        mock_store.read.return_value = (fm, body)
        result = await verify_artifact("art-1", repo, mock_store, mock_executor)
        assert result.drifted is False
        assert "step1" in result.details["skipped_steps"]

    async def test_proxmox_cluster_target(self, repo, mock_store, mock_executor):
        fm = _make_artifact_fm(kind="proxmox-api-sequence", status="applied")
        fm["target"] = {"kind": "cluster"}
        body = (
            "```yaml proxmox-api-spec\n"
            "steps:\n"
            "  - id: step1\n"
            "    method: POST\n"
            "    path: /nodes/pve1/lxc/100/config\n"
            "    precheck:\n"
            "      method: GET\n"
            "      path: /nodes/pve1/status\n"
            "      skip_if: \"response['status'] == 'online'\"\n"
            "```\n"
        )
        mock_store.read.return_value = (fm, body)
        mock_executor.proxmox.call = AsyncMock(return_value={"status": "online"})
        with patch(
            "homepilot.executor.proxmox_api._pick_cluster_node",
            new_callable=AsyncMock,
            return_value="pve1",
        ):
            result = await verify_artifact("art-1", repo, mock_store, mock_executor)
        assert result.drifted is False


class TestVerifyHttpSequence:
    async def test_http_no_spec(self, repo, mock_store, mock_executor):
        fm = _make_artifact_fm(kind="http-sequence", status="applied")
        mock_store.read.return_value = (fm, "no spec")
        result = await verify_artifact("art-1", repo, mock_store, mock_executor)
        assert result.drifted is False
        assert result.details["reason"] == "no_spec"

    async def test_http_no_drift(self, repo, mock_store, mock_executor):
        fm = _make_artifact_fm(kind="http-sequence", status="applied")
        body = (
            "```yaml http-spec\n"
            "steps:\n"
            "  - id: step1\n"
            "    name: test-svc\n"
            "    method: POST\n"
            "    path: /api/v1/config\n"
            "    precheck:\n"
            "      name: test-svc\n"
            "      method: GET\n"
            "      path: /api/v1/config\n"
            '      skip_if: "response.status_code == 200"\n'
            "```\n"
        )
        mock_store.read.return_value = (fm, body)

        mock_resp = MagicMock()
        mock_resp.status_code = 200

        mock_creds = {
            "base_url": "https://test.example.com",
            "headers": {},
            "verify_tls": True,
        }
        mock_executor.vault.get_secret = AsyncMock(return_value=mock_creds)

        with patch("httpx.AsyncClient") as mock_client:
            mock_client_instance = AsyncMock()
            mock_client_instance.request = AsyncMock(return_value=mock_resp)
            mock_client_instance.aclose = AsyncMock()
            mock_client.return_value = mock_client_instance
            result = await verify_artifact("art-1", repo, mock_store, mock_executor)

        assert result.drifted is False

    async def test_http_detected_drift(self, repo, mock_store, mock_executor):
        fm = _make_artifact_fm(kind="http-sequence", status="applied")
        body = (
            "```yaml http-spec\n"
            "steps:\n"
            "  - id: step1\n"
            "    name: test-svc\n"
            "    method: POST\n"
            "    path: /api/v1/config\n"
            "    precheck:\n"
            "      name: test-svc\n"
            "      method: GET\n"
            "      path: /api/v1/config\n"
            '      skip_if: "response.status_code == 200"\n'
            "```\n"
        )
        mock_store.read.return_value = (fm, body)

        mock_resp = MagicMock()
        mock_resp.status_code = 404

        mock_creds = {
            "base_url": "https://test.example.com",
            "headers": {},
            "verify_tls": True,
        }
        mock_executor.vault.get_secret = AsyncMock(return_value=mock_creds)

        with patch("httpx.AsyncClient") as mock_client:
            mock_client_instance = AsyncMock()
            mock_client_instance.request = AsyncMock(return_value=mock_resp)
            mock_client_instance.aclose = AsyncMock()
            mock_client.return_value = mock_client_instance
            result = await verify_artifact("art-1", repo, mock_store, mock_executor)

        assert result.drifted is True
        assert "step1" in result.details["drifted_steps"]

    async def test_verify_never_issues_non_get_requests(self, repo, mock_store, mock_executor):
        """#419 STANDING GATE: a verify pass must issue ZERO non-GET requests.

        Drift verification is read-only and runs unattended on a 1800s loop, so a
        single escaped DELETE is a live data-loss event. This asserts the OUTCOME
        on the transport - what actually went over the wire - not that some branch
        returned a particular value.

        Covers every dangerous shape in one spec:
          - non-GET step with NO precheck  (the original P0: fell through and ran)
          - non-GET step whose PRECHECK is itself declared non-GET
          - non-GET step with a valid GET precheck (only the GET may be issued)
          - plain GET step with no precheck

        Teeth: restore the fall-through in `_verify_http_sequence`, or drop the
        `pre_method != "GET"` guard, and this fails naming the escaped method.
        """
        fm = _make_artifact_fm(kind="http-sequence", status="applied")
        body = (
            "```yaml http-spec\n"
            "steps:\n"
            "  - id: delete-no-precheck\n"
            "    name: test-svc\n"
            "    method: DELETE\n"
            "    path: /api/things/42\n"
            "  - id: post-with-mutating-precheck\n"
            "    name: test-svc\n"
            "    method: POST\n"
            "    path: /api/things\n"
            "    precheck:\n"
            "      name: test-svc\n"
            "      method: DELETE\n"
            "      path: /api/things/43\n"
            '      skip_if: "response.status_code == 200"\n'
            "  - id: put-with-get-precheck\n"
            "    name: test-svc\n"
            "    method: PUT\n"
            "    path: /api/things/44\n"
            "    precheck:\n"
            "      name: test-svc\n"
            "      method: GET\n"
            "      path: /api/things/44\n"
            '      skip_if: "response.status_code == 200"\n'
            "  - id: plain-get\n"
            "    name: test-svc\n"
            "    method: GET\n"
            "    path: /api/things\n"
            "```\n"
        )
        mock_store.read.return_value = (fm, body)
        mock_executor.vault.get_secret = AsyncMock(
            return_value={
                "base_url": "https://test.example.com",
                "headers": {},
                "verify_tls": True,
            }
        )

        issued: list[str] = []

        async def _record(*args, **kwargs):
            issued.append((kwargs.get("method") or (args[0] if args else "")).upper())
            resp = MagicMock()
            resp.status_code = 200
            return resp

        with patch("httpx.AsyncClient") as mock_client:
            inst = AsyncMock()
            inst.request = AsyncMock(side_effect=_record)
            inst.aclose = AsyncMock()
            mock_client.return_value = inst
            await verify_artifact("art-1", repo, mock_store, mock_executor)

        offending = [m for m in issued if m != "GET"]
        assert not offending, (
            f"verify issued non-GET request(s) {offending}; all issued methods were {issued}"
        )


class TestVerifyComposite:
    async def test_composite_no_spec(self, repo, mock_store, mock_executor):
        fm = _make_artifact_fm(kind="composite", status="applied")
        mock_store.read.return_value = (fm, "no spec")
        result = await verify_artifact("art-1", repo, mock_store, mock_executor)
        assert result.drifted is False
        assert result.details["reason"] == "no_spec"

    async def test_composite_no_drift_in_subs(self, repo, mock_store, mock_executor):
        fm = _make_artifact_fm(kind="composite", status="applied")
        body = (
            "```yaml composite-spec\n"
            "steps:\n"
            "  - id: s1\n"
            "    artifact: sub-1\n"
            "  - id: s2\n"
            "    artifact: sub-2\n"
            "```\n"
        )
        mock_store.read.return_value = (fm, body)

        sub_fm = _make_artifact_fm(artifact_id="sub-1", kind="shell-script", status="applied")
        sub_fm2 = _make_artifact_fm(artifact_id="sub-2", kind="shell-script", status="applied")

        call_count = 0

        def side_effect(aid):
            nonlocal call_count
            call_count += 1
            data = {
                "art-1": (fm, body),
                "sub-1": (sub_fm, "shell script body"),
                "sub-2": (sub_fm2, "shell script body"),
            }
            if aid in data:
                return data[aid]
            raise FileNotFoundError(aid)

        mock_store.read = MagicMock(side_effect=side_effect)
        result = await verify_artifact("art-1", repo, mock_store, mock_executor)
        assert result.drifted is False

    async def test_composite_drift_in_sub(self, repo, mock_store, mock_executor):
        fm = _make_artifact_fm(kind="composite", status="applied")
        body = "```yaml composite-spec\nsteps:\n  - id: s1\n    artifact: sub-1\n```\n"
        mock_store.read.return_value = (fm, body)

        sub_fm_drifted = _make_artifact_fm(
            artifact_id="sub-1", kind="proxmox-api-sequence", status="applied"
        )
        sub_body_drifted = (
            "```yaml proxmox-api-spec\n"
            "steps:\n"
            "  - id: step1\n"
            "    method: POST\n"
            "    path: /test\n"
            "    precheck:\n"
            "      method: GET\n"
            "      path: /test\n"
            '      skip_if: "response.json.ok == true"\n'
            "```\n"
        )

        call_count = 0

        def side_effect(aid):
            nonlocal call_count
            if aid == "art-1":
                return (fm, body)
            elif aid == "sub-1":
                return (sub_fm_drifted, sub_body_drifted)
            raise FileNotFoundError(aid)

        mock_store.read = MagicMock(side_effect=side_effect)
        mock_executor.proxmox.call = AsyncMock(return_value={"ok": False})
        result = await verify_artifact("art-1", repo, mock_store, mock_executor)
        assert result.drifted is True
        assert "sub-1" in result.details["drifted_subs"]


class TestVerifyUnknownKind:
    async def test_unknown_kind(self, repo, mock_store, mock_executor):
        fm = _make_artifact_fm(kind="future-kind", status="applied")
        mock_store.read.return_value = (fm, "body")
        result = await verify_artifact("art-1", repo, mock_store, mock_executor)
        assert result.drifted is False
        assert result.details["reason"] == "unknown_kind"


class TestVerifyTimeout:
    async def test_a_timeout_is_not_a_clean_bill_of_health(self, repo, mock_store, mock_executor):
        """A check that timed out established nothing (#425).

        This test used to assert `drifted is False` for a timeout - which is the
        defect, written down as the requirement. It drove the ansible path via a
        manufactured `executor.ssh`; the proxmox path is a real one, so the
        timeout is exercised where it can actually happen.
        """
        fm = _make_artifact_fm(kind="proxmox-api-sequence", status="applied")
        body = (
            "```yaml proxmox-api-spec\n"
            "steps:\n"
            "  - id: s1\n"
            "    method: POST\n"
            "    path: /nodes/pve1/qemu\n"
            "    precheck:\n"
            "      method: GET\n"
            "      path: /nodes/pve1/qemu/100/status/current\n"
            "```"
        )
        mock_store.read.return_value = (fm, body)
        mock_executor.proxmox.call = AsyncMock(side_effect=TimeoutError())

        result = await verify_artifact("art-1", repo, mock_store, mock_executor)

        assert result.state is not DriftState.IN_SPEC, (
            "a check that never completed is reporting the artifact as in spec"
        )


class TestDriftReconciler:
    async def test_run_empty_store(self, repo, mock_store):
        mock_store.list.return_value = []
        reconciler = DriftReconciler(mock_store, repo, executor=None)
        result = await reconciler.run()
        assert result.name == "drift"
        assert result.success is True
        assert result.details["checked"] == 0
        assert result.details["drifted"] == 0
        assert result.details["skipped_kb_notes"] == 0

    async def test_run_skips_kb_notes(self, repo, mock_store):
        fm_kb = _make_artifact_fm(kind="kb-note", status="applied")
        mock_store.list.return_value = [fm_kb]
        reconciler = DriftReconciler(mock_store, repo, executor=None)
        result = await reconciler.run()
        assert result.success is True
        assert result.details["skipped_kb_notes"] == 1
        assert result.details["checked"] == 0

    async def test_run_checks_applied(self, repo, mock_store, mock_executor):
        fm1 = _make_artifact_fm(artifact_id="art-1", kind="shell-script", status="applied")
        mock_store.list.return_value = [fm1]

        def side_effect(aid):
            if aid == "art-1":
                return (fm1, "script body")
            raise FileNotFoundError(aid)

        mock_store.read = MagicMock(side_effect=side_effect)

        reconciler = DriftReconciler(mock_store, repo, executor=mock_executor, inter_check_delay=0)
        result = await reconciler.run()
        assert result.success is True
        assert result.details["checked"] == 1
        assert result.details["drifted"] == 0

    async def test_run_handles_errors(self, repo, mock_store, mock_executor):
        fm1 = _make_artifact_fm(artifact_id="art-1", kind="ansible-playbook", status="applied")
        mock_store.list.return_value = [fm1]
        mock_store.read.side_effect = RuntimeError("broken")

        reconciler = DriftReconciler(mock_store, repo, executor=mock_executor, inter_check_delay=0)
        result = await reconciler.run()
        assert result.success is True
        assert result.details["errors"] == 1

    async def test_run_audit_log_written(self, repo, mock_store, mock_executor):
        fm1 = _make_artifact_fm(artifact_id="art-1", kind="shell-script", status="applied")
        mock_store.list.return_value = [fm1]

        def side_effect(aid):
            return (fm1, "script body")

        mock_store.read = MagicMock(side_effect=side_effect)

        reconciler = DriftReconciler(mock_store, repo, executor=mock_executor, inter_check_delay=0)
        await reconciler.run()

        rows = await repo.db.fetchall(
            "SELECT * FROM audit_log WHERE source = 'reconciler:drift' AND action = 'check_cycle'"
        )
        assert len(rows) >= 1

    async def test_run_failure(self, repo, mock_store):
        mock_store.list.side_effect = RuntimeError("store broken")
        reconciler = DriftReconciler(mock_store, repo, executor=None)
        result = await reconciler.run()
        assert result.success is False
        assert "error" in result.details


class TestDriftReconcilerCheckSingle:
    async def test_check_single_persists_drift(self, repo, mock_store, mock_executor):
        fm = _make_artifact_fm(artifact_id="art-1", kind="shell-script", status="applied")

        def side_effect(aid):
            return (fm, "script body")

        mock_store.read = MagicMock(side_effect=side_effect)

        reconciler = DriftReconciler(mock_store, repo, executor=mock_executor)
        result = await reconciler.check_single("art-1")

        assert result.drifted is False
        row = await repo.get_drift_check("art-1")
        assert row is not None
        assert row["drifted"] == 0

    async def test_check_single_file_not_found(self, repo, mock_store):
        mock_store.read.side_effect = FileNotFoundError("not found")
        reconciler = DriftReconciler(mock_store, repo, executor=None)
        with pytest.raises(FileNotFoundError):
            await reconciler.check_single("missing")

    async def test_check_single_audit_log(self, repo, mock_store, mock_executor):
        fm = _make_artifact_fm(artifact_id="art-1", kind="shell-script", status="applied")

        def side_effect(aid):
            return (fm, "script body")

        mock_store.read = MagicMock(side_effect=side_effect)

        reconciler = DriftReconciler(mock_store, repo, executor=mock_executor)
        await reconciler.check_single("art-1")

        rows = await repo.db.fetchall(
            "SELECT * FROM audit_log WHERE source = 'reconciler:drift' AND action = 'check_drift'"
        )
        assert len(rows) >= 1
        assert rows[0]["artifact_id"] == "art-1"


class TestDriftEventEmission:
    """Drift must be actionable: a detected drift emits exactly one
    artifact_drifted event (SSE + webhook fan-out); an in-spec artifact emits
    none."""

    def _proxmox_drift_fm_body(self):
        fm = _make_artifact_fm(
            artifact_id="art-drift", kind="proxmox-api-sequence", status="applied"
        )
        body = (
            "```yaml proxmox-api-spec\n"
            "steps:\n"
            "  - id: step1\n"
            "    method: POST\n"
            "    path: /nodes/pve1/lxc/100/config\n"
            "    precheck:\n"
            "      method: GET\n"
            "      path: /nodes/pve1/lxc/100/config\n"
            "      skip_if: \"response['status'] == 'running'\"\n"
            "```\n"
        )
        return fm, body

    async def test_detected_drift_emits_one_event(self, repo, mock_store, mock_executor):
        fm, body = self._proxmox_drift_fm_body()
        mock_store.read.return_value = (fm, body)
        # precheck returns 'stopped' != 'running' → drift
        mock_executor.proxmox.call = AsyncMock(return_value={"status": "stopped", "data": {}})

        reconciler = DriftReconciler(mock_store, repo, executor=mock_executor)
        with patch("homepilot.reconciler.drift.emit_event", new_callable=AsyncMock) as mock_emit:
            result = await reconciler.check_single("art-drift")

        assert result.drifted is True
        assert mock_emit.await_count == 1
        (event_type, payload), _kwargs = mock_emit.await_args
        assert event_type == "artifact_drifted"
        assert payload["id"] == "art-drift"
        assert payload["drifted"] is True
        assert "drift_summary" in payload

    async def test_no_drift_emits_no_event(self, repo, mock_store, mock_executor):
        fm, body = self._proxmox_drift_fm_body()
        mock_store.read.return_value = (fm, body)
        # precheck returns 'running' == 'running' → no drift
        mock_executor.proxmox.call = AsyncMock(return_value={"status": "running", "data": {}})

        reconciler = DriftReconciler(mock_store, repo, executor=mock_executor)
        with patch("homepilot.reconciler.drift.emit_event", new_callable=AsyncMock) as mock_emit:
            result = await reconciler.check_single("art-drift")

        assert result.drifted is False
        assert mock_emit.await_count == 0

    async def test_emit_failure_does_not_break_cycle(self, repo, mock_store, mock_executor):
        # Emission is best-effort: a failing emit must not fail the drift check.
        fm, body = self._proxmox_drift_fm_body()
        mock_store.read.return_value = (fm, body)
        mock_executor.proxmox.call = AsyncMock(return_value={"status": "stopped", "data": {}})

        reconciler = DriftReconciler(mock_store, repo, executor=mock_executor)
        with patch(
            "homepilot.reconciler.drift.emit_event",
            new_callable=AsyncMock,
            side_effect=RuntimeError("bus down"),
        ):
            result = await reconciler.check_single("art-drift")

        assert result.drifted is True
        row = await repo.get_drift_check("art-drift")
        assert row["drifted"] == 1


class TestRepositoryDriftMethods:
    async def test_upsert_drift_check_insert(self, repo):
        await repo.upsert_drift_check("art-1", drifted=False)
        row = await repo.get_drift_check("art-1")
        assert row is not None
        assert row["artifact_id"] == "art-1"
        assert row["drifted"] == 0

    async def test_upsert_drift_check_update(self, repo):
        await repo.upsert_drift_check("art-1", drifted=False)
        await repo.upsert_drift_check("art-1", drifted=True, details_json='{"reason":"x"}')
        row = await repo.get_drift_check("art-1")
        assert row["drifted"] == 1
        assert row["details_json"] == '{"reason":"x"}'

    async def test_get_drift_checks_all(self, repo):
        await repo.upsert_drift_check("art-1", drifted=False)
        await repo.upsert_drift_check("art-2", drifted=True)
        rows = await repo.get_drift_checks()
        assert len(rows) == 2

    async def test_get_drift_checks_filter_drifted(self, repo):
        await repo.upsert_drift_check("art-1", drifted=False)
        await repo.upsert_drift_check("art-2", drifted=True)
        rows = await repo.get_drift_checks(drifted=True)
        assert len(rows) == 1
        assert rows[0]["artifact_id"] == "art-2"

    async def test_get_drift_check_not_found(self, repo):
        row = await repo.get_drift_check("nonexistent")
        assert row is None

    async def test_get_drift_checks_with_details(self, repo):
        await repo.upsert_drift_check(
            "art-1", drifted=True, details_json='{"reason":"config_changed"}'
        )
        rows = await repo.get_drift_checks(drifted=True)
        assert len(rows) == 1
        assert rows[0]["details_json"] == '{"reason":"config_changed"}'

    async def test_get_drift_checks_pagination(self, repo):
        for i in range(5):
            await repo.upsert_drift_check(f"art-{i}", drifted=False)
        rows = await repo.get_drift_checks(limit=2, offset=0)
        assert len(rows) == 2
        rows2 = await repo.get_drift_checks(limit=2, offset=2)
        assert len(rows2) == 2


class TestAnsibleOutputParsing:
    def test_no_changes(self):
        assert _ansible_output_has_changes("PLAY RECAP **** changed=0 unreachable=0") is False

    def test_has_changes(self):
        assert _ansible_output_has_changes("PLAY RECAP **** changed=3 unreachable=0") is True

    def test_no_recap_no_changes(self):
        assert _ansible_output_has_changes("ok: [host]") is False

    def test_changed_keyword_without_recap(self):
        assert _ansible_output_has_changes("changed: [host]") is True

    def test_changed_zero_in_text(self):
        assert _ansible_output_has_changes("changed=0 failed=0") is False


def _ansible_output_has_changes(output: str) -> bool:
    import re

    recap_match = re.search(r"changed=(\d+)", output)
    if recap_match:
        return int(recap_match.group(1)) > 0
    return "changed" in output.lower() and "changed=0" not in output


class TestVerifyArtifactEdgeCases:
    async def test_error_during_verify(self, repo, mock_store, mock_executor):
        fm = _make_artifact_fm(kind="proxmox-api-sequence", status="applied")
        body = (
            "```yaml proxmox-api-spec\n"
            "steps:\n"
            "  - id: step1\n"
            "    method: POST\n"
            "    path: /test\n"
            "    precheck:\n"
            "      method: GET\n"
            "      path: /test\n"
            '      skip_if: "response.status_code == 200"\n'
            "```\n"
        )
        mock_store.read.return_value = (fm, body)
        mock_executor.proxmox.call = AsyncMock(side_effect=httpx.ConnectError("conn refused"))
        result = await verify_artifact("art-1", repo, mock_store, mock_executor)
        assert result.drifted is False
        assert "step1" in result.details.get("skipped_steps", [])

    async def test_proxmox_precheck_no_skip_if(self, repo, mock_store, mock_executor):
        fm = _make_artifact_fm(kind="proxmox-api-sequence", status="applied")
        body = (
            "```yaml proxmox-api-spec\n"
            "steps:\n"
            "  - id: step1\n"
            "    method: POST\n"
            "    path: /test\n"
            "    precheck:\n"
            "      method: GET\n"
            "      path: /test\n"
            "```\n"
        )
        mock_store.read.return_value = (fm, body)
        mock_executor.proxmox.call = AsyncMock(return_value={"data": {}})
        result = await verify_artifact("art-1", repo, mock_store, mock_executor)
        assert result.drifted is False
        assert "step1" in result.details["skipped_steps"]


class TestDriftReconcilerMixedKinds:
    async def test_mixed_applied_and_kb_note(self, repo, mock_store, mock_executor):
        fm_shell = _make_artifact_fm(artifact_id="art-shell", kind="shell-script", status="applied")
        fm_kb = _make_artifact_fm(artifact_id="art-kb", kind="kb-note", status="applied")

        mock_store.list.return_value = [fm_shell, fm_kb]

        def side_effect(aid):
            if aid == "art-shell":
                return (fm_shell, "script body")
            elif aid == "art-kb":
                return (fm_kb, "note body")
            raise FileNotFoundError(aid)

        mock_store.read = MagicMock(side_effect=side_effect)

        reconciler = DriftReconciler(mock_store, repo, executor=mock_executor, inter_check_delay=0)
        result = await reconciler.run()
        assert result.success is True
        assert result.details["checked"] == 1
        assert result.details["skipped_kb_notes"] == 1


class TestDriftReconcilerCheckSingleSkips:
    async def test_check_single_kb_note_skipped(self, repo, mock_executor):
        fm_kb = _make_artifact_fm(artifact_id="art-kb", kind="kb-note", status="applied")
        store = MagicMock()
        store.read.return_value = (fm_kb, "note body")
        reconciler = DriftReconciler(store, repo, executor=mock_executor)
        result = await reconciler.check_single("art-kb")
        assert result.drifted is False
        assert result.details["reason"] == "kb_note_skipped"
        row = await repo.get_drift_check("art-kb")
        assert row is not None
        assert row["drifted"] == 0

    async def test_check_single_non_applied_skipped(self, repo, mock_executor):
        fm_proposed = _make_artifact_fm(
            artifact_id="art-prop", kind="shell-script", status="proposed"
        )
        store = MagicMock()
        store.read.return_value = (fm_proposed, "body")
        reconciler = DriftReconciler(store, repo, executor=mock_executor)
        result = await reconciler.check_single("art-prop")
        assert result.drifted is False
        assert result.details["reason"] == "not_applied"

    async def test_check_single_nonexistent_raises(self, repo, mock_executor):
        store = MagicMock()
        store.read.side_effect = FileNotFoundError("gone")
        reconciler = DriftReconciler(store, repo, executor=mock_executor)
        with pytest.raises(FileNotFoundError):
            await reconciler.check_single("ghost")


class TestRepositoryDriftRoundTrip:
    async def test_upsert_get_round_trip(self, repo):
        await repo.upsert_drift_check("art-rt", drifted=True, details_json='{"x":1}')
        row = await repo.get_drift_check("art-rt")
        assert row["artifact_id"] == "art-rt"
        assert row["drifted"] == 1
        assert row["details_json"] == '{"x":1}'
        assert row["checked_at"] is not None

        await repo.upsert_drift_check("art-rt", drifted=False, details_json=None)
        row2 = await repo.get_drift_check("art-rt")
        assert row2["drifted"] == 0
        assert row2["details_json"] is None
        assert row2["checked_at"] >= row["checked_at"]

    async def test_get_drift_checks_filter_not_drifted(self, repo):
        await repo.upsert_drift_check("art-a", drifted=False)
        await repo.upsert_drift_check("art-b", drifted=True)
        rows = await repo.get_drift_checks(drifted=False)
        assert len(rows) == 1
        assert rows[0]["artifact_id"] == "art-a"

    async def test_get_drift_checks_empty_table(self, repo):
        rows = await repo.get_drift_checks()
        assert rows == []


class TestSecurityHostnameValidation:
    async def test_ansible_invalid_hostname_rejected(self, repo, mock_store, mock_executor):
        fm = _make_artifact_fm(kind="ansible-playbook", status="applied")
        fm["target"]["host"] = "evil host\n[extra]"
        body = "```yaml ansible-spec\n- hosts: all\n  tasks: []\n```"
        mock_store.read.return_value = (fm, body)
        result = await verify_artifact("art-1", repo, mock_store, mock_executor)
        assert result.drifted is False
        assert result.details["reason"] == "invalid_host"

    async def test_ansible_valid_hostname_accepted(self, repo, mock_store, mock_executor):
        fm = _make_artifact_fm(kind="ansible-playbook", status="applied")
        body = "```yaml ansible-spec\n- hosts: all\n  tasks: []\n```"
        mock_store.read.return_value = (fm, body)
        mock_executor.ssh.exec = AsyncMock(
            return_value=(0, "PLAY RECAP *** changed=0 unreachable=0 failed=0", "")
        )
        with patch("homepilot.reconciler.verify._ansible_semaphore") as mock_sem:
            mock_sem.__aenter__ = AsyncMock(return_value=None)
            mock_sem.__aexit__ = AsyncMock(return_value=None)
            result = await verify_artifact("art-1", repo, mock_store, mock_executor)
        assert result.details.get("reason") != "invalid_host"

    async def test_ansible_hostname_with_dots(self, repo, mock_store, mock_executor):
        fm = _make_artifact_fm(kind="ansible-playbook", status="applied")
        fm["target"]["host"] = "web-server.example.com"
        body = "```yaml ansible-spec\n- hosts: all\n  tasks: []\n```"
        mock_store.read.return_value = (fm, body)
        mock_executor.ssh.exec = AsyncMock(
            return_value=(0, "PLAY RECAP *** changed=0 unreachable=0 failed=0", "")
        )
        with patch("homepilot.reconciler.verify._ansible_semaphore") as mock_sem:
            mock_sem.__aenter__ = AsyncMock(return_value=None)
            mock_sem.__aexit__ = AsyncMock(return_value=None)
            result = await verify_artifact("art-1", repo, mock_store, mock_executor)
        assert result.details.get("reason") != "invalid_host"


class TestSecurityDepthGuard:
    async def test_max_depth_exceeded(self, repo, mock_store, mock_executor):
        fm = _make_artifact_fm(kind="composite", status="applied")
        body = "```yaml composite-spec\nsteps:\n  - id: s1\n    artifact: sub-1\n```\n"
        mock_store.read.return_value = (fm, body)
        result = await verify_artifact("art-1", repo, mock_store, mock_executor, _depth=11)
        assert result.drifted is False
        assert result.details["reason"] == "max_depth"

    async def test_depth_at_limit_still_runs(self, repo, mock_store, mock_executor):
        fm = _make_artifact_fm(kind="shell-script", status="applied")
        mock_store.read.return_value = (fm, "script body")
        result = await verify_artifact("art-1", repo, mock_store, mock_executor, _depth=10)
        assert result.drifted is False
        assert result.details["reason"] == "shell_script_unverifiable"

    async def test_composite_passes_incremented_depth(self, repo, mock_store, mock_executor):
        fm = _make_artifact_fm(kind="composite", status="applied")
        body = "```yaml composite-spec\nsteps:\n  - id: s1\n    artifact: sub-1\n```\n"
        sub_fm = _make_artifact_fm(artifact_id="sub-1", kind="shell-script", status="applied")

        call_count = 0

        def side_effect(aid):
            nonlocal call_count
            call_count += 1
            if aid == "art-1":
                return (fm, body)
            elif aid == "sub-1":
                return (sub_fm, "shell script body")
            raise FileNotFoundError(aid)

        mock_store.read = MagicMock(side_effect=side_effect)
        result = await verify_artifact("art-1", repo, mock_store, mock_executor, _depth=5)
        assert result.drifted is False
