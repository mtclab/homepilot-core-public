from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import parse_qsl

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError

from ..common import SlidingWindowLimiter
from ..config import get_settings
from ..provision.models import validate_tailscale_auth_key
from ..provision.service import TailnetJoinConflictError
from .models import RedemptionIdentity, build_provision_request, caps_from_row
from .repository import InviteRepository, invite_state
from .templates import SECURITY_HEADERS, render
from .trust import PortalNotConfiguredError, PortalUntrustedError, assert_trusted_cn, load_trust

logger = logging.getLogger(__name__)

router = APIRouter()

# Redemption attempts per (source, CN) pair. The global per-IP HTTP middleware
# still applies underneath; behind a reverse proxy every portal request shares
# one source address, so a per-CN bucket is what actually separates one friend
# hammering the form from another friend being locked out.
_REDEEM_LIMIT = 10
_REDEEM_WINDOW_SECONDS = 300
_redeem_attempts = SlidingWindowLimiter(limit=_REDEEM_LIMIT, window_seconds=_REDEEM_WINDOW_SECONDS)

# Re-joins get their OWN bucket (#628). Sharing the redemption one would mean a
# friend who retried a tailnet join five times could no longer redeem a second
# invite, and a redemption storm would lock a machine's owner out of fixing its
# tailnet - two unrelated actions taking each other's budget.
_REJOIN_LIMIT = 5
_REJOIN_WINDOW_SECONDS = 300
_rejoin_attempts = SlidingWindowLimiter(limit=_REJOIN_LIMIT, window_seconds=_REJOIN_WINDOW_SECONDS)

# Bound the body we will parse as a form. The portal takes four short fields; a
# larger body is not a redemption.
_MAX_FORM_BYTES = 64 * 1024

# One text for every "this invite is not usable by you" outcome. Deliberately
# uniform: a mismatched CN must not learn whether the token exists, is expired,
# was already redeemed, or was revoked.
_NOT_USABLE_HEADING = "This invite cannot be used"
_NOT_USABLE_MESSAGE = (
    "This link is not valid for your certificate, or it has already been used or has expired. "
    "Ask the person who sent it for a new one."
)


def _unavailable(detail: str) -> HTMLResponse:
    return render(
        "message.html",
        status_code=503,
        heading="The portal is not available",
        message=(
            f"{detail}. Until that is fixed every invite link returns this page. "
            "See docs/portal.md."
        ),
    )


def _refused() -> HTMLResponse:
    return render(
        "message.html",
        status_code=403,
        heading="No client certificate",
        message=(
            "This page is only reachable through the lab's client-certificate gateway. "
            "Your request did not arrive with a verified certificate."
        ),
    )


def _not_usable() -> HTMLResponse:
    return render(
        "message.html",
        status_code=404,
        heading=_NOT_USABLE_HEADING,
        message=_NOT_USABLE_MESSAGE,
    )


def _too_many() -> HTMLResponse:
    return render(
        "message.html",
        status_code=429,
        heading="Too many attempts",
        message="Wait a few minutes and try again.",
    )


def _client_cn(request: Request) -> str:
    """The proxy-asserted client-certificate CN. Raises rather than guessing."""
    settings = getattr(request.app.state, "settings", None) or get_settings()
    trust = load_trust(settings)
    peer = request.client.host if request.client else None
    return assert_trusted_cn(peer, request.headers, trust)


def _hosts_repo(request: Request) -> Any:
    repo = getattr(request.app.state, "repo", None)
    if repo is None:
        raise PortalNotConfiguredError("The backend has not finished starting")
    return repo


def _repo(request: Request) -> InviteRepository:
    repo = getattr(request.app.state, "invite_repo", None)
    if not isinstance(repo, InviteRepository):
        raise PortalNotConfiguredError("The backend has not finished starting")
    return repo


async def _read_form(request: Request) -> dict[str, str]:
    """Parse an application/x-www-form-urlencoded body without python-multipart.

    Starlette's request.form() needs python-multipart, which this project does
    not declare as a dependency; the portal's own form posts urlencoded, so the
    stdlib parser is both sufficient and honest. Any other content type yields
    no fields and fails validation like an empty post.
    """
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type != "application/x-www-form-urlencoded":
        return {}
    raw = await request.body()
    if len(raw) > _MAX_FORM_BYTES:
        return {}
    return dict(parse_qsl(raw.decode("utf-8", errors="replace"), keep_blank_values=True))


