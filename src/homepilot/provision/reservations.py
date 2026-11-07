"""In-process reservations for the ids and addresses a new guest is about to take (#719).

Neither source of truth can answer "is this free" on its own:

* PVE's ``/cluster/nextid`` hands back the lowest free id and RESERVES NOTHING,
  so two provisions that read it a moment apart are both told 105.
* The address allocator scans the cluster's guest configs, and a guest whose
  config has not been written yet claims nothing in that scan.

So between the read and the moment the cluster itself holds the claim (the
clone's config for a vmid, the cloud-init write for an address) the claim lives
HERE, and every allocator in this process consults it under one lock. The lock
covers the read and the reservation together - check-then-reserve with nothing
able to slip between them - and never the clone itself.

One instance is shared by every service in the process that creates guests
(provisioning and template builds); a service built on its own gets a private
one, which is what a unit test wants.

This is deliberately NOT a table. A table has to be kept true by every path
that destroys a guest, including ones HomePilot never sees; a reservation only
has to live for the seconds between a read and a write, and it dies with the
process that made it - which is exactly when the window it covers closes too.
"""

from __future__ import annotations

import asyncio


class GuestReservations:
    """Claims on vmids and guest addresses that the cluster does not show yet."""

    def __init__(self) -> None:
        # Held while an allocator READS the cluster and RECORDS its claim, so a
        # second allocator cannot read the same "free" answer in between.
        self.lock = asyncio.Lock()
        self._vmids: set[int] = set()
        self._addresses: set[str] = set()

    @property
    def vmids(self) -> frozenset[int]:
        return frozenset(self._vmids)

    @property
    def addresses(self) -> frozenset[str]:
        return frozenset(self._addresses)

    def reserve_vmid(self, vmid: int) -> bool:
        """Claim one vmid. False when this process already holds a claim on it.

        No await between the check and the add, so on one event loop this is a
        latch, not a suggestion.
        """
        if vmid in self._vmids:
            return False
        self._vmids.add(vmid)
        return True

    def release_vmid(self, vmid: int | None) -> None:
        if vmid is not None:
            self._vmids.discard(vmid)

    def reserve_address(self, address: str) -> bool:
        if address in self._addresses:
            return False
        self._addresses.add(address)
        return True

    def release_address(self, address: str | None) -> None:
        if address:
            self._addresses.discard(address)


__all__ = ["GuestReservations"]
