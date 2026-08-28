from unittest.mock import AsyncMock

import pytest

from homepilot.adapters.proxmox import ProxmoxError
from homepilot.executor.proxmox_api import execute as proxmox_api_execute

SINGLE_GET_BODY = """\
## Plan
Check nodes

## Spec

```yaml proxmox-api-spec
steps:
  - id: step1
    method: GET
    path: /nodes
```
"""

MULTI_STEP_BODY = """\
## Plan
Create and update

## Spec

```yaml proxmox-api-spec
steps:
  - id: step1
    method: POST
    path: /nodes/pve1/lxc
    body:
      vmid: 100
  - id: step2
    method: PUT
    path: /nodes/pve1/lxc/100/config
    body:
      cores: 2
```
"""

HALT_ON_ERROR_BODY = """\
```yaml proxmox-api-spec
steps:
  - id: step1
    method: GET
    path: /nodes
  - id: step2
    method: POST
    path: /fail
    on_error: halt
```
"""

CONTINUE_ON_ERROR_BODY = """\
```yaml proxmox-api-spec
steps:
  - id: step1
    method: GET
    path: /nodes
  - id: step2
    method: POST
    path: /fail
    on_error: continue
  - id: step3
    method: GET
    path: /nodes/pve1/status
```
"""

PRECHECK_SKIP_BODY = """\
```yaml proxmox-api-spec
steps:
  - id: step1
    method: POST
    path: /nodes/pve1/lxc/100/start
    precheck:
      method: GET
      path: /nodes/pve1/lxc/100/status/current
      skip_if: 'response["data"]["status"] == "running"'
```
"""

PRECHECK_UNREACHABLE_BODY = """\
```yaml proxmox-api-spec
steps:
  - id: step1
    method: GET
    path: /x
    precheck:
      method: GET
      path: /x
      skip_if: "response.status_code == 200"
```
"""

CLUSTER_TARGET_BODY = """\
```yaml proxmox-api-spec
steps:
  - id: step1
    method: GET
    path: /nodes/{{ target.node }}/status
```
"""

JINJA2_BODY = """\
```yaml proxmox-api-spec
steps:
  - id: step1
    method: GET
    path: /nodes/{{ target.node }}/status
```
"""

ROLLBACK_BODY = """\
## Spec

```yaml proxmox-api-spec
steps:
  - id: step1
    method: GET
    path: /nodes
```

## Rollback

```yaml proxmox-api-rollback
steps:
  - id: rollback1
    method: DELETE
    path: /nodes/pve1/lxc/100
```
"""

NO_SPEC_BODY = """\
## Plan
No spec here at all.
"""


@pytest.fixture
def fm():
    return {"id": "test-1", "intent": "test"}


async def test_single_step_get(mock_proxmox, fm):
    mock_proxmox.call.return_value = {"data": {}}
    result = await proxmox_api_execute(fm, SINGLE_GET_BODY, {}, mock_proxmox)
    assert result["success"] is True
    assert "[step1] GET /nodes -> OK" in result["execution_log"]


async def test_multi_step_post_put(mock_proxmox, fm):
    mock_proxmox.call.return_value = {}
    result = await proxmox_api_execute(fm, MULTI_STEP_BODY, {}, mock_proxmox)
    assert result["success"] is True
    assert mock_proxmox.call.call_count == 2


async def test_step_failure_halts(mock_proxmox, fm):
    call_count = 0

    async def _call(method, path, body=None, query=None):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise ProxmoxError("POST", "/fail", 500, "err")
        return {"data": {}}

    mock_proxmox.call.side_effect = _call
    result = await proxmox_api_execute(fm, HALT_ON_ERROR_BODY, {}, mock_proxmox)
    assert result["success"] is False
    assert "step2" in result["failure_reason"]
    assert mock_proxmox.call.call_count == 2


