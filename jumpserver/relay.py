import asyncio
import hmac
import json
import logging
import os
import signal
import ssl
import struct
from datetime import UTC, datetime

logger = logging.getLogger("relay")

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 50051
HEADER_LEN = 4


def _encode(msg: dict) -> bytes:
    payload = json.dumps(msg).encode("utf-8")
    return struct.pack("!I", len(payload)) + payload


async def _read_exact(reader: asyncio.StreamReader, n: int) -> bytes:
    data = b""
    while len(data) < n:
        chunk = await reader.read(n - len(data))
        if not chunk:
            raise ConnectionError("client disconnected")
        data += chunk
    return data


MAX_MESSAGE_SIZE = 1_048_576


def _is_safe_path(path: str) -> bool:
    if not path.startswith("/"):
        return False
    parts = path.split("/")
    return ".." not in parts


async def _read_message(reader: asyncio.StreamReader) -> dict:
    hdr = await _read_exact(reader, HEADER_LEN)
    length = struct.unpack("!I", hdr)[0]
    if length > MAX_MESSAGE_SIZE:
        raise ConnectionError(f"message too large: {length} bytes")
    body = await _read_exact(reader, length)
    return json.loads(body)


class AuditLog:
    def __init__(self, log_dir: str = "/var/log/jumpserver") -> None:
        self.log_dir = log_dir
        os.makedirs(log_dir, exist_ok=True)

    async def record(self, host: str, command: str, exit_code: int, duration_ms: int) -> None:
        ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        entry = f"{ts} host={host} cmd={command!r} exit={exit_code} duration_ms={duration_ms}\n"
        log_path = os.path.join(
            self.log_dir,
            datetime.now(UTC).strftime("%Y-%m-%d") + ".log",
        )
        await asyncio.to_thread(self._write, log_path, entry)

    @staticmethod
    def _write(path: str, entry: str) -> None:
        with open(path, "a") as f:
            f.write(entry)


