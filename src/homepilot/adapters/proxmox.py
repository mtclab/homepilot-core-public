from __future__ import annotations

import asyncio
import logging
import time
from typing import Any
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)


class ProxmoxError(Exception):
    def __init__(self, method: str, path: str, status_code: int, body: str):
        self.method = method
        self.path = path
        self.status_code = status_code
        self.body = body
        super().__init__(f"{method} {path} -> {status_code}: {body[:200]}")


class ProxmoxClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        verify_ssl: bool = True,
        write_token: str | None = None,
    ):
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._write_token = write_token or token
        self._has_separate_write = write_token is not None and write_token != token
        verify = verify_ssl
        self._client = httpx.AsyncClient(
            base_url=f"{self._base_url}/api2/json",
            headers={"Authorization": f"PVEAPIToken={self._token}"},
            verify=verify,
            timeout=httpx.Timeout(30.0),
        )
        if self._has_separate_write:
            self._write_client = httpx.AsyncClient(
                base_url=f"{self._base_url}/api2/json",
                headers={"Authorization": f"PVEAPIToken={self._write_token}"},
                verify=verify,
                timeout=httpx.Timeout(30.0),
            )
        else:
            self._write_client = self._client

    async def call(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        path = path.lstrip("/")
        method_upper = method.upper()
        is_mutation = method_upper in ("POST", "PUT", "PATCH", "DELETE")
        client = self._write_client if is_mutation else self._client
        try:
            response = await client.request(
                method=method_upper,
                url=f"/{path}",
                json=body,
                params=query,
            )
            response.raise_for_status()
            result: dict[str, Any] = response.json()
            return result
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            try:
                resp_body = exc.response.text
            except (UnicodeDecodeError, httpx.ResponseNotRead):
                resp_body = "<unreadable>"
            raise ProxmoxError(
                method=method_upper,
                path=path,
                status_code=status,
                body=resp_body,
            ) from exc
        except httpx.RequestError as exc:
            raise ProxmoxError(
                method=method_upper,
                path=path,
                status_code=0,
                body=str(exc),
            ) from exc

    async def read(self, path: str, query: dict[str, Any] | None = None) -> dict[str, Any]:
        return await self.call("GET", path, query=query)

    async def snapshot(self, node: str, vmid: int, name: str) -> dict[str, Any]:
        try:
            return await self.call(
                "POST",
                f"/nodes/{node}/qemu/{vmid}/snapshot",
                body={"snapname": name},
            )
        except ProxmoxError:
            return await self.call(
                "POST",
                f"/nodes/{node}/lxc/{vmid}/snapshot",
                body={"snapname": name},
            )

    async def delete_snapshot(self, node: str, vmid: int, name: str) -> dict[str, Any]:
        try:
            return await self.call(
                "DELETE",
                f"/nodes/{node}/qemu/{vmid}/snapshot/{name}",
            )
        except ProxmoxError:
            return await self.call(
                "DELETE",
                f"/nodes/{node}/lxc/{vmid}/snapshot/{name}",
            )

    async def clone_vm(
        self,
        node: str,
        template_vmid: int,
        new_vmid: int,
        name: str,
        full: bool = True,
        pool: str | None = None,
    ) -> str:
        """Clone a template into a new VM. Returns the UPID of the clone task."""
        body: dict[str, Any] = {
            "newid": new_vmid,
            "name": name,
            "full": 1 if full else 0,
        }
        if pool:
            body["pool"] = pool
        result = await self.call("POST", f"/nodes/{node}/qemu/{template_vmid}/clone", body=body)
        return str(result.get("data", ""))

    async def wait_for_task(
        self,
        node: str,
        upid: str,
        timeout_s: float = 600.0,
        poll_interval: float = 2.0,
    ) -> dict[str, Any]:
        """Block until a PVE task reaches status 'stopped'; return its status dict.

        Raises ProxmoxError if the task ends with a non-OK exitstatus, or if the
        timeout elapses first — a caller must never mistake "still running" or
        "failed" for success.
        """
        # A UPID contains ':' separators, which are path-illegal unescaped.
        path = f"/nodes/{node}/tasks/{quote(upid, safe='')}/status"
        deadline = time.monotonic() + timeout_s
        while True:
            result = await self.read(path)
            data = result.get("data", result)
            if isinstance(data, dict) and data.get("status") == "stopped":
                exitstatus = data.get("exitstatus")
                if exitstatus != "OK":
                    raise ProxmoxError(
                        method="GET",
                        path=path,
                        status_code=0,
                        body=f"PVE task {upid} finished with exitstatus {exitstatus!r}",
                    )
                return data
            if time.monotonic() >= deadline:
                raise ProxmoxError(
                    method="GET",
                    path=path,
                    status_code=0,
                    body=f"PVE task {upid} did not finish within {timeout_s}s",
                )
            await asyncio.sleep(poll_interval)

    async def set_vm_config(self, node: str, vmid: int, config: dict[str, Any]) -> dict[str, Any]:
        body = dict(config)
        # PVE declares the cloud-init `sshkeys` property with format 'urlencoded'
        # and uri_unescape()s the stored value once, server-side, whatever the
        # request transport was. We send a JSON body, so no transport layer adds
        # an encoding of its own: exactly ONE quote() here is what the API
        # requires. The "double URL-encode sshkeys" folklore describes
        # form-encoded/query transports (pvesh, curl -d), where the transport
        # supplies the second layer — encoding twice over JSON would store a
        # literal '%2B'-riddled key and lock the user out.
        if body.get("sshkeys"):
            body["sshkeys"] = quote(str(body["sshkeys"]), safe="")
        return await self.call("POST", f"/nodes/{node}/qemu/{vmid}/config", body=body)

    async def resize_disk(self, node: str, vmid: int, disk: str, size: str) -> dict[str, Any]:
        return await self.call(
            "PUT",
            f"/nodes/{node}/qemu/{vmid}/resize",
            body={"disk": disk, "size": size},
        )

    async def start_vm(self, node: str, vmid: int) -> str:
        result = await self.call("POST", f"/nodes/{node}/qemu/{vmid}/status/start")
        return str(result.get("data", ""))

    async def get_vm_current(self, node: str, vmid: int) -> dict[str, Any]:
        return await self.read(f"/nodes/{node}/qemu/{vmid}/status/current")

    async def get_vm_agent_network(self, node: str, vmid: int) -> dict[str, Any] | None:
        """Guest-agent interface list, or None when the agent cannot answer.

        A missing/not-yet-started qemu-guest-agent is the normal case for a fresh
        clone, not an error — callers treat None as "no IP known yet".
        """
        try:
            return await self.read(f"/nodes/{node}/qemu/{vmid}/agent/network-get-interfaces")
        except ProxmoxError:
            return None

    async def agent_ping(self, node: str, vmid: int) -> bool:
        """True only when qemu-guest-agent actually answers inside the guest.

        The cheapest honest precondition for "HomePilot can run something in
        here": a guest with the Agent option enabled but no running qemu-guest-agent
        accepts the config and answers nothing.
        """
        try:
            await self.call("POST", f"/nodes/{node}/qemu/{vmid}/agent/ping")
            return True
        except ProxmoxError:
            return False

    async def agent_exec(
        self, node: str, vmid: int, command: list[str], capture_output: bool = False
    ) -> dict[str, Any]:
        """Run one command inside a guest through qemu-guest-agent.

        PVE takes the argv as repeated `command` parameters; a single string
        would be handed to the guest as one argv[0] and fail. Raises ProxmoxError
        when the agent is absent or the call is refused — callers decide whether
        that is fatal.

        Returns the guest agent's PID, NOT a result: exec is asynchronous, so a
        caller that needs the outcome must poll `agent_exec_status`.
        `capture_output` is what makes that outcome carry stdout/stderr.
        """
        body: dict[str, Any] = {"command": command}
        if capture_output:
            body["capture-output"] = 1
        return await self.call(
            "POST",
            f"/nodes/{node}/qemu/{vmid}/agent/exec",
            body=body,
        )

    async def agent_exec_status(self, node: str, vmid: int, pid: int) -> dict[str, Any]:
        """Status of a command started by `agent_exec`.

        Carries `exited`, `exitcode`, and (when the exec captured output)
        `out-data`/`err-data`.
        """
        return await self.read(
            f"/nodes/{node}/qemu/{vmid}/agent/exec-status",
            query={"pid": pid},
        )

    async def agent_write_file(
        self, node: str, vmid: int, path: str, content: str
    ) -> dict[str, Any]:
        """Write a file inside a guest through qemu-guest-agent.

        The way to hand a secret to a guest: an argv is visible in the guest's
        process list and is echoed back in PVE task errors, while a file the
        caller deletes immediately is not.
        """
        return await self.call(
            "POST",
            f"/nodes/{node}/qemu/{vmid}/agent/file-write",
            body={"file": path, "content": content},
        )

    async def next_vmid(self, node: str) -> int:
        result = await self.read("/cluster/nextid")
        return int(result.get("data", result))

    async def test_connection(self) -> bool:
        try:
            await self.read("/version")
            return True
        except ProxmoxError:
            return False

    async def get_node_status(self, node: str) -> dict[str, Any]:
        return await self.read(f"/nodes/{node}/status")

    async def close(self) -> None:
        await self._client.aclose()
        if self._has_separate_write:
            await self._write_client.aclose()
