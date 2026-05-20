from __future__ import annotations

import asyncio
import json
import logging
import re as _re
import ssl
import struct
import uuid
from typing import Any

logger = logging.getLogger(__name__)

_HOSTNAME_RE = _re.compile(r"^[a-zA-Z0-9]([a-zA-Z0-9.-]{0,253}[a-zA-Z0-9])?$")


def _validate_hostname(host: str) -> None:
    if not _HOSTNAME_RE.match(host):
        raise JumpServerError(f"Invalid hostname: {host!r}")


HEADER_LEN = 4
MAX_MESSAGE_SIZE = 1_048_576


def _encode(msg: dict[str, Any]) -> bytes:
    payload = json.dumps(msg).encode("utf-8")
    return struct.pack("!I", len(payload)) + payload


async def _read_exact(reader: asyncio.StreamReader, n: int) -> bytes:
    data = b""
    while len(data) < n:
        chunk = await reader.read(n - len(data))
        if not chunk:
            raise ConnectionError("server disconnected")
        data += chunk
    return data


async def _read_message(reader: asyncio.StreamReader) -> dict[str, Any]:
    hdr = await _read_exact(reader, HEADER_LEN)
    length = struct.unpack("!I", hdr)[0]
    if length > MAX_MESSAGE_SIZE:
        raise ValueError(f"Message too large: {length} bytes (max {MAX_MESSAGE_SIZE})")
    body = await _read_exact(reader, length)
    result: dict[str, Any] = json.loads(body)
    return result


class JumpServerError(Exception):
    pass


class JumpServerClient:
    def __init__(
        self,
        host: str = "jumpserver",
        port: int = 50051,
        auth_token: str | None = None,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        self.host = host
        self.port = port
        if auth_token is not None and not auth_token:
            raise JumpServerError("auth_token must be non-empty when provided")
        self.auth_token = auth_token
        self._ssl_context = ssl_context
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    async def connect(self) -> None:
        if self._ssl_context:
            if self._ssl_context.verify_mode.name == "CERT_NONE":
                logger.warning("Jump server TLS enabled but certificate verification is disabled")
            self._reader, self._writer = await asyncio.open_connection(
                self.host, self.port, ssl=self._ssl_context
            )
        else:
            logger.warning("Jump server connection is plaintext — enable HP_JUMP_SERVER_TLS")
            self._reader, self._writer = await asyncio.open_connection(self.host, self.port)
        logger.info(
            "connected to jumpserver at %s:%s (tls=%s)",
            self.host,
            self.port,
            bool(self._ssl_context),
        )

    async def close(self) -> None:
        if self._writer:
            self._writer.close()
            await self._writer.wait_closed()
            self._reader = None
            self._writer = None

    async def _ensure_connected(self) -> None:
        if not self._writer or self._writer.is_closing():
            await self.connect()

    def _base_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if self.auth_token:
            payload["auth_token"] = self.auth_token
        return payload

    async def _request(self, msg: dict[str, Any]) -> dict[str, Any]:
        await self._ensure_connected()
        assert self._writer is not None
        assert self._reader is not None
        msg["request_id"] = str(uuid.uuid4())
        self._writer.write(_encode(msg))
        await self._writer.drain()
        return await _read_message(self._reader)

    async def exec(self, host: str, command: str, timeout: int = 30) -> dict[str, Any]:
        _validate_hostname(host)
        payload = self._base_payload()
        payload.update(
            {
                "action": "exec",
                "host": host,
                "command": command,
                "timeout": timeout,
            }
        )
        response = await self._request(payload)
        if "error" in response:
            raise JumpServerError(response["error"])
        return response

    async def read_file(self, host: str, path: str) -> dict[str, Any]:
        _validate_hostname(host)
        payload = self._base_payload()
        payload.update(
            {
                "action": "read_file",
                "host": host,
                "path": path,
            }
        )
        response = await self._request(payload)
        if "error" in response:
            raise JumpServerError(response["error"])
        return response

    async def write_file(self, host: str, path: str, content: str) -> dict[str, Any]:
        _validate_hostname(host)
        payload = self._base_payload()
        payload.update(
            {
                "action": "write_file",
                "host": host,
                "path": path,
                "content": content,
            }
        )
        response = await self._request(payload)
        if "error" in response:
            raise JumpServerError(response["error"])
        return response

    async def test_connection(self) -> bool:
        payload = self._base_payload()
        payload["action"] = "ping"
        try:
            response = await self._request(payload)
            return response.get("action") == "pong"
        except (ConnectionError, OSError):
            return False
