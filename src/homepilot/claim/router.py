from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from ..admin.router import ProxmoxConfigIn, save_proxmox_settings, test_proxmox_settings
from ..auth.tokens import generate_api_token
from ..common import SlidingWindowLimiter
from ..config import get_settings
from ..db.repository import Repository
from .repository import ClaimRepository
from .startup import clear_claim_code
from .tokens import validate_token
from .trust import claim_source

logger = logging.getLogger(__name__)

router = APIRouter(tags=["claim"])

# Claim attempts per client address. The claim is a once-in-an-instance-lifetime
# action, so five tries per five minutes is generous for a person transcribing a
# code and useless to anyone guessing 128 bits. Deliberately per-address and not
# global: a global bucket would let any stranger lock the real operator out of
# their own first login, which is a worse failure than the one it prevents. The
# per-IP HTTP middleware limiter still applies underneath.
_CLAIM_ATTEMPT_LIMIT = 5
_CLAIM_ATTEMPT_WINDOW_SECONDS = 300
_claim_attempts = SlidingWindowLimiter(
    limit=_CLAIM_ATTEMPT_LIMIT, window_seconds=_CLAIM_ATTEMPT_WINDOW_SECONDS
)

# The scope the first credential carries. 'full' normalizes to '*', which is
# what makes it an ADMIN token - the same scope `hp init` and POST /auth/tokens
# hand out, so the claimed instance behaves exactly like a bootstrapped one.
_ADMIN_SCOPE = "full"

# The label of the box's own autocreated CLI credential (see
# _store_local_cli_token). Visible and revocable in Settings -> Tokens like any
# other token - it is a real token, not a hidden back door.
_LOCAL_CLI_LABEL = "local-cli"

# One text for every rejected code. An unclaimed instance ALWAYS has a code, so
# there is nothing to disclose about whether one exists; keeping the wording
# uniform keeps it that way if that ever changes.
_BAD_CODE = "Invalid claim code"

# Refusing the codeless path is not an authentication failure - the caller
# presented nothing and nothing was wrong with what they presented. 403 with a
# message that says what to do next, because the operator on the far side of a
# public address has a real path forward (`hp claim-code`).
_CODE_REQUIRED = (
    "This request did not come from the local network, so a claim code is required. "
    "Run `hp claim-code` on the host to read it."
)

# 410 rather than 401 when the instance is already claimed: the claim path is
# permanently gone, not a credential that failed. It is returned before the code
# is even looked at, so presenting the CORRECT code after the fact learns
# nothing that GET /claim/status does not already publish.
_ALREADY_CLAIMED = "This instance has already been claimed"


class ClaimRequest(BaseModel):
    # OPTIONAL by design: a request from the instance's own network claims it
    # without one (the appliance model - open the page, claim it). A request
    # from anywhere else must present the code. See claim_source().
    code: str | None = Field(None, max_length=256)
    label: str = Field("admin", max_length=64)
    # Proxmox is OPTIONAL: an operator may claim now and wire Proxmox up later
    # in Settings. Supplying one half of the pair is a typo, not a choice, so it
    # is refused rather than half-applied.
    proxmox_host: str | None = Field(None, max_length=253)
    proxmox_port: int | None = Field(None, ge=1, le=65535)
    proxmox_token: str | None = Field(None, max_length=512)
    proxmox_verify_ssl: bool | None = None


def _claims(request: Request) -> ClaimRepository:
    repo = getattr(request.app.state, "claim_repo", None)
    if not isinstance(repo, ClaimRepository):
        raise HTTPException(status_code=503, detail="The backend has not finished starting")
    return repo


def _data_dir(request: Request) -> Path:
    settings = getattr(request.app.state, "settings", None) or get_settings()
    return Path(settings.data_dir)


async def _store_local_cli_token(request: Request, db: Repository, user_id: str) -> None:
    """Mint the box's OWN admin credential and persist it in the data dir.

    Owner rule (2026-08-26): "I know only the login token - the rest should be
    autocreated and live somewhere safe so they won't be deleted". The CLI on the
    box has to authenticate to mint or revoke tokens now, and the human must not
    be handed a second credential to look after - so the claim autocreates one,
    exactly as `hp init` does, and writes it 0600 into the data dir where the
    vault passphrase and the agent-hub token already live. The operator's own
    login token is never written to disk.

    Best effort: a claim that cannot write this file is still a good claim (the
    operator can always export HP_ADMIN_TOKEN), so failures are logged, not
    raised.
    """
    try:
        full_token, prefix, token_hash = generate_api_token()
        await db.create_api_token(
            user_id=user_id,
            token_type="personal",
            prefix=prefix,
            hash=token_hash,
            scope=_ADMIN_SCOPE,
            label=_LOCAL_CLI_LABEL,
            expires_at=None,
        )
        path = _data_dir(request) / "api-token"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(full_token + "\n", encoding="utf-8")
        path.chmod(0o600)
        logger.info("Local CLI admin token autocreated (prefix=%s) at %s", prefix, path)
    except Exception as exc:  # never fail the claim over this
        logger.warning("Could not autocreate the local CLI admin token: %s", exc)


def _source(request: Request) -> tuple[bool, str]:
    """(came from this network, effective client address).

    The trusted-proxy list is the one the HTTP middleware already uses
    (HP_TRUSTED_PROXIES), published on app.state by the lifespan so this router
    does not import main and does not keep a second copy of it.
    """
    trusted = getattr(request.app.state, "trusted_proxy_networks", None) or ()
    return claim_source(request.client.host if request.client else None, request.headers, trusted)


