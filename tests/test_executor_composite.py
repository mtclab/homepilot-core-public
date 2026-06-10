from unittest.mock import AsyncMock, MagicMock

import pytest

from homepilot.executor.composite import execute as composite_execute


@pytest.fixture
def mock_vault():
    from unittest.mock import AsyncMock

    vault = AsyncMock()
    vault.get_secret = AsyncMock(
        return_value={
            "base_url": "https://example.com",
            "headers": {"Authorization": "Bearer tok"},
            "verify_tls": False,
        }
    )
    return vault


@pytest.fixture
def mock_lifecycle():
    lc = MagicMock()
    lc.store = MagicMock()
    return lc


@pytest.fixture
def make_frontmatter():
    def _factory(**overrides):
        fm = {
            "id": "2025-01-01-composite-test-abc123",
            "kind": "composite",
            "intent": "Test composite",
        }
        fm.update(overrides)
        return fm

    return _factory


def _make_store():
    store = MagicMock()
    store.read = MagicMock(return_value=({"status": "proposed"}, "body"))
    return store


def _make_executor(store=None, apply_return=None, revoke_side_effect=None):
    executor = MagicMock()
    executor.store = store or _make_store()
    if apply_return is not None:
        executor.apply = AsyncMock(return_value=apply_return)
    else:
        executor.apply = AsyncMock(return_value={"success": True})
    executor.revoke = AsyncMock(side_effect=revoke_side_effect)
    return executor


BASIC_BODY = """\
## Plan
Full deploy

## Spec

```yaml composite-spec
steps:
  - id: provision
    artifact: 2025-01-01-sub-provision-abc123
    on_error: halt

  - id: configure
    artifact: 2025-01-01-sub-configure-def456
    depends_on: [provision]
    on_error: halt
```
"""


async def test_applies_sub_artifacts_in_order(mock_lifecycle, make_frontmatter):
    fm = make_frontmatter()
    apply_order = []

    async def _track_apply(artifact_id, **kwargs):
        apply_order.append(artifact_id)
        return {"success": True}

    executor = _make_executor()
    executor.apply = AsyncMock(side_effect=_track_apply)
    result = await composite_execute(fm, BASIC_BODY, mock_lifecycle, executor)
    assert result["success"] is True
    assert apply_order == [
        "2025-01-01-sub-provision-abc123",
        "2025-01-01-sub-configure-def456",
    ]


async def test_topological_sort_respects_depends_on(mock_lifecycle, make_frontmatter):
    fm = make_frontmatter()
    body = """\
## Plan
Topo sort

## Spec

```yaml composite-spec
steps:
  - id: step-b
    artifact: 2025-01-01-sub-b-aaa111
    depends_on: [step-a]
    on_error: halt

  - id: step-a
    artifact: 2025-01-01-sub-a-bbb222
    on_error: halt
```
"""
    apply_order = []

    async def _track_apply(artifact_id, **kwargs):
        apply_order.append(artifact_id)
        return {"success": True}

    executor = _make_executor()
    executor.apply = AsyncMock(side_effect=_track_apply)
    result = await composite_execute(fm, body, mock_lifecycle, executor)
    assert result["success"] is True
    assert apply_order.index("2025-01-01-sub-a-bbb222") < apply_order.index(
        "2025-01-01-sub-b-aaa111"
    )


async def test_circular_dependency_detected(mock_lifecycle, make_frontmatter):
    fm = make_frontmatter()
    body = """\
## Plan
Circular

## Spec

```yaml composite-spec
steps:
  - id: step-a
    artifact: 2025-01-01-sub-a-aaa111
    depends_on: [step-b]
    on_error: halt

  - id: step-b
    artifact: 2025-01-01-sub-b-bbb222
    depends_on: [step-a]
    on_error: halt
```
"""
    executor = _make_executor()
    result = await composite_execute(fm, body, mock_lifecycle, executor)
    assert result["success"] is False
    assert "Circular" in result["execution_log"]


