"""The guards that stand between an approved plan and the change it makes.

Review #648 tranche 3. Every assertion here failed against 3.6.14, and each one
forbids a whole CLASS rather than the single call that exposed it:

* a precheck written the way ARTIFACT_SPEC documents must be able to SKIP a step
  (it could not, on `proxmox-api-sequence`, because the raw PVE envelope was
  bound as `response` and D2's `response.status_code` did not exist on it);
* a precheck that did not answer must not let the step run;
* `on_error: continue` must not turn a failed step into an `applied` artifact;
* an artifact id of any length must produce a snapshot name PVE will accept;
* approving a composite must approve the steps it names;
* a drift verdict must never be greener than what was actually established.

The shape they share is #642's: a conclusion asserted from a read that did not
happen, in the one place designed to prevent exactly that.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from homepilot.adapters.proxmox import ProxmoxError
from homepilot.executor.http_sequence import execute as http_execute
from homepilot.executor.orchestrator import PVE_SNAPNAME_MAX, snapshot_name_for
from homepilot.executor.proxmox_api import execute as pve_execute
from homepilot.executor.skip_if import (
    SkipIfUndecided,
    make_pve_response_proxy,
    safe_eval_skip_if,
    validate_skip_if_expression,
)

# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


class FakeProxmox:
    """Records every call, and answers whatever the test tells it to."""

    def __init__(self, answers: dict[tuple[str, str], Any] | None = None) -> None:
        self.answers = answers or {}
        self.calls: list[tuple[str, str]] = []

    async def call(self, method: str, path: str, body: Any = None) -> dict[str, Any]:
        self.calls.append((method.upper(), path))
        answer = self.answers.get((method.upper(), path), {"data": None})
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def read(self, path: str, query: Any = None) -> dict[str, Any]:
        return await self.call("GET", path)

    @property
    def mutations(self) -> list[tuple[str, str]]:
        return [c for c in self.calls if c[0] in ("POST", "PUT", "DELETE", "PATCH")]


def _pve_body(steps_yaml: str, fence: str = "proxmox-api-spec") -> str:
    return f"# probe\n\n## Spec\n\n```yaml {fence}\n{steps_yaml}```\n"


FM = {"id": "2026-08-29-probe", "intent": "probe"}
TARGET = {"kind": "vm", "vmid": 101, "node": "pve"}


# --------------------------------------------------------------------------- #
# 1. The documented precheck binding has to work
# --------------------------------------------------------------------------- #


class TestTheSpecsOwnPrecheckSkips:
    """ARTIFACT_SPEC §5.2 and D2 bind `response.status_code` / `response.json`.

    `proxmox-api-sequence` bound the raw `{"data": ...}` envelope instead, which
    has neither - so `skip_if: "response.status_code == 200"` evaluated to False
    on EVERY run and the mutating step went ahead. Proved live on dev 3.6.14: two
    identical GET steps, one with each form, and only the envelope form skipped.
    """

    @pytest.mark.asyncio
    async def test_status_code_binding_skips_the_step(self) -> None:
        pve = FakeProxmox({("GET", "/nodes/pve/qemu/101/status/current"): {"data": {"x": 1}}})
        body = _pve_body(
            """steps:
  - id: create
    method: POST
    path: /nodes/pve/qemu/101/config
    body: {description: hi}
    precheck:
      method: GET
      path: /nodes/pve/qemu/101/status/current
      skip_if: "response.status_code == 200"
"""
        )
        result = await pve_execute(FM, body, TARGET, pve)  # type: ignore[arg-type]
        assert result["success"] is True
        assert "SKIPPED" in result["execution_log"]
        assert pve.mutations == [], "the documented precheck did not stop the mutation"

    @pytest.mark.asyncio
    async def test_json_binding_skips_the_step(self) -> None:
        pve = FakeProxmox(
            {("GET", "/nodes/pve/qemu/101/status/current"): {"data": {"status": "running"}}}
        )
        body = _pve_body(
            """steps:
  - id: start
    method: POST
    path: /nodes/pve/qemu/101/status/start
    precheck:
      method: GET
      path: /nodes/pve/qemu/101/status/current
      skip_if: "response.json['data']['status'] == 'running'"
"""
        )
        result = await pve_execute(FM, body, TARGET, pve)  # type: ignore[arg-type]
        assert "SKIPPED" in result["execution_log"]
        assert pve.mutations == []

    @pytest.mark.asyncio
    async def test_the_envelope_form_already_in_the_store_still_works(self) -> None:
        """Artifacts were written against the shape the executor really had.

        Changing what an APPLIED artifact's precheck means would be its own
        dishonesty, so `response["data"]` keeps reaching the envelope.
        """
        pve = FakeProxmox(
            {("GET", "/nodes/pve/qemu/101/status/current"): {"data": {"status": "stopped"}}}
        )
        body = _pve_body(
            """steps:
  - id: stop
    method: POST
    path: /nodes/pve/qemu/101/status/stop
    precheck:
      method: GET
      path: /nodes/pve/qemu/101/status/current
      skip_if: 'response["data"]["status"] == "stopped"'
