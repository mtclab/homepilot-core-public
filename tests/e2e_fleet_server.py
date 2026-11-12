"""The e2e fleet server: the REAL app and the BUILT UI over a fake Proxmox.

tests/test_e2e.py runs against a live instance that has no Proxmox (the mirror's
e2e job, `make gate-e2e`), so the journeys that need guests - power from the
Hosts row menu, the address-clash refusals, approve-and-apply - cannot run
there. This starts the same app the image runs (`homepilot.main:app`: every
route, the lifespan, the task runner, PowerService, the guest portal, the
built `web/dist`) with ONE fake: Proxmox, at the ProxmoxClient boundary, as
the suite's `FakeCluster` (tests/test_power_controls.py) answers it. Nothing
leaves the process; the fleet is placeholder data (192.0.2.0/24).

    python tests/e2e_fleet_server.py <data-dir> <port>

The caller passes an EMPTY environment plus HP_ADMIN_SECRET and the portal
trust settings (see `TestFleetJourneys` in tests/test_e2e.py). Prints
``E2E_FLEET_READY`` once the fleet is synced and seeded.
"""

from __future__ import annotations

import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

DATA, PORT = Path(sys.argv[1]), int(sys.argv[2])
os.environ.update(
    {
        "HP_DATA_DIR": str(DATA),
        "HP_ARTIFACTS_DIR": str(DATA / "artifacts"),
        "HP_PORT": str(PORT),
        "HP_AGENT_HUB_ENABLED": "false",
        "HP_UPDATE_CHECK_ENABLED": "0",
        # The fleet is what the fixture says; no background sync rewrites it.
        "HP_INVENTORY_INTERVAL_SECONDS": "86400",
        "HP_DRIFT_INTERVAL_SECONDS": "86400",
        "HP_AUTO_APPLY_INTERVAL_SECONDS": "86400",
        "HP_PROXMOX_HOST": "",
    }
)

import uvicorn  # noqa: E402

import homepilot.main as main  # noqa: E402
from tests.test_power_controls import FakeCluster, lxc_net, qemu_net  # noqa: E402

# The fleet. relay holds 192.0.2.20; mirror (the operator's) and alice-web (a
# friend's) are configured for the same address, so starting either is refused.
pve = FakeCluster()
pve.add(101, "atlas", "running", qemu_net("192.0.2.10", "BC:24:11:00:00:01"))
pve.add(112, "relay", "running", qemu_net("192.0.2.20", "BC:24:11:00:00:12"))
pve.add(103, "forge-dev", "stopped", qemu_net("192.0.2.13", "BC:24:11:00:00:03"))
pve.add(116, "mirror", "stopped", qemu_net("192.0.2.20", "BC:24:11:00:00:16"))
pve.add(120, "llm", "running", qemu_net("192.0.2.30", "BC:24:11:00:00:20"))
pve.add(121, "llm-test", "stopped", qemu_net("192.0.2.31", "BC:24:11:00:00:21"))
pve.add(130, "alice-web", "stopped", qemu_net("192.0.2.20", "BC:24:11:00:00:30"))
pve.add(141, "batch-1", "stopped", qemu_net("192.0.2.41", "BC:24:11:00:00:41"))
pve.add(142, "batch-2", "stopped", qemu_net("192.0.2.42", "BC:24:11:00:00:42"))
pve.add(110, "dns", "running", lxc_net("192.0.2.3", "BC:24:11:00:00:10"), guest_type="lxc")

ENVS = {
    "atlas": "prod",
    "relay": "infra",
    "forge-dev": "dev",
    "mirror": "dev",
    "llm": "prod",
    "llm-test": "dev",
    "alice-web": "friend",
    "batch-1": "dev",
    "batch-2": "dev",
    "dns": "infra",
}

_create_app_state = main.create_app_state


async def _with_fake_proxmox(settings: Any) -> Any:
    """The app state the lifespan builds, with Proxmox answered by the fake."""
    state = await _create_app_state(settings)
    state.proxmox = pve
    return state


main.create_app_state = _with_fake_proxmox  # type: ignore[assignment]
_lifespan = main.app.router.lifespan_context


@asynccontextmanager
async def _seeded(app: Any) -> Any:
    async with _lifespan(app) as state:
        app.state.proxmox = pve
        repo = app.state.repo
        # The inventory reconciler's first cycle starts with the lifespan too, so
        # this sweep and that one run at the same time - the race 3.10.0's
        # mirror e2e caught (two rows for one guest, a refused commit). Strict
        # on purpose: the fleet is complete and every machine is ONE row.
        result = await app.state.inventory_service.refresh_inventory()
        if not result["complete"]:
            raise RuntimeError(f"the fleet's seed sweep did not complete: {result}")
        live = await repo.db.fetchall(
            "SELECT hostname, COUNT(*) AS n FROM hosts WHERE absent_since IS NULL "
            "GROUP BY hostname HAVING n > 1"
        )
        if live:
            raise RuntimeError(f"the fleet has duplicate host rows: {live}")
        for name, env in ENVS.items():
            row = await repo.get_host_by_hostname(name)
            assert row is not None, name
            await repo.update_host(
                str(row["id"]),
                env=env,
                env_source="user",
                import_state="adopted",
                source="imported",
                managed=1,
            )
        # The friend's machine, as provisioning hands it over.
        alice = await repo.get_host_by_hostname("alice-web")
        await repo.update_host(str(alice["id"]), owner="alice")
        print("E2E_FLEET_READY", flush=True)
        yield state


main.app.router.lifespan_context = _seeded

if __name__ == "__main__":
    uvicorn.run(main.app, host="127.0.0.1", port=PORT, log_level="warning")
