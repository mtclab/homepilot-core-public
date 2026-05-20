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


def scopes_for_role(role: str | None) -> list[str]:
    if role and role in ROLE_SCOPES:
        return ROLE_SCOPES[role]
    return []
