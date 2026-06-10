from __future__ import annotations

import json
import struct
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# hp_agent is the host-agent component (separate package/repo). It is not
# installed in the control-plane CI, so skip this module gracefully there.
pytest.importorskip("hp_agent.zabbix_sender")

from hp_agent.zabbix_sender import (
    ZABBIX_FLAGS,
    ZABBIX_PROTOCOL,
    ZabbixItem,
    ZabbixSender,
    ZabbixSenderConfig,
    _encode_sender_request,
    _parse_response,
)


class TestZabbixItemSerialization:
    def test_to_dict_without_clock(self):
        item = ZabbixItem(host="host1", key="agent.status", value=1)
        assert item.to_dict() == {"host": "host1", "key": "agent.status", "value": 1}

    def test_to_dict_with_clock(self):
        item = ZabbixItem(host="host1", key="agent.status", value=1, clock=1234567890)
        assert item.to_dict() == {
            "host": "host1",
            "key": "agent.status",
            "value": 1,
            "clock": 1234567890,
        }

    def test_to_dict_with_clock_and_ns(self):
        item = ZabbixItem(host="host1", key="agent.status", value=1, clock=1234567890, ns=500)
        assert item.to_dict() == {
            "host": "host1",
            "key": "agent.status",
            "value": 1,
            "clock": 1234567890,
            "ns": 500,
        }


class TestEncodeSenderRequest:
    def test_empty_list(self):
        with patch("hp_agent.zabbix_sender.time.time", return_value=999):
            raw = _encode_sender_request([])
        assert raw[:4] == ZABBIX_PROTOCOL
        assert raw[4] == ZABBIX_FLAGS
        datalen, reserved = struct.unpack("<II", raw[5:13])
        assert datalen == len(raw) - 13
        assert reserved == 0
        payload = json.loads(raw[13:].decode("utf-8"))
        assert payload["request"] == "sender data"
        assert payload["data"] == []
        assert payload["clock"] == 999

    def test_with_items(self):
        items = [
            ZabbixItem(host="h1", key="k1", value="v1", clock=1),
            ZabbixItem(host="h2", key="k2", value=42),
        ]
        with patch("hp_agent.zabbix_sender.time.time", return_value=1000):
            raw = _encode_sender_request(items)
        payload = json.loads(raw[13:].decode("utf-8"))
        assert payload["clock"] == 1000
        assert len(payload["data"]) == 2
        assert payload["data"][0] == {
            "host": "h1",
            "key": "k1",
            "value": "v1",
            "clock": 1,
        }
        assert payload["data"][1] == {"host": "h2", "key": "k2", "value": 42}

    def test_header_structure(self):
        item = ZabbixItem(host="h", key="k", value="v")
        raw = _encode_sender_request([item])
        assert len(raw) >= 13
        assert raw[:4] == b"ZBXD"
        assert raw[4:5] == struct.pack("<B", ZABBIX_FLAGS)
        datalen, reserved = struct.unpack("<II", raw[5:13])
        assert datalen == len(raw) - 13
        assert reserved == 0


class TestParseResponse:
    def _build_response(self, body_dict: dict) -> bytes:
        body = json.dumps(body_dict).encode("utf-8")
        datalen = len(body)
        header = ZABBIX_PROTOCOL + struct.pack("<B", ZABBIX_FLAGS)
        header += struct.pack("<II", datalen, 0)
        return header + body

    def test_valid_response(self):
        data = self._build_response({"response": "success", "info": "processed: 1"})
        result = _parse_response(data)
        assert result == {"response": "success", "info": "processed: 1"}

    def test_too_short(self):
        with pytest.raises(ValueError, match="Response too short"):
            _parse_response(b"ZBXD\x01\x00\x00")

    def test_invalid_protocol(self):
        data = b"FAIL\x01" + b"\x00" * 8 + b'{"a":1}'
        with pytest.raises(ValueError, match="Invalid protocol header"):
            _parse_response(data)

    def test_unsupported_flags(self):
        body = b'{"a":1}'
        datalen = len(body)
        header = ZABBIX_PROTOCOL + struct.pack("<B", 0x02)
        header += struct.pack("<II", datalen, 0)
        with pytest.raises(ValueError, match="Unsupported flags"):
            _parse_response(header + body)

    def test_truncated_body(self):
        body = b'{"a":1}'
        datalen = len(body) + 10
        header = ZABBIX_PROTOCOL + struct.pack("<B", ZABBIX_FLAGS)
        header += struct.pack("<II", datalen, 0)
        with pytest.raises(ValueError, match="Truncated response"):
            _parse_response(header + body)


