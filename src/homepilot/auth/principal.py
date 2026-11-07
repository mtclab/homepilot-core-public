"""Who is acting, and whether their credential carries a human's authority (#681).

HomePilot has two audiences for a credential:

* **operator** - a person. The token a human holds is the one the browser claim
  and `hp init` mint: the admin capability (scope ``admin`` or ``all``). The
  on-box CLI (`hp artifacts approve`), which reads the database directly and is
  therefore already root-equivalent on the control plane, is an operator too.
* **assistant** - everything below the admin capability. Settings -> Tokens
  mints assistants ``read,write`` (the MCP ``full`` tier) or ``read_only``, and
  every MCP call is an assistant call whatever its tier: MCP is the assistant's
  transport by definition.

The audience is DERIVED from the capability ladder rather than stored as a new
column. The reason is that the admin capability is already root on every agent
host (``exec_on_host``, ``write_file_on_host``) and mints tokens, so an
"assistant" label on an admin token would gate nothing real: such a token could
run the change directly, or mint itself an operator token. What the approval
gate must stop is a credential BELOW admin turning a proposal into a root shell,
and the ladder already draws exactly that line. Approving is authorising
arbitrary execution, so it sits with the capability that already has it.

A Principal is what the lifecycle is handed to decide approval authority and to
name the actor in the record - never a name taken from a request body.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .scopes import SCOPE_ADMIN
from .tokens import normalize_scope

AUDIENCE_OPERATOR = "operator"
AUDIENCE_ASSISTANT = "assistant"


@dataclass(frozen=True)
class Principal:
    """An authenticated actor: who to record, and which audience it belongs to."""

    name: str
    audience: str

    @property
    def is_operator(self) -> bool:
        return self.audience == AUDIENCE_OPERATOR

    @classmethod
    def operator(cls, name: str) -> Principal:
        return cls(name=name, audience=AUDIENCE_OPERATOR)

    @classmethod
    def assistant(cls, name: str) -> Principal:
        return cls(name=name, audience=AUDIENCE_ASSISTANT)


def is_operator_credential(scope: str | None, role: str | None = None) -> bool:
    """True when a token's capabilities make it an operator credential (admin)."""
    normalized = normalize_scope(scope, role)
    return "*" in normalized or SCOPE_ADMIN in normalized


def principal_from_token(token: dict[str, Any]) -> Principal:
    """The Principal for a `require_token` result.

    The name is the token's label plus its prefix, e.g. ``assistant
    (hp_1a2b3c4d5e6f7)``. The display name alone is not enough: every token
    Settings -> Tokens mints hangs off the instance's single user, so an
    assistant's approval would otherwise be recorded under the owner's name.
    The prefix is the token's public id (it is what revocation is keyed on) and
    is not secret.
    """
    label = token.get("label") or token.get("display_name") or token.get("user_id") or "api-token"
    name = credential_name(str(label), token.get("prefix"))
    if is_operator_credential(token.get("scope"), token.get("role")):
        return Principal.operator(name)
    return Principal.assistant(name)


def credential_name(label: str, prefix: str | None) -> str:
    """`label (prefix)` - the one spelling of a credential anywhere it is named."""
    return f"{label} ({prefix})" if prefix else label


# The name for a caller authenticated by the admin secret rather than a token.
ACTOR_ADMIN_SECRET = "admin-secret"  # nosec B105 # pragma: allowlist secret - an audit actor NAME, not a credential


def audit_actor(auth: dict[str, Any]) -> str:
    """WHO acted, as the audit trail records it - the ONE helper for every surface.

    The trail used to spell one credential two ways: `api:<prefix>` from the
    inventory and guest-admin routes (#702) and `label (prefix)` from the
    lifecycle and token rows (#681, #686), so a filter on one missed the
    other's rows. Every REST route, the MCP transport and the token audit now
    take the name from here: the Principal's name for a token (its label and
    public prefix), or `admin-secret`.

    Accepts a `require_token` result, a token row, or the admin-secret marker
    `{"auth": "admin-secret"}`.
    """
    if auth.get("auth") == ACTOR_ADMIN_SECRET:
        return ACTOR_ADMIN_SECRET
    return principal_from_token(auth).name
