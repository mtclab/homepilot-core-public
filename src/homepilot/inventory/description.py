"""How each host is built (#818): collect, store and compare host descriptions.

The agent answers ``describe_host`` with a versioned, secret-scrubbed JSON
document (agent/go/describe.go). This module is the control plane's half:

* ``HostDescriptionService.collect`` asks a host's agent for one and stores it
  as the LATEST, moving the one it replaces to PREVIOUS - so "what changed
  since last time" is a comparison of two stored rows (``changes_between``);
* ``HostDescriptionService.view`` is the one answer every surface gives - the
  host page, ``GET /inventory/{id}/description``, the MCP ``describe_host`` tool
  and ``hp hosts describe``;
* ``HostDescriptionService.export_asbuilt`` renders the description as a KB
  "as-built" note through the KB's own record path, superseding the host's
  previous as-built note;
* ``HostDescriptionReconciler`` re-describes every capable host on a slow
  schedule (``HP_HOST_DESCRIPTION_INTERVAL_SECONDS``, daily by default).

The document is NOT re-filtered here: secrets never leave the host, so the
agent is where the deny filter runs. The hub checks shape, schema and size.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Any

from homepilot.adapters.agent import (
    DESCRIBE_CAPABILITY,
    AgentAdapter,
    AgentAdapterError,
    AgentTooOldError,
    GuestHostError,
)
from homepilot.artifacts.models import compute_body_hash
from homepilot.reconciler.base import Reconciler, ReconcilerResult
from homepilot.runtime.blocking import run_blocking

from .asbuilt import render_asbuilt

logger = logging.getLogger(__name__)

#: The document versions this control plane can store and render.
KNOWN_SCHEMAS = frozenset({"hp.host-description/1"})

#: Largest description accepted from an agent (the agent caps at 256 KiB).
MAX_DESCRIPTION_BYTES = 320 * 1024

#: A fresh collection within this many seconds of the last one answers with
#: the stored description instead of asking the agent again.
MIN_REFRESH_SECONDS = 30

#: The KB note kind an export records.
ASBUILT_KIND = "as-built"


class DescriptionError(Exception):
    """A refusal or failure with the HTTP status the route surfaces."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _loads(raw: Any) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        value = json.loads(str(raw))
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def validate_description(doc: Any) -> dict[str, Any]:
    """Refuse a description the hub cannot store or render."""
    if not isinstance(doc, dict):
        raise DescriptionError(502, "the agent's description is not a JSON object")
    schema = doc.get("schema")
    if schema not in KNOWN_SCHEMAS:
        raise DescriptionError(
            502,
            f"the agent sent a description of schema {schema!r}, which this HomePilot "
            "does not know - upgrade HomePilot or match the agent version",
        )
    if len(json.dumps(doc)) > MAX_DESCRIPTION_BYTES:
        raise DescriptionError(502, "the agent's description exceeds the size cap")
    return doc


# ── What changed ─────────────────────────────────────────────────────────────


def _items(doc: dict[str, Any], *path: str) -> list[dict[str, Any]]:
    node: Any = doc
    for key in path:
        node = node.get(key) if isinstance(node, dict) else None
    return [x for x in node if isinstance(x, dict)] if isinstance(node, list) else []


def _keys(doc: dict[str, Any]) -> dict[str, set[str]]:
    """One readable line per item of each part, for set comparison."""
    out: dict[str, set[str]] = {}
    out["listening sockets"] = {
        f"{s.get('proto')} {s.get('address')}:{s.get('port')}"
        + (f" ({s.get('process')})" if s.get("process") else "")
        for s in _items(doc, "listening", "sockets")
    }
    out["units"] = {
        f"{u.get('name')} ({u.get('active') or '-'}, {u.get('enabled') or '-'})"
        for u in _items(doc, "units", "units")
    }
    out["containers"] = {
        f"{c.get('name')} {c.get('image')}" + (f"@{c['digest']}" if c.get("digest") else "")
        for c in _items(doc, "docker", "containers")
    }
    out["web server"] = {
        f"{' '.join(s.get('server_names') or ['_'])} [{', '.join(s.get('listen') or [])}]"
        for s in _items(doc, "nginx", "servers")
    }
    out["certificates"] = {
        f"{c.get('path')} until {c.get('not_after') or 'unknown'}"
        for c in _items(doc, "certificates", "certificates")
    }
    out["firewall"] = {
        f"ufw {r.get('action')} {r.get('proto')} {r.get('port')} from {r.get('from')}"
        for r in _items(doc, "firewall", "ufw", "rules")
    } | {
        f"nft {t.get('family')} {t.get('name')}"
        for t in _items(doc, "firewall", "nftables", "tables")
    }
    return out


def changes_between(
    previous: dict[str, Any] | None, latest: dict[str, Any]
) -> list[dict[str, Any]]:
    """What was added and removed, part by part, between two descriptions."""
    if previous is None:
        return []
    before, after = _keys(previous), _keys(latest)
    changes = []
    for part in after:
        added = sorted(after[part] - before.get(part, set()))
        removed = sorted(before.get(part, set()) - after[part])
        if added or removed:
            changes.append({"part": part, "added": added, "removed": removed})
    return changes


