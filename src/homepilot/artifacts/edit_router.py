"""Editing an artifact through the backend that owns it (#693).

`hp artifacts edit` opened $EDITOR on the store file itself and then asked a
lifecycle with no repository to record the edit. The DB row, the audit log and
the webhooks never heard of it, and an edit of an APPLIED artifact was saved
into the file before the lifecycle refused it - the command exited 0 and left a
tampered file with a stale hash.

The CLI now edits a COPY and submits it here (or, with the backend stopped,
hands it to the same lifecycle call directly). Nothing touches the store file
unless the edit is accepted, under the file lock, by
``ArtifactLifecycle.edit_content``.
"""

from __future__ import annotations

from typing import Any

import yaml
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..auth.deps import require_scope, require_token
from ..auth.principal import audit_actor
from .lifecycle import ArtifactLifecycle
from .models import ConflictError, LifecycleError

router = APIRouter()

# Generous for a playbook or a script, small enough that the body is not a
# vector for filling the artifacts repository.
MAX_ARTIFACT_TEXT = 1_000_000


class ArtifactEditRequest(BaseModel):
    content: str = Field(min_length=1, max_length=MAX_ARTIFACT_TEXT)


def parse_artifact_text(text: str) -> tuple[dict[str, Any], str]:
    """Split an artifact file's text into (frontmatter, body), as the store does.

    Raises ``LifecycleError`` naming what is wrong: an edit that breaks the file
    format is a refusal the operator can act on, not a crash.
    """
    if not text.startswith("---"):
        raise LifecycleError("The artifact must start with a '---' frontmatter block")
    end = text.find("\n---", 3)
    if end == -1:
        raise LifecycleError("The frontmatter block is not closed with '---'")
    try:
        fm = yaml.safe_load(text[3:end]) or {}
    except yaml.YAMLError as exc:
        raise LifecycleError(f"The frontmatter is not valid YAML: {exc}") from None
    if not isinstance(fm, dict):
        raise LifecycleError("The frontmatter must be a YAML mapping")
    body = text[end + 4 :].lstrip("\n")
    return fm, body


async def submit_edit(
    lifecycle: ArtifactLifecycle, artifact_id: str, text: str, user: str | None
) -> dict[str, Any]:
    """Validate an edited artifact text and hand it to the lifecycle."""
    fm, body = parse_artifact_text(text)
    return await lifecycle.edit_content(artifact_id, fm, body, user)


def _actor(token: dict[str, Any]) -> str:
    # The credential, as approve/reject/apply/revoke record it (#681): every
    # minted token shares the one user, so a display name cannot tell them apart.
    return audit_actor(token)


@router.post("/{artifact_id}/edit", dependencies=[Depends(require_scope("write"))])
async def edit_artifact(
    request: Request,
    artifact_id: str,
    body: ArtifactEditRequest,
    token: dict[str, Any] = Depends(require_token),  # noqa: B008
) -> dict[str, Any]:
    lifecycle: ArtifactLifecycle = request.app.state.artifact_lifecycle
    try:
        result = await submit_edit(lifecycle, artifact_id, body.content, _actor(token))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Artifact not found: {artifact_id}") from None
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except (LifecycleError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return {"id": artifact_id, **result}
