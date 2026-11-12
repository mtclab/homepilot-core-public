from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import httpx

from homepilot.adapters.agent import is_pve_node
from homepilot.adapters.proxmox import ProxmoxError
from homepilot.artifacts.models import ArtifactKind, ArtifactStatus
from homepilot.artifacts.store import ArtifactStore
from homepilot.db.repository import Repository
from homepilot.executor.jinja_utils import InterpolationError, interpolation_context
from homepilot.executor.skip_if import SkipIfUndecided, make_pve_response_proxy
from homepilot.runtime.blocking import run_blocking

logger = logging.getLogger(__name__)

_ansible_semaphore = asyncio.Semaphore(3)

_MAX_VERIFY_DEPTH = 10

# Everything a read against a target can fail with. `ProxmoxError` was NOT in
# this set, so a drift check on any sequence whose precheck path answered an
# error status raised straight out of the verifier: the reconciler counted an
# "error", wrote NO row - leaving the previous verdict standing as if it were
# current - and over MCP the caller got "Internal server error". Seen live on
# dev 3.6.14. A read that failed is an UNKNOWN, which is a verdict this module
# already has a word for.
_PROBE_FAILED = (
    httpx.HTTPError,
    httpx.TimeoutException,
    ConnectionError,
    OSError,
    ProxmoxError,
    SkipIfUndecided,
    InterpolationError,
)

_HOSTNAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9.-]*$")


class DriftState(StrEnum):
    """What a drift check actually established (#425).

    `drifted` was a BOOLEAN, so every unverifiable path and every errored one
    returned `drifted=False` - which the UI rendered as a green "in spec" for
    things that were never checked. "I looked and it matches" and "I could not
    look" are different answers, and an infrastructure tool that conflates them
    is confidently wrong exactly where it matters.
    """

    IN_SPEC = "in_spec"
    DRIFTED = "drifted"
    UNKNOWN = "unknown"


@dataclass
class VerifyResult:
    artifact_id: str
    drifted: bool = False
    verification_log: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    # UNKNOWN by default ON PURPOSE: it is the fail-safe direction. A path that
    # forgets to set a state reads as "not established" rather than as a green
    # tick, which is the inverse of the bug this field exists to kill.
    state: DriftState = DriftState.UNKNOWN

    def __post_init__(self) -> None:
        # One fact, two names: `drifted` stays for storage and for every existing
        # caller, and it can never disagree with the state.
        if self.state is DriftState.DRIFTED:
            self.drifted = True
        elif self.state is DriftState.IN_SPEC:
            self.drifted = False


