from __future__ import annotations

SCOPE_READ = "read"
SCOPE_WRITE = "write"
SCOPE_ADMIN = "admin"

ROLE_VIEWER = "viewer"
ROLE_OPERATOR = "operator"
ROLE_ADMIN = "admin"

ROLE_SCOPES: dict[str, list[str]] = {
    ROLE_VIEWER: [SCOPE_READ],
    ROLE_OPERATOR: [SCOPE_READ, SCOPE_WRITE],
    ROLE_ADMIN: [SCOPE_READ, SCOPE_WRITE, SCOPE_ADMIN],
}

VALID_ROLES = tuple(ROLE_SCOPES.keys())

# The MCP tool tiers, and the one true map from an API scope to the tier that
# matches it exactly. The MCP transport authenticates API tokens (an assistant is
# minted a token in Settings -> Tokens like any other client), so the API ladder
# read < write < admin has to line up with read_only < full < admin in exactly
# one place. Both the transport and the tier<->scope parity gate read THIS map; a
# second copy of it would be free to drift away from the enforcement.
MCP_TIER_READ_ONLY = "read_only"
MCP_TIER_FULL = "full"
MCP_TIER_ADMIN = "admin"

API_SCOPE_TO_MCP_TIER: dict[str, str] = {
    SCOPE_READ: MCP_TIER_READ_ONLY,
    SCOPE_WRITE: MCP_TIER_FULL,
    SCOPE_ADMIN: MCP_TIER_ADMIN,
}


def scopes_for_role(role: str | None) -> list[str]:
    if role and role in ROLE_SCOPES:
        return ROLE_SCOPES[role]
    return []
