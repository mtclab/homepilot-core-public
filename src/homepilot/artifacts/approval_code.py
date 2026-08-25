"""Per-artifact approval codes: the human-relay gate on MCP approval (#385 follow-up).

An artifact's approval code is a short, unguessable secret generated when the
artifact is PROPOSED. A human reads it from an operator surface (the web review
screen, `hp artifacts show`, or a webhook notification) and relays it to the
assistant, which passes it to `approve_artifact(artifact_id, approval_code)` over
MCP. The server verifies it constant-time against the stored code, so a valid
code is proof a human decided - the assistant cannot fabricate one.

The code is NEVER returned by any MCP tool (get_artifact / query_artifacts read
the artifact FILE, and the code lives only in a DB table those reads never
touch), so the assistant has no way to learn it except from the human.

Design choices (all vetoable):
* Alphabet: Crockford-style, no 0/1/I/L/O/U so a human never misreads a relay.
* Length: 10 chars over a 30-char alphabet ~= 49 bits, well past the >=8 floor.
* Lock: 5 wrong codes locks approval for that artifact until an operator resets.
"""

from __future__ import annotations

import hmac
import re
import secrets
from typing import Any

# No 0/O, 1/I/L, U - the characters a human most often mis-relays.
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTVWXYZ23456789"  # pragma: allowlist secret
CODE_LENGTH = 10
# Wrong-code attempts before approval is locked for the artifact (operator reset
# required). Small enough that even a weak code cannot be brute-forced over MCP.
LOCK_THRESHOLD = 5

_NON_ALNUM = re.compile(r"[^A-Za-z0-9]")


def generate_code() -> str:
    """A fresh, cryptographically-random approval code in the canonical form."""
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


def normalize_code(raw: str) -> str:
    """Canonicalise a human-relayed code: strip spaces/hyphens, uppercase.

    So "abcd-efgh-jk", "ABCDEFGHJK" and "abcd efgh jk" all compare equal to the
    stored "ABCDEFGHJK" - a human relaying with or without the display grouping
    still verifies."""
    return _NON_ALNUM.sub("", raw or "").upper()


def verify_code(candidate: str, stored: str) -> bool:
    """Constant-time compare of a relayed code against the stored one.

    Normalises the candidate first; the stored value is already canonical.
    hmac.compare_digest handles length mismatches without leaking via timing."""
    return hmac.compare_digest(normalize_code(candidate), stored or "")


async def ensure_approval_code(repo: Any, artifact_id: str) -> str:
    """The artifact's approval code, generating and storing one if none exists.

    propose() calls this eagerly so the webhook carries the code; the operator
    surfaces call it lazily so an artifact PROPOSED before this feature shipped
    (no code row) gets one the first time a human opens it to review - the
    backfill path. Idempotent: an existing code is returned unchanged, so the
    code is stable for the artifact's PROPOSED life."""
    row = await repo.get_approval_code_row(artifact_id)
    if row is not None:
        return str(row["code"])
    code = generate_code()
    await repo.set_approval_code(artifact_id, code)
    return code


def format_for_display(code: str) -> str:
    """Group the canonical code for human reading, e.g. ABCDE-FGHJK.

    Display only - `normalize_code` reverses it, so a human may relay either the
    grouped or the ungrouped form."""
    if not code:
        return code
    mid = len(code) // 2
    return f"{code[:mid]}-{code[mid:]}" if mid else code