async def test_step_failure_continue(mock_proxmox, fm):
    call_count = 0

    async def _call(method, path, body=None, query=None):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise ProxmoxError("POST", "/fail", 500, "err")
        return {"data": {}}

    mock_proxmox.call.side_effect = _call
    result = await proxmox_api_execute(fm, CONTINUE_ON_ERROR_BODY, {}, mock_proxmox)
    assert result["success"] is True


async def test_precheck_skip_if(mock_proxmox, fm):
    mock_proxmox.call.return_value = {"data": {"status": "running"}}
    result = await proxmox_api_execute(fm, PRECHECK_SKIP_BODY, {}, mock_proxmox)
    assert result["success"] is True
    assert "SKIPPED" in result["execution_log"]


async def test_precheck_unreachable_halts(mock_proxmox, fm):
    mock_proxmox.call.side_effect = ProxmoxError("GET", "/x", 0, "unreachable")
    result = await proxmox_api_execute(fm, PRECHECK_UNREACHABLE_BODY, {}, mock_proxmox)
    assert result["success"] is False
    assert "precheck unreachable" in result["failure_reason"]


async def test_cluster_target_picks_node(mock_proxmox, fm):
    mock_proxmox.read.return_value = {"data": [{"node": "pve2", "status": "online"}]}
    mock_proxmox.call.return_value = {"data": {}}
    target = {"kind": "cluster"}
    result = await proxmox_api_execute(fm, CLUSTER_TARGET_BODY, target, mock_proxmox)
    assert result["success"] is True
    call_args = mock_proxmox.call.call_args
    assert "pve2" in call_args[0][1] or "pve2" in str(call_args)


async def test_missing_spec_block(mock_proxmox, fm):
    result = await proxmox_api_execute(fm, NO_SPEC_BODY, {}, mock_proxmox)
    assert result["success"] is False
    assert result["failure_reason"] == "missing spec"


async def test_jinja2_interpolation(mock_proxmox, fm):
    mock_proxmox.call.return_value = {"data": {}}
    target = {"node": "pve1"}
    result = await proxmox_api_execute(fm, JINJA2_BODY, target, mock_proxmox)
    assert result["success"] is True
    call_args = mock_proxmox.call.call_args
    assert "/nodes/pve1/status" in str(call_args)


async def test_rollback_uses_rollback_fence(mock_proxmox, fm):
    mock_proxmox.call.return_value = {"data": {}}
    result = await proxmox_api_execute(fm, ROLLBACK_BODY, {}, mock_proxmox, rollback=True)
    assert result["success"] is True


async def test_pick_cluster_node_proxmox_error(mock_proxmox, fm):
    mock_proxmox.call.return_value = {"data": {}}
    mock_proxmox.read = AsyncMock(side_effect=ProxmoxError("GET", "/nodes", 500, "cluster error"))
    target = {"kind": "cluster"}
    result = await proxmox_api_execute(fm, CLUSTER_TARGET_BODY, target, mock_proxmox)
    assert result["success"] is True


async def test_pick_cluster_node_empty_nodes(mock_proxmox, fm):
    mock_proxmox.call.return_value = {"data": {}}
    mock_proxmox.read.return_value = {"data": []}
    target = {"kind": "cluster"}
    result = await proxmox_api_execute(fm, CLUSTER_TARGET_BODY, target, mock_proxmox)
    assert result["success"] is True


