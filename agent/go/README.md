# hp-agent (Go)

The HomePilot managed-host agent — a Go rewrite of the original Python
`agent/hp_agent/`. Connects outbound to the HomePilot **agent hub** over a
persistent TCP connection and serves `exec` / `read-file` / `write-file` /
`zabbix-push` requests behind command + path allowlists.

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

## Configure (env, prefix `HP_AGENT_` / `HP_ZABBIX_`)

| Variable | Default | Description |
|---|---|---|
| `HP_AGENT_HUB_HOST` | `localhost` | Hub host |
| `HP_AGENT_HUB_PORT` | `8443` | Hub port |
| `HP_AGENT_AUTH_TOKEN` | — | Shared/bootstrap enrollment token |
| `HP_AGENT_PRIVILEGED` | `false` | Allow docker/systemctl/apt/file management commands |
| `HP_AGENT_HEARTBEAT_INTERVAL` | `30` | Heartbeat seconds |
| `HP_AGENT_TLS` / `_TLS_CA` / `_TLS_CERT` / `_TLS_KEY` | off | TLS to the hub |
| `HP_ZABBIX_ENABLED` / `_SERVER` / `_PORT` / `_HOSTNAME` / `_SEND_INTERVAL` | off | Zabbix trapper push |

## Run / systemd

```bash
sudo install -m 0755 hp-agent-amd64 /usr/local/bin/hp-agent
# unit (agent/hp-agent.service) sets the Environment= lines, then:
sudo systemctl enable --now hp-agent
```

Replace a running binary atomically: `scp` to `/usr/local/bin/hp-agent.new`,
`mv -f` over `/usr/local/bin/hp-agent`, `systemctl restart hp-agent`.

## Protocol

Wire-compatible with the hub and the Python agent: 4-byte big-endian length
prefix + JSON. Register handshake (`auth_token`), then the hub sends
`exec`/`read_file`/`write_file`/`zabbix_push` and the agent replies
`command_result` (matched by `request_id`); the agent sends periodic
`heartbeat`s. Connect uses capped exponential backoff (no crash on an
unreachable hub).
