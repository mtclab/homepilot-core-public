"""Vault references in artifact bodies, resolved at execute time (#505).

Vault was wired into `http_sequence` and `proxmox_api` only. `host_provision`
and `shell_script` - the two kinds most likely to need a credential - received no
vault at all, so a database password in a config file or an API token in a script
was written as LITERAL TEXT into the artifact body. The artifact store is a git
repository designed to be pushed to a remote, so that credential is in history
from the first commit, and `git push` is a one-way door.

The shape here is deliberately narrow:

* the BODY stores only a reference, `{{ vault.name.field }}`;
* resolution happens at execute time, in memory, immediately before the value is
  handed to the agent;
* the resolved value never reaches an execution log, a task result or an audit
  row - all three are read back by operators, and one of them is persisted on
  purpose (#487).

`redact()` is the counterpart to `resolve()` and they are used as a pair: if a
body was resolved, whatever is logged about it must be redacted first.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# `{{ vault.NAME }}` or `{{ vault.NAME.FIELD }}`. Names match the vault's own
# rules (see VaultManager._validate_secret_name); the field defaults to
# "value", which is what a single-value secret is stored under.
VAULT_REF_RE = re.compile(
    r"\{\{\s*vault\.([A-Za-z0-9][A-Za-z0-9_-]*)(?:\.([A-Za-z0-9_-]+))?\s*\}\}"
)

_REDACTED = "***"


class SecretResolutionError(Exception):
    """A body names a credential that cannot be resolved.

    Raised rather than substituted-with-empty: a config file written with an
    empty password is a working-looking file that fails at 3am, and a shell
    script with an empty token may do something worse than nothing.
    """


def references(text: str) -> list[tuple[str, str]]:
    """Every `(secret_name, field)` a body refers to."""
    return [(m.group(1), m.group(2) or "value") for m in VAULT_REF_RE.finditer(text or "")]


async def resolve(text: str, vault: Any) -> tuple[str, list[str]]:
    """Substitute vault references. Returns ``(resolved_text, secret_values)``.

    The second element exists so the caller can redact those exact values out of
    anything it logs. It is never returned to a caller that does not immediately
    use it for redaction.
    """
    refs = references(text)
    if not refs:
        return text, []
    if vault is None:
        raise SecretResolutionError(
            "this artifact references vault credentials but no vault is configured"
        )

    resolved = text
    values: list[str] = []
    cache: dict[str, dict[str, Any]] = {}
    for name, field in refs:
        if name not in cache:
            try:
                cache[name] = await vault.get_secret(name)
            except Exception as exc:
                raise SecretResolutionError(
                    f"vault credential '{name}' could not be read: {exc}"
                ) from None
        secret = cache[name]
        if field not in secret:
            raise SecretResolutionError(f"vault credential '{name}' has no field '{field}'")
        value = str(secret[field])
        values.append(value)
        pattern = re.compile(
            r"\{\{\s*vault\." + re.escape(name) + r"(?:\." + re.escape(field) + r")?\s*\}\}"
        )

        # `re.sub` with a plain string would interpret backslashes and \g in the
        # SECRET as replacement syntax; a function replacement never does.
        def _replacement(_match: re.Match[str], v: str = value) -> str:
            return v

        resolved = pattern.sub(_replacement, resolved)
    return resolved, values


def redact(text: str, values: list[str]) -> str:
    """Replace resolved secret values with `***`.

    Called on everything that leaves the executor as text. The values are matched
    literally rather than by re-running the reference regex, because by the time
    a log line exists the reference is gone - the value is what is in there.
    """
    if not text or not values:
        return text
    out = text
    for value in values:
        if value:
            out = out.replace(value, _REDACTED)
    return out


# Assignments that look like a credential written out in full. Deliberately
# narrow: this refuses a PROPOSE, and a guard that fires on prose would train
# people to work around it. Each pattern wants a key whose NAME says secret and a
# value that is not already a vault reference.
_LITERAL_SECRET_RE = re.compile(
    r"""(?ix)
    \b(password|passwd|secret|api[_-]?key|apikey|token|private[_-]?key)\b
    \s*[:=]\s*
    (?!\s*$)                     # not an empty value
    (?!["']?\{\{\s*vault\.)     # not already a vault reference
    (?!["']?(changeme|xxx+|\.\.\.|<[^>]+>|\$\{)) # not an obvious placeholder
    ["']?(?P<value>[^\s"'#]{8,})
    """,
    re.VERBOSE,
)


def literal_secrets(text: str) -> list[str]:
    """Key names in `text` that appear to carry a literal credential (#505).

    The moment to catch a committed secret is BEFORE it is committed: the
    artifact store is a git repository designed to be pushed, so history is a
    one-way door. Returns the offending key names, never the values.
    """
    found: list[str] = []
    for match in _LITERAL_SECRET_RE.finditer(text or ""):
        key = match.group(1)
        if key not in found:
            found.append(key)
    return found