class TestProxmoxApiIntegration:
    async def test_precheck_idempotence_passes_then_executes(self, mock_proxmox, fm):
        body = """\
```yaml proxmox-api-spec
steps:
  - id: start-vm
    method: POST
    path: /nodes/pve1/lxc/100/start
    precheck:
      method: GET
      path: /nodes/pve1/lxc/100/status/current
      skip_if: 'response["data"]["status"] == "running"'
```
"""
        call_count = 0

        async def _call(method, path, body=None, query=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return {"data": {"status": "stopped"}}
            return {"data": {}}

        mock_proxmox.call.side_effect = _call
        result = await proxmox_api_execute(fm, body, {}, mock_proxmox)
        assert result["success"] is True
        assert mock_proxmox.call.call_count == 2

    async def test_step_execution_sends_body_for_mutating_methods(self, mock_proxmox, fm):
        mock_proxmox.call.return_value = {}
        result = await proxmox_api_execute(fm, MULTI_STEP_BODY, {}, mock_proxmox)
        assert result["success"] is True
        first_call = mock_proxmox.call.call_args_list[0]
        assert first_call.kwargs.get("body") == {"vmid": 100} or first_call[1].get("body") == {
            "vmid": 100
        }

    async def test_rollback_deletes_created_resource(self, mock_proxmox, fm):
        mock_proxmox.call.return_value = {"data": {}}
        result = await proxmox_api_execute(fm, ROLLBACK_BODY, {}, mock_proxmox, rollback=True)
        assert result["success"] is True
        call_method = mock_proxmox.call.call_args[0][0]
        call_path = mock_proxmox.call.call_args[0][1]
        assert call_method == "DELETE"
        assert "/nodes/pve1/lxc/100" in call_path

    async def test_rollback_steps_run_in_order(self, mock_proxmox, fm):
        body = """\
## Spec

```yaml proxmox-api-spec
steps:
  - id: s1
    method: GET
    path: /nodes
```

## Rollback

```yaml proxmox-api-rollback
steps:
  - id: rb1
    method: DELETE
    path: /nodes/pve1/lxc/100
  - id: rb2
    method: DELETE
    path: /nodes/pve1/lxc/101
```
"""
        mock_proxmox.call.return_value = {"data": {}}
        result = await proxmox_api_execute(fm, body, {}, mock_proxmox, rollback=True)
        assert result["success"] is True
        calls = [(c[0][0], c[0][1]) for c in mock_proxmox.call.call_args_list]
        assert calls[0] == ("DELETE", "/nodes/pve1/lxc/100")
        assert calls[1] == ("DELETE", "/nodes/pve1/lxc/101")

    async def test_cluster_target_fallback_picks_first_node(self, mock_proxmox, fm):
        mock_proxmox.read.return_value = {
            "data": [{"node": "pve3", "status": "online"}, {"node": "pve4", "status": "online"}]
        }
        mock_proxmox.call.return_value = {"data": {}}
        target = {"kind": "cluster"}
        result = await proxmox_api_execute(fm, CLUSTER_TARGET_BODY, target, mock_proxmox)
        assert result["success"] is True
        used_path = mock_proxmox.call.call_args[0][1]
        assert "pve3" in used_path

    async def test_cluster_target_no_online_node_still_succeeds(self, mock_proxmox, fm):
        mock_proxmox.read.return_value = {"data": [{"node": "pve5"}]}
        mock_proxmox.call.return_value = {"data": {}}
        target = {"kind": "cluster"}
        result = await proxmox_api_execute(fm, CLUSTER_TARGET_BODY, target, mock_proxmox)
        assert result["success"] is True

    async def test_execution_log_tracks_applied_steps(self, mock_proxmox, fm):
        mock_proxmox.call.return_value = {"data": {}}
        result = await proxmox_api_execute(fm, MULTI_STEP_BODY, {}, mock_proxmox)
        assert result["success"] is True
        assert "[step1]" in result["execution_log"]
        assert "[step2]" in result["execution_log"]

    async def test_precheck_skip_then_main_step_skipped(self, mock_proxmox, fm):
        mock_proxmox.call.return_value = {"data": {"status": "running"}}
        result = await proxmox_api_execute(fm, PRECHECK_SKIP_BODY, {}, mock_proxmox)
        assert result["success"] is True
        assert "[step1]" in result["execution_log"]
        assert "SKIPPED" in result["execution_log"]
        assert mock_proxmox.call.call_count == 1


# --- The steps must not race the cluster (#629/#626) -------------------------
#
# PVE answers a stop, a clone or a destroy with a UPID the instant it ACCEPTS
# the work. The executor logged "-> OK" on that acceptance and went straight to
# the next step, so a sequence outran its own cluster: on dev the stop step
# reported OK and the destroy behind it got back "VM 101 is running - destroy
# failed", leaving the guest up. It also called an asynchronously-failed task a
# success. These assert the OUTCOME an operator gets, not that execute()
# returned.

STOP_THEN_DESTROY_BODY = """\
```yaml proxmox-api-spec
steps:
  - id: stop-101
    method: POST
    path: /nodes/pve/qemu/101/status/stop
  - id: destroy-101
    method: DELETE
    path: /nodes/pve/qemu/101
```
"""

STOP_UPID = "UPID:pve:00001234:0000ABCD:66000000:qmstop:101:root@pam:"


async def test_a_step_waits_for_the_task_it_spawned_before_the_next_one_runs(mock_proxmox, fm):
    """The destroy must not be issued until the stop task has actually finished."""
    order: list[str] = []

    async def call(method, path, body=None):
        order.append(f"call {method} {path}")
        if path.endswith("/status/stop"):
            return {"data": STOP_UPID}
        return {"data": None}

    async def wait_for_task(node, upid, **kwargs):
        order.append(f"wait {node} {upid}")
        return {"status": "stopped", "exitstatus": "OK"}

    mock_proxmox.call = AsyncMock(side_effect=call)
    mock_proxmox.wait_for_task = AsyncMock(side_effect=wait_for_task)

    result = await proxmox_api_execute(fm, STOP_THEN_DESTROY_BODY, {}, mock_proxmox)

    assert result["success"] is True
    assert order == [
        "call POST /nodes/pve/qemu/101/status/stop",
        f"wait pve {STOP_UPID}",
        "call DELETE /nodes/pve/qemu/101",
    ], "the destroy was issued before the stop task finished"


async def test_a_task_that_fails_asynchronously_fails_the_step(mock_proxmox, fm):
    """Acceptance is not success: a task that dies must not be logged as OK."""

    async def call(method, path, body=None):
        return {"data": STOP_UPID} if path.endswith("/status/stop") else {"data": None}

    mock_proxmox.call = AsyncMock(side_effect=call)
    mock_proxmox.wait_for_task = AsyncMock(
        side_effect=ProxmoxError("GET", "/tasks", 0, "exitstatus 'shutdown timeout'")
    )

    result = await proxmox_api_execute(fm, STOP_THEN_DESTROY_BODY, {}, mock_proxmox)

    assert result["success"] is False
    assert "stop-101" in result["failure_reason"]
    # The step behind the dead task must never have been attempted.
    assert all(c.args[1] != "/nodes/pve/qemu/101" for c in mock_proxmox.call.call_args_list), (
        "the destroy ran behind a stop that never finished"
    )


async def test_the_node_comes_from_the_upid_not_the_target(mock_proxmox, fm):
    """A step may address a node the target does not name; the UPID always does."""
    seen: list[str] = []

    async def call(method, path, body=None):
        return {"data": STOP_UPID} if path.endswith("/status/stop") else {"data": None}

    async def wait_for_task(node, upid, **kwargs):
        seen.append(node)
        return {"status": "stopped", "exitstatus": "OK"}

    mock_proxmox.call = AsyncMock(side_effect=call)
    mock_proxmox.wait_for_task = AsyncMock(side_effect=wait_for_task)

    await proxmox_api_execute(fm, STOP_THEN_DESTROY_BODY, {"node": "other-node"}, mock_proxmox)

    assert seen == ["pve"]


async def test_a_synchronous_call_is_not_waited_on(mock_proxmox, fm):
    """A config write answers with null - there is no task, and no hang."""
    mock_proxmox.call = AsyncMock(return_value={"data": None})
    mock_proxmox.wait_for_task = AsyncMock()

    result = await proxmox_api_execute(fm, MULTI_STEP_BODY, {}, mock_proxmox)

    assert result["success"] is True
    mock_proxmox.wait_for_task.assert_not_called()
