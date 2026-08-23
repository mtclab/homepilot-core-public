from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from typing import Any

from homepilot.db.repository import Repository
from homepilot.inventory.service import InventoryService

from .base import Reconciler, ReconcilerResult

logger = logging.getLogger(__name__)


class InventoryReconciler(Reconciler):
    def __init__(
        self,
        inventory_service: InventoryService,
        repo: Repository,
    ) -> None:
        self.inventory_service = inventory_service
        self.repo = repo

    # One page's worth per round-trip; the reconciler needs every host, and a
    # single unbounded SELECT on a large estate is the other way to get this
    # wrong.
    _PAGE = 500

    async def _all_hosts(self) -> list[dict[str, Any]]:
        """Every host, paged through rather than truncated at the default 100."""
        out: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = await self.repo.list_hosts(limit=self._PAGE, offset=offset)
            out.extend(page)
            if len(page) < self._PAGE:
                return out
            offset += self._PAGE

    async def run(self) -> ReconcilerResult:
        try:
            # UNPAGINATED. `list_hosts()` defaults to 100 rows, so on an estate
            # with more than a hundred hosts the absent/changed sets below were
            # computed from an arbitrary first page (#428).
            existing_hosts = await self._all_hosts()
            pre_ids: set[str] = {h["id"] for h in existing_hosts}
            pre_checksums: dict[str, str] = {
                h["id"]: self._host_checksum(h) for h in existing_hosts
            }

            result = await self.inventory_service.refresh_inventory()

            # Enrich after refresh so IPs and derived status stay current without
            # a manual Sync. Best-effort: enrichment does per-host network calls
            # (guest agent / DNS) and must never fail the reconciler cycle.
            enriched = 0
            try:
                enrich_result = await self.inventory_service.enrich_inventory()
                if isinstance(enrich_result, dict):
                    enriched = int(enrich_result.get("enriched", 0))
            except Exception:
                logger.warning("InventoryReconciler enrichment pass failed", exc_info=True)

            proxmox_ids: set[str] = set(result.get("proxmox_host_ids", []))

            absent_ids = pre_ids - proxmox_ids
            new_ids = proxmox_ids - pre_ids
            changed_ids: set[str] = set()

            updated_hosts = await self._all_hosts()
            for h in updated_hosts:
                hid = h["id"]
                if hid in pre_checksums and self._host_checksum(h) != pre_checksums[hid]:
                    changed_ids.add(hid)

            details: dict[str, Any] = {
                "hosts_refreshed": result.get("hosts", 0),
                "services_refreshed": result.get("services", 0),
                "hosts_enriched": enriched,
                "absent_hosts": len(absent_ids),
                "new_hosts": len(new_ids),
                "changed_hosts": len(changed_ids),
            }

            if absent_ids:
                details["absent_host_ids"] = sorted(absent_ids)
            if changed_ids:
                details["changed_host_ids"] = sorted(changed_ids)

            try:
                await self.repo.log_audit(
                    user_id="system",
                    source="reconciler:inventory",
                    action="refresh",
                    details_json=json.dumps(details),
                )
            except (sqlite3.OperationalError, sqlite3.IntegrityError, OSError, RuntimeError) as exc:
                logger.exception("InventoryReconciler audit log failed: %s", exc)
                details["audit_log_error"] = True

            return ReconcilerResult(
                name="inventory",
                success=True,
                details=details,
            )

        except Exception as exc:
            logger.exception("InventoryReconciler failed")
            return ReconcilerResult(
                name="inventory",
                success=False,
                details={"error": str(exc)},
            )

    @staticmethod
    def _host_checksum(host: dict[str, Any]) -> str:
        # Exclude derived/timestamp fields: enrichment changes must not trigger
        # reconciler "changed" alerts.
        excluded = {"updated_at", "role_source", "ip_source", "status"}
        relevant = {k: v for k, v in host.items() if k not in excluded}
        serialized = json.dumps(relevant, sort_keys=True, default=str)
        return hashlib.sha256(serialized.encode()).hexdigest()[:16]
