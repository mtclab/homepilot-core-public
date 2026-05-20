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
