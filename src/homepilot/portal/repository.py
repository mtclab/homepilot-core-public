from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from ..db.connection import Database
from .models import InviteCaps
from .tokens import PREFIX_LENGTH, generate_invite_token, validate_token

# Columns an invite listing may show. The token hash is deliberately absent:
# nothing outside redemption ever needs it, and an operator listing must not
# print material that helps replay an invite.
_LIST_COLUMNS = (
    "id, token_prefix, bound_cn, template_vmid, node, pool, cores, memory_mb, "
    "disk_gb, disk, ipconfig0, expires_at, created_by, created_at, redeemed_at, "
    "redeemed_cn, resulting_host_id, resulting_task_id, revoked_at"
)


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_duration(value: str) -> timedelta:
    """'7d' / '48h' / '90m' -> timedelta. Rejects anything else."""
    text = value.strip().lower()
    units = {"m": "minutes", "h": "hours", "d": "days"}
    if len(text) < 2 or text[-1] not in units:
        raise ValueError("expiry must look like '30m', '48h' or '7d'")
    try:
        amount = int(text[:-1])
    except ValueError as exc:
        raise ValueError("expiry must look like '30m', '48h' or '7d'") from exc
    if amount <= 0:
        raise ValueError("expiry must be positive")
    return timedelta(**{units[text[-1]]: amount})


def invite_state(row: dict[str, Any], now: str | None = None) -> str:
    """One word for what an invite is: revoked / redeemed / expired / open.

    Revoked beats redeemed beats expired so an operator listing never calls a
    revoked invite 'open' because its expiry happens to be in the future.
    """
    moment = now or _now()
    if row.get("revoked_at"):
        return "revoked"
    if row.get("redeemed_at"):
        return "redeemed"
    if str(row["expires_at"]) <= moment:
        return "expired"
    return "open"


class InviteRepository:
    def __init__(self, db: Database):
        self.db = db

    async def create_invite(
        self,
        bound_cn: str,
        caps: InviteCaps,
        created_by: str,
        ttl: timedelta,
    ) -> tuple[str, str]:
        """Mint an invite. Returns (invite_id, full_token) — the token exists in
        this return value and nowhere else, ever."""
        full_token, prefix, token_hash = generate_invite_token()
        invite_id = str(uuid.uuid4())
        await self.db.execute(
            """INSERT INTO invites (
                   id, token_prefix, token_hash, bound_cn, template_vmid, node, pool,
                   cores, memory_mb, disk_gb, disk, ipconfig0, expires_at, created_by, created_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                invite_id,
                prefix,
                token_hash,
                bound_cn,
                caps.template_vmid,
                caps.node,
                caps.pool,
                caps.cores,
                caps.memory_mb,
                caps.disk_gb,
                caps.disk,
                caps.ipconfig0,
                _iso(datetime.now(UTC) + ttl),
                created_by,
                _now(),
            ),
        )
        await self.db.conn.commit()
        return invite_id, full_token

    async def get_by_token(self, token: str) -> dict[str, Any] | None:
        """Resolve a presented token to its invite row, or None.

        Looks up by prefix (indexed) and then compares the sha256 in constant
        time — the same two-step api_tokens uses, so a timing signal cannot
        distinguish a wrong token from a missing one.
        """
        if not token:
            return None
        row = await self.db.fetchone(
            "SELECT * FROM invites WHERE token_prefix = ?", (token[:PREFIX_LENGTH],)
        )
        if row is None:
            return None
        if not validate_token(token, str(row["token_hash"])):
            return None
        return row

    async def get_by_prefix(self, prefix: str) -> dict[str, Any] | None:
        return await self.db.fetchone(
            f"SELECT {_LIST_COLUMNS} FROM invites WHERE token_prefix = ?", (prefix,)
        )

    async def list_invites(self) -> list[dict[str, Any]]:
        return await self.db.fetchall(
            f"SELECT {_LIST_COLUMNS} FROM invites ORDER BY created_at DESC"
        )

    async def revoke(self, prefix: str) -> bool:
        """Revoke by prefix. Idempotent-ish: an already-revoked invite reports False."""
        cursor = await self.db.execute(
            "UPDATE invites SET revoked_at = ? WHERE token_prefix = ? AND revoked_at IS NULL",
            (_now(), prefix),
        )
        await self.db.conn.commit()
        return cursor.rowcount == 1

    async def claim(self, invite_id: str, redeemer_cn: str) -> bool:
        """Take single-use ownership of an invite. True exactly once.

        A conditional UPDATE plus rowcount, never read-then-write: two requests
        that both read 'not yet redeemed' would otherwise both provision. The
        expiry and revocation checks live in this same statement for the same
        reason — a check made before the claim is a check that can go stale.
        """
        cursor = await self.db.execute(
            """UPDATE invites SET redeemed_at = ?, redeemed_cn = ?
               WHERE id = ?
                 AND redeemed_at IS NULL
                 AND revoked_at IS NULL
                 AND expires_at > ?
                 AND bound_cn = ?""",
            (_now(), redeemer_cn, invite_id, _now(), redeemer_cn),
        )
        await self.db.conn.commit()
        return cursor.rowcount == 1

    async def release_claim(self, invite_id: str) -> None:
        """Undo a claim whose provision never started.

        Only ever called when task creation itself raised, so no VM and no task
        exist: burning the friend's one invite on a transient backend error
        would be the wrong trade. Guarded on resulting_task_id IS NULL so it can
        never unlatch an invite that did start a provision.
        """
        await self.db.execute(
            """UPDATE invites SET redeemed_at = NULL, redeemed_cn = NULL
               WHERE id = ? AND resulting_task_id IS NULL""",
            (invite_id,),
        )
        await self.db.conn.commit()

    async def record_task(self, invite_id: str, task_id: str) -> None:
        await self.db.execute(
            "UPDATE invites SET resulting_task_id = ? WHERE id = ?", (task_id, invite_id)
        )
        await self.db.conn.commit()

    async def record_host(self, invite_id: str, host_id: str) -> None:
        await self.db.execute(
            "UPDATE invites SET resulting_host_id = ? WHERE id = ?", (host_id, invite_id)
        )
        await self.db.conn.commit()