@router.get("/claim/status")
async def claim_status(request: Request) -> dict[str, Any]:
    """Whether this instance can still be claimed, and nothing about the secret.

    An unclaimed instance also says whether THIS caller needs the code. That
    field describes the CALLER'S OWN source address, which the caller already
    knows better than we do - it discloses nothing about the instance. Without
    it the UI would have to guess which of the two screens to draw. A claimed
    instance answers with the single word and no second field at all: no code,
    no prefix, no age, no attempt count.
    """
    local, _client = _source(request)
    if await _claims(request).is_claimed():
        return {"state": "claimed"}
    return {"state": "unclaimed", "code_required": not local}


@router.post("/claim")
async def claim_instance(request: Request, body: ClaimRequest) -> dict[str, Any]:
    """Claim the instance: mint the first admin token and close the path.

    Order matters and is the security of this endpoint:
      1. already claimed  -> 410, before anything else is read at all
      2. rate limit       -> 429, keyed on the EFFECTIVE client address
      3. credential:
           - from this network, no code given -> allowed (the appliance model)
           - from anywhere else, no code       -> 403, the code is required
           - a code given                      -> must be right, 401 otherwise
      4. Proxmox verified against the LIVE API - a failure here must leave the
         claim untouched, so it happens before the latch and before any write
      5. latch (single-use), then store Proxmox, then mint
    A failure after the latch releases it: a half-finished claim must not leave
    an instance that nobody can ever reach.
    """
    claims = _claims(request)
    if await claims.is_claimed():
        raise HTTPException(status_code=status.HTTP_410_GONE, detail=_ALREADY_CLAIMED)

    local, client_ip = _source(request)
    if not _claim_attempts.allow(client_ip):
        raise HTTPException(status_code=429, detail="Too many claim attempts - wait a few minutes")

    presented = (body.code or "").strip()
    if presented:
        # A code that was offered is always checked, local or not: silently
        # ignoring a wrong one would teach an operator that any code works.
        row = await claims.get()
        if row is None or not validate_token(presented, str(row["code_hash"])):
            logger.warning("Rejected claim attempt from %s (bad code)", client_ip)
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=_BAD_CODE)
    elif not local:
        logger.warning("Rejected codeless claim attempt from non-local source %s", client_ip)
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=_CODE_REQUIRED)

    proxmox_config = _proxmox_config(body)
    if proxmox_config is not None:
        if getattr(request.app.state, "vault", None) is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    "The vault is not configured, so Proxmox credentials cannot be stored. "
                    "Claim without them and configure Proxmox once the vault is unlocked."
                ),
            )
        # The admin settings page's own verification, called rather than
        # reimplemented, so the claim can never accept credentials the Settings
        # screen would reject.
        result = await test_proxmox_settings(request, proxmox_config)
        if result.get("status") != "ok":
            raise HTTPException(
                status_code=400,
                detail=f"Proxmox verification failed: {result.get('message', 'unknown error')}",
            )

    if not await claims.claim(body.label):
        # Lost a race with a simultaneous claim. The other one won; this one
        # mints nothing.
        raise HTTPException(status_code=status.HTTP_410_GONE, detail=_ALREADY_CLAIMED)

    try:
        proxmox_configured = False
        if proxmox_config is not None:
            # Same PUT /admin/settings/proxmox path the Settings screen uses: it
            # stores into the vault AND runs the reload that rebinds the live
            # ProxmoxClient onto the inventory service, so the reconciler picks
            # the credentials up without a restart.
            saved = await save_proxmox_settings(request, proxmox_config)
            if saved.get("status") != "ok":
                raise RuntimeError(f"storing Proxmox settings failed: {saved.get('message')}")
            proxmox_configured = True

        full_token, prefix, token_hash = generate_api_token()
        db: Repository = request.app.state.repo
        user_id = await db.create_user(display_name=body.label, auth_source="claim")
        await db.create_api_token(
            user_id=user_id,
            token_type="personal",
            prefix=prefix,
            hash=token_hash,
            scope=_ADMIN_SCOPE,
            label=body.label,
            expires_at=None,
        )
        await claims.record_minted_token(prefix)
        # The box's own credential, so the CLI on it can mint and revoke without
        # the operator managing a second secret (owner rule, 2026-08-26).
        await _store_local_cli_token(request, db, str(user_id))
    except Exception:
        # Releasing the latch leaves the instance claimable, which is the point.
        # Proxmox settings stored just above are deliberately NOT rolled back:
        # they were verified against the live API and are what the operator
        # asked for, and a retry of the claim overwrites them with the same
        # values anyway.
        await claims.release()
        logger.exception("Claim failed after the latch was taken - claim released")
        raise

    clear_claim_code(_data_dir(request))
    logger.warning(
        "Instance CLAIMED from %s (local=%s, code_presented=%s, token prefix=%s, "
        "proxmox_configured=%s) - the claim path is now closed permanently",
        client_ip,
        local,
        bool(presented),
        prefix,
        proxmox_configured,
    )
    return {
        "token": full_token,
        "scope": _ADMIN_SCOPE,
        "proxmox_configured": proxmox_configured,
    }


def _proxmox_config(body: ClaimRequest) -> ProxmoxConfigIn | None:
    """The Proxmox half of the body, or None when it was omitted."""
    host = (body.proxmox_host or "").strip()
    token = (body.proxmox_token or "").strip()
    if not host and not token:
        return None
    if not host or not token:
        raise HTTPException(
            status_code=400,
            detail="Proxmox needs both an address and an API token, or neither",
        )
    return ProxmoxConfigIn(
        host=host,
        port=body.proxmox_port,
        verify_ssl=body.proxmox_verify_ssl,
        token=token,
    )
