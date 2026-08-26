from __future__ import annotations

import hashlib
import hmac
import json
import logging
from datetime import UTC, datetime
from typing import Any

import httpx

logger = logging.getLogger(__name__)


def sign_payload(payload_bytes: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), payload_bytes, hashlib.sha256).hexdigest()


def build_artifact_payload(
    event_type: str,
    fm: dict[str, Any],
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "event": event_type,
        "id": fm.get("id", ""),
        "kind": fm.get("kind", ""),
        "intent": fm.get("intent", ""),
        "status": fm.get("status", ""),
        "mutating": fm.get("mutating", True),
        "timestamp": datetime.now(UTC).isoformat(),
        "source": fm.get("produced_by", {}),
    }
    target = fm.get("target")
    if target:
        payload["target"] = target
    idempotence = fm.get("idempotence")
    if idempotence:
        payload["idempotence"] = idempotence
    superseded_by = fm.get("superseded_by")
    if superseded_by:
        payload["superseded_by"] = superseded_by
    drift_summary = fm.get("drift_summary")
    if drift_summary:
        payload["drift_summary"] = drift_summary
    approved_by = fm.get("approved_by")
    if approved_by:
        payload["approved_by"] = approved_by
    rejected_by = fm.get("rejected_by")
    if rejected_by:
        payload["rejected_by"] = rejected_by
    revoked_by = fm.get("revoked_by")
    if revoked_by:
        payload["revoked_by"] = revoked_by
    if extra:
        payload.update(extra)
    return payload


async def send_webhook(
    event_type: str,
    fm: dict[str, Any],
    extra: dict[str, Any] | None = None,
) -> None:
    from ..app_settings import effective
    from ..config import get_settings

    settings = get_settings()
    # Per send (#553 C2): an operator who points events at a new receiver is
    # obeyed by the next event, not by the next restart. The SIGNING SECRET is
    # deliberately not part of that - it stays env/vault only.
    url = await effective("events_webhook_url", settings)
    if not url:
        return

    payload = build_artifact_payload(event_type, fm, extra)
    payload_bytes = json.dumps(payload).encode()

    headers: dict[str, str] = {"Content-Type": "application/json"}
    secret = settings.events_webhook_secret
    if secret:
        headers["X-Webhook-Signature"] = sign_payload(payload_bytes, secret)

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(url, content=payload_bytes, headers=headers)
            if resp.status_code >= 400:
                logger.warning(
                    "Webhook returned status=%d for event=%s url=%s",
                    resp.status_code,
                    event_type,
                    url,
                )
    except (httpx.HTTPError, httpx.TimeoutException, ConnectionError, OSError) as exc:
        logger.warning(
            "Webhook delivery failed for event=%s url=%s: %s",
            event_type,
            url,
            exc,
        )