class TestZabbixSenderConfigFromEnv:
    def test_defaults_when_no_env(self, monkeypatch):
        for key in [
            "HP_ZABBIX_SERVER",
            "HP_ZABBIX_PORT",
            "HP_ZABBIX_HOSTNAME",
            "HP_ZABBIX_ENABLED",
            "HP_ZABBIX_SEND_INTERVAL",
        ]:
            monkeypatch.delenv(key, raising=False)
        cfg = ZabbixSenderConfig.from_env()
        assert cfg.server == "localhost"
        assert cfg.port == 10051
        assert cfg.hostname == ""
        assert cfg.enabled is False
        assert cfg.send_interval == 60

    def test_reads_all_env_vars(self, monkeypatch):
        monkeypatch.setenv("HP_ZABBIX_SERVER", "zabbix.example.com")
        monkeypatch.setenv("HP_ZABBIX_PORT", "20051")
        monkeypatch.setenv("HP_ZABBIX_HOSTNAME", "web01")
        monkeypatch.setenv("HP_ZABBIX_ENABLED", "true")
        monkeypatch.setenv("HP_ZABBIX_SEND_INTERVAL", "120")
        cfg = ZabbixSenderConfig.from_env()
        assert cfg.server == "zabbix.example.com"
        assert cfg.port == 20051
        assert cfg.hostname == "web01"
        assert cfg.enabled is True
        assert cfg.send_interval == 120

    @pytest.mark.parametrize("val", ["1", "true", "yes", "TRUE", "YES"])
    def test_enabled_true_variants(self, monkeypatch, val):
        monkeypatch.setenv("HP_ZABBIX_ENABLED", val)
        cfg = ZabbixSenderConfig.from_env()
        assert cfg.enabled is True

    @pytest.mark.parametrize("val", ["0", "false", "no", "", "maybe"])
    def test_enabled_false_variants(self, monkeypatch, val):
        monkeypatch.setenv("HP_ZABBIX_ENABLED", val)
        cfg = ZabbixSenderConfig.from_env()
        assert cfg.enabled is False


class TestSystemInfoToMetrics:
    def test_full_dict(self):
        sender = ZabbixSender(ZabbixSenderConfig(hostname="h1"))
        sys_info = {
            "cpu_count": 4,
            "disk": {"total_gb": 100, "free_gb": 50},
            "memory": {"total_gb": 16, "free_gb": 8},
            "load": {"load_1m": 0.5, "load_5m": 0.6, "load_15m": 0.7},
        }
        metrics = sender.system_info_to_metrics(sys_info)
        assert metrics["hp.agent.status"] == 1
        assert metrics["hp.agent.cpu.count"] == 4
        assert metrics["hp.agent.disk.total_gb"] == 100
        assert metrics["hp.agent.disk.free_gb"] == 50
        assert metrics["hp.agent.memory.total_gb"] == 16
        assert metrics["hp.agent.memory.free_gb"] == 8
        assert metrics["hp.agent.load.1m"] == 0.5
        assert metrics["hp.agent.load.5m"] == 0.6
        assert metrics["hp.agent.load.15m"] == 0.7

    def test_partial_dict(self):
        sender = ZabbixSender(ZabbixSenderConfig(hostname="h1"))
        sys_info = {"cpu_count": 2, "disk": {"total_gb": 50, "free_gb": 25}}
        metrics = sender.system_info_to_metrics(sys_info)
        assert metrics["hp.agent.status"] == 1
        assert metrics["hp.agent.cpu.count"] == 2
        assert metrics["hp.agent.disk.total_gb"] == 50
        assert "hp.agent.memory.total_gb" not in metrics
        assert "hp.agent.load.1m" not in metrics

    def test_missing_sections_default_to_empty(self):
        sender = ZabbixSender(ZabbixSenderConfig(hostname="h1"))
        metrics = sender.system_info_to_metrics({})
        assert metrics["hp.agent.status"] == 1
        assert metrics["hp.agent.cpu.count"] == 0
        assert "hp.agent.disk.total_gb" not in metrics