def _first_validation_message(exc: ValidationError) -> str:
    error = exc.errors()[0]
    message = str(error.get("msg", "That value is not valid."))
    return message.removeprefix("Value error, ")


def _default_name(cn: str, invite_id: str) -> str:
    """A guest name derived from the certificate CN, unique per invite.

    The invite-id suffix is not decoration: two invites for the same CN would
    otherwise collide on the hostname, and ProvisionService refuses a second
    in-flight provision for a name it is already building.
    """
    slug = "".join(ch if ch.isalnum() else "-" for ch in cn.lower())
    slug = "-".join(part for part in slug.split("-") if part)[:40].strip("-")
    if not slug or not slug[0].isalnum():
        slug = "guest"
    return f"{slug}-{invite_id[:6]}"


def _machine_facts(task: dict[str, Any] | None) -> dict[str, Any]:
    """The handful of fields from a provision task that belong to the redeemer.

    Built by naming each field, never by passing the task through: the task row
    carries operator-facing error text and step names that are not the friend's
    business.
    """
    if not task or not task.get("result_json"):
        return {}
    try:
        parsed = json.loads(str(task["result_json"]))
    except ValueError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {
        "name": parsed.get("name"),
        "vmid": parsed.get("vmid"),
        # The node, so the re-join knows where the machine is without asking the
        # inventory a second time. Never rendered: it is infrastructure, not the
        # friend's business.
        "node": parsed.get("node"),
        "ip": parsed.get("ip"),
        "ciuser": parsed.get("ciuser"),
        "tailnet": parsed.get("tailnet"),
        # WHY the tailnet is not up, in the redeemer's own words. Before this
        # the page said "join failed - run tailscale up yourself" to somebody
        # who had just been handed a machine and had no idea which of six
        # things had gone wrong (#628).
        "tailnet_detail": parsed.get("tailnet_detail"),
        "host_id": parsed.get("host_id"),
    }


@router.get("/{token}", response_class=HTMLResponse)
async def invite_form(request: Request, token: str) -> HTMLResponse:
    try:
        cn = _client_cn(request)
        invites = _repo(request)
    except PortalNotConfiguredError as exc:
        return _unavailable(str(exc))
    except PortalUntrustedError:
        return _refused()

    row = await invites.get_by_token(token)
    if row is None or row["bound_cn"] != cn or invite_state(row) != "open":
        return _not_usable()

    return render(
        "form.html",
        cn=cn,
        caps=caps_from_row(row),
        expires_at=row["expires_at"],
        post_url=request.url.path,
        submitted={"ciuser": "", "ssh_authorized_key": "", "hostname": ""},
        error=None,
    )