class RelayServer:
    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        auth_token: str | None = None,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.auth_token = auth_token or os.environ.get("JUMPSERVER_AUTH_TOKEN", "")
        self._ssl_context = ssl_context
        self._audit = AuditLog(log_dir=os.environ.get("JUMPSERVER_LOG_DIR", "/var/log/jumpserver"))
        self._ssh_key_path = os.environ.get("SSH_KEY_PATH", "/root/.ssh/id_ed25519")
        self._known_hosts_path = os.environ.get(
            "JUMPSERVER_KNOWN_HOSTS_FILE", "/etc/ssh/ssh_known_hosts"
        )
        self._known_hosts = self._load_known_hosts()
        self._pve_nodes: list[str] = [
            n.strip() for n in os.environ.get("PVE_NODE_NAMES", "").split(",") if n.strip()
        ]
        self._server: asyncio.Server | None = None

    def _load_known_hosts(self):
        import asyncssh

        kh_path = self._known_hosts_path
        if os.path.isfile(kh_path):
            try:
                return asyncssh.load_known_hosts(kh_path)
            except Exception:
                logger.warning(
                    "Failed to load known_hosts from %s — proceeding without host verification",
                    kh_path,
                )
        else:
            logger.warning(
                "known_hosts file %s not found — "
                "first-time connections will proceed without host verification",
                kh_path,
            )
        return None

    def _verify_auth(self, request: dict) -> bool:
        if not self.auth_token:
            return False
        token = request.get("auth_token", "")
        return hmac.compare_digest(token, self.auth_token)

    def _reject_guest_only(self, host: str) -> str | None:
        host_lower = host.lower().strip()
        for node in self._pve_nodes:
            if host_lower == node.lower().strip():
                return f"SSH to PVE node '{host}' forbidden — use Proxmox API"
        return None

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = writer.get_extra_info("peername")
        logger.info("connection from %s (tls=%s)", peer, bool(self._ssl_context))
        try:
            while True:
                try:
                    request = await asyncio.wait_for(_read_message(reader), timeout=300)
                except (TimeoutError, ConnectionError):
                    break

                request_id = request.get("request_id", "")

                if not self._verify_auth(request):
                    await self._write_response(
                        writer,
                        {"error": "invalid auth_token", "request_id": request_id},
                    )
                    continue

                action = request.get("action", "")
                if action == "exec":
                    await self._handle_exec(request, writer)
                elif action == "read_file":
                    await self._handle_read_file(request, writer)
                elif action == "write_file":
                    await self._handle_write_file(request, writer)
                elif action == "ping":
                    await self._write_response(writer, {"action": "pong", "request_id": request_id})
                elif action == "check_connection":
                    await self._handle_check_connection(request, writer)
                else:
                    await self._write_response(
                        writer,
                        {"error": f"unknown action: {action}", "request_id": request_id},
                    )
        finally:
            writer.close()
            await writer.wait_closed()
            logger.info("disconnected %s", peer)

    async def _handle_exec(self, request: dict, writer: asyncio.StreamWriter) -> None:
        host = request.get("host", "")
        command = request.get("command", "")
        timeout = request.get("timeout", 30)
        request_id = request.get("request_id", "")

        guest_reject = self._reject_guest_only(host)
        if guest_reject:
            await self._write_response(
                writer,
                {"error": guest_reject, "exit_code": -1, "request_id": request_id},
            )
            return

        if not host or not command:
            await self._write_response(
                writer,
                {
                    "error": "host and command required",
                    "exit_code": -1,
                    "request_id": request_id,
                },
            )
            return

        start = datetime.now(UTC)
        try:
            result = await asyncio.wait_for(
                self._run_ssh(host, "root", command),
                timeout=timeout,
            )
        except TimeoutError:
            duration = int((datetime.now(UTC) - start).total_seconds() * 1000)
            await self._audit.record(host, command, -1, duration)
            await self._write_response(
                writer,
                {
                    "exit_code": -1,
                    "stdout": "",
                    "stderr": f"command timed out after {timeout}s",
                    "request_id": request_id,
                },
            )
            return
        except Exception as exc:
            duration = int((datetime.now(UTC) - start).total_seconds() * 1000)
            await self._audit.record(host, command, -1, duration)
            await self._write_response(
                writer,
                {
                    "exit_code": -1,
                    "stdout": "",
                    "stderr": str(exc),
                    "request_id": request_id,
                },
            )
            return

        duration = int((datetime.now(UTC) - start).total_seconds() * 1000)
        exit_code = result.exit_status if result.exit_status is not None else -1
        await self._audit.record(host, command, exit_code, duration)
        await self._write_response(
            writer,
            {
                "exit_code": exit_code,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "request_id": request_id,
            },
        )

    async def _handle_read_file(self, request: dict, writer: asyncio.StreamWriter) -> None:
        host = request.get("host", "")
        path = request.get("path", "")
        request_id = request.get("request_id", "")

        guest_reject = self._reject_guest_only(host)
        if guest_reject:
            await self._write_response(writer, {"error": guest_reject, "request_id": request_id})
            return

        if not host or not path or not _is_safe_path(path):
            await self._write_response(
                writer, {"error": "host and safe absolute path required", "request_id": request_id}
            )
            return

        try:
            content = await self._sftp_read(host, path)
            await self._write_response(writer, {"content": content, "request_id": request_id})
        except Exception as exc:
            await self._write_response(writer, {"error": str(exc), "request_id": request_id})

    async def _handle_write_file(self, request: dict, writer: asyncio.StreamWriter) -> None:
        host = request.get("host", "")
        path = request.get("path", "")
        content = request.get("content", "")
        request_id = request.get("request_id", "")

        guest_reject = self._reject_guest_only(host)
        if guest_reject:
            await self._write_response(writer, {"error": guest_reject, "request_id": request_id})
            return

        if not host or not path or not _is_safe_path(path):
            await self._write_response(
                writer, {"error": "host and safe absolute path required", "request_id": request_id}
            )
            return

        try:
            await self._sftp_write(host, path, content)
            await self._write_response(writer, {"status": "ok", "request_id": request_id})
        except Exception as exc:
            await self._write_response(writer, {"error": str(exc), "request_id": request_id})

    async def _handle_check_connection(self, request: dict, writer: asyncio.StreamWriter) -> None:
        host = request.get("host", "")
        port = request.get("port", 22)
        request_id = request.get("request_id", "")

        try:
            async with await asyncio.wait_for(
                self._connect_ssh(host, port, "root"),
                timeout=10,
            ):
                pass
            await self._write_response(writer, {"reachable": True, "request_id": request_id})
        except Exception as exc:
            await self._write_response(
                writer, {"reachable": False, "error": str(exc), "request_id": request_id}
            )

    async def _write_response(self, writer: asyncio.StreamWriter, msg: dict) -> None:
        writer.write(_encode(msg))
        await writer.drain()

    async def _connect_ssh(self, host: str, port: int = 22, user: str = "root"):
        import asyncssh

        return await asyncssh.connect(
            host,
            port=port,
            username=user,
            client_keys=[self._ssh_key_path],
            known_hosts=self._known_hosts,
        )

    async def _run_ssh(self, host: str, user: str, command: str):
        import asyncssh

        async with await asyncssh.connect(
            host,
            username=user,
            client_keys=[self._ssh_key_path],
            known_hosts=self._known_hosts,
        ) as conn:
            return await conn.run(command)

    async def _sftp_read(self, host: str, path: str) -> str:
        import asyncssh

        async with (
            await asyncssh.connect(
                host,
                username="root",
                client_keys=[self._ssh_key_path],
                known_hosts=self._known_hosts,
            ) as conn,
            conn.start_sftp_client() as sftp,
            sftp.open(path, "r") as f,
        ):
            return await f.read()

    async def _sftp_write(self, host: str, path: str, content: str) -> None:
        import asyncssh

        async with (
            await asyncssh.connect(
                host,
                username="root",
                client_keys=[self._ssh_key_path],
                known_hosts=self._known_hosts,
            ) as conn,
            conn.start_sftp_client() as sftp,
            sftp.open(path, "w") as f,
        ):
            await f.write(content)

    async def start(self) -> None:
        if self._ssl_context:
            self._server = await asyncio.start_server(
                self._handle_client,
                self.host,
                self.port,
                ssl=self._ssl_context,
            )
        else:
            self._server = await asyncio.start_server(self._handle_client, self.host, self.port)
        tls_label = " (TLS)" if self._ssl_context else ""
        logger.info("relay listening on %s:%s%s", self.host, self.port, tls_label)

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
            logger.info("relay stopped")


def build_ssl_context() -> ssl.SSLContext:
    cert_path = os.environ.get("JUMPSERVER_TLS_CERT", "")
    key_path = os.environ.get("JUMPSERVER_TLS_KEY", "")
    ca_path = os.environ.get("JUMPSERVER_TLS_CA", "")

    if not cert_path or not key_path:
        raise RuntimeError("JUMPSERVER_TLS_CERT and JUMPSERVER_TLS_KEY required for mTLS")

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert_path, key_path)
    if ca_path:
        ctx.load_verify_locations(ca_path)
        ctx.verify_mode = ssl.CERT_REQUIRED
    else:
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    use_tls = os.environ.get("JUMPSERVER_TLS", "").lower() in ("1", "true", "yes")
    ssl_ctx = build_ssl_context() if use_tls else None

    server = RelayServer(
        host=os.environ.get("RELAY_HOST", DEFAULT_HOST),
        port=int(os.environ.get("RELAY_PORT", str(DEFAULT_PORT))),
        ssl_context=ssl_ctx,
    )
    asyncio.run(server.serve_forever())