async def test_sub_artifact_failure_halts(mock_lifecycle, make_frontmatter):
    fm = make_frontmatter()
    body = """\
## Plan
Failure halt

## Spec

```yaml composite-spec
steps:
  - id: step-a
    artifact: 2025-01-01-sub-a-aaa111
    on_error: halt

  - id: step-b
    artifact: 2025-01-01-sub-b-bbb222
    on_error: halt

  - id: step-c
    artifact: 2025-01-01-sub-c-ccc333
    on_error: halt
```
"""
    call_count = 0

    async def _fail_second(artifact_id, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            return {"success": False, "failure_reason": "bad"}
        return {"success": True}

    executor = _make_executor()
    executor.apply = AsyncMock(side_effect=_fail_second)
    result = await composite_execute(fm, body, mock_lifecycle, executor)
    assert result["success"] is False
    assert call_count == 2


async def test_sub_artifact_failure_continue(mock_lifecycle, make_frontmatter):
    fm = make_frontmatter()
    body = """\
## Plan
Failure continue

## Spec

```yaml composite-spec
steps:
  - id: step-a
    artifact: 2025-01-01-sub-a-aaa111
    on_error: continue

  - id: step-b
    artifact: 2025-01-01-sub-b-bbb222
    on_error: halt
```
"""
    call_count = 0

    async def _fail_first(artifact_id, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return {"success": False, "failure_reason": "bad"}
        return {"success": True}

    executor = _make_executor()
    executor.apply = AsyncMock(side_effect=_fail_first)
    result = await composite_execute(fm, body, mock_lifecycle, executor)
    assert call_count == 2
    assert "FAILED" in result["execution_log"]
    assert "step-b" in result["execution_log"]


async def test_already_applied_sub_skipped(mock_lifecycle, make_frontmatter):
    fm = make_frontmatter()
    mock_store = MagicMock()

    def _read(artifact_id):
        return ({"status": "applied"}, "body")

    mock_store.read = _read
    executor = _make_executor(store=mock_store)
    result = await composite_execute(fm, BASIC_BODY, mock_lifecycle, executor)
    assert "already applied" in result["execution_log"]
    executor.apply.assert_not_awaited()


async def test_missing_sub_artifact_halts(mock_lifecycle, make_frontmatter):
    fm = make_frontmatter()
    mock_store = MagicMock()
    mock_store.read = MagicMock(side_effect=FileNotFoundError("not found"))
    executor = _make_executor(store=mock_store)
    result = await composite_execute(fm, BASIC_BODY, mock_lifecycle, executor)
    assert result["success"] is False
    assert "not found" in result["failure_reason"]


async def test_missing_spec_block(mock_lifecycle, make_frontmatter):
    fm = make_frontmatter()
    body = "## Plan\nNo spec\n"
    executor = _make_executor()
    result = await composite_execute(fm, body, mock_lifecycle, executor)
    assert result["success"] is False
    assert result["failure_reason"] == "missing spec"


async def test_rollback_revokes_in_reverse(mock_lifecycle, make_frontmatter):
    fm = make_frontmatter()
    mock_store = MagicMock()

    def _read(artifact_id):
        return ({"status": "applied"}, "body")

    mock_store.read = _read
    executor = _make_executor(store=mock_store)
    result = await composite_execute(fm, BASIC_BODY, mock_lifecycle, executor, rollback=True)
    assert result["success"] is True
    revoke_calls = [call.args[0] for call in executor.revoke.call_args_list]
    assert revoke_calls == [
        "2025-01-01-sub-configure-def456",
        "2025-01-01-sub-provision-abc123",
    ]


async def test_step_missing_artifact_ref_skipped(mock_lifecycle, make_frontmatter):
    fm = make_frontmatter()
    body = """\
## Plan
Missing ref

## Spec

```yaml composite-spec
steps:
  - id: no-ref
    on_error: halt
```
"""
    executor = _make_executor()
    result = await composite_execute(fm, body, mock_lifecycle, executor)
    assert "missing artifact reference" in result["execution_log"]


async def test_on_error_continue_sets_failed_flag(mock_lifecycle, make_frontmatter):
    fm = make_frontmatter()
    body = """\
## Plan
Continue on error

## Spec

```yaml composite-spec
steps:
  - id: step-a
    artifact: 2025-01-01-sub-a-aaa111
    on_error: continue

  - id: step-b
    artifact: 2025-01-01-sub-b-bbb222
    on_error: halt
```
"""
    call_count = 0

    async def _fail_first(artifact_id, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("sub-artifact exploded")
        return {"success": True}

    executor = _make_executor()
    executor.apply = AsyncMock(side_effect=_fail_first)
    result = await composite_execute(fm, body, mock_lifecycle, executor)
    assert result["success"] is False
    assert "ERROR" in result["execution_log"]
