---
name: homepilot-agent-install
description: Install the HomePilot agent (hp-agent) binary on managed hosts. Trigger on install agent, agent install, hp-agent, enroll agent, managed host, agent setup, agent deployment.
---

# HomePilot Agent Installation

Install and enroll the `hp-agent` binary on managed hosts so HomePilot can manage them via the Agent Hub.

## When to Use

- User says "install agent", "deploy agent", "enroll host", "set up managed host"
- User mentions `hp-agent`, Agent Hub, or managed host connectivity
- User wants to add a new host to HomePilot without SSH key management

## Prerequisites

1. **HomePilot backend running** with Agent Hub enabled:
   ```env
   HP_AGENT_HUB_ENABLED=true
   HP_AGENT_HUB_PORT=8443
   HP_AGENT_HUB_AUTH_TOKEN=<shared-secret>
   ```
2. **Host can reach HomePilot** on port 8443 (outbound TCP, no inbound ports needed)
3. **Root/sudo access** on the managed host

## Installation

### One-line install (recommended)

```bash
curl -fsSL https://raw.githubusercontent.com/mtclab/homepilot-core-public/main/scripts/install-agent.sh | bash
```

With auto-enroll:
```bash
HUB_URL=https://your-homepilot:8443 HUB_TOKEN=your-token \
bash <(curl -fsSL https://raw.githubusercontent.com/mtclab/homepilot-core-public/main/scripts/install-agent.sh)
```

### Manual install

```bash
# Download binary for your arch (amd64 or arm64)
curl -LO https://github.com/mtclab/homepilot-core-public/releases/latest/download/hp-agent-linux-amd64
chmod +x hp-agent-linux-amd64
sudo mv hp-agent-linux-amd64 /usr/local/bin/hp-agent

# Verify
hp-agent --version
```

### Enroll with Agent Hub

```bash
hp-agent enroll --hub https://your-homepilot:8443 --token <shared-secret>
```

This stores the hub config in `~/.config/hp-agent/config.json`.

### Start the agent

```bash
# Foreground (for testing)
hp-agent start

# Background with systemd (recommended)
hp-agent service install
sudo systemctl enable --now hp-agent
```

### Verify connection

In HomePilot web UI, go to **Agents** page. The host should appear as "connected" with a green indicator.

Or via CLI:
```bash
# On the HomePilot host
docker compose exec backend hp agent list
```

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| "Connection refused" on enroll | Hub not enabled or firewall | Check `HP_AGENT_HUB_ENABLED=true`, open port 8443 on HomePilot host |
| "Unauthorized" on enroll | Token mismatch | Verify `HP_AGENT_HUB_AUTH_TOKEN` matches the token passed to `--token` |
| Agent not in UI list | Enrollment incomplete | Run `hp-agent enroll` again, check logs with `hp-agent start --verbose` |
| Agent disconnects after reboot | No systemd service | Run `hp-agent service install` and `systemctl enable hp-agent` |
| Agent exits with `FATAL: HP_AGENT_PRIVILEGED is set but the agent is running as euid N` | Privileged grant on a non-root unit (the pre-#422 install) | Re-run `scripts/install-agent.sh --hub ... --token ... --privileged` to regenerate a root unit, or remove `HP_AGENT_PRIVILEGED` from `/etc/homepilot/agent.env` |
| Agent exits naming write prefixes that are "NOT WRITABLE" | Unit's `ReadWritePaths` does not cover `HP_AGENT_WRITE_PREFIXES` | Re-run the installer with the flags you want, or narrow `HP_AGENT_WRITE_PREFIXES` |
| `install_package` fails with "not granted on this host" | Package management is a separate opt-in | Re-run the installer with `--allow-package-install` |

## Key Files

| File | Purpose |
|---|---|
| `~/.config/hp-agent/config.json` | Hub URL + token (auto-created by `enroll`) |
| `~/.config/hp-agent/known_hubs` | Trusted hub fingerprints |
| `/usr/local/bin/hp-agent` | Binary location (default) |
| `/etc/systemd/system/hp-agent.service` | Systemd service file |

## Architecture Notes

- **Agent connects outbound** to HomePilot on TCP 8443 — no inbound ports on managed host
- **Protocol**: Length-prefixed JSON over TCP, encrypted with TLS
- **Authentication**: Shared secret (`HP_AGENT_HUB_AUTH_TOKEN`) set once at enrollment
- **Authorization**: Command allowlist — safe commands always allowed, privileged commands require `HP_AGENT_PRIVILEGED=true` **and** a root systemd unit (`install-agent.sh --privileged`); apt/`install_package` require `--allow-package-install` on top. The unit's `ReadWritePaths` must equal `HP_AGENT_WRITE_PREFIXES`, and the agent refuses to start when it does not (#422)