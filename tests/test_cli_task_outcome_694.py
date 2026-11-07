"""`hp artifacts apply|replay|revoke` report the task's REAL outcome (#694).

WHAT THIS FORBIDS, all three observed on the shipped CLI:

* an apply that runs longer than the CLI's 15 s HTTP timeout ended in an
  unhandled ``httpx.ReadTimeout`` traceback and exit 1 - while the apply carried
  on in the backend, so the operator was told it crashed when it had not;
* a task still ``running`` when the server's ``?sync=true`` wait gave up came
  back as a 200, and the CLI treated anything that was not failed/cancelled as
  success: it printed "Applied: <id>" for a change that had not finished;
* ``await_task`` did not count ``cancelled`` as terminal, so a cancelled task
  held a sync caller for the full 300 s.

The contract now: the CLI starts the task, then follows ``GET /tasks/{id}``.
Only ``succeeded`` is success (exit 0). ``failed``/``cancelled`` are failures
(exit 1). A task still going at the deadline, or a backend that stops
answering, is "outcome unknown" (exit 3) - never success, never a traceback.

The backend here is a fake that speaks the real routes' shapes; the command is
driven through Typer exactly as an operator runs it.

Teeth: restore the `?sync=true` single request (or the "anything not failed is
success" rule) and the unknown/timeout tests fail; drop `cancelled` from
``await_task``'s terminal set and the runner test waits out its budget.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from homepilot.cli import main as cli

AID = "2026-09-26-audit-cli-abc123"


# What the real routes demand (require_scope on each): starting a task is a
# write, following it is a READ. A "write" token does not include "read", so a
# CLI that minted only "write" could start an apply and then never see it end.
_ROUTE_SCOPE = {"tasks": "read", "start": "write"}


class FakeBackend:
    """Answers the task-start routes with 202 + task id, then a status script.

    It enforces the same scopes the real routes do, with the real
    ``normalize_scope``, against the scope the CLI minted."""

    def __init__(self, statuses: list[Any]) -> None:
        self.statuses = list(statuses)
        self.requests: list[tuple[str, str]] = []
        self.minted_scope = ""

    def _allowed(self, need: str) -> bool:
        from homepilot.auth.tokens import normalize_scope

        granted = normalize_scope(self.minted_scope)
        return "*" in granted or need in granted

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append((request.method, str(request.url)))
        kind = "tasks" if path.startswith("/tasks/") else "start"
        if not path.startswith("/auth/") and not self._allowed(_ROUTE_SCOPE[kind]):
            return httpx.Response(403, json={"detail": f"requires '{_ROUTE_SCOPE[kind]}'"})
        if request.method == "DELETE" and path.startswith("/auth/tokens/"):
            return httpx.Response(204)
        if path.startswith("/tasks/"):
            step = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
            if isinstance(step, Exception):
                raise step
            body: dict[str, Any] = {"id": "t-1", "artifact_id": AID, "status": step}
            if step == "failed":
                body["error"] = "playbook exited 2"
            return httpx.Response(200, json=body)
        if path.endswith("/apply") or path.endswith("/replay") or request.method == "DELETE":
            if "sync=true" in str(request.url):
                raise httpx.ReadTimeout("the server is waiting on the apply")
            return httpx.Response(
                202, json={"task_id": "t-1", "artifact_id": AID, "status": "pending"}
            )
        return httpx.Response(404, json={"detail": "not found"})


@pytest.fixture
def backend(monkeypatch):
    def install(statuses: list[Any]) -> FakeBackend:
        fake = FakeBackend(statuses)

        async def mint(_settings: Any, _label: str, scope: str, *_a: Any, **_k: Any) -> Any:
            fake.minted_scope = scope
            return "hp_" + "a" * 64, None

        def client(timeout: float) -> httpx.AsyncClient:
            return httpx.AsyncClient(
                transport=httpx.MockTransport(fake.handler), base_url="http://127.0.0.1"
            )

        monkeypatch.setattr(cli, "_mint_token_via_api", mint)
        monkeypatch.setattr(cli, "_backend_http_client", client)
        monkeypatch.setattr(cli, "_TASK_POLL_INTERVAL", 0.0)
        return fake

    return install


def _run(*args: str) -> Any:
    return CliRunner().invoke(cli.app, ["artifacts", *args])


def test_a_long_apply_is_followed_to_success(backend):
    fake = backend(["pending", "running", "running", "running", "succeeded"])

    r = _run("apply", AID)

    assert r.exit_code == 0, r.output
    assert f"Applied: {AID}" in r.output
    assert not any("sync=true" in url for _m, url in fake.requests), (
        "the CLI still holds one HTTP request open for the whole apply"
    )
    assert sum(1 for _m, url in fake.requests if "/tasks/t-1" in url) >= 5


def test_a_task_still_running_at_the_deadline_is_not_success(backend, monkeypatch):
    backend(["running"])
    monkeypatch.setattr(cli, "_TASK_WAIT_SECONDS", 0.0)

    r = _run("apply", AID)

    assert r.exit_code == cli.EXIT_OUTCOME_UNKNOWN, r.output
    assert "Applied:" not in r.output
    assert "outcome" in r.output.lower() and "t-1" in r.output


def test_a_backend_that_stops_answering_is_outcome_unknown_not_a_traceback(backend, monkeypatch):
    backend([httpx.ReadTimeout("gone")])
    monkeypatch.setattr(cli, "_TASK_WAIT_SECONDS", 0.0)

    r = _run("apply", AID)

    assert r.exit_code == cli.EXIT_OUTCOME_UNKNOWN, r.output
    assert not isinstance(r.exception, httpx.HTTPError), "an httpx error escaped as a crash"
    assert "Applied:" not in r.output


def test_one_poll_blip_does_not_end_the_wait(backend):
    backend([httpx.ReadTimeout("blip"), "running", "succeeded"])

    r = _run("apply", AID)

    assert r.exit_code == 0, r.output
    assert f"Applied: {AID}" in r.output


def test_a_failed_task_fails_the_command(backend):
    backend(["running", "failed"])

    r = _run("apply", AID)

    assert r.exit_code == 1
    assert "playbook exited 2" in r.output


def test_a_cancelled_task_is_terminal_and_a_failure(backend):
    backend(["running", "cancelled"])

    r = _run("replay", AID, "--yes")

    assert r.exit_code == 1
    assert "cancelled" in r.output.lower()
    assert "Replayed:" not in r.output


def test_an_unknown_status_is_not_success(backend, monkeypatch):
    backend(["exploded"])
    monkeypatch.setattr(cli, "_TASK_WAIT_SECONDS", 0.0)

    r = _run("apply", AID)

    assert r.exit_code != 0
    assert "Applied:" not in r.output


def test_revoke_follows_its_task_too(backend):
    fake = backend(["running", "succeeded"])

    r = _run("revoke", AID)

    assert r.exit_code == 0, r.output
    assert f"Revoked: {AID}" in r.output
    assert any(m == "DELETE" and f"/artifacts/{AID}" in u for m, u in fake.requests)


class _Repo:
    def __init__(self, status: str) -> None:
        self.status = status
        self.reads = 0

    async def get_task(self, task_id: str) -> dict[str, Any]:
        self.reads += 1
        return {"id": task_id, "status": self.status}


async def test_await_task_returns_at_once_for_a_cancelled_task():
    from homepilot.tasks.runner import TaskRunner

    runner = TaskRunner.__new__(TaskRunner)
    runner.repo = _Repo("cancelled")  # type: ignore[attr-defined]

    task = await asyncio.wait_for(runner.await_task("t-1", timeout=30.0), timeout=5.0)

    assert task["status"] == "cancelled"
    assert runner.repo.reads == 1  # type: ignore[attr-defined]