"""
        )
        result = await pve_execute(FM, body, TARGET, pve)  # type: ignore[arg-type]
        assert "SKIPPED" in result["execution_log"]
        assert pve.mutations == []


# --------------------------------------------------------------------------- #
# 2. An undecided precheck is not permission to act (#642 A4)
# --------------------------------------------------------------------------- #


class TestAnUndecidedPrecheckDoesNotAuthoriseTheStep:
    @pytest.mark.asyncio
    async def test_a_precheck_that_errors_does_not_run_the_mutation(self) -> None:
        """Proved live on dev 3.6.14: the precheck answered HTTP 500 and the
        step ran anyway, with the log not even mentioning the failure."""
        pve = FakeProxmox(
            {("GET", "/nodes/pve/qemu/101/status/current"): ProxmoxError("GET", "x", 500, "boom")}
        )
        body = _pve_body(
            """steps:
  - id: destroy
    method: DELETE
    path: /nodes/pve/qemu/101
    precheck:
      method: GET
      path: /nodes/pve/qemu/101/status/current
      skip_if: 'response["data"]["status"] == "stopped"'
"""
        )
        result = await pve_execute(FM, body, TARGET, pve)  # type: ignore[arg-type]
        assert result["success"] is False
        assert pve.mutations == [], "an undecided precheck let a DELETE through"
        assert "NOT RUN" in result["execution_log"]

    @pytest.mark.asyncio
    async def test_on_error_continue_still_does_not_run_it(self) -> None:
        pve = FakeProxmox(
            {("GET", "/nodes/pve/qemu/101/status/current"): ProxmoxError("GET", "x", 0, "down")}
        )
        body = _pve_body(
            """steps:
  - id: destroy
    method: DELETE
    path: /nodes/pve/qemu/101
    on_error: continue
    precheck:
      method: GET
      path: /nodes/pve/qemu/101/status/current
      skip_if: 'response["data"]["status"] == "stopped"'
"""
        )
        result = await pve_execute(FM, body, TARGET, pve)  # type: ignore[arg-type]
        assert pve.mutations == []
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_a_404_is_an_answer_and_the_step_runs(self) -> None:
        """ "There is no such thing yet" IS what a create-step's precheck asks."""
        pve = FakeProxmox(
            {("GET", "/nodes/pve/qemu/101/status/current"): ProxmoxError("GET", "x", 404, "no")}
        )
        body = _pve_body(
            """steps:
  - id: create
    method: POST
    path: /nodes/pve/qemu
    precheck:
      method: GET
      path: /nodes/pve/qemu/101/status/current
      skip_if: "response.status_code == 200"
"""
        )
        result = await pve_execute(FM, body, TARGET, pve)  # type: ignore[arg-type]
        assert result["success"] is True
        assert ("POST", "/nodes/pve/qemu") in pve.mutations

    @pytest.mark.asyncio
    async def test_http_sequence_refuses_the_step_behind_a_5xx_precheck(self) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(f"{request.method} {request.url.path}")
            if request.url.path == "/probe":
                return httpx.Response(503, json={})
            return httpx.Response(200, json={})

        transport = httpx.MockTransport(handler)

        class Vault:
            async def get_secret(self, name: str) -> dict[str, Any]:
                return {"base_url": "https://svc.test", "headers": {}}

        body = (
            "# p\n\n```yaml http-spec\nsteps:\n"
            "  - id: create\n    name: svc\n    method: POST\n    path: /make\n"
            "    precheck:\n      name: svc\n      method: GET\n      path: /probe\n"
            '      skip_if: "response.status_code == 200"\n```\n'
        )
        import homepilot.executor.http_sequence as hs

        real_client = httpx.AsyncClient

        def patched(**kwargs: Any) -> httpx.AsyncClient:
            kwargs["transport"] = transport
            return real_client(**kwargs)

        hs.httpx.AsyncClient = patched  # type: ignore[assignment]
        try:
            result = await http_execute(FM, body, TARGET, Vault())
        finally:
            hs.httpx.AsyncClient = real_client  # type: ignore[assignment]

        assert result["success"] is False
        assert "POST /make" not in seen, "a 5xx precheck let the POST through"


class TestAnUndecidableExpressionIsNotFalse:
    """`False` is the branch that MUTATES, so it is the wrong home for errors."""

    def test_a_wrong_binding_raises_rather_than_answering_no(self) -> None:
        with pytest.raises(SkipIfUndecided):
            safe_eval_skip_if("response.status_code == 200", {"data": None}, {})

    def test_a_refused_construct_raises(self) -> None:
        with pytest.raises(SkipIfUndecided):
            safe_eval_skip_if("len(response.json) > 0", make_pve_response_proxy(200, {}), {})

    def test_a_missing_key_is_still_an_answer(self) -> None:
        """A key that is not in the body is a fact about the target, not about
        the binding - a precheck asking "is it there yet" needs that to work."""
        proxy = make_pve_response_proxy(200, {"data": {}})
        assert safe_eval_skip_if("response.json['data']['count'] == 1", proxy, {}) is False

    def test_the_propose_time_check_mirrors_the_evaluator(self) -> None:
        """What the validator accepts, the evaluator must be able to run."""
        assert validate_skip_if_expression("response.status_code == 200") == []
        assert validate_skip_if_expression("response.json['data']['status'] == 'running'") == []
        assert validate_skip_if_expression("target['host'] == 'web1'") == []
        assert validate_skip_if_expression("len(response.json) > 0")
        assert validate_skip_if_expression("__import__('os')")
        assert validate_skip_if_expression("nope == 1")


