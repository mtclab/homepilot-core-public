"""What "reversible" means, per kind, in ONE place (#426).

`rollback` in an artifact's frontmatter is documented by ARTIFACT_SPEC as *"true
if a rollback section exists in the body"* - a FACT about the body, not a switch
the proposer flips. Nothing derived it. `orchestrator._run_rollback` was gated on
`if fm.get("rollback")`, so a body with a perfectly good rollback section and no
`rollback: true` line silently never rolled back, and revoke relabelled the
artifact while the host kept the change.

The other half of the same problem: some kinds cannot honour `rollback: true` at
all. Claiming it and discovering that on revoke - after the operator has decided
to undo something - is the worst possible moment to find out, so a claim that
cannot be honoured is refused at propose instead.
"""

from __future__ import annotations

import re

from homepilot.artifacts.models import ArtifactKind

# The fenced block each kind's executor actually looks for when it rolls back.
# These strings are duplicated from the executors on purpose: this module is
# read at PROPOSE time, and importing five executors (each pulling adapters,
# httpx and the vault) to answer "does this body have a rollback section" would
# make proposing an artifact depend on the whole execution stack. The gate in
# tests/test_rollback_truth.py asserts they still match their executors.
_ROLLBACK_FENCE_BY_KIND: dict[ArtifactKind, str] = {
    ArtifactKind.ANSIBLE_PLAYBOOK: "ansible-rollback",
    ArtifactKind.PROXMOX_API_SEQUENCE: "proxmox-api-rollback",
    ArtifactKind.HTTP_SEQUENCE: "http-rollback",
    ArtifactKind.SHELL_SCRIPT: "shell-rollback",
}

# Kinds whose rollback is a property of the KIND rather than of the body.
#
# * composite carries no fence of its own: its rollback walks the sub-artifacts
#   in reverse and revokes each applied one.
# * host-provision inverts a capture taken at apply time (#426), so there is
#   nothing for an author to write either. Its inverse is PARTIAL - the agent has
#   no package-removal or file-deletion verb - and the revoke reports exactly
#   what it could not put back rather than guessing.
_ALWAYS_REVERSIBLE: frozenset[ArtifactKind] = frozenset(
    {ArtifactKind.COMPOSITE, ArtifactKind.HOST_PROVISION}
)


def kind_can_roll_back(kind: ArtifactKind) -> bool:
    """Can this kind reverse itself AT ALL, given a suitable body?"""
    return kind in _ROLLBACK_FENCE_BY_KIND or kind in _ALWAYS_REVERSIBLE


def has_rollback_section(kind: ArtifactKind, body: str) -> bool:
    """Does this body carry the rollback block this kind's executor runs?

    Matches the opening fence the executor's own extractor matches - a
    ``## Rollback`` heading with no fenced block under it is what the deleted CLI
    engine counted, and it is exactly the thing that made it report a rollback
    that could never run.
    """
    fence = _ROLLBACK_FENCE_BY_KIND.get(kind)
    if fence is None:
        return False
    return re.search(rf"^```\s*\w+\s+{re.escape(fence)}\s*$", body, re.MULTILINE) is not None


def derive_rollback(kind: ArtifactKind, body: str) -> bool:
    """The value `rollback` should have for this artifact, per the spec."""
    if kind in _ALWAYS_REVERSIBLE:
        return True
    return has_rollback_section(kind, body)