async def verify_artifact(
    artifact_id: str,
    repo: Repository,
    store: ArtifactStore,
    executor: Any | None = None,
    _depth: int = 0,
) -> VerifyResult:
    if _depth > _MAX_VERIFY_DEPTH:
        return VerifyResult(
            artifact_id=artifact_id,
            drifted=False,
            verification_log="max recursion depth exceeded",
            details={"reason": "max_depth"},
        )

    try:
        fm, body = await run_blocking("io", store.read, artifact_id)
    except FileNotFoundError:
        raise

    kind_str = fm.get("kind", "")
    try:
        kind = ArtifactKind(kind_str)
    except ValueError:
        return VerifyResult(
            artifact_id=artifact_id,
            drifted=False,
            verification_log=f"unknown kind: {kind_str}",
            details={"reason": "unknown_kind"},
        )

    status_str = fm.get("status", "")
    try:
        status = ArtifactStatus(status_str)
    except ValueError:
        return VerifyResult(
            artifact_id=artifact_id,
            drifted=False,
            verification_log=f"unknown status: {status_str}",
            details={"reason": "unknown_status"},
        )

    if status != ArtifactStatus.APPLIED:
        return VerifyResult(
            artifact_id=artifact_id,
            drifted=False,
            verification_log=f"not applied (status={status_str})",
            details={"reason": "not_applied"},
        )

    if kind == ArtifactKind.KB_NOTE:
        return VerifyResult(
            artifact_id=artifact_id,
            drifted=False,
            verification_log="kb-note: unverifiable, skipped",
            details={"reason": "kb_note_skipped"},
        )

    if executor is None:
        return VerifyResult(
            artifact_id=artifact_id,
            drifted=False,
            verification_log="no executor available",
            details={"reason": "no_executor"},
        )

    try:
        if kind == ArtifactKind.ANSIBLE_PLAYBOOK:
            return await _verify_ansible(artifact_id, fm, body, executor)
        elif kind == ArtifactKind.PROXMOX_API_SEQUENCE:
            return await _verify_proxmox_api(artifact_id, fm, body, executor)
        elif kind == ArtifactKind.HTTP_SEQUENCE:
            return await _verify_http_sequence(artifact_id, fm, body, executor)
        elif kind == ArtifactKind.COMPOSITE:
            return await _verify_composite(artifact_id, fm, body, store, repo, executor, _depth)
        elif kind == ArtifactKind.SHELL_SCRIPT:
            return VerifyResult(
                artifact_id=artifact_id,
                drifted=False,
                verification_log="shell-script: unverifiable",
                details={"reason": "shell_script_unverifiable"},
            )
        elif kind == ArtifactKind.HOST_PROVISION:
            return await _verify_host_provision(artifact_id, fm, body, executor)
        elif kind == ArtifactKind.GUEST_NETWORK:
            return await _verify_guest_network(artifact_id, fm, body, executor)
        elif kind == ArtifactKind.GUEST_POWER:
            # A power action is a moment, not a state: the guest being stopped
            # tomorrow is not drift from yesterday's start (#781).
            return VerifyResult(
                artifact_id=artifact_id,
                drifted=False,
                verification_log="guest-power: a one-time action, nothing to drift from",
                details={"reason": "guest_power_one_time"},
            )
        else:
            return VerifyResult(
                artifact_id=artifact_id,
                drifted=False,
                verification_log=f"unhandled kind: {kind.value}",
                details={"reason": "unknown_kind"},
            )
    except (FileNotFoundError, asyncio.CancelledError):
        raise
    except Exception as exc:
        # A verifier that RAISED left no verdict at all: the reconciler counted
        # an error and wrote no row, so the artifact kept whatever colour its
        # last successful check gave it, indefinitely and invisibly. UNKNOWN is
        # the answer this module already has for "I could not establish it", and
        # it is the one a failed check has earned (#425/#642).
        logger.warning("verify raised for %s: %s", artifact_id, exc, exc_info=True)
        return VerifyResult(
            artifact_id=artifact_id,
            state=DriftState.UNKNOWN,
            verification_log=f"the drift check itself failed: {exc}"[:2000],
            details={"reason": "check_failed", "error": str(exc)[:500]},
        )


def _extract_spec(body: str, tag: str) -> str | None:
    pattern = re.compile(rf"```yaml\s+{tag}\s*\n(.*?)```", re.DOTALL)
    m = pattern.search(body)
    return m.group(1).strip() if m else None


def _resolve_host(fm: dict[str, Any]) -> str:
    target: dict[str, Any] = fm.get("target", {})
    host: Any = target.get("host") or target.get("node", "")
    return str(host)


def _ansible_output_has_changes(output: str) -> bool:
    recap_match = re.search(r"changed=(\d+)", output)
    if recap_match:
        return int(recap_match.group(1)) > 0
    return "changed" in output.lower() and "changed=0" not in output


