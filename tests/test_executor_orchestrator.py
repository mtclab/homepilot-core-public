from unittest.mock import AsyncMock, MagicMock

import pytest

from homepilot.artifacts.file_store import ArtifactFileStore
from homepilot.artifacts.lifecycle import ArtifactLifecycle
from homepilot.artifacts.models import ArtifactKind
from homepilot.artifacts.store import ArtifactStore
from homepilot.executor.orchestrator import ArtifactExecutor, ExecutorError, TamperError

ANSIBLE_SPEC_BODY = """\
## Plan
Install nginx

## Spec

```yaml ansible-spec
- name: Install nginx
  hosts: all
  tasks:
    - name: Install
      apt:
        name: nginx
        state: present
```
"""

SHELL_SPEC_BODY = """\
## Plan
Install something

## Idempotence preamble
This script is idempotent.

## Spec

```bash shell-spec
#!/bin/bash
set -euo pipefail
echo "done"
```
"""

PROXMOX_SPEC_BODY = """\
## Plan
Create LXC

## Spec

```yaml proxmox-api-spec
steps:
  - id: create-lxc
    method: POST
    path: /nodes/pve1/lxc
    body:
      vmid: 100
    on_error: halt
```
"""


def _make_spec(kind="ansible-playbook", body=None, target=None, **overrides):
    spec = {
        "id": "2025-01-01-test-artifact-abc123",
        "kind": kind,
        "intent": f"Test {kind}",
        "body": body or ANSIBLE_SPEC_BODY,
        "target": target or {"kind": "vm", "vmid": 100, "node": "pve1", "host": "web1"},
        "idempotence": "via-precheck",
        "produced_by": {"session": "s1", "agent": "a1", "user": "u1"},
        "rollback": True,
    }
    spec.update(overrides)
    return spec


class TestFullLifecycleAnsible:
    async def test_full_lifecycle_propose_approve_apply_revoke(self, tmp_artifacts_dir):
        store = ArtifactStore(tmp_artifacts_dir)
        lifecycle = ArtifactLifecycle(store=store)
        mock_ssh = AsyncMock()
        mock_ssh.exec = AsyncMock(return_value=(0, "PLAY OK", ""))
        mock_ssh._validate_guest_only = MagicMock()
        mock_proxmox = AsyncMock()
        mock_proxmox.snapshot = AsyncMock(return_value={"data": "snap1"})
        mock_repo = AsyncMock()
        mock_vault = AsyncMock()

        executor = ArtifactExecutor(
            store=store,
            lifecycle=lifecycle,
            repo=mock_repo,
            proxmox=mock_proxmox,
            agent=mock_ssh,
            vault=mock_vault,
            pve_nodes=["pve1"],
        )

        spec = _make_spec(kind="ansible-playbook", body=ANSIBLE_SPEC_BODY)
        aid = await lifecycle.propose(spec)

        fm, _ = store.read(aid)
        assert fm["status"] == "proposed"

        await lifecycle.approve(aid, user="admin")
        fm, _ = store.read(aid)
        assert fm["status"] == "approved"

        result = await executor.apply(aid, approved_by="admin")
        assert result.success
        fm, _ = store.read(aid)
        assert fm["status"] == "applied"
        assert fm["applied_at"] is not None
        mock_ssh.exec.assert_called()

        await executor.revoke(aid, user="admin", reason="done")
        fm, _ = store.read(aid)
        assert fm["status"] == "revoked"