@router.post("/{token}", response_class=HTMLResponse)
async def invite_redeem(request: Request, token: str) -> Any:
    """Redeem an invite.

    Exactly four field names are read from the body. Caps come from the invite
    row, so a post carrying cores/template_vmid/node changes nothing at all.
    """
    try:
        cn = _client_cn(request)
        invites = _repo(request)
    except PortalNotConfiguredError as exc:
        return _unavailable(str(exc))
    except PortalUntrustedError:
        return _refused()

    peer = request.client.host if request.client else "?"
    if not _redeem_attempts.allow(f"{peer}|{cn}"):
        return _too_many()

    row = await invites.get_by_token(token)
    if row is None or row["bound_cn"] != cn or invite_state(row) != "open":
        return _not_usable()

    caps = caps_from_row(row)
    fields = await _read_form(request)
    try:
        identity = RedemptionIdentity(
            ciuser=fields.get("ciuser", ""),
            ssh_authorized_key=fields.get("ssh_authorized_key", ""),
            hostname=fields.get("hostname") or None,
            tailscale_auth_key=fields.get("tailscale_auth_key") or None,
        )
    except ValidationError as exc:
        return render(
            "form.html",
            status_code=400,
            cn=cn,
            caps=caps,
            expires_at=row["expires_at"],
            post_url=request.url.path,
            submitted={
                "ciuser": fields.get("ciuser", ""),
                "ssh_authorized_key": fields.get("ssh_authorized_key", ""),
                "hostname": fields.get("hostname", ""),
            },
            error=_first_validation_message(exc),
        )

    service = getattr(request.app.state, "provision_service", None)
    if service is None or getattr(service, "proxmox", None) is None:
        return render(
            "message.html",
            status_code=503,
            heading="Cannot build machines right now",
            message="The lab's hypervisor connection is not available. Try again later.",
        )

    # Per-guest budget (#442 G1.5): the invite caps THIS machine, the quota
    # caps the GUEST. Checked before the claim so a blocked redemption leaves
    # the invite open - the friend can free resources and try again.
    from ..guest.quota import check_provision

    decision = await check_provision(
        _hosts_repo(request),
        cn,
        cores=caps.cores or 0,
        memory_mb=caps.memory_mb or 0,
        disk_gb=caps.disk_gb or 0,
    )
    if not decision.allowed:
        return render(
            "message.html",
            status_code=409,
            heading="This would exceed your resource budget",
            message=(
                "Adding this machine would go over your allowance for: "
                + ", ".join(decision.exceeded)
                + ". Remove or shrink one of your machines, or ask for a bigger budget."
            ),
        )

    name = identity.hostname or _default_name(cn, str(row["id"]))
    provision_request = build_provision_request(caps, identity, name=name, owner=cn)

    # Claim BEFORE starting anything: the conditional UPDATE inside claim() is
    # what makes two simultaneous posts produce exactly one machine.
    if not await invites.claim(str(row["id"]), cn):
        return _not_usable()

    try:
        task_id = await service.start(provision_request, actor=f"invite:{row['token_prefix']}")
    except Exception as exc:
        # No task and no VM exist, so the invite must not stay burned: release
        # the claim (guarded on resulting_task_id IS NULL) and let them retry.
        await invites.release_claim(str(row["id"]))
        logger.warning("Invite %s could not start a provision: %s", row["token_prefix"], exc)
        return render(
            "message.html",
            status_code=503,
            heading="The build did not start",
            message="Something went wrong before your machine was created. Please try again.",
        )

    await invites.record_task(str(row["id"]), task_id)
    return RedirectResponse(
        url=f"{request.url.path}/status", status_code=303, headers=dict(SECURITY_HEADERS)
    )


def _cleanup_verdict(task: dict[str, Any] | None) -> str:
    """What a failed provision established about what it left behind.

    "" when the task says nothing - which is the answer for a task written by an
    older build, and is deliberately NOT treated as "nothing remains".
    """
    if not task or not task.get("result_json"):
        return ""
    try:
        parsed = json.loads(str(task["result_json"]))
    except ValueError:
        return ""
    if not isinstance(parsed, dict):
        return ""
    return str(parsed.get("cleanup") or "")


def _rejoin_facts(task: dict[str, Any] | None) -> dict[str, Any]:
    """What the LAST tailnet re-join established, if one was ever started (#628).

    Named fields only, like `_machine_facts` and for the same reason: a task row
    carries operator-facing error text that is not the friend's business. The
    outcome and its reason are.
    """
    if not task:
        return {}
    facts: dict[str, Any] = {"status": str(task.get("status") or "")}
    raw = task.get("result_json")
    if raw:
        try:
            parsed = json.loads(str(raw))
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            facts["tailnet"] = parsed.get("tailnet")
            facts["tailnet_detail"] = parsed.get("tailnet_detail")
    return facts


