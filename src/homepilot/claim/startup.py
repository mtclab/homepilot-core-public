from __future__ import annotations

import contextlib
import logging
from pathlib import Path

from .repository import ClaimRepository, has_admin_credential
from .tokens import generate_claim_code, validate_token

logger = logging.getLogger(__name__)

# The operator-facing copy of the pending claim code, beside .vault_passphrase
# and .secret_key in the data directory and written with the same 0600 mode.
#
# The DATABASE stores only the sha256 - a copy of the database can never redeem
# a claim. This file exists so `hp claim-code` can print the code on demand: a
# hash cannot be printed, and the alternative (a fresh code per boot) would leave
# a stale code in the operator's scrollback. Holding it costs nothing extra -
# anyone who can read it also holds the vault passphrase sitting beside it, which
# decrypts the Proxmox token - and it is deleted the moment the claim succeeds.
_CLAIM_CODE_FILENAME = ".claim_code"

_BOX_WIDTH = 68


def claim_code_path(data_dir: Path | str) -> Path:
    return Path(data_dir) / _CLAIM_CODE_FILENAME


def clear_claim_code(data_dir: Path | str) -> None:
    """Remove the operator-facing copy once the instance is claimed."""
    with contextlib.suppress(OSError):
        claim_code_path(data_dir).unlink(missing_ok=True)


def read_claim_code(data_dir: Path | str) -> str:
    try:
        return claim_code_path(data_dir).read_text().strip()
    except OSError:
        return ""


def _write_claim_code(data_dir: Path | str, code: str) -> bool:
    path = claim_code_path(data_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(code)
        path.chmod(0o600)
        return True
    except OSError:
        logger.warning(
            "Could not persist the claim code to %s - it is printed above and "
            "will NOT be printed again on the next start.",
            path,
            exc_info=True,
        )
        return False


def _box(lines: list[str]) -> str:
    top = "┌" + "─" * _BOX_WIDTH + "┐"
    bottom = "└" + "─" * _BOX_WIDTH + "┘"
    body = [f"│ {line:<{_BOX_WIDTH - 2}} │" for line in lines]
    return "\n".join(["", top, *body, bottom])


def _log_first_boot(code: str, url_hint: str, rotated: bool) -> None:
    """Announce an unclaimed instance, once, when its code is created.

    The NORMAL way in is the first line of this box, not the code: a browser on
    the same network claims the instance with nothing to type. The code is
    printed underneath for the one case the browser path refuses - an instance
    reached from outside its own network - and `hp claim-code` reprints it, so
    reading container logs is a fallback and never the documented path.

    WARNING level on purpose: `info` is the default level but scrolls past in a
    wall of startup lines, and this is the one thing the operator must act on.
    """
    lines = [
        "HomePilot is UNCLAIMED - nobody can sign in yet.",
        "",
        f"Open {url_hint} from this network and claim it.",
        "Anyone on this network can, so do it now.",
        "",
        "The same screen takes your Proxmox address and API token,",
        "so the instance is finished in one step. Both are optional.",
        "",
        "Reaching it from OUTSIDE this network needs the claim code",
        "below. `hp claim-code` prints it again at any time.",
        "",
        f"    {code}",
    ]
    if rotated:
        lines += [
            "",
            "NOTE: this is a NEW code. Any code printed before this line",
            "no longer works (the saved copy was gone).",
        ]
    logger.warning("%s", _box(lines))


def _log_still_unclaimed(url_hint: str) -> None:
    """Every later start while unclaimed: a reminder, WITHOUT the code.

    Reprinting the secret on every restart would spread it through the log
    history for no gain - the browser path needs no code, and `hp claim-code`
    covers the exposed case on demand.
    """
    logger.warning(
        "HomePilot is UNCLAIMED - open %s from this network to claim it "
        "(reaching it from outside needs the code: run `hp claim-code`).",
        url_hint,
    )


async def ensure_claim_code(
    claims: ClaimRepository,
    data_dir: Path | str,
    url_hint: str = "http://<this-host>:8000/ui",
) -> str | None:
    """Make sure an unclaimed instance has a claim code, and show it.

    Returns the plaintext code while the instance is claimable, or None when it
    is already claimed - in which case NOTHING is generated, logged or written.

    Restart safety: an unclaimed instance keeps the code it already has. The
    stored hash is only replaced when the operator-facing copy has gone missing,
    and the replacement says so loudly rather than leaving a stale code in the
    scrollback silently.
    """
    if await has_admin_credential(claims.db):
        return None

    row = await claims.get()
    if row is not None and row["claimed_at"] is not None:
        return None

    if row is not None:
        existing = read_claim_code(data_dir)
        if existing and validate_token(existing, str(row["code_hash"])):
            _log_still_unclaimed(url_hint)
            return existing

    code, prefix, code_hash = generate_claim_code()
    await claims.install_code(prefix, code_hash)
    _log_first_boot(code, url_hint, rotated=row is not None)
    _write_claim_code(data_dir, code)
    return code