async def _verify_ansible(
    artifact_id: str,
    fm: dict[str, Any],
    body: str,
    executor: Any,
) -> VerifyResult:
    spec_yaml = _extract_spec(body, "ansible-spec")
    if spec_yaml is None:
        return VerifyResult(
            artifact_id=artifact_id,
            drifted=False,
            verification_log="no ansible-spec found",
            details={"reason": "no_spec"},
        )

    host = _resolve_host(fm)
    if not host:
        return VerifyResult(
            artifact_id=artifact_id,
            drifted=False,
            verification_log="no target host resolved",
            details={"reason": "no_host"},
        )

    if not _HOSTNAME_RE.match(host):
        return VerifyResult(
            artifact_id=artifact_id,
            drifted=False,
            verification_log=f"invalid hostname: {host!r}",
            details={"reason": "invalid_host"},
        )

    # The PVE-node refusal stays AHEAD of the "not implemented" answer: it is a
    # real safety check about the target, and it must keep its own reason. An
    # operator pointing a playbook at a hypervisor should be told that, not told
    # the checker is missing.
    pve_nodes: list[str] = getattr(executor, "pve_nodes", []) or []
    if pve_nodes and is_pve_node(host, pve_nodes):
        return VerifyResult(
            artifact_id=artifact_id,
            state=DriftState.UNKNOWN,
            verification_log=f"PVE node '{host}' - use the Proxmox API instead",
            details={"reason": "forbidden_host"},
        )

    # The ansible drift check is NOT implemented, and now says so (#425).
    #
    # What stood here called `executor.ssh.exec(...)` and
    # `executor.ssh._validate_guest_only(...)`. `ArtifactExecutor` has no `.ssh`
    # attribute - it went with the jump server - so every call raised
    # AttributeError, which the broad handler below swallowed into
    # `drifted=False`. EVERY applied ansible artifact therefore reported "in
    # spec" forever, in green, having checked nothing.
    #
    # It was structurally dead beyond the missing attribute: it wrote a temporary
    # inventory and playbook on the CONTROL PLANE and then ran ansible-playbook
    # on the REMOTE host, where neither the files nor ansible exist. Reviving it
    # needs a real transport design, which is #388's first item - not a rename.
    #
    # So: no check, and the honest answer for "did this drift" is that nobody
    # knows. That is exactly what UNKNOWN is for; before the tri-state existed
    # there was no way to say it.
    return VerifyResult(
        artifact_id=artifact_id,
        state=DriftState.UNKNOWN,
        verification_log=(
            "ansible drift checking is not implemented: the playbook transport was "
            "removed with the jump server (#388). This artifact has NOT been checked."
        ),
        details={"reason": "ansible_unverifiable"},
    )


async def _verify_proxmox_api(
    artifact_id: str,
    fm: dict[str, Any],
    body: str,
    executor: Any,
) -> VerifyResult:
    from homepilot.executor.proxmox_api import _extract_steps as extract_proxmox_steps

    steps = extract_proxmox_steps(body, "proxmox-api-spec")
    if steps is None:
        return VerifyResult(
            artifact_id=artifact_id,
            drifted=False,
            verification_log="no proxmox-api-spec found",
            details={"reason": "no_spec"},
        )

    target: dict[str, Any] = fm.get("target", {})
    context = interpolation_context(target, fm)

    if target.get("kind") == "cluster":
        from homepilot.executor.proxmox_api import _pick_cluster_node

        try:
            pve_node = await _pick_cluster_node(executor.proxmox)
        except _PROBE_FAILED:
            pve_node = None
        if pve_node:
            context["target"] = dict(target)
            context["target"]["node"] = pve_node

    from homepilot.executor.jinja_utils import _eval_skip_if, _interpolate
    from homepilot.executor.proxmox_api import _PRECHECK_ANSWERS_BELOW

    drifted_steps: list[str] = []
    # Steps whose precheck was ASKED and said "already converged" (#712). They
    # used to be filed under skipped_steps, next to the ones nobody could ask,
    # so a fully converged artifact had every step "skipped" and read
    # `unknown / nothing_evaluated` - it could never be in spec.
    in_spec_steps: list[str] = []
    skipped_steps: list[str] = []
    # The subset of skipped_steps whose precheck was ASKED and got no answer.
    # "Nothing to compare" is the artifact's nature; "could not ask" is a fault,
    # and only this list tells the two apart in the verdict.
    unanswered_steps: list[str] = []

    for step in steps:
        step_id = step.get("id", "unknown")
        precheck = step.get("precheck")

        if step.get("method", "GET").upper() == "GET" and not precheck:
            skipped_steps.append(step_id)
            continue

        if precheck:
            pre_method = precheck.get("method", "GET").upper()
            skip_if_expr = precheck.get("skip_if")

            if not skip_if_expr:
                skipped_steps.append(step_id)
                continue

            # Same guard as the HTTP verifier: an author-supplied precheck may
            # declare any method, and verify must issue no non-GET (#419).
            if pre_method != "GET":
                skipped_steps.append(step_id)
                continue

            try:
                pre_path = _interpolate(precheck.get("path", ""), context)
                try:
                    resp = await executor.proxmox.call(pre_method, pre_path)
                    # The same binding the executor uses, so drift and apply
                    # cannot disagree about what a precheck means (#642).
                    proxy = make_pve_response_proxy(200, resp)
                except ProxmoxError as exc:
                    # A 4xx IS an answer about the resource - a 404 is "it is
                    # gone" - exactly as the executor reads it (#712). Treating
                    # it as a failed probe made a deleted resource read
                    # `unknown` while apply would re-create it: real drift,
                    # invisible. Only a 5xx or no answer at all is a failed probe.
                    if not exc.status_code or exc.status_code >= _PRECHECK_ANSWERS_BELOW:
                        raise
                    proxy = make_pve_response_proxy(exc.status_code, {"data": None})
                if _eval_skip_if(skip_if_expr, proxy, context.get("target", {})):
                    in_spec_steps.append(step_id)
                    continue
                else:
                    drifted_steps.append(step_id)
                    continue
            except _PROBE_FAILED:
                skipped_steps.append(step_id)
                unanswered_steps.append(step_id)
                continue
        else:
            skipped_steps.append(step_id)
            continue

    return _sequence_verdict(
        artifact_id, drifted_steps, in_spec_steps, skipped_steps, unanswered_steps
    )


