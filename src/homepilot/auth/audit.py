"""Durable audit rows for the credential lifecycle (#686).

Minting, revoking and claiming create or destroy fleet-root credentials, and a
login opens a 30-day session with one - yet none of them wrote an audit row, so
the trail an operator reads could not say who made a token, who revoked one or
when the instance was claimed. Every such action now records:

* ``user_id`` - WHO acted: the authenticated credential (label and token
  prefix, via the same Principal naming the artifact lifecycle uses), or
  ``admin-secret``, ``claim`` or ``cli``;
* ``action`` - ``token_minted`` / ``token_revoked`` / ``instance_claimed`` /
  ``login``;
* ``details_json`` - WHICH credential: its prefix, label and scope, plus the
  client address where there is one. Never the token itself: the prefix is the
  token's public id (revocation is keyed on it), the rest is a secret.
"""

from __future__ import annotations

import json
from typing import Any

from .principal import ACTOR_ADMIN_SECRET, audit_actor

ACTION_MINTED = "token_minted"
ACTION_REVOKED = "token_revoked"
ACTION_CLAIMED = "instance_claimed"
ACTION_LOGIN = "login"

__all__ = ["ACTOR_ADMIN_SECRET", "audit_actor", "audit_credential_event"]


async def audit_credential_event(
    repo: Any,
    *,
    action: str,
    actor: str,
    source: str,
    prefix: str | None,
    label: str | None = None,
    scope: str | None = None,
    client: str | None = None,
) -> None:
    """Write one audit row about a credential. Commits (log_audit does)."""
    details: dict[str, Any] = {"prefix": prefix, "label": label, "scope": scope}
    if client:
        details["client"] = client
    await repo.log_audit(
        user_id=actor,
        source=source,
        action=action,
        details_json=json.dumps(details),
    )
