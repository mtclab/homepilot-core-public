from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from homepilot.executor.http_sequence import execute as http_sequence_execute
from homepilot.vault.manager import VaultError


def _passthrough_interpolate(template_str, context):
    return template_str


def _passthrough_interpolate_obj(obj, context):
    return obj


def _true_skip_if(expression, response, target):
    return True


def _make_response(status_code: int, json_data: dict | None = None) -> httpx.Response:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    if json_data is not None:
        resp.json.return_value = json_data
    else:
        resp.json.return_value = {}
    resp.headers = {}
    resp.text = ""
    return resp


def _mock_async_client(response: httpx.Response, connect_error: bool = False):
    client = MagicMock()
    if connect_error:
        client.request = AsyncMock(side_effect=httpx.ConnectError("connection refused"))
    else:
        client.request = AsyncMock(return_value=response)
    client.aclose = AsyncMock()
    return client


@pytest.fixture
def mock_vault():
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
def make_frontmatter():
    def _factory(**overrides):
        fm = {
            "id": "2025-01-01-http-test-abc123",
            "kind": "http-sequence",
            "intent": "Test HTTP sequence",
        }
        fm.update(overrides)
        return fm

    return _factory


async def test_single_get_step(mock_vault, make_frontmatter):
    fm = make_frontmatter()
    body = """\
## Plan
Single GET

## Spec

```yaml http-spec
steps:
  - id: get-item
    name: svc
    method: GET
    path: /api/test
```
"""
    response = _make_response(200, {"ok": True})
    client = _mock_async_client(response)
    with (
        patch("homepilot.executor.http_sequence.httpx.AsyncClient", return_value=client),
        patch(
            "homepilot.executor.http_sequence._interpolate", side_effect=_passthrough_interpolate
        ),
        patch(
            "homepilot.executor.http_sequence._interpolate_obj",
            side_effect=_passthrough_interpolate_obj,
        ),
    ):
        result = await http_sequence_execute(fm, body, {}, mock_vault)
    assert result["success"] is True
    assert "[get-item]" in result["execution_log"]
    client.request.assert_awaited_once()


async def test_post_step_with_body(mock_vault, make_frontmatter):
    fm = make_frontmatter()
    body = """\
## Plan
POST step

## Spec

```yaml http-spec
steps:
  - id: create-item
    name: svc
    method: POST
    path: /api/items
    body:
      name: test-obj
```
"""
    response = _make_response(201, {"id": 1})
    client = _mock_async_client(response)
    with (
        patch("homepilot.executor.http_sequence.httpx.AsyncClient", return_value=client),
        patch(
            "homepilot.executor.http_sequence._interpolate", side_effect=_passthrough_interpolate
        ),
        patch(
            "homepilot.executor.http_sequence._interpolate_obj",
            side_effect=_passthrough_interpolate_obj,
        ),
    ):
        result = await http_sequence_execute(fm, body, {}, mock_vault)
    assert result["success"] is True
    call_kwargs = client.request.call_args.kwargs
    assert call_kwargs.get("json") == {"name": "test-obj"}


async def test_4xx_halts(mock_vault, make_frontmatter):
    fm = make_frontmatter()
    body = """\
## Plan
404 test

## Spec

```yaml http-spec
steps:
  - id: fetch-missing
    name: svc
    method: GET
    path: /api/missing
    on_error: halt
```
"""
    response = _make_response(404)
    client = _mock_async_client(response)
    with (
        patch("homepilot.executor.http_sequence.httpx.AsyncClient", return_value=client),
        patch(
            "homepilot.executor.http_sequence._interpolate", side_effect=_passthrough_interpolate
        ),
        patch(
            "homepilot.executor.http_sequence._interpolate_obj",
            side_effect=_passthrough_interpolate_obj,
        ),
    ):
        result = await http_sequence_execute(fm, body, {}, mock_vault)
    assert result["success"] is False
    assert "HTTP 404" in result["failure_reason"]


async def test_4xx_continue(mock_vault, make_frontmatter):
    fm = make_frontmatter()
    body = """\
## Plan
404 continue

## Spec

```yaml http-spec
steps:
  - id: fetch-missing
    name: svc
    method: GET
    path: /api/missing
    on_error: continue
```
"""
    response = _make_response(404)
    client = _mock_async_client(response)
    with (
        patch("homepilot.executor.http_sequence.httpx.AsyncClient", return_value=client),
        patch(
            "homepilot.executor.http_sequence._interpolate", side_effect=_passthrough_interpolate
        ),
        patch(
            "homepilot.executor.http_sequence._interpolate_obj",
            side_effect=_passthrough_interpolate_obj,
        ),
    ):
        result = await http_sequence_execute(fm, body, {}, mock_vault)
    # `continue` kept the sequence going and the step is logged, but a 404 is
    # still a step that did not do what it was asked - so the artifact must not
    # come out `applied` over it (#642 B5, review #648).
    assert result["success"] is False
    assert "fetch-missing" in result["failure_reason"]
    assert "404" in result["execution_log"]