def _sequence_verdict(
    artifact_id: str,
    drifted_steps: list[str],
    in_spec_steps: list[str],
    skipped_steps: list[str],
    unanswered_steps: list[str] | None = None,
) -> VerifyResult:
    """The verdict for a step sequence, from what each step ESTABLISHED.

    drifted  - at least one precheck was asked and said "not converged";
    in_spec  - none drifted and at least one precheck was asked and said
               "converged" (#712: these used to be counted as skipped, so a
               fully converged artifact could never read in spec);
    unknown  - nothing was compared at all: every step was skipped because it
               has no precheck, its precheck is not a GET, or asking it failed.
               "Every step was skipped" is not "in spec" (#425). The reason is
               `probe_failed` when any precheck was asked and went unanswered,
               and `nothing_evaluated` only when there was nothing to ask - the
               Overview calls the second one "describe-only (fine)", so it must
               never cover a failed probe.
    """
    verification_log = (
        f"drifted_steps={drifted_steps}, in_spec_steps={in_spec_steps}, "
        f"skipped_steps={skipped_steps}"
    )
    details: dict[str, Any] = {
        "drifted_steps": drifted_steps,
        "in_spec_steps": in_spec_steps,
        "skipped_steps": skipped_steps,
    }
    if drifted_steps:
        state = DriftState.DRIFTED
    elif in_spec_steps:
        state = DriftState.IN_SPEC
    else:
        reason = "probe_failed" if unanswered_steps else "nothing_evaluated"
        if unanswered_steps:
            details["unanswered_steps"] = list(unanswered_steps)
        return VerifyResult(
            artifact_id=artifact_id,
            state=DriftState.UNKNOWN,
            verification_log=verification_log[:2000],
            details={"reason": reason, **details},
        )
    return VerifyResult(
        artifact_id=artifact_id,
        state=state,
        verification_log=verification_log[:2000],
        details=details,
    )