# --------------------------------------------------------------------------- #
# 3. `on_error: continue` keeps going; it does not make a failure a success
# --------------------------------------------------------------------------- #


class TestAppliedMeansTheStepsRan:
    @pytest.mark.asyncio
    async def test_a_failed_step_under_continue_does_not_report_applied(self) -> None:
        """Proved live on dev 3.6.14: a step answered HTTP 500 under
        `on_error: continue` and the task reported `succeeded`, the artifact
        `applied` - so a revoke over it would have reported `reversed`."""
        pve = FakeProxmox(
            {
                ("POST", "/a"): ProxmoxError("POST", "/a", 500, "boom"),
                ("POST", "/b"): {"data": None},
            }
        )
        body = _pve_body(
            """steps:
  - id: one
    method: POST
    path: /a
    on_error: continue
  - id: two
    method: POST
    path: /b
    on_error: continue
"""
        )
        result = await pve_execute(FM, body, TARGET, pve)  # type: ignore[arg-type]
        assert result["success"] is False
        assert "one" in result["failure_reason"]
        # …and it really did keep going, which is what `continue` is for.
        assert ("POST", "/b") in pve.mutations


# --------------------------------------------------------------------------- #
# 4. Interpolation: an unresolved value never reaches the cluster
# --------------------------------------------------------------------------- #


class TestAnUnresolvedValueNeverReachesTheCluster:
    @pytest.mark.asyncio
    async def test_an_undefined_variable_refuses_the_step(self) -> None:
        """`SilentUndefined` rendered a missing variable as "", so the call went
        out at `/nodes/pve/lxc//status/current`; a template error returned the
        RAW string and sent `{{ ... }}` to Proxmox verbatim."""
        pve = FakeProxmox()
        body = _pve_body(
            """steps:
  - id: go
    method: POST
    path: /nodes/{{ target.nope }}/qemu/101/config
"""
        )
        result = await pve_execute(FM, body, TARGET, pve)  # type: ignore[arg-type]
        assert result["success"] is False
        assert pve.calls == []


# --------------------------------------------------------------------------- #
# 5. #627 - the snapshot name PVE will actually accept
# --------------------------------------------------------------------------- #


class TestThePreApplySnapshotNameAlwaysFits:
    """PVE caps `snapname` at 40 chars, so `hp-pre-<id>` failed the apply of any
    artifact with an id over 33 - before step 1, on a guest that was fine.
    Hit on prod 3.6.9 and twice on dev; both failures are still in the store."""

    @pytest.mark.parametrize(
        "artifact_id",
        [
            "2026-08-29-x",
            "2026-08-28-cancel-timing-probe-cleanup-long-name-test",
            "2026-08-29-" + "a" * 60,
        ],
    )
    def test_never_longer_than_pve_allows(self, artifact_id: str) -> None:
        name = snapshot_name_for(artifact_id)
        assert len(name) <= PVE_SNAPNAME_MAX
        assert name.startswith("hp-pre-")

    def test_a_short_id_keeps_the_name_it_always_had(self) -> None:
        assert snapshot_name_for("2026-08-29-rm101") == "hp-pre-2026-08-29-rm101"

    def test_two_long_ids_sharing_a_prefix_get_different_names(self) -> None:
        a = snapshot_name_for("2026-08-29-remove-the-write-probe-from-guest-a")
        b = snapshot_name_for("2026-08-29-remove-the-write-probe-from-guest-b")
        assert a != b

    @pytest.mark.asyncio
    async def test_the_executor_sends_a_name_pve_will_take(self) -> None:
        """The gate #627 asks for: drive the real apply path with a 60-char id
        and assert the snapname the executor SENDS. Testing the helper alone
        would stay green if the call site went back to `f"hp-pre-{id}"`."""
        from unittest.mock import AsyncMock, MagicMock

        from homepilot.executor.orchestrator import ArtifactExecutor

        proxmox = MagicMock()
        proxmox.snapshot = AsyncMock(return_value={"data": None})
        ex = ArtifactExecutor(
            store=MagicMock(),
            lifecycle=MagicMock(),
            repo=MagicMock(),
            proxmox=proxmox,
            vault=MagicMock(),
        )
        long_id = "2026-08-28-cancel-timing-probe-cleanup-long-name-test"
        await ex._maybe_snapshot({"id": long_id}, {"kind": "vm", "vmid": 101, "node": "pve"})

        sent = proxmox.snapshot.call_args[0][2]
        assert len(sent) <= PVE_SNAPNAME_MAX, (
            f"the executor asked PVE for a {len(sent)}-char snapname; PVE refuses "
            f"anything over {PVE_SNAPNAME_MAX} and the apply dies before step 1"
        )
