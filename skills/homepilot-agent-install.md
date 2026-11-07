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

1. **HomePilot backend running.** The Agent Hub is on by default
   (`HP_AGENT_HUB_ENABLED=true`, port `HP_AGENT_HUB_PORT=8443`), generates its own
   shared token and TLS certificate on first start, and needs no configuration.
2. **Host can reach HomePilot** on the hub port (8443, raw TCP - not HTTP) and on
   the API port (8000) to download the installer and binary. Outbound only; no
   inbound ports on the managed host.
3. **Root/sudo access** on the managed host, and **systemd**.

## Installation

`hp-agent` has no subcommands (only `--version`). It is configured entirely by
`/etc/homepilot/agent.env` and run by the `hp-agent` systemd unit, and both are
written by `scripts/install-agent.sh`. Never hand-write either: the unit and the
env file must agree on the privilege grant, and the agent refuses to start when
they do not (#422).

### Proxmox QEMU guest (zero-touch, preferred)

Host page -> **Install agent**. HomePilot mints a bootstrap token, stages it in
the guest over qemu-guest-agent, runs the installer from the control plane, and
the task succeeds only once the agent has connected.

### Any other Linux host: the one-liner

Hosts page -> **Enroll agent** -> **Generate Bootstrap Token**, copy the
one-liner, run it as root on the host. It looks like:

```bash
( f="$(mktemp)" && trap 'rm -f "$f"' EXIT && \
  curl -fsSL -H 'x-hp-agent-token: <bootstrap-token>' http://<homepilot-host>:8000/agents/dist/install-agent.sh -o "$f" && \
  bash "$f" --hp-api http://<homepilot-host>:8000 --hub <homepilot-host>:8443 \
      --token <bootstrap-token> --tls-pin sha256:<fingerprint> ) \
  || { echo 'HomePilot agent install FAILED - see the error above' >&2; false; }
```

It downloads the installer to a file and runs it, rather than piping `curl` into
`bash`: in a pipe a refused download ran bash on nothing and reported success.
Any failed step ends the command non-zero with that last line.

- The installer AND the binary come from the control plane (`--hp-api`), so the
  agent is the control plane's own version and the host needs no internet.
- A bootstrap token is single-use, expires in 24 h, and enrols a new host with
  or without an enrolment window. On first connect the agent trades it for its
  own per-agent credential (`/etc/homepilot/agent.token`).
- The CLI equivalent of the button is `hp agent bootstrap`.
- Grant flags: `--privileged`, `--allow-package-install`, `--write-prefix P`.
- Fallback only when the control plane cannot serve the payload (a source
  checkout with no built binaries): the UI then shows a GitHub one-liner pinned
  to the control plane's version (`--version vX.Y.Z`) and says why.

### Verify connection

On the **Hosts** page the host shows a connected agent. A host that is being
refused shows under **Waiting to join** with the reason; one refused only because
no enrolment window is open (it used the shared token) joins on its next retry
after **Open enrolment window**.

Or via CLI:
```bash
# On the HomePilot host
docker compose exec backend hp agent list
```

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| One-liner fails with `curl: (22) ... 401` | Token spent, expired, or mistyped | Generate a fresh bootstrap token and re-run |
| Agent log: `connect attempt N failed (dial tcp ...)` | Hub port blocked, or the hub address does not resolve from the host | Open 8443 on the HomePilot host; check `HP_AGENT_HUB_HOST` in `/etc/homepilot/agent.env` |
| Agent log: `not accepting new hosts right now` | Shared token, new hostname, no enrolment window | Hosts page -> **Waiting to join** -> **Open enrolment window**; the next retry joins |
| Agent log: `invalid auth_token` | Token matches nothing (5 in a row = 60 s ban) | Re-run the installer with a fresh bootstrap token |
| Agent not in UI list | Check the host's log | `journalctl -u hp-agent -n 50` on the host; refusals also show under **Waiting to join** |
| Agent exits with `FATAL: HP_AGENT_PRIVILEGED is set but the agent is running as euid N` | Privileged grant on a non-root unit (the pre-#422 install) | Re-run `scripts/install-agent.sh --hub ... --token ... --privileged` to regenerate a root unit, or remove `HP_AGENT_PRIVILEGED` from `/etc/homepilot/agent.env` |
| Agent exits naming write prefixes that are "NOT WRITABLE" | Unit's `ReadWritePaths` does not cover `HP_AGENT_WRITE_PREFIXES` | Re-run the installer with the flags you want, or narrow `HP_AGENT_WRITE_PREFIXES` |
| `install_package` fails with "not granted on this host" | Package management is a separate opt-in | Re-run the installer with `--allow-package-install` |

## Key Files

| File | Purpose |
|---|---|
| `/etc/homepilot/agent.env` | Hub host/port, TLS pin, privilege grant, write prefixes (written by the installer) |
| `/etc/homepilot/agent.id` | The agent's stable id |
| `/etc/homepilot/agent.token` | Its per-agent credential (minted by the hub on first register) |
| `/usr/local/bin/hp-agent` | Binary location (default) |
| `/etc/systemd/system/hp-agent.service` | Systemd unit (rendered from the same grant as `agent.env`) |

## Architecture Notes

- **Agent connects outbound** to HomePilot on TCP 8443 — no inbound ports on managed host
- **Protocol**: Length-prefixed JSON over TCP, encrypted with TLS
- **Authentication**: a bootstrap token or the shared token enrols once; from then on a per-agent credential the hub minted
- **Authorization**: Command allowlist — safe commands always allowed, privileged commands require `HP_AGENT_PRIVILEGED=true` **and** a root systemd unit (`install-agent.sh --privileged`); apt/`install_package` require `--allow-package-install` on top. The unit's `ReadWritePaths` must equal `HP_AGENT_WRITE_PREFIXES`, and the agent refuses to start when it does not (#422)