async def _verify_http_sequence(
    artifact_id: str,
    fm: dict[str, Any],
    body: str,
    executor: Any,
) -> VerifyResult:
    import httpx

    from homepilot.executor.http_sequence import (
        ExecutorError,
        _resolve_credential,
    )
    from homepilot.executor.http_sequence import (
        _extract_steps as extract_http_steps,
    )
    from homepilot.executor.jinja_utils import _eval_skip_if, _interpolate
    from homepilot.executor.skip_if import make_response_proxy

    steps = extract_http_steps(body, "http-spec")
    if steps is None:
        return VerifyResult(
            artifact_id=artifact_id,
            drifted=False,
            verification_log="no http-spec found",
            details={"reason": "no_spec"},
        )

    target: dict[str, Any] = fm.get("target", {})
    context = interpolation_context(target, fm)

    from homepilot.executor.http_sequence import _PRECHECK_ANSWERS_BELOW

    client_cache: dict[str, httpx.AsyncClient] = {}
    drifted_steps: list[str] = []
    in_spec_steps: list[str] = []
    skipped_steps: list[str] = []
    unanswered_steps: list[str] = []  # asked, no usable answer (see _sequence_verdict)

    try:
        for step in steps:
            step_id = step.get("id", "unknown")
            precheck = step.get("precheck")
            cred_name = step.get("name", "")

            if precheck:
                pre_cred_name = precheck.get("name", cred_name)
                pre_method = precheck.get("method", "GET").upper()
                skip_if_expr = precheck.get("skip_if")

                if not skip_if_expr:
                    skipped_steps.append(step_id)
                    continue

                try:
                    pre_path = _interpolate(precheck.get("path", ""), context)
                except InterpolationError:
                    skipped_steps.append(step_id)
                    continue

                # A precheck is author-supplied and can declare any method. Verify
                # is read-only, so a non-GET precheck is refused rather than run
                # (#419). Without this the invariant "a verify pass issues no
                # non-GET" is still violable via `precheck: {method: DELETE}`.
                if pre_method != "GET":
                    skipped_steps.append(step_id)
                    continue

                try:
                    pre_cred = await _resolve_credential(pre_cred_name, executor.vault)
                except ExecutorError:
                    skipped_steps.append(step_id)
                    unanswered_steps.append(step_id)
                    continue

                base_url = pre_cred.get("base_url", "").rstrip("/")
                headers = pre_cred.get("headers", {})
                verify_tls = pre_cred.get("verify_tls", True)

                client_key = f"{base_url}:{pre_cred_name}"
                if client_key not in client_cache:
                    client_cache[client_key] = httpx.AsyncClient(
                        base_url=base_url,
                        headers=headers,
                        verify=verify_tls,
                        timeout=30.0,
                    )
                pre_client = client_cache[client_key]

                try:
                    resp = await pre_client.request(method=pre_method, url=pre_path)
                    if resp.status_code >= _PRECHECK_ANSWERS_BELOW:
                        # A 5xx says nothing about the resource - the same line
                        # the executor draws (#712).
                        skipped_steps.append(step_id)
                        unanswered_steps.append(step_id)
                        continue
                    proxy = make_response_proxy(resp)
                    if _eval_skip_if(skip_if_expr, proxy, context.get("target", {})):
                        in_spec_steps.append(step_id)
                        continue
                    else:
                        drifted_steps.append(step_id)
                        continue
                except (httpx.HTTPError, SkipIfUndecided):
                    skipped_steps.append(step_id)
                    unanswered_steps.append(step_id)
                    continue

            # No precheck: report unknown rather than probing. A verify pass must
            # never issue the step's OWN request - it is the mutating one.
            #
            # This previously skipped only GET and then fell through and executed
            # every non-GET for real, so a `method: DELETE` step fired a live
            # DELETE against the target on the unattended 1800s drift loop (#419).
            # _verify_proxmox_api always skipped here; the two verifiers had
            # drifted apart and the HTTP one was the dangerous side.
            skipped_steps.append(step_id)
            continue
    finally:
        for c in client_cache.values():
            with contextlib.suppress(Exception):
                await c.aclose()

    return _sequence_verdict(
        artifact_id, drifted_steps, in_spec_steps, skipped_steps, unanswered_steps
    )