# ── The service ──────────────────────────────────────────────────────────────


class HostDescriptionService:
    def __init__(self, repo: Any, adapter: AgentAdapter | None) -> None:
        self.repo = repo
        self.adapter = adapter
        self._locks: dict[str, asyncio.Lock] = {}

    async def host(self, host_id: str | None = None, hostname: str | None = None) -> dict[str, Any]:
        """The inventory row a call addresses, by id or by name."""
        row = None
        if host_id:
            row = await self.repo.get_host(host_id)
        elif hostname:
            row = await self.repo.get_live_host_by_hostname(hostname)
        if row is None:
            raise DescriptionError(404, f"Host not found: {host_id or hostname}")
        return dict(row)

    def _agent_state(self, hostname: str) -> dict[str, Any]:
        hub = getattr(self.adapter, "_hub", None) if self.adapter is not None else None
        agent = hub.registry.get_by_hostname(hostname) if hub is not None else None
        if agent is None:
            return {"connected": False, "can_describe": False}
        caps = (agent.system_info or {}).get("capabilities") or []
        return {
            "connected": True,
            "can_describe": DESCRIBE_CAPABILITY in caps,
            "version": (agent.system_info or {}).get("agent_version"),
        }

    async def view(self, host: dict[str, Any]) -> dict[str, Any]:
        """The stored description of one host, with what changed and its export."""
        row = await self.repo.get_host_description(host["id"]) or {}
        latest = _loads(row.get("description_json"))
        previous = _loads(row.get("previous_json"))
        note = row.get("asbuilt_note_id")
        return {
            "host_id": host["id"],
            "hostname": host.get("hostname"),
            "description": latest,
            "collected_at": row.get("collected_at"),
            "received_at": row.get("received_at"),
            "previous_collected_at": row.get("previous_collected_at"),
            "changes": changes_between(previous, latest) if latest else [],
            "last_error": row.get("last_error"),
            "last_error_at": row.get("last_error_at"),
            "as_built": (
                {"note_id": note, "exported_at": row.get("asbuilt_exported_at")} if note else None
            ),
            "agent": self._agent_state(str(host.get("hostname") or "")),
        }

    async def collect(self, host: dict[str, Any], *, force: bool = False) -> dict[str, Any]:
        """Ask the host's agent for a description now and store it.

        One collection per host at a time; a request within
        ``MIN_REFRESH_SECONDS`` of the last one answers with that one (``force``
        - the schedule - skips the wait). A failure is recorded on the host's
        row, where the page shows it, and raised with its status.
        """
        host_id, hostname = str(host["id"]), str(host.get("hostname") or "")
        lock = self._locks.setdefault(host_id, asyncio.Lock())
        async with lock:
            if not force:
                row = await self.repo.get_host_description(host_id) or {}
                received = _parse_ts(row.get("received_at"))
                if (
                    received
                    and (datetime.now(UTC) - received).total_seconds() < MIN_REFRESH_SECONDS
                ):
                    return await self.view(host)
            try:
                if self.adapter is None:
                    raise DescriptionError(
                        409, "the agent hub is not running in this process: no agent to ask"
                    )
                doc = validate_description(await self.adapter.describe_host(hostname))
            except DescriptionError as exc:
                await self.repo.record_host_description_error(host_id, hostname, exc.detail)
                raise
            except AgentTooOldError as exc:
                await self.repo.record_host_description_error(host_id, hostname, str(exc))
                raise DescriptionError(409, str(exc)) from None
            except GuestHostError as exc:
                raise DescriptionError(409, str(exc)) from None
            except TimeoutError:
                detail = f"the agent on {hostname} did not answer in time"
                await self.repo.record_host_description_error(host_id, hostname, detail)
                raise DescriptionError(504, detail) from None
            except AgentAdapterError as exc:
                await self.repo.record_host_description_error(host_id, hostname, str(exc))
                raise DescriptionError(409, str(exc)) from None
            except Exception as exc:  # the agent reported an error: store and say it
                detail = f"the agent on {hostname} could not describe the host: {exc}"
                await self.repo.record_host_description_error(host_id, hostname, detail)
                raise DescriptionError(502, detail) from None
            await self.repo.save_host_description(
                host_id,
                hostname,
                str(doc["schema"]),
                str(doc.get("collected_at") or ""),
                json.dumps(doc, sort_keys=True),
            )
        return await self.view(host)

    async def export_asbuilt(self, host: dict[str, Any], kb_service: Any) -> dict[str, Any]:
        """Record the latest description as the host's KB as-built note.

        Through the KB's own record path (``KBService.record_fact``), so the
        note is an applied kb-note artifact, indexed and searchable at once. The
        host's previous as-built note is SUPERSEDED - its status changes and its
        KB document is removed - so a search finds one current recipe per host.
        Exporting an unchanged description returns the existing note.
        """
        if kb_service is None:
            raise DescriptionError(503, "the knowledge base is not configured")
        row = await self.repo.get_host_description(host["id"]) or {}
        latest = _loads(row.get("description_json"))
        if latest is None:
            raise DescriptionError(
                409, "no description of this host yet: collect one first, then export it"
            )
        hostname = str(host.get("hostname") or "")
        markdown = render_asbuilt(hostname, latest)
        previous = row.get("asbuilt_note_id")
        if previous and await self._note_holds(kb_service, str(previous), markdown):
            # Nothing to record: the current note already says exactly this. Asking
            # the record path again would collide with the note it wrote before.
            doc_row = await self.repo.get_doc_by_source(f"artifact:{previous}")
            return {
                "note_id": previous,
                "superseded": None,
                "unchanged": True,
                "indexed": doc_row is not None,
                "doc_id": int(doc_row["id"]) if doc_row else None,
            }
        note_id = await kb_service.record_fact(
            target={"kind": "global", "host": hostname},
            kind=ASBUILT_KIND,
            content=markdown,
            supersedes=[previous] if previous else None,
            intent=f"As-built: {hostname} (collected {latest.get('collected_at', 'unknown')})",
        )
        superseded = None
        if previous and previous != note_id:
            superseded = await self._supersede(kb_service, str(previous), note_id)
        await self.repo.set_host_asbuilt_note(host["id"], note_id)
        doc_row = await self.repo.get_doc_by_source(f"artifact:{note_id}")
        return {
            "note_id": note_id,
            "superseded": superseded,
            "unchanged": previous == note_id,
            "indexed": doc_row is not None,
            "doc_id": int(doc_row["id"]) if doc_row else None,
        }

    @staticmethod
    async def _note_holds(kb_service: Any, note_id: str, markdown: str) -> bool:
        """Whether the applied note ``note_id`` already has exactly this body."""
        try:
            fm, _body = await run_blocking("io", kb_service.store.read, note_id)
        except (FileNotFoundError, OSError, ValueError):
            return False
        return bool(fm.get("status") == "applied" and fm.get("hash") == compute_body_hash(markdown))

    async def _supersede(self, kb_service: Any, old_id: str, new_id: str) -> str | None:
        """Mark the previous note superseded and take it out of the KB index.

        The lifecycle's own reindex is a background task; the old document is
        removed here as well, so the KB answers with one recipe as soon as the
        export returns. A previous note that is gone or already retired is
        left alone.
        """
        try:
            await kb_service.lifecycle.supersede(old_id, new_id)
        except (FileNotFoundError, ValueError, OSError) as exc:
            logger.info("previous as-built note %s not superseded: %s", old_id, exc)
            return None
        except Exception as exc:  # a ConflictError: already superseded or revoked
            logger.info("previous as-built note %s not superseded: %s", old_id, exc)
            return None
        old_doc = await self.repo.get_doc_by_source(f"artifact:{old_id}")
        if old_doc is not None:
            await self.repo.delete_doc_metadata(int(old_doc["id"]))
        return old_id


