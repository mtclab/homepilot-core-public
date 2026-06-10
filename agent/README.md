# hp-agent

Lightweight agent daemon for HomePilot managed hosts.

Connects outbound to the HomePilot agent hub via persistent TCP, eliminating the need for inbound SSH access on managed hosts.

## Install

### Binary (recommended for production)

Download or build the standalone binary — no Python needed on the target host:

```bash
# Build from source
cd agent/
pip install pyinstaller
pyinstaller hp-agent.spec
# Output: dist/hp-agent

# Deploy to managed host
scp dist/hp-agent target:/usr/local/bin/
ssh target 'chmod +x /usr/local/bin/hp-agent'
```

### Pip (for development)

```bash
pip install "git+https://github.com/mtclab/homepilot-core-public.git#subdirectory=agent"
# or, from a checkout of this repo:
pip install ./agent
```

## Configure

Environment variables (prefix `HP_AGENT_`):

| Variable | Required | Default | Description |
|---|---|---|---|
| `HP_AGENT_HUB_HOST` | yes | — | Hub server hostname |
| `HP_AGENT_HUB_PORT` | no | `8443` | Hub server port |
| `HP_AGENT_AUTH_TOKEN` | yes | — | Persistent token or one-time bootstrap token |
| `HP_AGENT_PRIVILEGED` | no | `false` | Enable docker/systemctl/apt commands |
| `HP_AGENT_TLS` | no | `false` | Enable TLS |
| `HP_AGENT_TLS_CA` | no | — | CA cert for TLS |
| `HP_AGENT_TLS_CERT` | no | — | Client cert for mTLS |
| `HP_AGENT_TLS_KEY` | no | — | Client key for mTLS |
| `HP_ZABBIX_ENABLED` | no | `false` | Enable Zabbix trapper push |
| `HP_ZABBIX_SERVER` | no | `localhost` | Zabbix server IP |
| `HP_ZABBIX_PORT` | no | `10051` | Zabbix trapper port |
| `HP_ZABBIX_HOSTNAME` | no | system hostname | Zabbix host name |
| `HP_ZABBIX_SEND_INTERVAL` | no | `60` | Push interval in seconds |

## Run

```bash
export HP_AGENT_HUB_HOST=homelab.local
export HP_AGENT_AUTH_TOKEN=hp_xxxx...
hp-agent
```

## Systemd

```bash
sudo cp hp-agent.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hp-agent
```

Edit the `Environment=` lines in the service file for your deployment.

## Bootstrap token

Instead of sharing the persistent hub auth token, generate a one-time bootstrap token:

```bash
hp agent bootstrap    # on the HomePilot server
```

Set the returned `hpbat_*` token as `HP_AGENT_AUTH_TOKEN` on the agent. It is consumed on first connection and cannot be reused.

## Security

- **Command allowlist**: Only whitelisted commands can execute. Safe commands (ls, cat, ps, etc.) always work. Privileged commands (docker, systemctl, apt-get) require `HP_AGENT_PRIVILEGED=true`.
- **File read prefixes**: `/var/log/`, `/etc/`, `/opt/homepilot/`, `/proc/`, `/sys/`, `/tmp/homepilot/`, `/home/`, `/usr/local/bin/`
- **File write prefixes**: `/etc/homepilot/`, `/opt/homepilot/`, `/tmp/homepilot/`, `/etc/systemd/system/`, `/etc/docker/`, `/etc/nginx/`, `/etc/zabbix/`
- **TLS**: Optional encryption for hub connection. mTLS supported.
- **No inbound ports**: Agent connects outbound only.

## Zabbix integration

When enabled, the agent pushes 9 system metrics to your Zabbix server via the native sender protocol (TCP 10051). Create **Zabbix trapper** items on the target host with these keys:

`hp.agent.status`, `hp.agent.cpu.count`, `hp.agent.disk.total_gb`, `hp.agent.disk.free_gb`, `hp.agent.memory.total_gb`, `hp.agent.memory.free_gb`, `hp.agent.load.1m`, `hp.agent.load.5m`, `hp.agent.load.15m`

A template JSON is at `monitoring/zabbix/template_hp_agent.json`.

## Protocol

Length-prefixed JSON over TCP (same as HomePilot jumpserver relay):

```
[4 bytes: big-endian length N][N bytes: UTF-8 JSON]
```

Zero-protocol-migration — `AgentAdapter` switches between agent hub and SSH relay without changing the wire format.