async def _agent_absent(adapter: Any, host: str) -> bool:
    """True only when the adapter positively says no agent serves `host`.

    Anything that cannot answer the question (an adapter without
    `test_connection`, a test double) is not taken as "absent": the probe then
    runs and reports what it could and could not read, as before.
    """
    ask = getattr(adapter, "test_connection", None)
    if not callable(ask):
        return False
    answer = ask(host)
    if inspect.isawaitable(answer):
        answer = await answer
    return answer is False


async def _verify_host_provision(
    artifact_id: str,
    fm: dict[str, Any],
    body: str,
    executor: Any,
) -> VerifyResult:
    from homepilot.artifacts.models import parse_host_provision_spec
    from homepilot.executor.host_provision import check_drift

    try:
        spec = parse_host_provision_spec(body)
    except ValueError as exc:
        return VerifyResult(
            artifact_id=artifact_id,
            drifted=False,
            verification_log=f"no valid host-provision-spec: {exc}",
            details={"reason": "no_spec"},
        )

    host = _resolve_host(fm)
    if not host:
        return VerifyResult(
            artifact_id=artifact_id,
            drifted=False,
            verification_log="no target host resolved",
            details={"reason": "no_host"},
        )
    if not _HOSTNAME_RE.match(host):
        return VerifyResult(
            artifact_id=artifact_id,
            drifted=False,
            verification_log=f"invalid hostname: {host!r}",
            details={"reason": "invalid_host"},
        )

    pve_nodes: list[str] = getattr(executor, "pve_nodes", []) or []
    if any(host.lower().strip() == n.lower().strip() for n in pve_nodes):
        return VerifyResult(
            artifact_id=artifact_id,
            drifted=False,
            verification_log=f"host-provision forbidden for PVE node '{host}'",
            details={"reason": "forbidden_host"},
        )

    # The host's agent is the only way to read it. Without one every item is
    # unanswered, and saying WHY - no agent - is what turns "unknown" into
    # something an operator can act on (install or reconnect the agent) rather
    # than a reason they have to dig out of a probe log.
    adapter = executor.host_adapter
    if await _agent_absent(adapter, host):
        return VerifyResult(
            artifact_id=artifact_id,
            state=DriftState.UNKNOWN,
            verification_log=f"no agent is connected for {host}, so nothing on it could be read",
            details={"reason": "no_agent", "host": host},
        )

    result = await check_drift(adapter, host, spec)
    drifted_items: list[str] = result["drifted_items"]
    unknown_items: list[str] = result.get("unknown_items") or []
    details: dict[str, Any] = {"drifted_items": drifted_items, "unknown_items": unknown_items}
    if result["drifted"]:
        state = DriftState.DRIFTED
    elif unknown_items:
        # Nothing said it had drifted, but something never answered. "I looked
        # and it matches" and "I could not look" are different answers (#425).
        state = DriftState.UNKNOWN
        details["reason"] = "items_unanswered"
    else:
        state = DriftState.IN_SPEC
    return VerifyResult(
        artifact_id=artifact_id,
        state=state,
        verification_log=(result["log"] or "all items in desired state")[:2000],
        details=details,
    )