class TestFullLifecycleProxmox:
    async def test_full_lifecycle_proxmox_api(self, tmp_artifacts_dir):
        store = ArtifactStore(tmp_artifacts_dir)
        lifecycle = ArtifactLifecycle(store=store)
        mock_proxmox = AsyncMock()
        mock_proxmox.call = AsyncMock(return_value={"data": {}})
        mock_proxmox.snapshot = AsyncMock(return_value={"data": "snap1"})
        mock_ssh = AsyncMock()
        mock_repo = AsyncMock()
        mock_vault = AsyncMock()

        executor = ArtifactExecutor(
            store=store,
            lifecycle=lifecycle,
            repo=mock_repo,
            proxmox=mock_proxmox,
            agent=mock_ssh,
            vault=mock_vault,
        )

        spec = _make_spec(kind="proxmox-api-sequence", body=PROXMOX_SPEC_BODY)
        aid = await lifecycle.propose(spec)
        await lifecycle.approve(aid, user="admin")

        result = await executor.apply(aid, approved_by="admin")
        assert result.success
        mock_proxmox.call.assert_called()

        await executor.revoke(aid, user="admin")
        fm, _ = store.read(aid)
        assert fm["status"] == "revoked"


class TestFullLifecycleShellScript:
    async def test_full_lifecycle_shell_script(self, tmp_artifacts_dir):
        store = ArtifactStore(tmp_artifacts_dir)
        lifecycle = ArtifactLifecycle(store=store)
        mock_ssh = AsyncMock()
        mock_ssh.exec = AsyncMock(return_value=(0, "done", ""))
        mock_ssh._validate_guest_only = MagicMock()
        mock_proxmox = AsyncMock()
        mock_repo = AsyncMock()
        mock_vault = AsyncMock()

        executor = ArtifactExecutor(
            store=store,
            lifecycle=lifecycle,
            repo=mock_repo,
            proxmox=mock_proxmox,
            agent=mock_ssh,
            vault=mock_vault,
            pve_nodes=["pve1"],
        )

        spec = _make_spec(kind="shell-script", body=SHELL_SPEC_BODY)
        aid = await lifecycle.propose(spec)
        await lifecycle.approve(aid, user="admin")

        result = await executor.apply(aid, approved_by="admin")
        assert result.success
        mock_ssh.exec.assert_called()


HOST_PROVISION_SPEC_BODY = """\
## Plan
Ensure nginx installed, running, configured.

```yaml host-provision-spec
packages:
  - nginx
services:
  - name: nginx
    state: started
config_files:
  - path: /etc/nginx/conf.d/app.conf
    content: |
      server { listen 80; }
    mode: "0644"
```
"""


class TestFullLifecycleHostProvision:
    async def test_full_lifecycle_host_provision(self, tmp_artifacts_dir):
        store = ArtifactStore(tmp_artifacts_dir)
        lifecycle = ArtifactLifecycle(store=store)
        mock_agent = AsyncMock()
        mock_agent.install_package = AsyncMock(return_value={"changed": True, "detail": "ok"})
        mock_agent.manage_service = AsyncMock(return_value={"changed": True, "detail": "ok"})
        mock_agent.write_config = AsyncMock(return_value={"changed": False, "detail": "same"})
        mock_proxmox = AsyncMock()
        mock_proxmox.snapshot = AsyncMock(return_value={"data": "snap1"})
        mock_repo = AsyncMock()
        mock_vault = AsyncMock()

        executor = ArtifactExecutor(
            store=store,
            lifecycle=lifecycle,
            repo=mock_repo,
            proxmox=mock_proxmox,
            agent=mock_agent,
            vault=mock_vault,
            pve_nodes=["pve1"],
        )

        spec = _make_spec(kind="host-provision", body=HOST_PROVISION_SPEC_BODY)
        aid = await lifecycle.propose(spec)
        await lifecycle.approve(aid, user="admin")

        result = await executor.apply(aid, approved_by="admin")
        assert result.success
        # the orchestrator routed host-provision to the native B1 actions
        mock_agent.install_package.assert_awaited_once_with("web1", "nginx")
        mock_agent.manage_service.assert_awaited_once_with("web1", "nginx", "started")
        mock_agent.write_config.assert_awaited_once()

        fm, _ = store.read(aid)
        assert fm["status"] == "applied"


