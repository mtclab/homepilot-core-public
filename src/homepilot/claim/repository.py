from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ..auth.tokens import normalize_scope
from ..db.connection import Database

# The one row. Written as a constant so no call site can drift onto another id.
_ROW_ID = 1


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


async def has_admin_credential(db: Database) -> bool:
    """Whether this instance already holds an admin-capable API token.

    Every install that predates the claim path has one, which is exactly what
    makes it claimed by definition: the claim exists to hand out the FIRST admin
    credential, and an instance that already issued one has nothing to hand out.

    Expiry is deliberately NOT considered. An expired admin token still proves an
    operator once held one, and treating expiry as "unclaimed again" would reopen
    the claim path on a live instance simply by waiting - the wrong direction to
    fail. Such an instance is recovered through `hp init` / the admin secret.
    """
    rows = await db.fetchall("SELECT scope, role FROM api_tokens")
    for row in rows:
        normalized = normalize_scope(row.get("scope"), row.get("role"))
        if "*" in normalized or "admin" in normalized:
            return True
    return False


class ClaimRepository:
    """The instance's claim row: the code hash, and the single-use latch."""

    def __init__(self, db: Database):
        self.db = db

    async def get(self) -> dict[str, Any] | None:
        return await self.db.fetchone(
            "SELECT * FROM instance_claim WHERE id = ?",
            (_ROW_ID,),
        )

    async def install_code(self, prefix: str, code_hash: str) -> None:
        """Write (or replace) the pending claim code.

        REPLACE rather than INSERT so a rotation - the recovery path when the
        operator-facing copy of the code is gone - reuses this one statement.
        Replacing a CLAIMED row would silently reopen a claimed instance, so
        every caller must check `claimed_at` first; ensure_claim_code does.
        """
        await self.db.execute(
            "INSERT OR REPLACE INTO instance_claim "
            "(id, code_prefix, code_hash, created_at, claimed_at, claimed_label, "
            "minted_token_prefix) VALUES (?, ?, ?, ?, NULL, NULL, NULL)",
            (_ROW_ID, prefix, code_hash, _now()),
        )
        await self.db.conn.commit()

    async def claim(self, label: str) -> bool:
        """Take the claim. True exactly once, for the whole life of the instance.

        A conditional UPDATE plus rowcount, never read-then-write: two requests
        that both read 'not yet claimed' would otherwise both mint an admin
        token. This latch is what makes the claim single-use.
        """
        cursor = await self.db.execute(
            "UPDATE instance_claim SET claimed_at = ?, claimed_label = ? "
            "WHERE id = ? AND claimed_at IS NULL",
            (_now(), label, _ROW_ID),
        )
        await self.db.conn.commit()
        return cursor.rowcount == 1

    async def release(self) -> None:
        """Undo a latch whose token was never minted.

        Only reachable when minting (or storing the Proxmox credentials) raised
        AFTER the latch, so no admin token exists and nobody can reach the
        instance: burning the one claim on a transient failure would leave the
        install permanently unreachable. Guarded on minted_token_prefix IS NULL
        so it can never unlatch a claim that did mint a token.
        """
        await self.db.execute(
            "UPDATE instance_claim SET claimed_at = NULL, claimed_label = NULL "
            "WHERE id = ? AND minted_token_prefix IS NULL",
            (_ROW_ID,),
        )
        await self.db.conn.commit()

    async def record_minted_token(self, prefix: str) -> None:
        await self.db.execute(
            "UPDATE instance_claim SET minted_token_prefix = ? WHERE id = ?",
            (prefix, _ROW_ID),
        )
        await self.db.conn.commit()

    async def is_claimed(self) -> bool:
        """Whether the claim path is closed.

        BOTH conditions are checked on every call, in the same order, so the
        answer costs the same two queries either way and an existing install
        never has to be restarted for the claim path to close behind it: minting
        an admin token through any other route (`hp init`, the admin secret)
        shuts the claim down on the very next request.
        """
        row = await self.get()
        admin_exists = await has_admin_credential(self.db)
        return admin_exists or (row is not None and row["claimed_at"] is not None)