async def _verify_guest_network(
    artifact_id: str,
    fm: dict[str, Any],
    body: str,
    executor: Any,
) -> VerifyResult:
    """Drift for a guest network is its plan (#553).

    The apply runs ``plan()`` and does what it says; drift runs the SAME
    function and asks whether it would still do anything. So "in spec" here
    means precisely "re-applying this artifact would change nothing", which is
    the only definition that cannot disagree with the apply.

    A cluster that could not be read is UNKNOWN, never green: an unreachable
    Proxmox tells you nothing about whether a zone still exists.
    """
    from homepilot.artifacts.models import parse_guest_network_spec
    from homepilot.provision.guest_network import (
        desired_from_settings,
        gateway_for,
        plan,
        survey,
    )

    gateway = getattr(executor, "sdn_gateway", None) or gateway_for(
        getattr(executor, "proxmox", None)
    )
    if gateway is None:
        return VerifyResult(
            artifact_id=artifact_id,
            state=DriftState.UNKNOWN,
            verification_log="no Proxmox client: the guest network could not be checked",
            details={"reason": "no_proxmox"},
        )

    defaults = None
    try:
        defaults = await desired_from_settings(getattr(executor, "settings_source", None))
    except ValueError:
        defaults = None

    try:
        desired = parse_guest_network_spec(body, defaults)
    except ValueError as exc:
        return VerifyResult(
            artifact_id=artifact_id,
            state=DriftState.UNKNOWN,
            verification_log=f"no usable guest-network-spec: {exc}",
            details={"reason": "no_spec"},
        )

    target: dict[str, Any] = fm.get("target", {}) or {}
    try:
        current = await survey(gateway, desired, str(target.get("node") or ""))
    except Exception as exc:  # a survey raising at all means nothing was read
        return VerifyResult(
            artifact_id=artifact_id,
            state=DriftState.UNKNOWN,
            verification_log=f"the cluster could not be surveyed: {exc}",
            details={"reason": "survey_failed"},
        )

    if current.errors and not current.zones and not current.vnets:
        return VerifyResult(
            artifact_id=artifact_id,
            state=DriftState.UNKNOWN,
            verification_log="; ".join(current.errors)[:2000],
            details={"reason": "nothing_read", "errors": current.errors},
        )

    the_plan = plan(desired, current)
    if the_plan.converged:
        return VerifyResult(
            artifact_id=artifact_id,
            state=DriftState.IN_SPEC,
            verification_log="the cluster matches the desired guest network",
            details={"steps": [], "blockers": []},
        )
    summary = "; ".join([*(s.description for s in the_plan.steps), *the_plan.blockers])
    return VerifyResult(
        artifact_id=artifact_id,
        state=DriftState.DRIFTED,
        verification_log=summary[:2000],
        details={
            "steps": [s.id for s in the_plan.steps],
            "blockers": list(the_plan.blockers),
        },
    )


async def _verify_composite(
    artifact_id: str,
    fm: dict[str, Any],
    body: str,
    store: ArtifactStore,
    repo: Repository,
    executor: Any,
    depth: int,
) -> VerifyResult:
    from homepilot.artifacts.models import extract_composite_steps

    steps = extract_composite_steps(body)
    if not steps:
        return VerifyResult(
            artifact_id=artifact_id,
            drifted=False,
            verification_log="no composite-spec found",
            details={"reason": "no_spec"},
        )

    drifted_subs: list[str] = []
    skipped_subs: list[str] = []
    unknown_subs: list[str] = []

    for step in steps:
        sub_id = step.get("artifact", "")
        if not sub_id:
            skipped_subs.append(step.get("id", "unknown"))
            continue

        try:
            sub_result = await verify_artifact(
                sub_id,
                repo,
                store,
                executor,
                _depth=depth + 1,
            )
        except FileNotFoundError:
            skipped_subs.append(sub_id)
            continue
        except _PROBE_FAILED:
            skipped_subs.append(sub_id)
            continue

        if sub_result.state is DriftState.DRIFTED:
            drifted_subs.append(sub_id)
        elif sub_result.state is not DriftState.IN_SPEC:
            unknown_subs.append(sub_id)

    verification_log = (
        f"drifted_subs={drifted_subs}, unknown_subs={unknown_subs}, skipped_subs={skipped_subs}"
    )
    # Both sibling verifiers already refuse to call "nothing established" green
    # (`_sequence_verdict`, #425). This one tested `sub_result.drifted`, a BOOLEAN that
    # is False for UNKNOWN as well as for IN_SPEC - so a composite of ansible
    # sub-artifacts, every one of which reports "not checked", reported in spec.
    # Confirmed live on dev 3.6.14: sub state `unknown`, composite `in_spec`.
    if drifted_subs:
        state = DriftState.DRIFTED
    elif unknown_subs or skipped_subs:
        state = DriftState.UNKNOWN
    else:
        state = DriftState.IN_SPEC
    return VerifyResult(
        artifact_id=artifact_id,
        state=state,
        verification_log=verification_log[:2000],
        details={
            "drifted_subs": drifted_subs,
            "unknown_subs": unknown_subs,
            "skipped_subs": skipped_subs,
        },
    )