class TestSend:
    @pytest.mark.asyncio
    async def test_empty_items_returns_zero_counts(self):
        sender = ZabbixSender(ZabbixSenderConfig())
        result = await sender.send([])
        assert result == {"processed": 0, "failed": 0, "total": 0}

    @pytest.mark.asyncio
    async def test_connection_error_returns_failed(self):
        sender = ZabbixSender(ZabbixSenderConfig(server="10.0.0.1", port=12345))
        items = [ZabbixItem(host="h", key="k", value=1)]
        with patch(
            "hp_agent.zabbix_sender.asyncio.open_connection",
            side_effect=ConnectionRefusedError("Connection refused"),
        ):
            result = await sender.send(items)
        assert result == {"processed": 0, "failed": 1, "total": 1}

    @pytest.mark.asyncio
    async def test_oserror_returns_failed(self):
        sender = ZabbixSender(ZabbixSenderConfig(server="x", port=1))
        items = [ZabbixItem(host="h", key="k", value=1)]
        with patch(
            "hp_agent.zabbix_sender.asyncio.open_connection",
            side_effect=OSError("Network unreachable"),
        ):
            result = await sender.send(items)
        assert result == {"processed": 0, "failed": 1, "total": 1}

    @pytest.mark.asyncio
    async def test_successful_send(self):
        sender = ZabbixSender(ZabbixSenderConfig())
        items = [ZabbixItem(host="h", key="k", value=1)]
        response_body = {"response": "success", "info": "processed: 1"}
        body_bytes = json.dumps(response_body).encode("utf-8")
        datalen = len(body_bytes)
        header = ZABBIX_PROTOCOL + struct.pack("<B", ZABBIX_FLAGS)
        header += struct.pack("<II", datalen, 0)
        response_data = header + body_bytes

        mock_reader = MagicMock()
        mock_reader.read = AsyncMock(return_value=response_data)
        mock_writer = MagicMock()
        mock_writer.write = MagicMock()
        mock_writer.drain = AsyncMock()
        mock_writer.close = MagicMock()
        mock_writer.wait_closed = AsyncMock()

        with patch(
            "hp_agent.zabbix_sender.asyncio.open_connection",
            return_value=(mock_reader, mock_writer),
        ):
            result = await sender.send(items)
        assert result["response"] == "success"
        mock_writer.write.assert_called_once()
        mock_writer.close.assert_called_once()
        mock_writer.wait_closed.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_send_exception_inside_writer_returns_failed(self):
        sender = ZabbixSender(ZabbixSenderConfig())
        items = [ZabbixItem(host="h", key="k", value=1)]
        mock_reader = MagicMock()
        mock_writer = MagicMock()
        mock_writer.write = MagicMock()
        mock_writer.drain = AsyncMock(side_effect=BrokenPipeError("broken"))
        mock_writer.close = MagicMock()
        mock_writer.wait_closed = AsyncMock()

        with patch(
            "hp_agent.zabbix_sender.asyncio.open_connection",
            return_value=(mock_reader, mock_writer),
        ):
            result = await sender.send(items)
        assert result == {"processed": 0, "failed": 1, "total": 1}


class TestSendMetrics:
    @pytest.mark.asyncio
    async def test_send_multiple_items(self):
        sender = ZabbixSender(ZabbixSenderConfig(hostname="host01"))
        sender.send = AsyncMock(return_value={"response": "success"})
        metrics = {"key1": 1, "key2": "two", "key3": 3.0}
        with patch("hp_agent.zabbix_sender.time.time", return_value=555):
            result = await sender.send_metrics(metrics)
        assert result == {"response": "success"}
        sent_items = sender.send.call_args[0][0]
        assert len(sent_items) == 3
        keys = {item.key for item in sent_items}
        assert keys == {"key1", "key2", "key3"}
        for item in sent_items:
            assert item.host == "host01"
            assert item.clock == 555

    @pytest.mark.asyncio
    async def test_send_value(self):
        sender = ZabbixSender(ZabbixSenderConfig(hostname="host01"))
        sender.send = AsyncMock(return_value={"response": "success"})
        with patch("hp_agent.zabbix_sender.time.time", return_value=777):
            result = await sender.send_value("custom.key", 42)
        assert result == {"response": "success"}
        sent_items = sender.send.call_args[0][0]
        assert len(sent_items) == 1
        assert sent_items[0].host == "host01"
        assert sent_items[0].key == "custom.key"
        assert sent_items[0].value == 42
        assert sent_items[0].clock == 777
