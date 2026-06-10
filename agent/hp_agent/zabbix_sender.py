from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import struct
import time
from dataclasses import dataclass

logger = logging.getLogger("hp-agent.zabbix")

ZABBIX_PROTOCOL = b"ZBXD"
ZABBIX_FLAGS = 0x01
ZABBIX_HEADER = ZABBIX_PROTOCOL + struct.pack("<B", ZABBIX_FLAGS)


@dataclass
class ZabbixItem:
    host: str
    key: str
    value: str | int | float
    clock: int | None = None
    ns: int | None = None

    def to_dict(self) -> dict:
        d: dict = {"host": self.host, "key": self.key, "value": self.value}
        if self.clock is not None:
            d["clock"] = self.clock
        if self.ns is not None:
            d["ns"] = self.ns
        return d


@dataclass
class ZabbixSenderConfig:
    server: str = "localhost"
    port: int = 10051
    hostname: str = ""
    enabled: bool = False
    send_interval: int = 60

    @classmethod
    def from_env(cls) -> ZabbixSenderConfig:
        import os

        return cls(
            server=os.environ.get("HP_ZABBIX_SERVER", "localhost"),
            port=int(os.environ.get("HP_ZABBIX_PORT", "10051")),
            hostname=os.environ.get("HP_ZABBIX_HOSTNAME", ""),
            enabled=os.environ.get("HP_ZABBIX_ENABLED", "").lower() in ("1", "true", "yes"),
            send_interval=int(os.environ.get("HP_ZABBIX_SEND_INTERVAL", "60")),
        )


def _encode_sender_request(items: list[ZabbixItem]) -> bytes:
    now = int(time.time())
    data = [item.to_dict() for item in items]
    payload = json.dumps(
        {
            "request": "sender data",
            "data": data,
            "clock": now,
        }
    )
    payload_bytes = payload.encode("utf-8")
    datalen = len(payload_bytes)
    reserved = 0
    header = ZABBIX_HEADER + struct.pack("<II", datalen, reserved)
    return header + payload_bytes


def _parse_response(data: bytes) -> dict:
    if len(data) < 13:
        raise ValueError(f"Response too short: {len(data)} bytes")
    if data[:4] != ZABBIX_PROTOCOL:
        raise ValueError(f"Invalid protocol header: {data[:4]!r}")
    flags = data[4]
    if flags != ZABBIX_FLAGS:
        raise ValueError(f"Unsupported flags: {flags:#x}")
    datalen, _reserved = struct.unpack("<II", data[5:13])
    if len(data) < 13 + datalen:
        raise ValueError(f"Truncated response: expected {datalen} bytes, got {len(data) - 13}")
    body = data[13 : 13 + datalen]
    return json.loads(body)


class ZabbixSender:
    def __init__(self, config: ZabbixSenderConfig) -> None:
        self._config = config
        self._hostname = config.hostname

    async def send(self, items: list[ZabbixItem]) -> dict:
        if not items:
            return {"processed": 0, "failed": 0, "total": 0}
        raw = _encode_sender_request(items)
        try:
            reader, writer = await asyncio.open_connection(self._config.server, self._config.port)
        except OSError as exc:
            logger.warning(
                "cannot connect to Zabbix at %s:%s: %s",
                self._config.server,
                self._config.port,
                exc,
            )
            return {"processed": 0, "failed": len(items), "total": len(items)}

        try:
            writer.write(raw)
            await writer.drain()
            response_data = await asyncio.wait_for(reader.read(4096), timeout=10)
            result = _parse_response(response_data)
            info = result.get("info", "")
            logger.debug("Zabbix response: %s", info)
            return result
        except Exception as exc:
            logger.warning("Zabbix send failed: %s", exc)
            return {"processed": 0, "failed": len(items), "total": len(items)}
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def send_value(self, key: str, value: str | int | float) -> dict:
        item = ZabbixItem(host=self._hostname, key=key, value=value, clock=int(time.time()))
        return await self.send([item])

    async def send_metrics(self, metrics: dict[str, str | int | float]) -> dict:
        now = int(time.time())
        items = [
            ZabbixItem(host=self._hostname, key=k, value=v, clock=now) for k, v in metrics.items()
        ]
        return await self.send(items)

    def system_info_to_metrics(self, sys_info: dict) -> dict[str, str | int | float]:
        metrics: dict[str, str | int | float] = {}
        metrics["hp.agent.status"] = 1
        metrics["hp.agent.cpu.count"] = sys_info.get("cpu_count", 0)

        disk = sys_info.get("disk", {})
        if disk:
            metrics["hp.agent.disk.total_gb"] = disk.get("total_gb", 0)
            metrics["hp.agent.disk.free_gb"] = disk.get("free_gb", 0)

        memory = sys_info.get("memory", {})
        if memory:
            metrics["hp.agent.memory.total_gb"] = memory.get("total_gb", 0)
            metrics["hp.agent.memory.free_gb"] = memory.get("free_gb", 0)

        load = sys_info.get("load", {})
        if load:
            metrics["hp.agent.load.1m"] = load.get("load_1m", 0)
            metrics["hp.agent.load.5m"] = load.get("load_5m", 0)
            metrics["hp.agent.load.15m"] = load.get("load_15m", 0)

        return metrics
