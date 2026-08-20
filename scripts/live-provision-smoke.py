#!/usr/bin/env python3
"""Run one real clone-from-template provision against a live Proxmox host.

NOT part of the pytest suite: it creates a real VM and leaves it running. Run it
by hand against a lab cluster when the mocked gates have already passed.

Usage:
    PVE_HOST=pve1.lab:8006 \
    PVE_TOKEN='root@pam!hp=00000000-0000-0000-0000-000000000000' \
    NODE=pve1 TEMPLATE_VMID=9000 \
    [NAME=smoke-01] [SSH_KEY="$(cat ~/.ssh/id_ed25519.pub)"] [DISK_GB=20] \
    [VERIFY_SSL=0] \
    .venv/bin/python scripts/live-provision-smoke.py

Prints the provision task result (vmid / name / node / ip) and exits non-zero if
any step fails. Clean-up (`qm stop && qm destroy <vmid>`) is left to the
operator on purpose — a failed run's VM is the evidence.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from homepilot.adapters.proxmox import ProxmoxClient
from homepilot.db.connection import Database
from homepilot.db.migrations import run_migrations
from homepilot.db.repository import Repository
from homepilot.provision.models import ProvisionRequest
from homepilot.provision.service import ProvisionService
from homepilot.tasks.repository import TaskRepository


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        print(f"error: {name} is required", file=sys.stderr)
        raise SystemExit(2)
    return value


async def main() -> int:
    host = _required("PVE_HOST")
    token = _required("PVE_TOKEN")
    node = _required("NODE")
    template_vmid = int(_required("TEMPLATE_VMID"))
    name = os.environ.get("NAME", "hp-smoke-01")
    ssh_key = os.environ.get("SSH_KEY") or None
    disk_gb = int(os.environ["DISK_GB"]) if os.environ.get("DISK_GB") else None
    verify_ssl = os.environ.get("VERIFY_SSL", "1") not in ("0", "false", "no")

    base_url = host if host.startswith("http") else f"https://{host}"
    proxmox = ProxmoxClient(base_url=base_url, token=token, verify_ssl=verify_ssl)

    db_path = os.environ.get("SMOKE_DB", "/tmp/hp-live-provision-smoke.db")
    database = Database(db_path)
    await database.connect()
    await run_migrations(database)

    service = ProvisionService(
        proxmox=proxmox,
        task_repo=TaskRepository(database),
        repo=Repository(database),
    )
    request = ProvisionRequest(
        name=name,
        node=node,
        template_vmid=template_vmid,
        disk_gb=disk_gb,
        ssh_authorized_key=ssh_key,
        owner=os.environ.get("OWNER") or None,
    )

    print(f"provisioning {name} from template {template_vmid} on {node}...")
    task_id = await service.start(request, actor="live-smoke")
    task: dict | None = None
    while True:
        task = await service.task_repo.get_task(task_id)
        if task is not None and task["status"] in ("succeeded", "failed", "cancelled"):
            break
        await asyncio.sleep(2.0)

    assert task is not None
    print(f"task {task_id}: {task['status']}")
    if task["result_json"]:
        print(json.dumps(json.loads(task["result_json"]), indent=2))
    if task["error"]:
        print(f"error: {task['error']}", file=sys.stderr)

    await proxmox.close()
    await database.close()
    return 0 if task["status"] == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
