from __future__ import annotations

import logging
from typing import Any

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
    def __init__(self, base_url: str, token: str, verify_ssl: bool = True):
        self._base_url = base_url.rstrip("/")
        self._token = token
        verify = verify_ssl
        self._client = httpx.AsyncClient(
            base_url=f"{self._base_url}/api2/json",
            headers={"Authorization": f"PVEAPIToken={self._token}"},
            verify=verify,
            timeout=httpx.Timeout(30.0),
        )

    async def call(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        path = path.lstrip("/")
        try:
            response = await self._client.request(
                method=method.upper(),
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
                method=method.upper(),
                path=path,
                status_code=status,
                body=resp_body,
            ) from exc
        except httpx.RequestError as exc:
            raise ProxmoxError(
                method=method.upper(),
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
