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
| `HP_AGENT_PRIVILEGED` | no | `false` | Enable docker/systemctl/file-management commands + provisioning. Needs a **root** unit; the agent refuses to start otherwise (#422) |
| `HP_AGENT_ALLOW_PACKAGE_INSTALL` | no | `false` | Additionally enable apt/apt-get and `install_package` |
| `HP_AGENT_WRITE_PREFIXES` | no | see Security | Colon-separated write allowlist; must match the unit's `ReadWritePaths` |
| `HP_AGENT_TLS` | no | `false` | Enable TLS |
| `HP_AGENT_TLS_CA` | no | — | CA cert for TLS |
| `HP_AGENT_TLS_CERT` | no | — | Client cert for mTLS |
| `HP_AGENT_TLS_KEY` | no | — | Client key for mTLS |
| `HP_AGENT_METRICS_ENABLED` | no | `true` | Report system metrics to the hub; only an explicit false turns it off |
| `HP_AGENT_METRICS_INTERVAL` | no | `60` | Seconds between samples |
| `HP_AGENT_METRICS_BUFFER` | no | `1440` | Samples held while the hub is unreachable; past the bound the oldest are dropped (logged) |

## Run

```bash
export HP_AGENT_HUB_HOST=homelab.local
export HP_AGENT_AUTH_TOKEN=hp_xxxx...
hp-agent
```

## Systemd

Use the installer — it writes `agent.env` and the unit from one grant decision,
so they cannot disagree:

```bash
sudo scripts/install-agent.sh --hub HOST:PORT --token TOKEN              # unprivileged
sudo scripts/install-agent.sh --hub HOST:PORT --token TOKEN --privileged # root unit
```

`hp-agent.service` in this directory is a reference copy of the **unprivileged**
unit. Do not set `HP_AGENT_PRIVILEGED=true` under it: a `User=hp-agent` unit with
`ProtectSystem=strict` cannot run any privileged command or write any system
config path, and the agent now detects that at startup and refuses to run (#422).

## Bootstrap token

Instead of sharing the persistent hub auth token, generate a one-time bootstrap token:

```bash
hp agent bootstrap    # on the HomePilot server
```

Set the returned `hpbat_*` token as `HP_AGENT_AUTH_TOKEN` on the agent. It is consumed on first connection and cannot be reused.

## Security

- **Command allowlist**: Only whitelisted commands can execute. Safe commands (ls, cat, ps, etc.) always work. Privileged commands (docker, systemctl, file management) require `HP_AGENT_PRIVILEGED=true`; apt/apt-get additionally require `HP_AGENT_ALLOW_PACKAGE_INSTALL=true`. `sudo` is not allowlisted in any mode.
- **File read prefixes**: `/var/log/`, `/etc/`, `/opt/homepilot/`, `/proc/`, `/sys/`, `/tmp/homepilot/`, `/home/`, `/usr/local/bin/`
- **File write prefixes** (`HP_AGENT_WRITE_PREFIXES`): the binary's built-in default is `/etc/homepilot/`, `/opt/homepilot/`, `/tmp/homepilot/`, `/etc/systemd/system/`, `/etc/docker/`, `/etc/nginx/`. `install-agent.sh` always pins the variable: a privileged install grants that full set, an unprivileged install grants only HomePilot's own three directories (the system config dirs need root, so a non-root agent could never write them).
- **Runtime self-check**: at startup the agent probes every configured write prefix. In privileged mode a non-root euid or an unwritable prefix is fatal — it exits non-zero naming the path and the fix, instead of accepting provisioning work it cannot perform.
- **TLS**: Optional encryption for hub connection. mTLS supported.
- **No inbound ports**: Agent connects outbound only.

## Metrics

The agent reports these over the hub connection every `HP_AGENT_METRICS_INTERVAL` seconds, with nothing to install or configure:

`cpu.count`, `disk.total_gb`, `disk.free_gb`, `memory.total_gb`, `memory.free_gb`, `load.1m`, `load.5m`, `load.15m`

While the hub is unreachable samples are buffered (bounded, oldest dropped first and logged) and flushed on reconnect; a batch leaves the buffer only once the hub acks it. HomePilot stores them for `HP_METRICS_RETENTION_DAYS` and serves them under `/monitoring`.

## Protocol

Length-prefixed JSON over TCP:

```
[4 bytes: big-endian length N][N bytes: UTF-8 JSON]
```