async def _render_status(
    request: Request,
    invites: InviteRepository,
    row: dict[str, Any],
    token: str,
    *,
    join_error: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    """The redeemer's own page: what their machine is, and what its tailnet is doing.

    Shared by GET /{token}/status and by every failing branch of the re-join
    POST, so a rejected key comes back on the page it was typed on rather than
    on a dead end the friend has to navigate away from.
    """
    task_repo = getattr(request.app.state, "task_repo", None)
    task = None
    if task_repo is not None and row["resulting_task_id"]:
        task = await task_repo.get_task(str(row["resulting_task_id"]))

    status_name = str(task["status"]) if task else "pending"
    facts = _machine_facts(task)

    if status_name == "succeeded":
        state = "ok"
        # The invite learns its host the moment the task reports one, so the
        # operator can answer "which machine came from this invite" from the
        # invite row alone.
        if facts.get("host_id") and not row["resulting_host_id"]:
            await invites.record_host(str(row["id"]), str(facts["host_id"]))
    elif status_name in ("failed", "cancelled"):
        state = "bad"
        # A build that failed WITHOUT creating a machine must not burn the
        # friend's one link (#625). The first real redemption on prod died on a
        # stale operator write token and left a blameless person needing a
        # freshly minted invite. Only when nothing was built: a failure that
        # left a VM behind is an operator's clean-up, not a link to hand back.
        # ESTABLISHED, not assumed. The provision task now records its unwind
        # structurally, so "nothing is left on the node" is something this can
        # READ - `nothing_created` (the failure was before the clone) or
        # `deleted` (the guest was taken back and the destroy was waited on). A
        # cleanup that FAILED, or a task too old to say either way, leaves a
        # machine that may still exist, and handing the link back then would
        # give out a second machine past the quota.
        cleanup = _cleanup_verdict(task)
        nothing_built = (
            cleanup in ("nothing_created", "deleted")
            and facts.get("vmid") is None
            and not row["resulting_host_id"]
        )
        if nothing_built and await invites.reopen_after_failed_build(
            str(row["id"]), str(row["resulting_task_id"])
        ):
            logger.info(
                "Invite %s reopened: the build failed before any machine existed",
                row["token_prefix"],
            )
            return render(
                "message.html",
                status_code=status_code,
                heading="The build failed - your link still works",
                message=(
                    "Something went wrong while your machine was being created, and "
                    "nothing was built. Your invite link has been reopened, so open "
                    "it again to retry. If it fails a second time, the operator needs "
                    "to look at it."
                ),
            )
    else:
        state = "running"

    # A re-join, if one was ever started from this page. Its answer WINS over
    # the provision's: it is the newer reading of the same question, and the
    # whole point of the retry is that the first answer is no longer current.
    rejoin: dict[str, Any] = {}
    if task_repo is not None and row["rejoin_task_id"]:
        rejoin = _rejoin_facts(await task_repo.get_task(str(row["rejoin_task_id"])))
    tailnet = facts.get("tailnet")
    tailnet_detail = facts.get("tailnet_detail")
    rejoin_running = bool(rejoin) and rejoin.get("status") in ("pending", "running")
    if rejoin and not rejoin_running and rejoin.get("tailnet") is not None:
        tailnet = rejoin.get("tailnet")
        tailnet_detail = rejoin.get("tailnet_detail")
    elif rejoin and not rejoin_running and rejoin.get("status") == "failed":
        # A re-join task that failed outright established nothing, and must not
        # leave the older verdict standing as if it had (#642).
        tailnet = "unknown"
        tailnet_detail = "The retry could not be run. Ask the operator to look at it."

    return render(
        "status.html",
        status_code=status_code,
        state=state,
        # The page stops refreshing when the machine is settled AND no re-join
        # is in flight - otherwise it would sit on "joining" forever.
        finished=state != "running" and not rejoin_running,
        result={k: facts.get(k) for k in ("name", "vmid", "ip")},
        tailnet=tailnet,
        tailnet_detail=tailnet_detail,
        rejoin_running=rejoin_running,
        # The form posts back to the invite's own path, exactly like the
        # redemption form does - the token is already in the address bar, and
        # building the URL here keeps it out of the template's hands.
        join_url=f"/invite/{token}/tailnet-join",
        # Only a machine that exists can be re-joined, and only when a key was
        # part of the deal in the first place: an invite whose redeemer never
        # gave one gets no form, because there would be nothing to retry.
        can_rejoin=state == "ok" and facts.get("vmid") is not None and tailnet is not None,
        join_error=join_error,
        username=facts.get("ciuser") or "",
    )


@router.get("/{token}/status", response_class=HTMLResponse)
async def invite_status(request: Request, token: str) -> HTMLResponse:
    try:
        cn = _client_cn(request)
        invites = _repo(request)
    except PortalNotConfiguredError as exc:
        return _unavailable(str(exc))
    except PortalUntrustedError:
        return _refused()

    row = await invites.get_by_token(token)
    # No open-state check here: this page exists precisely for an invite that
    # has already been redeemed. The CN binding is still enforced.
    if row is None or row["bound_cn"] != cn or row["redeemed_at"] is None:
        return _not_usable()

    return await _render_status(request, invites, row, token)


@router.post("/{token}/tailnet-join", response_class=HTMLResponse)
async def invite_rejoin_tailnet(request: Request, token: str) -> Any:
    """Retry the tailnet join on the machine this invite built, with a FRESH key.

    The redeemer's surface, and the primary one: they are the person holding the
    key, and the commonest reason a join failed - an expired or already-used key
    - can only be fixed by somebody who can mint another. Before this, the
    status page told them to go and run `tailscale up` themselves on a machine
    they had just been handed and could not necessarily reach.

    Deliberately NOT a CLI command: an `--auth-key tskey-...` flag puts the key
    in an argv and in the operator's shell history, which is the one property
    this entire code path exists to protect.

    The key is read from the form, handed to ProvisionService, and forgotten. It
    is not stored on the invite, in the task row, in the audit row or in a log
    line - exactly like the one the redemption form takes.
    """
    try:
        cn = _client_cn(request)
        invites = _repo(request)
    except PortalNotConfiguredError as exc:
        return _unavailable(str(exc))
    except PortalUntrustedError:
        return _refused()

    row = await invites.get_by_token(token)
    if row is None or row["bound_cn"] != cn or row["redeemed_at"] is None:
        return _not_usable()
    if row["revoked_at"]:
        # A revoked invite is an operator saying "this person is done here". The
        # machine keeps running - revoking an invite has never destroyed one -
        # but nothing new is started from it.
        return _not_usable()

    peer = request.client.host if request.client else "?"
    if not _rejoin_attempts.allow(f"{peer}|{cn}"):
        return _too_many()

    fields = await _read_form(request)
    try:
        key = validate_tailscale_auth_key(fields.get("tailscale_auth_key", ""))
    except ValueError as exc:
        # str(exc) is the validator's own message and names the FIELD, never the
        # value: an error that quoted the key back would put it in the friend's
        # browser history and in the server log.
        return await _render_status(
            request, invites, row, token, join_error=str(exc), status_code=400
        )

    service = getattr(request.app.state, "provision_service", None)
    if service is None or getattr(service, "proxmox", None) is None:
        return await _render_status(
            request,
            invites,
            row,
            token,
            join_error="The lab's hypervisor connection is not available. Try again later.",
            status_code=503,
        )

    task_repo = getattr(request.app.state, "task_repo", None)
    task = None
    if task_repo is not None and row["resulting_task_id"]:
        task = await task_repo.get_task(str(row["resulting_task_id"]))
    facts = _machine_facts(task)
    vmid = facts.get("vmid")
    node = facts.get("node")
    if not isinstance(vmid, int) or not node:
        # The machine this invite built is the ONLY one this form can reach, and
        # it is named by the invite's own provision result - never by anything
        # the browser posted. A redeemer cannot aim this at a guest that is not
        # theirs, because they have no say in the target at all.
        return await _render_status(
            request,
            invites,
            row,
            token,
            join_error=(
                "This invite has no finished machine to join yet. Wait for the build "
                "to finish, then try again."
            ),
            status_code=409,
        )

    try:
        task_id = await service.start_tailnet_join(
            node=str(node),
            vmid=vmid,
            hostname=str(facts.get("name") or ""),
            key=key,
            actor=f"invite:{row['token_prefix']}",
        )
    except TailnetJoinConflictError:
        return await _render_status(
            request,
            invites,
            row,
            token,
            join_error=(
                "A tailnet join is already running for this machine. Wait for it to "
                "finish before sending another key."
            ),
            status_code=409,
        )
    except Exception as exc:
        logger.warning("Invite %s could not start a tailnet re-join: %s", row["token_prefix"], exc)
        return await _render_status(
            request,
            invites,
            row,
            token,
            join_error="The retry did not start. Please try again.",
            status_code=503,
        )

    await invites.record_rejoin_task(str(row["id"]), task_id)
    return RedirectResponse(
        url=f"/invite/{token}/status", status_code=303, headers=dict(SECURITY_HEADERS)
    )
