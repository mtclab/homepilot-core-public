from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from homepilot.db.connection import Database

logger = logging.getLogger(__name__)

TOKEN_LENGTH = 32
TOKEN_EXPIRY_SECONDS = 86400  # 24 hours


def generate_bootstrap_token() -> str:
    """Generate a one-time bootstrap token for agent enrollment."""
    return f"hpbat_{secrets.token_urlsafe(TOKEN_LENGTH)}"


def hash_token(token: str) -> str:
    """Hash a token for storage/comparison."""
    return hashlib.sha256(token.encode()).hexdigest()


def verify_token(token: str, stored_hash: str) -> bool:
    """Verify a token against its stored hash."""
    return hmac.compare_digest(hash_token(token), stored_hash)


def generate_agent_id() -> str:
    """Generate a unique agent ID."""
    return f"agent-host-{secrets.token_urlsafe(16)}"


class BootstrapTokenStore:
    """Token store with optional DB persistence.

    When a ``Database`` instance is provided, tokens are persisted to the
    ``bootstrap_tokens`` table so they survive hub restarts.  The in-memory
    cache is always kept as a fast lookup layer; the DB is the source of
    truth.

    If no DB is available (``db=None``), the store operates in pure
    in-memory mode, which matches the original behaviour.
    """

    def __init__(
        self,
        expiry_seconds: int = TOKEN_EXPIRY_SECONDS,
        db: Database | None = None,
    ) -> None:
        self._tokens: dict[str, tuple[str, float]] = {}
        self._expiry_seconds = expiry_seconds
        self._db = db

    async def _ensure_table(self) -> None:
        """Create the bootstrap_tokens table if it doesn't exist."""
        if self._db is None:
            return
        await self._db.execute(
            """CREATE TABLE IF NOT EXISTS bootstrap_tokens (
                token_hash TEXT PRIMARY KEY,
                expires_at REAL NOT NULL
            )""",
        )
        await self._db.conn.commit()

    async def load_from_db(self) -> None:
        """Load existing tokens from DB into the in-memory cache.

        Should be called once during startup after the DB connection is
        established.  Expired tokens are pruned during load.
        """
        if self._db is None:
            return
        await self._ensure_table()
        now = time.time()
        rows = await self._db.fetchall(
            "SELECT token_hash, expires_at FROM bootstrap_tokens",
        )
        loaded = 0
        pruned = 0
        for row in rows:
            if row["expires_at"] <= now:
                await self._db.execute(
                    "DELETE FROM bootstrap_tokens WHERE token_hash = ?",
                    (row["token_hash"],),
                )
                pruned += 1
            else:
                # We store the hash as the cache key since we can't
                # reconstruct the raw token from the hash.
                self._tokens[row["token_hash"]] = (
                    row["token_hash"],
                    row["expires_at"],
                )
                loaded += 1
        if pruned > 0:
            await self._db.conn.commit()
        if loaded or pruned:
            logger.info(
                "BootstrapTokenStore: loaded %d tokens, pruned %d expired",
                loaded,
                pruned,
            )

    def create_sync(self) -> str:
        """Generate a bootstrap token (in-memory only, no DB write).

        Use :meth:`create` for the async version that also persists to DB.
        """
        token = generate_bootstrap_token()
        expires_at = time.time() + self._expiry_seconds
        # Key the cache by the token HASH (not the raw token). Tokens reloaded
        # from the DB after a restart are only known by their hash, so verify()/
        # consume() must look them up by hash too — keep all paths consistent.
        token_hash = hash_token(token)
        self._tokens[token_hash] = (token_hash, expires_at)
        return token

    async def create(self) -> str:
        """Generate a bootstrap token and persist it to DB."""
        token = self.create_sync()
        if self._db is not None:
            token_hash, expires_at = self._tokens[hash_token(token)]
            await self._db.execute(
                "INSERT INTO bootstrap_tokens (token_hash, expires_at) VALUES (?, ?)",
                (token_hash, expires_at),
            )
            await self._db.conn.commit()
        return token

    def verify(self, token: str) -> bool:
        """Check whether a token is valid (not expired). Does NOT consume."""
        token_hash = hash_token(token)
        entry = self._tokens.get(token_hash)
        if entry is None:
            return False
        _, expires_at = entry
        if time.time() > expires_at:
            del self._tokens[token_hash]
            return False
        return True

    async def consume(self, token: str) -> bool:
        """Verify and consume (one-time use) a bootstrap token.

        Removes the token from both the in-memory cache and the DB.
        """
        if not self.verify(token):
            return False
        entry = self._tokens.pop(hash_token(token), None)
        if entry is None:
            return False
        if self._db is not None:
            await self._db.execute(
                "DELETE FROM bootstrap_tokens WHERE token_hash = ?",
                (entry[0],),
            )
            await self._db.conn.commit()
        return True

    def cleanup(self) -> int:
        """Remove expired tokens from in-memory cache. Returns count removed.

        This only cleans the in-memory layer.  Use :meth:`cleanup_db` for
        the persisted layer.
        """
        now = time.time()
        expired = [t for t, (_, exp) in self._tokens.items() if now > exp]
        for t in expired:
            del self._tokens[t]
        return len(expired)

    async def cleanup_db(self) -> int:
        """Remove expired tokens from DB. Returns count removed."""
        if self._db is None:
            return 0
        now = time.time()
        cursor = await self._db.execute(
            "DELETE FROM bootstrap_tokens WHERE expires_at <= ?",
            (now,),
        )
        await self._db.conn.commit()
        return cursor.rowcount
