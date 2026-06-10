from __future__ import annotations

import asyncio
import json
import logging
import platform
import struct
import sys
import uuid
from typing import Any

from hp_agent.command_executor import CommandAllowlist, CommandExecutor
from hp_agent.config import AgentConfig
from hp_agent.file_ops import FileOps
from hp_agent.system_info import collect_system_info
from hp_agent.zabbix_sender import ZabbixSender, ZabbixSenderConfig

logger = logging.getLogger("hp-agent")

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
            raise ConnectionError("hub disconnected")
        data += chunk
    return data


async def _read_message(reader: asyncio.StreamReader) -> dict[str, Any]:
    hdr = await _read_exact(reader, HEADER_LEN)
    length = struct.unpack("!I", hdr)[0]
    if length > MAX_MESSAGE_SIZE:
        raise ValueError(f"Message too large: {length} bytes (max {MAX_MESSAGE_SIZE})")
    body = await _read_exact(reader, length)
    return json.loads(body)


class HPAgent:
    def __init__(self, config: AgentConfig | None = None) -> None:
        self.config = config or AgentConfig.from_env()
        self.agent_id = self.config.agent_id or str(uuid.uuid4())
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        allowlist = CommandAllowlist.default(privileged=self.config.privileged)
        self._executor = CommandExecutor(allowlist=allowlist)
        self._file_ops = FileOps()
        self._running = False
        self._heartbeat_interval = self.config.heartbeat_interval
        self._zabbix: ZabbixSender | None = None
        if self.config.zabbix_enabled:
            zbx_cfg = ZabbixSenderConfig(
                server=self.config.zabbix_server,
                port=self.config.zabbix_port,
                hostname=self.config.zabbix_hostname or platform.node(),
                enabled=True,
                send_interval=self.config.zabbix_send_interval,
            )
            self._zabbix = ZabbixSender(zbx_cfg)
            logger.info(
                "Zabbix trapper enabled: %s:%s as %s",
                zbx_cfg.server,
                zbx_cfg.port,
                zbx_cfg.hostname,
            )

    async def connect(self) -> None:
        ssl_ctx = self.config.get_ssl_context()
        tls_label = " (TLS)" if ssl_ctx else ""
        logger.info(
            "connecting to hub at %s:%s%s",
            self.config.hub_host,
            self.config.hub_port,
            tls_label,
        )
        if ssl_ctx:
            self._reader, self._writer = await asyncio.open_connection(
                self.config.hub_host, self.config.hub_port, ssl=ssl_ctx
            )
        else:
            self._reader, self._writer = await asyncio.open_connection(
                self.config.hub_host, self.config.hub_port
            )
        logger.info("connected to hub")

    async def register(self) -> dict[str, Any]:
        sys_info = await collect_system_info()
        msg = {
            "action": "register",
            "agent_id": self.agent_id,
            "hostname": platform.node(),
            "system_info": sys_info,
            "auth_token": self.config.auth_token,
            "request_id": str(uuid.uuid4()),
        }
        self._writer.write(_encode(msg))
        await self._writer.drain()
        response = await _read_message(self._reader)
        if "error" in response:
            raise ConnectionError(f"registration failed: {response['error']}")
        self.agent_id = response.get("agent_id", self.agent_id)
        logger.info("registered as agent %s", self.agent_id)
        return response

    async def run(self) -> None:
        self._running = True
        await self._connect_with_retry()
        if not self._running:
            return

        heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        zabbix_task: asyncio.Task | None = None
        if self._zabbix:
            zabbix_task = asyncio.create_task(self._zabbix_metrics_loop())
        try:
            while self._running:
                try:
                    msg = await asyncio.wait_for(_read_message(self._reader), timeout=300)
                except (TimeoutError, ConnectionError):
                    logger.warning("hub connection lost, reconnecting...")
                    await self._reconnect()
                    continue

                request_id = msg.get("request_id", "")
                action = msg.get("action", "")

                if action == "exec":
                    await self._handle_exec(msg, request_id)
                elif action == "read_file":
                    await self._handle_read_file(msg, request_id)
                elif action == "write_file":
                    await self._handle_write_file(msg, request_id)
                elif action == "zabbix_push":
                    await self._handle_zabbix_push(msg, request_id)
                else:
                    self._writer.write(
                        _encode(
                            {
                                "error": f"unknown action: {action}",
                                "request_id": request_id,
                            }
                        )
                    )
                    await self._writer.drain()
        finally:
            heartbeat_task.cancel()
            if zabbix_task:
                zabbix_task.cancel()
            self._running = False

    async def _handle_exec(self, msg: dict[str, Any], request_id: str) -> None:
        command = msg.get("command", "")
        timeout = msg.get("timeout", 30)

        exit_code, stdout, stderr = await self._executor.exec(command, timeout)
        response = {
            "action": "command_result",
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
            "request_id": request_id,
        }
        self._writer.write(_encode(response))
        await self._writer.drain()

    async def _handle_read_file(self, msg: dict[str, Any], request_id: str) -> None:
        path = msg.get("path", "")
        try:
            content = await self._file_ops.read_file(path)
            self._writer.write(
                _encode(
                    {
                        "action": "command_result",
                        "content": content,
                        "request_id": request_id,
                    }
                )
            )
        except Exception as exc:
            self._writer.write(
                _encode(
                    {
                        "action": "command_result",
                        "error": str(exc),
                        "request_id": request_id,
                    }
                )
            )
        await self._writer.drain()

    async def _handle_write_file(self, msg: dict[str, Any], request_id: str) -> None:
        path = msg.get("path", "")
        content = msg.get("content", "")
        try:
            await self._file_ops.write_file(path, content)
            self._writer.write(
                _encode(
                    {
                        "action": "command_result",
                        "status": "ok",
                        "request_id": request_id,
                    }
                )
            )
        except Exception as exc:
            self._writer.write(
                _encode(
                    {
                        "action": "command_result",
                        "error": str(exc),
                        "request_id": request_id,
                    }
                )
            )
        await self._writer.drain()

    async def _handle_zabbix_push(self, msg: dict[str, Any], request_id: str) -> None:
        if not self._zabbix:
            self._writer.write(
                _encode(
                    {
                        "error": "zabbix not configured",
                        "request_id": request_id,
                    }
                )
            )
            await self._writer.drain()
            return
        try:
            sys_info = await collect_system_info()
            metrics = self._zabbix.system_info_to_metrics(sys_info)
            result = await self._zabbix.send_metrics(metrics)
            self._writer.write(
                _encode(
                    {
                        "action": "zabbix_push_result",
                        "metrics_sent": len(metrics),
                        "zabbix_ok": result.get("response") == "success",
                        "request_id": request_id,
                    }
                )
            )
        except Exception as exc:
            self._writer.write(_encode({"error": str(exc), "request_id": request_id}))
        await self._writer.drain()

    async def _heartbeat_loop(self) -> None:
        while self._running:
            await asyncio.sleep(self._heartbeat_interval)
            if self._writer and not self._writer.is_closing():
                try:
                    self._writer.write(
                        _encode(
                            {
                                "action": "heartbeat",
                                "request_id": str(uuid.uuid4()),
                            }
                        )
                    )
                    await self._writer.drain()
                except (ConnectionError, OSError):
                    logger.warning("heartbeat send failed, hub may be disconnected")

    async def _zabbix_metrics_loop(self) -> None:
        if not self._zabbix:
            return
        interval = self.config.zabbix_send_interval
        while self._running:
            await asyncio.sleep(interval)
            try:
                sys_info = await collect_system_info()
                metrics = self._zabbix.system_info_to_metrics(sys_info)
                await self._zabbix.send_metrics(metrics)
                logger.debug(
                    "Zabbix metrics sent: %d items",
                    len(metrics),
                )
            except Exception as exc:
                logger.warning("Zabbix metrics send failed: %s", exc)

    async def _close_writer(self) -> None:
        if self._writer is not None:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass

    async def _connect_with_retry(self) -> None:
        """Connect + register with capped exponential backoff, retrying until
        successful or stop() is called.

        Prevents the agent from crashing with a traceback (and relying on the
        systemd restart loop / spamming logs) when the hub is briefly
        unreachable at boot or transiently rejects registration."""
        delay = 1.0
        attempt = 0
        while self._running:
            attempt += 1
            try:
                await self.connect()
                await self.register()
                if attempt > 1:
                    logger.info("connected to hub after %d attempts", attempt)
                return
            except Exception as exc:
                await self._close_writer()
                logger.warning(
                    "connect attempt %d failed (%s); retrying in %.0fs",
                    attempt,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30.0)

    async def _reconnect(self) -> None:
        await self._close_writer()
        await self._connect_with_retry()

    async def stop(self) -> None:
        self._running = False
        if self._writer:
            self._writer.close()
            await self._writer.wait_closed()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    config = AgentConfig.from_env()
    agent = HPAgent(config)
    try:
        asyncio.run(agent.run())
    except KeyboardInterrupt:
        logger.info("shutting down")
    except Exception:
        logger.exception("fatal error")
        sys.exit(1)