class TestOrchestratorValidation:
    async def test_apply_non_approved_raises_executor_error(self, tmp_artifacts_dir):
        store = ArtifactStore(tmp_artifacts_dir)
        lifecycle = ArtifactLifecycle(store=store)
        mock_ssh = AsyncMock()
        mock_proxmox = AsyncMock()
        mock_repo = AsyncMock()
        mock_vault = AsyncMock()

        executor = ArtifactExecutor(
            store=store,
            lifecycle=lifecycle,
            repo=mock_repo,
            proxmox=mock_proxmox,
            agent=mock_ssh,
            vault=mock_vault,
        )

        spec = _make_spec(kind="ansible-playbook", body=ANSIBLE_SPEC_BODY)
        aid = await lifecycle.propose(spec)

        with pytest.raises(ExecutorError, match="expected approved"):
            await executor.apply(aid, approved_by="admin")

    async def test_tamper_detected_raises_tamper_error(self, tmp_artifacts_dir):
        store = ArtifactStore(tmp_artifacts_dir)
        lifecycle = ArtifactLifecycle(store=store)
        mock_ssh = AsyncMock()
        mock_ssh.exec = AsyncMock(return_value=(0, "", ""))
        mock_ssh._validate_guest_only = MagicMock()
        mock_proxmox = AsyncMock()
        mock_repo = AsyncMock()
        mock_vault = AsyncMock()

        executor = ArtifactExecutor(
            store=store,
            lifecycle=lifecycle,
            repo=mock_repo,
            proxmox=mock_proxmox,
            agent=mock_ssh,
            vault=mock_vault,
            pve_nodes=["pve1"],
        )

        spec = _make_spec(kind="ansible-playbook", body=ANSIBLE_SPEC_BODY)
        aid = await lifecycle.propose(spec)
        await lifecycle.approve(aid, user="admin")

        _fm, body = store.read(aid)
        store._storage = {}
        tampered_body = body + "\nTAMPERED"
        path = store.resolve_path(aid)
        path.write_text(
            f"---\nkind: ansible-playbook\nstatus: approved\nhash: fake\n---\n\n{tampered_body}"
        )

        with pytest.raises(TamperError, match="Hash mismatch"):
            await executor.apply(aid, approved_by="admin")

    async def test_non_replay_safe_re_apply_raises(self, tmp_artifacts_dir):
        store = ArtifactStore(tmp_artifacts_dir)
        lifecycle = ArtifactLifecycle(store=store)
        mock_ssh = AsyncMock()
        mock_ssh.exec = AsyncMock(return_value=(0, "", ""))
        mock_ssh._validate_guest_only = MagicMock()
        mock_proxmox = AsyncMock()
        mock_repo = AsyncMock()
        mock_vault = AsyncMock()

        executor = ArtifactExecutor(
            store=store,
            lifecycle=lifecycle,
            repo=mock_repo,
            proxmox=mock_proxmox,
            agent=mock_ssh,
            vault=mock_vault,
            pve_nodes=["pve1"],
        )

        spec = _make_spec(
            kind="ansible-playbook",
            body=ANSIBLE_SPEC_BODY,
            replay_safe=False,
        )
        aid = await lifecycle.propose(spec)
        await lifecycle.approve(aid, user="admin")

        result = await executor.apply(aid, approved_by="admin")
        assert result.success

        with pytest.raises(ExecutorError, match="not replay-safe"):
            await executor.replay(aid)

    async def test_kb_note_apply_is_noop(self, tmp_artifacts_dir):
        store = ArtifactStore(tmp_artifacts_dir)
        lifecycle = ArtifactLifecycle(store=store)
        mock_ssh = AsyncMock()
        mock_proxmox = AsyncMock()
        mock_repo = AsyncMock()
        mock_vault = AsyncMock()

        executor = ArtifactExecutor(
            store=store,
            lifecycle=lifecycle,
            repo=mock_repo,
            proxmox=mock_proxmox,
            agent=mock_ssh,
            vault=mock_vault,
        )

        spec = {
            "id": "2025-01-01-kb-note-abc123",
            "kind": "kb-note",
            "intent": "A note",
            "body": "Some knowledge",
            "produced_by": {"session": "s1", "agent": "a1", "user": "u1"},
        }
        aid = await lifecycle.propose(spec)

        result = await executor.apply(aid, approved_by="admin")
        assert result.success
        assert result.execution_log == "kb-note auto-applied on propose"


