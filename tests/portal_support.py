"""Shared scaffolding for the invite-portal tests (#442 stage 2).

Helpers only - the pytest fixtures that build on these live in conftest.py under
``portal_*`` names, so no test module has to shadow another's fixture.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import httpx
from fastapi import FastAPI
from httpx import Request, Response

from homepilot.db.connection import Database
from homepilot.portal.models import InviteCaps
from homepilot.portal.repository import InviteRepository

PUBKEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGwqABCm8wgfVU6mDkhzQAaGT1kEJmpTB/J0UODkf1Xq me@lab"
CLONE_UPID = "UPID:pve1:0000A1B2:0123ABCD:65F0C0DE:qmclone:9000:root@pam:"
START_UPID = "UPID:pve1:0000A1B3:0123ABCE:65F0C0DF:qmstart:105:root@pam:"

PROXY_IP = "10.9.9.1"
PROXY_SECRET = "shared-secret-the-proxy-sets"
CN_A = "friend-a"
CN_B = "friend-b"

REDEEM_FORM = {
    "ciuser": "olli",
    "ssh_authorized_key": PUBKEY,
    "hostname": "lab-box",
    # A hostile client submitting caps: every one of these must be ignored in
    # favour of the invite row.
    "cores": "64",
    "memory_mb": "999999",
    "disk_gb": "4000",
    "template_vmid": "1",
    "node": "not-a-node",
    "owner": "somebody-else",
}


class FakePVE:
    """A minimal, stateful stand-in for the Proxmox API at the httpx boundary.

    Deliberately a copy of the stage-1 journey's fake rather than an import: one
    shared fake that drifts would weaken both gates at once.
    """

    def __init__(self) -> None:
        self.seen: list[tuple[str, str]] = []
        self.bodies: dict[str, Any] = {}
        # The guest's side of a tailnet join, which the journey gates drive both
        # ways (#628): a first attempt whose key is refused, then a retry with a
        # fresh one that works. Keyed by pid, because exec-status is a SEPARATE
        # call that knows nothing but the pid - which is the whole reason
        # `agent_run` exists and the whole reason the 3.6.12 join was wrong.
        self.scripts: dict[int, str] = {}
        self.next_pid = 4242
        self.tailscale_up_rc = 0
        self.tailscale_up_err = ""

    def handle(self, request: Request) -> Response:
        path = request.url.path.removeprefix("/api2/json")
        self.seen.append((request.method, path))
        if request.content:
            self.bodies[path] = json.loads(request.content)

        if path == "/cluster/nextid":
            return Response(200, json={"data": 105})
        if path == "/nodes/pve1/qemu/9000/clone":
            return Response(200, json={"data": CLONE_UPID})
        if path.startswith("/nodes/pve1/tasks/") and path.endswith("/status"):
            return Response(200, json={"data": {"status": "stopped", "exitstatus": "OK"}})
        if path == "/nodes/pve1/qemu/105/config":
            return Response(200, json={"data": None})
        if path == "/nodes/pve1/qemu/105/resize":
            return Response(200, json={"data": None})
        if path == "/nodes/pve1/qemu/105/status/start":
            return Response(200, json={"data": START_UPID})
        # The guest-agent surface, ALL of it (#628). This fake used to answer
        # /agent/exec with a pid and nothing else, so `agent_run` raised on the
        # exec-status poll and every tailnet join in these journeys came out
        # "failed" - which the tests then asserted as if it were the shipped
        # behaviour. A fake that cannot succeed proves nothing about success.
        if path == "/nodes/pve1/qemu/105/agent/ping":
            return Response(200, json={"data": {}})
        if path == "/nodes/pve1/qemu/105/agent/file-write":
            return Response(200, json={"data": {}})
        if path == "/nodes/pve1/qemu/105/agent/exec":
            # PVE declares `additionalProperties => 0` on this endpoint and
            # refuses a body carrying anything it does not name. A fake that
            # shrugs at an extra parameter is how 3.6.12 shipped an
            # `agent_exec` sending `capture-output: 1`, which PVE rejected on
            # EVERY call - the whole of #628's live failure (see
            # tests/test_proxmox_provision_adapter.py for the dedicated gate).
            extra = sorted(set(self.bodies.get(path) or {}) - {"command"})
            if extra:
                return Response(
                    400,
                    json={
                        "data": None,
                        "message": "Parameter verification failed.\n",
                        "errors": {
                            name: (
                                "property is not defined in schema and the schema "
                                "does not allow additional properties"
                            )
                            for name in extra
                        },
                    },
                )
            self.next_pid += 1
            command = (self.bodies.get(path) or {}).get("command") or []
            self.scripts[self.next_pid] = command[-1] if command else ""
            return Response(200, json={"data": {"pid": self.next_pid}})
        if path == "/nodes/pve1/qemu/105/agent/exec-status":
            pid = int(request.url.params.get("pid", 0))
            script = self.scripts.get(pid, "")
            # A guest that already has tailscale: the probe finds it, so nothing
            # is installed and the only command with an interesting outcome is
            # the join itself.
            if "tailscale up" in script and self.tailscale_up_rc:
                return Response(
                    200,
                    json={
                        "data": {
                            "exited": 1,
                            "exitcode": self.tailscale_up_rc,
                            "out-data": "",
                            "err-data": self.tailscale_up_err,
                        }
                    },
                )
            return Response(200, json={"data": {"exited": 1, "exitcode": 0, "out-data": ""}})
        if path == "/nodes/pve1/qemu/105/agent/network-get-interfaces":
            return Response(
                200,
                json={
                    "data": {
                        "result": [
                            {"name": "lo", "ip-addresses": [{"ip-address": "127.0.0.1"}]},
                            {"name": "eth0", "ip-addresses": [{"ip-address": "10.0.0.42"}]},
                        ]
                    }
                },
            )
        return Response(501, text=f"unhandled {request.method} {path}")


def portal_settings(**overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "portal_cn_header": "ssl-client-subject-dn",
        "portal_verify_header": "ssl-client-verify",
        "portal_trusted_proxy": f"{PROXY_IP}/32",
        "portal_proxy_secret": PROXY_SECRET,
        "portal_base_url": "https://portal.example",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def client_for(app: FastAPI, peer: str = PROXY_IP) -> httpx.AsyncClient:
    """An HTTP client whose requests appear to come from `peer`."""
    transport = httpx.ASGITransport(app=app, client=(peer, 44444))
    return httpx.AsyncClient(transport=transport, base_url="http://portal.example")


def cert_headers(
    cn: str = CN_A,
    secret: str = PROXY_SECRET,
    verify: str = "SUCCESS",
) -> dict[str, str]:
    return {
        "ssl-client-subject-dn": f"CN={cn},OU=lab,O=MTC Lab",
        "ssl-client-verify": verify,
        "x-hp-portal-secret": secret,
    }


async def mint(
    db: Database,
    cn: str = CN_A,
    ttl: timedelta = timedelta(days=7),
    **caps_overrides: Any,
) -> tuple[str, str]:
    caps_values: dict[str, Any] = {
        "template_vmid": 9000,
        "node": "pve1",
        "cores": 2,
        "memory_mb": 2048,
        "disk_gb": 20,
    }
    caps_values.update(caps_overrides)
    invites = InviteRepository(db)
    return await invites.create_invite(
        bound_cn=cn,
        caps=InviteCaps(**caps_values),
        created_by="olli",
        ttl=ttl,
    )


def invite_row(db_path: str, invite_id: str) -> dict[str, Any]:
    """Read the invite with a plain sqlite3 connection - assert what landed on disk."""
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM invites WHERE id = ?", (invite_id,)).fetchone()
        assert row is not None
        return dict(row)
    finally:
        conn.close()


def task_rows(db_path: str) -> list[dict[str, Any]]:
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute("SELECT * FROM tasks").fetchall()]
    finally:
        conn.close()


async def poll_status(
    client: httpx.AsyncClient,
    token: str,
    timeout: float = 10.0,
) -> httpx.Response:
    """Fetch the status page until the build is no longer running."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        response = await client.get(f"/invite/{token}/status", headers=cert_headers())
        assert response.status_code == 200, response.text
        if "Building" not in response.text:
            return response
        await asyncio.sleep(0.05)
    raise AssertionError("the status page never left the 'Building' state")