class HostDescriptionReconciler(Reconciler):
    """Re-describe every capable, connected host whose description is older
    than ``max_age`` seconds - the slow schedule behind the on-demand path."""

    def __init__(
        self, service: HostDescriptionService, max_age: float, concurrency: int = 4
    ) -> None:
        self._service = service
        self._max_age = max_age
        self._concurrency = concurrency

    async def run(self) -> ReconcilerResult:
        service = self._service
        hub = getattr(service.adapter, "_hub", None) if service.adapter is not None else None
        if hub is None:
            return ReconcilerResult(
                name="host_descriptions", success=True, details={"skipped": "no agent hub"}
            )
        stored = {r["host_id"]: r for r in await service.repo.list_host_descriptions()}
        now = datetime.now(UTC)
        due: list[dict[str, Any]] = []
        for agent in hub.registry.list_connected():
            caps = (agent.get("system_info") or {}).get("capabilities") or []
            if DESCRIBE_CAPABILITY not in caps:
                continue
            host = await service.repo.get_host_by_agent_id(
                str(agent.get("agent_id"))
            ) or await service.repo.get_live_host_by_hostname(str(agent.get("hostname")))
            if host is None:
                continue
            last = _parse_ts((stored.get(host["id"]) or {}).get("received_at"))
            if last is None or (now - last).total_seconds() >= self._max_age * 0.9:
                due.append(host)
        gate = asyncio.Semaphore(self._concurrency)
        failures: list[str] = []

        async def one(host: dict[str, Any]) -> None:
            async with gate:
                try:
                    await service.collect(host, force=True)
                except DescriptionError as exc:
                    failures.append(f"{host.get('hostname')}: {exc.detail}")

        await asyncio.gather(*(one(h) for h in due))
        return ReconcilerResult(
            name="host_descriptions",
            success=not failures,
            details={"described": len(due) - len(failures), "failed": failures[:20]},
        )