class TestOrchestratorSnapshot:
    async def test_vm_target_triggers_snapshot(self, tmp_artifacts_dir):
        store = ArtifactStore(tmp_artifacts_dir)
        lifecycle = ArtifactLifecycle(store=store)
        mock_ssh = AsyncMock()
        mock_ssh.exec = AsyncMock(return_value=(0, "", ""))
        mock_ssh._validate_guest_only = MagicMock()
        mock_proxmox = AsyncMock()
        mock_proxmox.snapshot = AsyncMock(return_value={"data": "snap-hp-pre-test"})
        mock_proxmox.call = AsyncMock(return_value={"data": {}})
        mock_repo = AsyncMock()
        mock_vault = AsyncMock()

        executor = ArtifactExecutor(
            store=store,
            lifecycle=lifecycle,
            repo=mock_repo,
            proxmox=mock_proxmox,
            agent=mock_ssh,
            vault=mock_vault,
            pve_nodes=["pve1"],
        )

        spec = _make_spec(kind="proxmox-api-sequence", body=PROXMOX_SPEC_BODY)
        aid = await lifecycle.propose(spec)
        await lifecycle.approve(aid, user="admin")

        result = await executor.apply(aid, approved_by="admin")
        assert result.success
        assert result.snapshot_id is not None
        mock_proxmox.snapshot.assert_called_once()

    async def test_snapshot_skipped_when_disabled(self, tmp_artifacts_dir):
        store = ArtifactStore(tmp_artifacts_dir)
        lifecycle = ArtifactLifecycle(store=store)
        mock_ssh = AsyncMock()
        mock_ssh.exec = AsyncMock(return_value=(0, "", ""))
        mock_ssh._validate_guest_only = MagicMock()
        mock_proxmox = AsyncMock()
        mock_proxmox.call = AsyncMock(return_value={"data": {}})
        mock_repo = AsyncMock()
        mock_vault = AsyncMock()

        executor = ArtifactExecutor(
            store=store,
            lifecycle=lifecycle,
            repo=mock_repo,
            proxmox=mock_proxmox,
            agent=mock_ssh,
            vault=mock_vault,
            pve_nodes=["pve1"],
        )

        spec = _make_spec(
            kind="proxmox-api-sequence",
            body=PROXMOX_SPEC_BODY,
            requires_snapshot=False,
        )
        aid = await lifecycle.propose(spec)
        await lifecycle.approve(aid, user="admin")

        result = await executor.apply(aid, approved_by="admin")
        assert result.success
        assert result.snapshot_id is None
        mock_proxmox.snapshot.assert_not_called()

    async def test_required_snapshot_failure_aborts_apply(self, tmp_artifacts_dir):
        # Regression for #388: a required pre-apply snapshot that FAILS must
        # abort the apply — never silently proceed to mutate the guest with no
        # rollback. The guest command must not run and the artifact ends 'failed'.
        from homepilot.adapters.proxmox import ProxmoxError

        store = ArtifactStore(tmp_artifacts_dir)
        lifecycle = ArtifactLifecycle(store=store)
        mock_ssh = AsyncMock()
        mock_ssh.exec = AsyncMock(return_value=(0, "", ""))
        mock_ssh._validate_guest_only = MagicMock()
        mock_proxmox = AsyncMock()
        mock_proxmox.snapshot = AsyncMock(
            side_effect=ProxmoxError("POST", "/snap", 500, "snap failed")
        )
        mock_proxmox.call = AsyncMock(return_value={"data": {}})
        mock_repo = AsyncMock()
        mock_vault = AsyncMock()

        executor = ArtifactExecutor(
            store=store,
            lifecycle=lifecycle,
            repo=mock_repo,
            proxmox=mock_proxmox,
            agent=mock_ssh,
            vault=mock_vault,
            pve_nodes=["pve1"],
        )

        # No requires_snapshot key => snapshot is required (default).
        spec = _make_spec(kind="proxmox-api-sequence", body=PROXMOX_SPEC_BODY)
        aid = await lifecycle.propose(spec)
        await lifecycle.approve(aid, user="admin")

        result = await executor.apply(aid, approved_by="admin")
        assert not result.success
        assert "snapshot" in (result.failure_reason or "").lower()
        # The spec steps must NOT have executed without a snapshot.
        mock_proxmox.call.assert_not_called()
        fm, _ = store.read(aid)
        assert fm["status"] == "failed"

    async def test_executor_failure_marks_failed(self, tmp_artifacts_dir):
        store = ArtifactStore(tmp_artifacts_dir)
        lifecycle = ArtifactLifecycle(store=store)
        mock_ssh = AsyncMock()
        mock_ssh.exec = AsyncMock(return_value=(1, "", "error"))
        mock_ssh._validate_guest_only = MagicMock()
        mock_proxmox = AsyncMock()
        mock_repo = AsyncMock()
        mock_vault = AsyncMock()

        executor = ArtifactExecutor(
            store=store,
            lifecycle=lifecycle,
            repo=mock_repo,
            proxmox=mock_proxmox,
            agent=mock_ssh,
            vault=mock_vault,
            pve_nodes=["pve1"],
        )

        spec = _make_spec(kind="ansible-playbook", body=ANSIBLE_SPEC_BODY)
        aid = await lifecycle.propose(spec)
        await lifecycle.approve(aid, user="admin")

        result = await executor.apply(aid, approved_by="admin")
        assert not result.success
        fm, _ = store.read(aid)
        assert fm["status"] == "failed"

    async def test_revoke_with_rollback_runs_rollback(self, tmp_artifacts_dir):
        store_dir = tmp_artifacts_dir / "revoke_test"
        store_dir.mkdir()

        store = ArtifactStore(store_dir)
        lifecycle = ArtifactLifecycle(store=store)
        mock_ssh = AsyncMock()
        mock_ssh.exec = AsyncMock(return_value=(0, "ROLLBACK OK", ""))
        mock_ssh._validate_guest_only = MagicMock()
        mock_proxmox = AsyncMock()
        mock_proxmox.call = AsyncMock(return_value={"data": {}})
        mock_proxmox.snapshot = AsyncMock(return_value={"data": "snap1"})
        mock_repo = AsyncMock()
        mock_vault = AsyncMock()

        executor = ArtifactExecutor(
            store=store,
            lifecycle=lifecycle,
            repo=mock_repo,
            proxmox=mock_proxmox,
            agent=mock_ssh,
            vault=mock_vault,
            pve_nodes=["pve1"],
        )

        rollback_body = """\
## Plan
Create LXC

## Spec

```yaml proxmox-api-spec
steps:
  - id: create-lxc
    method: POST
    path: /nodes/pve1/lxc
    on_error: halt
```

## Rollback

```yaml proxmox-api-rollback
steps:
  - id: delete-lxc
    method: DELETE
    path: /nodes/pve1/lxc/100
    on_error: continue
```
"""
        spec = _make_spec(kind="proxmox-api-sequence", body=rollback_body, rollback=True)
        aid = await lifecycle.propose(spec)
        await lifecycle.approve(aid, user="admin")

        result = await executor.apply(aid, approved_by="admin")
        assert result.success

        await executor.revoke(aid, user="admin", reason="done")
        fm, _ = store.read(aid)
        assert fm["status"] == "revoked"

    async def test_revoke_without_rollback_skips_rollback(self, tmp_artifacts_dir):
        store = ArtifactStore(tmp_artifacts_dir)
        lifecycle = ArtifactLifecycle(store=store)
        mock_ssh = AsyncMock()
        mock_ssh.exec = AsyncMock(return_value=(0, "", ""))
        mock_ssh._validate_guest_only = MagicMock()
        mock_proxmox = AsyncMock()
        mock_proxmox.call = AsyncMock(return_value={"data": {}})
        mock_proxmox.snapshot = AsyncMock(return_value={"data": "snap1"})
        mock_repo = AsyncMock()
        mock_vault = AsyncMock()

        executor = ArtifactExecutor(
            store=store,
            lifecycle=lifecycle,
            repo=mock_repo,
            proxmox=mock_proxmox,
            agent=mock_ssh,
            vault=mock_vault,
            pve_nodes=["pve1"],
        )

        spec = _make_spec(kind="proxmox-api-sequence", body=PROXMOX_SPEC_BODY, rollback=False)
        aid = await lifecycle.propose(spec)
        await lifecycle.approve(aid, user="admin")

        result = await executor.apply(aid, approved_by="admin")
        assert result.success

        mock_proxmox.call.reset_mock()
        await executor.revoke(aid, user="admin")
        mock_proxmox.call.assert_not_called()

    async def test_replay_allowed_for_replay_safe(self, tmp_artifacts_dir):
        store = ArtifactStore(tmp_artifacts_dir)
        lifecycle = ArtifactLifecycle(store=store)
        mock_ssh = AsyncMock()
        mock_ssh.exec = AsyncMock(return_value=(0, "", ""))
        mock_ssh._validate_guest_only = MagicMock()
        mock_proxmox = AsyncMock()
        mock_proxmox.call = AsyncMock(return_value={"data": {}})
        mock_proxmox.snapshot = AsyncMock(return_value={"data": "snap1"})
        mock_repo = AsyncMock()
        mock_vault = AsyncMock()

        executor = ArtifactExecutor(
            store=store,
            lifecycle=lifecycle,
            repo=mock_repo,
            proxmox=mock_proxmox,
            agent=mock_ssh,
            vault=mock_vault,
            pve_nodes=["pve1"],
        )

        spec = _make_spec(
            kind="proxmox-api-sequence",
            body=PROXMOX_SPEC_BODY,
            idempotence="declared-natural",
            replay_safe=True,
        )
        aid = await lifecycle.propose(spec)
        await lifecycle.approve(aid, user="admin")

        result = await executor.apply(aid, approved_by="admin")
        assert result.success

        mock_proxmox.call.reset_mock()
        mock_proxmox.snapshot.reset_mock()
        result2 = await executor.replay(aid)
        assert result2.success

    async def test_replay_blocked_replay_only(self, tmp_artifacts_dir):
        store = ArtifactStore(tmp_artifacts_dir)
        lifecycle = ArtifactLifecycle(store=store)
        mock_ssh = AsyncMock()
        mock_proxmox = AsyncMock()
        mock_repo = AsyncMock()
        mock_vault = AsyncMock()

        executor = ArtifactExecutor(
            store=store,
            lifecycle=lifecycle,
            repo=mock_repo,
            proxmox=mock_proxmox,
            agent=mock_ssh,
            vault=mock_vault,
        )

        spec = _make_spec(
            kind="proxmox-api-sequence",
            body=PROXMOX_SPEC_BODY,
            idempotence="replay-only",
        )
        aid = await lifecycle.propose(spec)
        await lifecycle.approve(aid, user="admin")

        result = await executor.apply(aid, approved_by="admin")
        assert result.success

        with pytest.raises(ExecutorError, match="replay-only"):
            await executor.replay(aid)

    async def test_dispatch_unknown_kind_raises(self, tmp_artifacts_dir):
        with pytest.raises(ValueError):
            ArtifactKind("unknown-kind")

    async def test_replay_of_applied_artifact(self, tmp_artifacts_dir):
        store = ArtifactStore(tmp_artifacts_dir)
        lifecycle = ArtifactLifecycle(store=store)
        mock_ssh = AsyncMock()
        mock_ssh.exec = AsyncMock(return_value=(0, "PLAY OK", ""))
        mock_ssh._validate_guest_only = MagicMock()
        mock_proxmox = AsyncMock()
        mock_repo = AsyncMock()
        mock_vault = AsyncMock()

        executor = ArtifactExecutor(
            store=store,
            lifecycle=lifecycle,
            repo=mock_repo,
            proxmox=mock_proxmox,
            agent=mock_ssh,
            vault=mock_vault,
            pve_nodes=["pve1"],
        )

        spec = _make_spec(
            kind="ansible-playbook",
            body=ANSIBLE_SPEC_BODY,
            idempotence="declared-natural",
            replay_safe=True,
        )
        aid = await lifecycle.propose(spec)
        await lifecycle.approve(aid, user="admin")

        result = await executor.apply(aid, approved_by="admin")
        assert result.success
        fm, _ = store.read(aid)
        assert fm["status"] == "applied"
        assert fm["applied_at"] is not None

        fm, body = store.read(aid)
        fm["status"] = "approved"
        fm.pop("applied_at", None)
        fm_yml = ArtifactFileStore.serialize_frontmatter(fm)
        store.write(aid, fm_yml, body, "reapprove")

        mock_ssh.exec.reset_mock()
        result2 = await executor.replay(aid)
        assert result2.success
        mock_ssh.exec.assert_called()
        fm2, body2 = store.read(aid)
        assert fm2["status"] == "applied"
        assert "Execution log" in body2

    async def test_replay_calls_mark_applied(self, tmp_artifacts_dir):
        store = ArtifactStore(tmp_artifacts_dir)
        lifecycle = ArtifactLifecycle(store=store)
        mock_proxmox = AsyncMock()
        mock_proxmox.call = AsyncMock(return_value={"data": {}})
        mock_proxmox.snapshot = AsyncMock(return_value={"data": "snap1"})
        mock_ssh = AsyncMock()
        mock_repo = AsyncMock()
        mock_vault = AsyncMock()

        executor = ArtifactExecutor(
            store=store,
            lifecycle=lifecycle,
            repo=mock_repo,
            proxmox=mock_proxmox,
            agent=mock_ssh,
            vault=mock_vault,
        )

        spec = _make_spec(
            kind="proxmox-api-sequence",
            body=PROXMOX_SPEC_BODY,
            idempotence="declared-natural",
            replay_safe=True,
        )
        aid = await lifecycle.propose(spec)
        await lifecycle.approve(aid, user="admin")

        result = await executor.apply(aid, approved_by="admin")
        assert result.success
        fm, _ = store.read(aid)
        assert fm["status"] == "applied"

        fm, body = store.read(aid)
        fm["status"] = "approved"
        fm.pop("applied_at", None)
        fm_yml = ArtifactFileStore.serialize_frontmatter(fm)
        store.write(aid, fm_yml, body, "reapprove")

        mock_proxmox.call.reset_mock()
        result2 = await executor.replay(aid)
        assert result2.success
        mock_proxmox.call.assert_called()
        fm2, _ = store.read(aid)
        assert fm2["status"] == "applied"

    async def test_composite_rollback_via_revoke(self, tmp_artifacts_dir):

        composite_dir = tmp_artifacts_dir / "composite_rb"
        composite_dir.mkdir()
        store = ArtifactStore(composite_dir)
        lifecycle = ArtifactLifecycle(store=store)
        mock_ssh = AsyncMock()
        mock_ssh.exec = AsyncMock(return_value=(0, "done", ""))
        mock_ssh._validate_guest_only = MagicMock()
        mock_proxmox = AsyncMock()
        mock_proxmox.call = AsyncMock(return_value={"data": {}})
        mock_proxmox.snapshot = AsyncMock(return_value={"data": "snap1"})
        mock_repo = AsyncMock()
        mock_vault = AsyncMock()

        executor = ArtifactExecutor(
            store=store,
            lifecycle=lifecycle,
            repo=mock_repo,
            proxmox=mock_proxmox,
            agent=mock_ssh,
            vault=mock_vault,
            pve_nodes=["pve1"],
        )

        sub_body = """\
## Plan
Sub step
## Idempotence preamble
Idempotent.
## Spec
```bash shell-spec
#!/bin/bash
echo "sub"
```
"""
        sub_spec = {
            "id": "2025-01-01-sub-shell-rb-abc123",
            "kind": "shell-script",
            "intent": "Sub step",
            "body": sub_body,
            "target": {"kind": "vm", "vmid": 100, "node": "pve1", "host": "web1"},
            "idempotence": "via-precheck",
            "rollback": True,
            "produced_by": {"session": "s1", "agent": "a1", "user": "u1"},
        }
        sub_aid = await lifecycle.propose(sub_spec)
        await lifecycle.approve(sub_aid, user="admin")

        composite_body = f"""\
## Plan
Composite
## Spec
```yaml composite-spec
steps:
  - id: step1
    artifact: {sub_aid}
    on_error: halt
```
"""
        spec = {
            "id": "2025-01-01-composite-rb-abc123",
            "kind": "composite",
            "intent": "Composite rollback",
            "body": composite_body,
            "target": {"kind": "vm", "vmid": 100, "node": "pve1", "host": "web1"},
            "idempotence": "via-precheck",
            "rollback": True,
            "produced_by": {"session": "s1", "agent": "a1", "user": "u1"},
        }
        aid = await lifecycle.propose(spec)
        await lifecycle.approve(aid, user="admin")

        result = await executor.apply(aid, approved_by="admin")
        assert result.success

        await executor.revoke(aid, user="admin", reason="done")
        fm, _ = store.read(aid)
        assert fm["status"] == "revoked"

    async def test_rollback_via_revoke_ansible(self, tmp_artifacts_dir):
        store = ArtifactStore(tmp_artifacts_dir)
        lifecycle = ArtifactLifecycle(store=store)
        mock_ssh = AsyncMock()
        mock_ssh.exec = AsyncMock(return_value=(0, "PLAY OK", ""))
        mock_ssh._validate_guest_only = MagicMock()
        mock_proxmox = AsyncMock()
        mock_repo = AsyncMock()
        mock_vault = AsyncMock()

        executor = ArtifactExecutor(
            store=store,
            lifecycle=lifecycle,
            repo=mock_repo,
            proxmox=mock_proxmox,
            agent=mock_ssh,
            vault=mock_vault,
            pve_nodes=["pve1"],
        )

        rollback_body = """\
## Plan
Install nginx
## Spec
```yaml ansible-spec
- name: Install nginx
  hosts: all
  tasks:
    - name: Install
      apt:
        name: nginx
        state: present
```
## Rollback
```yaml ansible-rollback
- name: Uninstall nginx
  hosts: all
  tasks:
    - name: Uninstall
      apt:
        name: nginx
        state: absent
```
"""
        spec = _make_spec(kind="ansible-playbook", body=rollback_body, rollback=True)
        aid = await lifecycle.propose(spec)
        await lifecycle.approve(aid, user="admin")

        result = await executor.apply(aid, approved_by="admin")
        assert result.success

        mock_ssh.exec.reset_mock()
        await executor.revoke(aid, user="admin", reason="rollback test")
        fm, _ = store.read(aid)
        assert fm["status"] == "revoked"
        mock_ssh.exec.assert_called()