async def test_missing_credential_halts(mock_vault, make_frontmatter):
    fm = make_frontmatter()
    body = """\
## Plan
Bad cred

## Spec

```yaml http-spec
steps:
  - id: bad-cred
    name: missing-svc
    method: GET
    path: /api/test
```
"""
    mock_vault.get_secret = AsyncMock(side_effect=VaultError("secret not found"))
    with (
        patch(
            "homepilot.executor.http_sequence._interpolate", side_effect=_passthrough_interpolate
        ),
        patch(
            "homepilot.executor.http_sequence._interpolate_obj",
            side_effect=_passthrough_interpolate_obj,
        ),
    ):
        result = await http_sequence_execute(fm, body, {}, mock_vault)
    assert result["success"] is False


async def test_precheck_skip_if(mock_vault, make_frontmatter):
    fm = make_frontmatter()
    body = """\
## Plan
Skip via precheck

## Spec

```yaml http-spec
steps:
  - id: maybe-create
    name: svc
    method: POST
    path: /api/items/
    body:
      name: homepilot-pve
    precheck:
      name: svc
      method: GET
      path: /api/items/?name=homepilot-pve
      skip_if: 'response.json().get("count", 0) > 0'
    on_error: halt
```
"""
    precheck_resp = _make_response(200, {"count": 1})
    main_resp = _make_response(201, {"id": 99})
    call_count = 0

    async def _request(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return precheck_resp
        return main_resp

    client = MagicMock()
    client.request = AsyncMock(side_effect=_request)
    client.aclose = AsyncMock()
    with (
        patch("homepilot.executor.http_sequence.httpx.AsyncClient", return_value=client),
        patch(
            "homepilot.executor.http_sequence._interpolate", side_effect=_passthrough_interpolate
        ),
        patch(
            "homepilot.executor.http_sequence._interpolate_obj",
            side_effect=_passthrough_interpolate_obj,
        ),
        patch("homepilot.executor.http_sequence._eval_skip_if", side_effect=_true_skip_if),
    ):
        result = await http_sequence_execute(fm, body, {}, mock_vault)
    assert result["success"] is True
    assert "SKIPPED" in result["execution_log"]
    client.request.assert_awaited_once()


async def test_missing_spec_block(mock_vault, make_frontmatter):
    fm = make_frontmatter()
    body = "## Plan\nNo spec here\n"
    result = await http_sequence_execute(fm, body, {}, mock_vault)
    assert result["success"] is False
    assert result["failure_reason"] == "missing spec"


async def test_clients_closed_on_success(mock_vault, make_frontmatter):
    fm = make_frontmatter()
    body = """\
## Plan
Close test

## Spec

```yaml http-spec
steps:
  - id: step-a
    name: svc
    method: GET
    path: /api/a
  - id: step-b
    name: svc
    method: GET
    path: /api/b
```
"""
    response = _make_response(200)
    client = _mock_async_client(response)
    with (
        patch("homepilot.executor.http_sequence.httpx.AsyncClient", return_value=client),
        patch(
            "homepilot.executor.http_sequence._interpolate", side_effect=_passthrough_interpolate
        ),
        patch(
            "homepilot.executor.http_sequence._interpolate_obj",
            side_effect=_passthrough_interpolate_obj,
        ),
    ):
        result = await http_sequence_execute(fm, body, {}, mock_vault)
    assert result["success"] is True
    client.aclose.assert_awaited_once()


async def test_httpx_error_halts(mock_vault, make_frontmatter):
    fm = make_frontmatter()
    body = """\
## Plan
Connect error

## Spec

```yaml http-spec
steps:
  - id: fail-step
    name: svc
    method: GET
    path: /api/test
```
"""
    client = _mock_async_client(None, connect_error=True)
    with (
        patch("homepilot.executor.http_sequence.httpx.AsyncClient", return_value=client),
        patch(
            "homepilot.executor.http_sequence._interpolate", side_effect=_passthrough_interpolate
        ),
        patch(
            "homepilot.executor.http_sequence._interpolate_obj",
            side_effect=_passthrough_interpolate_obj,
        ),
    ):
        result = await http_sequence_execute(fm, body, {}, mock_vault)
    assert result["success"] is False


async def test_rollback_uses_rollback_fence(mock_vault, make_frontmatter):
    fm = make_frontmatter()
    body = """\
## Plan
Rollback test

## Spec

```yaml http-spec
steps:
  - id: create-item
    name: svc
    method: POST
    path: /api/items/
```

```yaml http-rollback
steps:
  - id: delete-item
    name: svc
    method: DELETE
    path: /api/items/1
```
"""
    response = _make_response(200)
    client = _mock_async_client(response)
    with (
        patch("homepilot.executor.http_sequence.httpx.AsyncClient", return_value=client),
        patch(
            "homepilot.executor.http_sequence._interpolate", side_effect=_passthrough_interpolate
        ),
        patch(
            "homepilot.executor.http_sequence._interpolate_obj",
            side_effect=_passthrough_interpolate_obj,
        ),
    ):
        result = await http_sequence_execute(fm, body, {}, mock_vault, rollback=True)
    assert result["success"] is True
    call_kwargs = client.request.call_args.kwargs
    assert call_kwargs.get("method") == "DELETE"


async def test_real_jinja2_interpolation_path(mock_vault, make_frontmatter):
    fm = make_frontmatter()
    body = """\
## Plan
Jinja2 path interpolation

## Spec

```yaml http-spec
steps:
  - id: get-host
    name: svc
    method: GET
    path: /api/hosts/{{ target.host }}
```
"""
    response = _make_response(200, {"ok": True})
    client = _mock_async_client(response)
    target = {"host": "web1"}
    with patch("homepilot.executor.http_sequence.httpx.AsyncClient", return_value=client):
        result = await http_sequence_execute(fm, body, target, mock_vault)
    assert result["success"] is True
    call_args = client.request.call_args
    url_used = call_args.kwargs.get("url", "") or str(call_args)
    assert "web1" in url_used


async def test_real_jinja2_interpolation_body(mock_vault, make_frontmatter):
    fm = make_frontmatter()
    body = """\
## Plan
Jinja2 body interpolation

## Spec

```yaml http-spec
steps:
  - id: create-host
    name: svc
    method: POST
    path: /api/hosts
    body:
      name: "{{ target.host }}"
```
"""
    response = _make_response(201, {"id": 1})
    client = _mock_async_client(response)
    target = {"host": "node42"}
    with patch("homepilot.executor.http_sequence.httpx.AsyncClient", return_value=client):
        result = await http_sequence_execute(fm, body, target, mock_vault)
    assert result["success"] is True
    call_kwargs = client.request.call_args.kwargs
    assert call_kwargs.get("json") == {"name": "node42"}


class TestHttpSequenceIntegration:
    async def test_credential_resolution_per_step(self, mock_vault, make_frontmatter):
        fm = make_frontmatter()
        body = """\
## Plan
Multi cred

## Spec

```yaml http-spec
steps:
  - id: step-a
    name: svc-alpha
    method: GET
    path: /api/a
  - id: step-b
    name: svc-beta
    method: GET
    path: /api/b
```
"""
        response = _make_response(200)
        client = _mock_async_client(response)
        cred_calls = []

        async def _track_get_secret(name):
            cred_calls.append(name)
            return {
                "base_url": f"https://{name}.example.com",
                "headers": {"Authorization": f"Bearer {name}-tok"},
                "verify_tls": False,
            }

        mock_vault.get_secret = AsyncMock(side_effect=_track_get_secret)
        with (
            patch("homepilot.executor.http_sequence.httpx.AsyncClient", return_value=client),
            patch(
                "homepilot.executor.http_sequence._interpolate",
                side_effect=_passthrough_interpolate,
            ),
            patch(
                "homepilot.executor.http_sequence._interpolate_obj",
                side_effect=_passthrough_interpolate_obj,
            ),
        ):
            result = await http_sequence_execute(fm, body, {}, mock_vault)
        assert result["success"] is True
        assert cred_calls == ["svc-alpha", "svc-beta"]

    async def test_credential_missing_on_precheck_halts(self, mock_vault, make_frontmatter):
        fm = make_frontmatter()
        body = """\
## Plan
Precheck bad cred

## Spec

```yaml http-spec
steps:
  - id: step1
    name: svc
    method: POST
    path: /api/items
    precheck:
      name: missing-svc
      method: GET
      path: /api/items/check
```
"""
        call_count = 0

        async def _fail_on_precheck_cred(name):
            nonlocal call_count
            call_count += 1
            if name == "missing-svc":
                raise VaultError("secret not found")
            return {
                "base_url": "https://example.com",
                "headers": {"Authorization": "Bearer tok"},
                "verify_tls": False,
            }

        mock_vault.get_secret = AsyncMock(side_effect=_fail_on_precheck_cred)
        with (
            patch(
                "homepilot.executor.http_sequence._interpolate",
                side_effect=_passthrough_interpolate,
            ),
            patch(
                "homepilot.executor.http_sequence._interpolate_obj",
                side_effect=_passthrough_interpolate_obj,
            ),
        ):
            result = await http_sequence_execute(fm, body, {}, mock_vault)
        assert result["success"] is False

    async def test_interpolation_with_target_context(self, make_frontmatter):
        fm = make_frontmatter()
        body = """\
## Plan
Target interpolation

## Spec

```yaml http-spec
steps:
  - id: get-svc
    name: svc
    method: GET
    path: /api/services/{{ target.service }}/status
```
"""
        response = _make_response(200, {"running": True})
        client = _mock_async_client(response)
        mock_v = AsyncMock()
        mock_v.get_secret = AsyncMock(
            return_value={
                "base_url": "https://example.com",
                "headers": {"Authorization": "Bearer tok"},
                "verify_tls": False,
            }
        )
        with patch("homepilot.executor.http_sequence.httpx.AsyncClient", return_value=client):
            result = await http_sequence_execute(fm, body, {"service": "authentik"}, mock_v)
        assert result["success"] is True
        url = client.request.call_args.kwargs.get("url", "")
        assert "authentik" in url

    async def test_skip_if_false_executes_main_step(self, make_frontmatter):
        fm = make_frontmatter()
        body = """\
## Plan
No skip

## Spec

```yaml http-spec
steps:
  - id: maybe-create
    name: svc
    method: POST
    path: /api/items/
    body:
      name: new-thing
    precheck:
      name: svc
      method: GET
      path: /api/items/?name=new-thing
      skip_if: 'response.json().get("count", 0) > 0'
    on_error: halt
```
"""
        precheck_resp = _make_response(200, {"count": 0})
        main_resp = _make_response(201, {"id": 42})
        call_count = 0

        async def _request(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return precheck_resp
            return main_resp

        mock_v = AsyncMock()
        mock_v.get_secret = AsyncMock(
            return_value={
                "base_url": "https://example.com",
                "headers": {"Authorization": "Bearer tok"},
                "verify_tls": False,
            }
        )
        client = MagicMock()
        client.request = AsyncMock(side_effect=_request)
        client.aclose = AsyncMock()

        def _false_skip_if(expression, response, target):
            return False

        with (
            patch("homepilot.executor.http_sequence.httpx.AsyncClient", return_value=client),
            patch("homepilot.executor.http_sequence._eval_skip_if", side_effect=_false_skip_if),
        ):
            result = await http_sequence_execute(fm, body, {}, mock_v)
        assert result["success"] is True
        assert client.request.call_count == 2

    async def test_client_reuse_same_credential(self, mock_vault, make_frontmatter):
        fm = make_frontmatter()
        body = """\
## Plan
Same cred reuse

## Spec

```yaml http-spec
steps:
  - id: step-a
    name: svc
    method: GET
    path: /api/a
  - id: step-b
    name: svc
    method: GET
    path: /api/b
```
"""
        response = _make_response(200)
        clients_created = []

        def _track_client(**kwargs):
            c = _mock_async_client(response)
            clients_created.append(c)
            return c

        with (
            patch("homepilot.executor.http_sequence.httpx.AsyncClient", side_effect=_track_client),
            patch(
                "homepilot.executor.http_sequence._interpolate",
                side_effect=_passthrough_interpolate,
            ),
            patch(
                "homepilot.executor.http_sequence._interpolate_obj",
                side_effect=_passthrough_interpolate_obj,
            ),
        ):
            result = await http_sequence_execute(fm, body, {}, mock_vault)
        assert result["success"] is True
        assert len(clients_created) == 1

    async def test_rollback_uses_rollback_steps_not_spec(self, mock_vault, make_frontmatter):
        fm = make_frontmatter()
        body = """\
## Plan
Rollback only rollback

## Spec

```yaml http-spec
steps:
  - id: create-item
    name: svc
    method: POST
    path: /api/items/
```

```yaml http-rollback
steps:
  - id: delete-item
    name: svc
    method: DELETE
    path: /api/items/99
```
"""
        response = _make_response(200)
        client = _mock_async_client(response)
        with (
            patch("homepilot.executor.http_sequence.httpx.AsyncClient", return_value=client),
            patch(
                "homepilot.executor.http_sequence._interpolate",
                side_effect=_passthrough_interpolate,
            ),
            patch(
                "homepilot.executor.http_sequence._interpolate_obj",
                side_effect=_passthrough_interpolate_obj,
            ),
        ):
            result = await http_sequence_execute(fm, body, {}, mock_vault, rollback=True)
        assert result["success"] is True
        assert client.request.call_count == 1
        method = client.request.call_args.kwargs.get("method", "")
        assert method == "DELETE"

    async def test_5xx_halts(self, mock_vault, make_frontmatter):
        fm = make_frontmatter()
        body = """\
## Plan
500 test

## Spec

```yaml http-spec
steps:
  - id: server-error
    name: svc
    method: GET
    path: /api/broken
    on_error: halt
```
"""
        response = _make_response(500)
        client = _mock_async_client(response)
        with (
            patch("homepilot.executor.http_sequence.httpx.AsyncClient", return_value=client),
            patch(
                "homepilot.executor.http_sequence._interpolate",
                side_effect=_passthrough_interpolate,
            ),
            patch(
                "homepilot.executor.http_sequence._interpolate_obj",
                side_effect=_passthrough_interpolate_obj,
            ),
        ):
            result = await http_sequence_execute(fm, body, {}, mock_vault)
        assert result["success"] is False
        assert "500" in result["failure_reason"]

    async def test_clients_closed_on_failure(self, mock_vault, make_frontmatter):
        fm = make_frontmatter()
        body = """\
## Plan
Fail then close

## Spec

```yaml http-spec
steps:
  - id: fail-step
    name: svc
    method: GET
    path: /api/fail
    on_error: halt
```
"""
        response = _make_response(500)
        client = _mock_async_client(response)
        with (
            patch("homepilot.executor.http_sequence.httpx.AsyncClient", return_value=client),
            patch(
                "homepilot.executor.http_sequence._interpolate",
                side_effect=_passthrough_interpolate,
            ),
            patch(
                "homepilot.executor.http_sequence._interpolate_obj",
                side_effect=_passthrough_interpolate_obj,
            ),
        ):
            result = await http_sequence_execute(fm, body, {}, mock_vault)
        assert result["success"] is False
        client.aclose.assert_awaited_once()


async def test_halt_on_missing_credential_closes_open_clients(mock_vault, make_frontmatter):
    """#388: every halt path must close the httpx clients it opened.

    Three `halt` returns inside the step loop returned WITHOUT running the
    `for c in client_cache.values(): await c.aclose()` that the other exits did,
    leaking an AsyncClient and its connection pool on every apply whose step
    named a missing vault credential. The loop is now wrapped in try/finally.

    This asserts the OUTCOME - that the client actually got closed - rather than
    that some branch returned a particular dict.

    Teeth: replace the `finally:` in `http_sequence.execute` with a plain close
    after the loop and this fails, because the halt returns skip it.
    """
    fm = make_frontmatter()
    body = """\
## Plan
First step opens a client, second step halts on a missing credential.

## Spec

```yaml http-spec
steps:
  - id: ok-step
    name: svc
    method: GET
    path: /api/test
  - id: bad-cred
    name: missing-svc
    method: GET
    path: /api/other
```
"""
    good_cred = {
        "base_url": "https://example.com",
        "headers": {"Authorization": "Bearer tok"},
        "verify_tls": False,
    }
    # step 1 resolves and opens a client; step 2 raises -> the halt path
    mock_vault.get_secret = AsyncMock(side_effect=[good_cred, VaultError("secret not found")])

    client = _mock_async_client(_make_response(200, {"ok": True}))
    with (
        patch("homepilot.executor.http_sequence.httpx.AsyncClient", return_value=client),
        patch(
            "homepilot.executor.http_sequence._interpolate", side_effect=_passthrough_interpolate
        ),
        patch(
            "homepilot.executor.http_sequence._interpolate_obj",
            side_effect=_passthrough_interpolate_obj,
        ),
    ):
        result = await http_sequence_execute(fm, body, {}, mock_vault)

    assert result["success"] is False
    assert client.aclose.await_count >= 1, (
        "halt path returned without closing the httpx.AsyncClient it opened (#388)"
    )
