"""Per-guest resource quotas (#442 G1.5).

An invite caps one machine; the quota caps the GUEST: the total their machines
may consume, and how many they may hold. Enforced at provision time (the
invite redemption is today's only provision path for guests) and shown to the
guest in their portal, so "over budget" is never a surprise.

No quota row for a CN means no quota - opt-in per friend. A NULL column inside
a row means that axis is unlimited.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..db.repository import now


@dataclass(frozen=True)
class GuestUsage:
    vms: int
    cores: int
    memory_mb: int
    disk_gb: int


@dataclass(frozen=True)
class QuotaDecision:
    allowed: bool
    # Which axes would overflow, in the guest's own vocabulary. Empty when allowed.
    exceeded: tuple[str, ...] = ()


async def get_quota(repo: Any, cn: str) -> dict[str, Any] | None:
    row = await repo.db.fetchone("SELECT * FROM guest_quotas WHERE cn = ?", (cn,))
    return dict(row) if row else None


async def set_quota(
    repo: Any,
    cn: str,
    *,
    max_vms: int | None,
    max_cores: int | None,
    max_memory_mb: int | None,
    max_disk_gb: int | None,
) -> None:
    ts = now()
    await repo.db.execute(
        """INSERT INTO guest_quotas (cn, max_vms, max_cores, max_memory_mb, max_disk_gb,
                                     created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(cn) DO UPDATE SET
             max_vms=excluded.max_vms, max_cores=excluded.max_cores,
             max_memory_mb=excluded.max_memory_mb, max_disk_gb=excluded.max_disk_gb,
             updated_at=excluded.updated_at""",
        (cn, max_vms, max_cores, max_memory_mb, max_disk_gb, ts, ts),
    )
    await repo.db.conn.commit()


async def delete_quota(repo: Any, cn: str) -> bool:
    """Remove a guest's budget entirely (#607). Returns whether a row went.

    Deleting the ROW is the removal, not writing a row of NULLs: "no row" is
    what `check_provision` reads as "this friend has no budget", and a row of
    NULLs would leave a guest_quotas entry behind that the console and
    query_guests both keep listing as a guest with limits set to unlimited.
    The two states look identical to the enforcement path and are NOT identical
    to the operator, so removal means the row is gone.
    """
    cursor = await repo.db.execute("DELETE FROM guest_quotas WHERE cn = ?", (cn,))
    await repo.db.conn.commit()
    return bool(getattr(cursor, "rowcount", 0) > 0)


async def usage_for(repo: Any, cn: str) -> GuestUsage:
    # ABSENT machines do not count. The inventory reconciler stamps
    # absent_since the moment the hypervisor stops reporting a guest, so a
    # machine destroyed out of band is provably gone - and billing a guest for
    # it locks them out of their own budget forever. Live on prod: a guest's
    # VM was destroyed, HomePilot recorded absent_since within four minutes,
    # and his quota still read 1/1 machines used, so his next invite would have
    # been refused at redemption with "Cannot build machines right now" (#613).
    row = await repo.db.fetchone(
        """SELECT COUNT(*) AS vms,
                  COALESCE(SUM(cpu_cores), 0) AS cores,
                  COALESCE(SUM(memory_mb), 0) AS memory_mb,
                  COALESCE(SUM(disk_gb), 0) AS disk_gb
           FROM hosts WHERE owner = ? AND absent_since IS NULL""",
        (cn,),
    )
    r = dict(row or {})
    return GuestUsage(
        vms=int(r.get("vms") or 0),
        cores=int(r.get("cores") or 0),
        memory_mb=int(r.get("memory_mb") or 0),
        disk_gb=int(r.get("disk_gb") or 0),
    )


async def check_provision(
    repo: Any,
    cn: str,
    *,
    cores: int,
    memory_mb: int,
    disk_gb: int,
) -> QuotaDecision:
    """Would adding this machine keep the guest inside their budget?"""
    quota = await get_quota(repo, cn)
    if quota is None:
        return QuotaDecision(allowed=True)
    used = await usage_for(repo, cn)

    exceeded: list[str] = []
    if quota.get("max_vms") is not None and used.vms + 1 > int(quota["max_vms"]):
        exceeded.append("machines")
    if quota.get("max_cores") is not None and used.cores + cores > int(quota["max_cores"]):
        exceeded.append("CPU cores")
    if quota.get("max_memory_mb") is not None and used.memory_mb + memory_mb > int(
        quota["max_memory_mb"]
    ):
        exceeded.append("memory")
    if quota.get("max_disk_gb") is not None and used.disk_gb + disk_gb > int(quota["max_disk_gb"]):
        exceeded.append("disk")
    return QuotaDecision(allowed=not exceeded, exceeded=tuple(exceeded))
