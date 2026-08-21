# hp-agent (Go)

The HomePilot managed-host agent — a Go rewrite of the original Python
`agent/hp_agent/`. Connects outbound to the HomePilot **agent hub** over a
persistent TCP connection, serves `exec` / `read-file` / `write-file` requests
behind command + path allowlists, and reports system metrics on an interval.

Why Go: a single **static** binary (`CGO_ENABLED=0`, no glibc/libc dependency),
trivial cross-compilation to amd64 + arm64, instant startup, ~4.5 MB. Stdlib
only — no third-party modules.

## Build

```bash
cd agent/go
CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -ldflags="-s -w" -o hp-agent-amd64 .
CGO_ENABLED=0 GOOS=linux GOARCH=arm64 go build -ldflags="-s -w" -o hp-agent-arm64 .
go vet ./... && go test ./...
```

## Configure (env, prefix `HP_AGENT_`)

| Variable | Default | Description |
|---|---|---|
| `HP_AGENT_HUB_HOST` | `localhost` | Hub host |
| `HP_AGENT_HUB_PORT` | `8443` | Hub port |
| `HP_AGENT_AUTH_TOKEN` | — | Shared/bootstrap enrollment token |
| `HP_AGENT_TOKEN_FILE` | — | Where the durable per-agent credential handed back at enrollment is persisted |
| `HP_AGENT_ID` | persisted id | Explicit agent identity; wins over the id file |
| `HP_AGENT_ID_FILE` | `agent.id` beside `HP_AGENT_TOKEN_FILE`, else `/etc/homepilot/agent.id` | Where a generated agent id is persisted. The identity MUST be stable: the hub's per-agent credential is bound to it, so a new id per start would orphan the stored token |
| `HP_AGENT_PRIVILEGED` | `false` | Allow docker/systemctl/file-management commands + the provisioning actions. Requires a root unit (see below) |
| `HP_AGENT_ALLOW_PACKAGE_INSTALL` | `false` | Additionally allow apt/apt-get and `install_package` |
| `HP_AGENT_WRITE_PREFIXES` | `/etc/homepilot/:/opt/homepilot/:/tmp/homepilot/:/etc/systemd/system/:/etc/docker/:/etc/nginx/` | Colon-separated write allowlist. Must match the unit's `ReadWritePaths` |
| `HP_AGENT_HEARTBEAT_INTERVAL` | `30` | Heartbeat seconds |
| `HP_AGENT_TLS` / `_TLS_CA` / `_TLS_CERT` / `_TLS_KEY` | off | TLS to the hub |
| `HP_AGENT_TLS_PIN` | — | `sha256:<hex>` of the hub certificate. The hub's certificate is self-signed, so this pin is what "verify the hub" means: the presented certificate must match byte-for-byte, and `HP_AGENT_TLS_INSECURE` cannot bypass it |
| `HP_AGENT_METRICS_ENABLED` | `true` | Report system metrics to the hub; only an explicit false turns it off |
| `HP_AGENT_METRICS_INTERVAL` | `60` | Seconds between samples |
| `HP_AGENT_METRICS_BUFFER` | `1440` | Samples held while the hub is unreachable. Past the bound the OLDEST are dropped and the drop is logged |

## Run / systemd

```bash
sudo install -m 0755 hp-agent-amd64 /usr/local/bin/hp-agent
# scripts/install-agent.sh writes /etc/homepilot/agent.env AND the matching unit
sudo scripts/install-agent.sh --hub HOST:PORT --token TOKEN [--privileged]
```

### Privileged mode is a unit-shape decision (#422)

`HP_AGENT_PRIVILEGED` only says what the agent may be ASKED to do. Whether it
CAN do it is decided by the unit:

* `--privileged` installs `User=root` with `ProtectSystem=strict` and
  `ReadWritePaths` set to exactly `HP_AGENT_WRITE_PREFIXES` — the mount namespace
  is what confines the path-unconstrained `mkdir`/`chmod`/`cp`/`mv` and
  `bash /opt/homepilot/*.sh` entries.
* `--allow-package-install` additionally sets `ProtectSystem=no`, because dpkg
  unpacks into `/usr`, `/etc` and `/var`. Without it, apt is refused by the
  allowlist with a message naming the flag.
* `sudo` is not allowlisted in any mode: root units have nothing to escalate to,
  and unprivileged units run under `NoNewPrivileges=yes`.

At startup the agent prints a self-check (mode, euid, one line per write prefix)
and, in privileged mode, exits non-zero if it is not root or a configured prefix
is not writable. `agent/go/unit_matrix_test.go` gates the unit the installer
generates against `privilegedCommands` and `defaultWritePrefixes`.

Replace a running binary atomically: `scp` to `/usr/local/bin/hp-agent.new`,
`mv -f` over `/usr/local/bin/hp-agent`, `systemctl restart hp-agent`.

## Protocol

Wire-compatible with the hub and the Python agent: 4-byte big-endian length
prefix + JSON. Register handshake (`auth_token`), then the hub sends
`exec`/`read_file`/`write_file` and the agent replies `command_result` (matched
by `request_id`); the agent sends periodic `heartbeat` and `metrics` frames, and
drops a metric batch from its buffer only once the hub's `metrics_ack` arrives. Connect uses capped exponential backoff (no crash on an
unreachable hub).
