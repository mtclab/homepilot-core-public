from __future__ import annotations

import os
import ssl
from dataclasses import dataclass


@dataclass
class AgentConfig:
    hub_host: str = "localhost"
    hub_port: int = 8443
    auth_token: str = ""
    agent_id: str = ""
    heartbeat_interval: int = 30
    log_level: str = "INFO"
    tls: bool = False
    tls_ca: str = ""
    tls_cert: str = ""
    tls_key: str = ""
    privileged: bool = False
    zabbix_enabled: bool = False
    zabbix_server: str = "localhost"
    zabbix_port: int = 10051
    zabbix_hostname: str = ""
    zabbix_send_interval: int = 60

    @classmethod
    def from_env(cls) -> AgentConfig:
        return cls(
            hub_host=os.environ.get("HP_AGENT_HUB_HOST", "localhost"),
            hub_port=int(os.environ.get("HP_AGENT_HUB_PORT", "8443")),
            auth_token=os.environ.get("HP_AGENT_AUTH_TOKEN", ""),
            agent_id=os.environ.get("HP_AGENT_ID", ""),
            heartbeat_interval=int(os.environ.get("HP_AGENT_HEARTBEAT_INTERVAL", "30")),
            log_level=os.environ.get("HP_AGENT_LOG_LEVEL", "INFO"),
            tls=os.environ.get("HP_AGENT_TLS", "").lower() in ("1", "true", "yes"),
            tls_ca=os.environ.get("HP_AGENT_TLS_CA", ""),
            tls_cert=os.environ.get("HP_AGENT_TLS_CERT", ""),
            tls_key=os.environ.get("HP_AGENT_TLS_KEY", ""),
            privileged=os.environ.get("HP_AGENT_PRIVILEGED", "").lower() in ("1", "true", "yes"),
            zabbix_enabled=os.environ.get("HP_ZABBIX_ENABLED", "").lower() in ("1", "true", "yes"),
            zabbix_server=os.environ.get("HP_ZABBIX_SERVER", "localhost"),
            zabbix_port=int(os.environ.get("HP_ZABBIX_PORT", "10051")),
            zabbix_hostname=os.environ.get("HP_ZABBIX_HOSTNAME", ""),
            zabbix_send_interval=int(os.environ.get("HP_ZABBIX_SEND_INTERVAL", "60")),
        )

    def get_ssl_context(self) -> ssl.SSLContext | None:
        if not self.tls:
            return None
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        if self.tls_ca:
            ctx.load_verify_locations(self.tls_ca)
        else:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        if self.tls_cert and self.tls_key:
            ctx.load_cert_chain(certfile=self.tls_cert, keyfile=self.tls_key)
        return ctx
