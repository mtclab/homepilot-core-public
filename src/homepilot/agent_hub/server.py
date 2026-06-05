from __future__ import annotations

import asyncio
import hmac
import json
import logging
import signal
import ssl
import struct
import uuid
from typing import Any, cast

from .registry import AgentRegistry
from .tokens import BootstrapTokenStore

logger = logging.getLogger(__name__)

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
            raise ConnectionError("agent disconnected")
        data += chunk
    return data


async def _read_message(reader: asyncio.StreamReader) -> dict[str, Any]:
    hdr = await _read_exact(reader, HEADER_LEN)
    length = struct.unpack("!I", hdr)[0]
    if length > MAX_MESSAGE_SIZE:
        raise ValueError(f"Message too large: {length} bytes (max {MAX_MESSAGE_SIZE})")
    body = await _read_exact(reader, length)
    return cast(dict[str, Any], json.loads(body))


class AgentCommandError(Exception):
    """An agent executed the request but returned an application-level error
    (e.g. path not in allowlist, file not found). Distinct from a transport
    failure (ConnectionError/TimeoutError)."""


class AgentHubServer:
    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 8443,
        auth_token: str | None = None,
        registry: AgentRegistry | None = None,
        ssl_context: ssl.SSLContext | None = None,
        token_store: BootstrapTokenStore | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.auth_token = auth_token
        self.registry = registry or AgentRegistry()
        self._ssl_context = ssl_context
        self._token_store = token_store or BootstrapTokenStore()
        self._server: asyncio.Server | None = None

    async def _verify_auth(self, request: dict[str, Any]) -> bool:
        token = request.get("auth_token", "")
        if not token:
            return False
        if self.auth_token and hmac.compare_digest(token, self.auth_token):
            return True
        return bool(await self._token_store.consume(token))

    async def _handle_agent(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = writer.get_extra_info("peername")
        agent_id: str | None = None
        try:
            handshake = await asyncio.wait_for(_read_message(reader), timeout=30)
            if not await self._verify_auth(handshake):
                writer.write(
                    _encode(
                        {
                            "error": "invalid auth_token",
                            "request_id": handshake.get("request_id", ""),
                        }
                    )
                )
                await writer.drain()
                writer.close()
                await writer.wait_closed()
                return

            action = handshake.get("action", "")
            if action != "register":
                writer.write(
                    _encode(
                        {
                            "error": "must register first",
                            "request_id": handshake.get("request_id", ""),
                        }
                    )
                )
                await writer.drain()
                writer.close()
                await writer.wait_closed()
                return

            hostname = handshake.get("hostname", "unknown")
            agent_id = handshake.get("agent_id", str(uuid.uuid4()))
            system_info = handshake.get("system_info", {})

            self.registry.register(
                agent_id=agent_id,
                hostname=hostname,
                system_info=system_info,
                writer=writer,
            )
            logger.info("agent registered: %s (host=%s)", agent_id, hostname)

            writer.write(
                _encode(
                    {
                        "action": "register_ack",
                        "agent_id": agent_id,
                        "request_id": handshake.get("request_id", ""),
                    }
                )
            )
            await writer.drain()

            while True:
                try:
                    msg = await asyncio.wait_for(_read_message(reader), timeout=300)
                except (TimeoutError, ConnectionError):
                    break

                request_id = msg.get("request_id", "")
                msg_action = msg.get("action", "")

                if msg_action == "heartbeat":
                    self.registry.update_heartbeat(agent_id)
                    writer.write(_encode({"action": "heartbeat_ack", "request_id": request_id}))
                    await writer.drain()
                elif msg_action == "command_result":
                    self.registry.store_command_result(agent_id, msg)
                    writer.write(_encode({"action": "result_ack", "request_id": request_id}))
                    await writer.drain()
                elif msg_action == "report_state":
                    self.registry.update_state(agent_id, msg.get("state", {}))
                    writer.write(_encode({"action": "state_ack", "request_id": request_id}))
                    await writer.drain()
                elif msg_action == "zabbix_push_result":
                    self.registry.store_command_result(agent_id, msg)
                else:
                    writer.write(
                        _encode(
                            {
                                "error": f"unknown action: {msg_action}",
                                "request_id": request_id,
                            }
                        )
                    )
                    await writer.drain()

        except Exception:
            logger.exception("error handling agent %s", agent_id or peer)
        finally:
            if agent_id:
                self.registry.unregister(agent_id)
                logger.info("agent disconnected: %s", agent_id)
            writer.close()
            await writer.wait_closed()

    async def start(self) -> None:
        if self._ssl_context:
            self._server = await asyncio.start_server(
                self._handle_agent, self.host, self.port, ssl=self._ssl_context
            )
            logger.info("agent hub listening on %s:%s (TLS)", self.host, self.port)
        else:
            self._server = await asyncio.start_server(self._handle_agent, self.host, self.port)
            logger.info("agent hub listening on %s:%s", self.host, self.port)

    async def serve_forever(self) -> None:
        await self.start()
        stop = asyncio.Event()

        def _signal_handler() -> None:
            stop.set()

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _signal_handler)
        await stop.wait()
        await self.stop()

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
            logger.info("agent hub stopped")

    def _finalize_result(
        self,
        agent_id: str,
        action: str,
        command_or_path: str,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        """Audit by real outcome and raise if the agent reported an error.

        The agent replies with an ``error`` key when the request failed on the
        host (path not in allowlist, file not found, …). Without this check the
        caller would treat a rejected write as success."""
        error = result.get("error")
        self.registry.audit_log.log(
            agent_id=agent_id,
            action=action,  # type: ignore[arg-type]
            command_or_path=command_or_path,
            result="error" if error is not None else "success",
            exit_code=result.get("exit_code"),
        )
        if error is not None:
            raise AgentCommandError(str(error))
        return result

    async def send_command(self, agent_id: str, command: str, timeout: int = 30) -> dict[str, Any]:
        agent = self.registry.get(agent_id)
        if not agent or not agent.writer:
            self.registry.audit_log.log(
                agent_id=agent_id,
                action="exec",
                command_or_path=command,
                result="error",
                exit_code=None,
            )
            raise ConnectionError(f"agent {agent_id} not connected")

        request_id = str(uuid.uuid4())
        msg = {
            "action": "exec",
            "command": command,
            "timeout": timeout,
            "request_id": request_id,
        }
        agent.writer.write(_encode(msg))
        await agent.writer.drain()

        result = await asyncio.wait_for(
            self.registry.wait_for_result(agent_id, request_id),
            timeout=30,
        )
        return self._finalize_result(agent_id, "exec", command, result)

    async def send_zabbix_push(self, agent_id: str) -> dict[str, Any]:
        agent = self.registry.get(agent_id)
        if not agent or not agent.writer:
            raise ConnectionError(f"agent {agent_id} not connected")

        request_id = str(uuid.uuid4())
        msg = {"action": "zabbix_push", "request_id": request_id}
        agent.writer.write(_encode(msg))
        await agent.writer.drain()

        result = await asyncio.wait_for(
            self.registry.wait_for_result(agent_id, request_id),
            timeout=30,
        )
        return result

    async def send_read_file(self, agent_id: str, path: str) -> dict[str, Any]:
        agent = self.registry.get(agent_id)
        if not agent or not agent.writer:
            self.registry.audit_log.log(
                agent_id=agent_id,
                action="read_file",
                command_or_path=path,
                result="error",
                exit_code=None,
            )
            raise ConnectionError(f"agent {agent_id} not connected")

        request_id = str(uuid.uuid4())
        msg = {"action": "read_file", "path": path, "request_id": request_id}
        agent.writer.write(_encode(msg))
        await agent.writer.drain()

        result = await asyncio.wait_for(
            self.registry.wait_for_result(agent_id, request_id),
            timeout=30,
        )
        return self._finalize_result(agent_id, "read_file", path, result)

    async def send_write_file(self, agent_id: str, path: str, content: str) -> dict[str, Any]:
        agent = self.registry.get(agent_id)
        if not agent or not agent.writer:
            self.registry.audit_log.log(
                agent_id=agent_id,
                action="write_file",
                command_or_path=path,
                result="error",
                exit_code=None,
            )
            raise ConnectionError(f"agent {agent_id} not connected")

        request_id = str(uuid.uuid4())
        msg = {"action": "write_file", "path": path, "content": content, "request_id": request_id}
        agent.writer.write(_encode(msg))
        await agent.writer.drain()

        result = await asyncio.wait_for(
            self.registry.wait_for_result(agent_id, request_id),
            timeout=30,
        )
        return self._finalize_result(agent_id, "write_file", path, result)
