# Spike: ansible over the hp-agent transport (#396)

**Status:** complete — feasibility answered. Gates Phase B of the manage-imported-hosts
epic (#397). Verified empirically with ansible-core 2.21 and a prototype
`connection: homepilot_agent` plugin that logged every transport primitive while
driving a real ping / command / copy playbook.

## Question

Epic #397 decided (under discussion) that in-guest provisioning would use **B =
ansible as the declarative layer over the hp-agent as transport, via a custom
connection plugin**, on the assumption that ansible maps cleanly onto the agent's
`exec` / `write_file` / `read_file`. This spike tests that assumption before Phase B
is built.

## What works

The ansible **connection contract is exactly three transport primitives**, and they
map 1:1 onto the agent:

| ansible `ConnectionBase` (required) | hp-agent |
|---|---|
| `exec_command(cmd, in_data, sudoable) -> (rc, stdout, stderr)` | `exec(host, command)` |
| `put_file(in_path, out_path)` | `write_file(host, path, content)` |
| `fetch_file(in_path, out_path)` | `read_file(host, path)` |
| `_connect` / `close` | no-op (the hub connection already exists) |

The prototype plugin loaded, ansible drove all three primitives, and modules ran
(ping ok, command changed, copy attempted). **Writing the plugin is trivial.**

## The blocker (decisive)

ansible does **not** issue clean, allowlistable commands. Every module invocation is
`/bin/sh -c '<arbitrary shell>'`, using shell metacharacters and a pushed python
payload. Captured from the real run:

```
/bin/sh -c 'echo ~'
/bin/sh -c '( umask 77 && mkdir -p "` echo ~/.ansible/tmp `" && mkdir "…" && echo …)'
/bin/sh -c 'chmod u+rwx …/AnsiballZ_ping.py …'
/bin/sh -c '<python> …/AnsiballZ_ping.py'          # runs a ~160KB pushed python module
/bin/sh -c 'rm -f -r …/ansible-tmp-… > /dev/null 2>&1'
```

The hp-agent allowlist's shell-metacharacter filter rejects **all** of these
(they contain `&`, backticks, `~`, `>`, `(`, `;`). The allowlist exists precisely to
forbid arbitrary shell. So:

- **ansible-on-agent requires a full target shell** — running it means the agent must
  execute arbitrary `/bin/sh -c` for that host, i.e. the allowlist is bypassed and the
  agent is trusted as root there.
- **python3 is required on every target** (`<python> AnsiballZ_*.py`). python-less
  minimal containers (Alpine LXC) can run only `raw`/`command` — no idempotency, so no
  real module ecosystem. That is the coverage floor.
- ansible's `remote_tmp` defaults to `~/.ansible/tmp` (writes + chmods + executes
  there); it is not under the agent write allowlist. Each `put_file` is ~160KB (within
  MAX_MESSAGE_SIZE 1MB, but note it).

## Impact on the mechanism decision (needs owner re-decision)

The **B** decision assumed a clean map; it maps at the *connection* layer, but
ansible's *execution* layer needs a shell the allowlist is built to deny. So B is only
viable as a **per-host "provisioning mode"** that lets a trusted agent run arbitrary
`/bin/sh -c`. Options:

1. **B with an explicit per-agent provisioning mode** — an opt-in flag (like the
   `HP_AGENT_PRIVILEGED` precedent, and the #388 `bash /opt/homepilot/*.sh` opt-in)
   that lets a trusted agent run a full shell so ansible works fully. Honest framing:
   any host you provision via ansible becomes a fully-trusted (root-shell) agent; the
   allowlist then only protects read-only / diagnostic agents. Provisioning runs
   arbitrary code by nature, so this may be acceptable — but it IS the security call.
2. **A = thin native agent actions** (`install_package` / `manage_service` /
   `write_config` as structured RPCs, no target shell or python) now looks like the
   better fit for the agent's containment model, **inverting the earlier B-over-A
   lean**. Smaller vocabulary, but it preserves the allowlist and needs nothing on the
   target.
3. **Hybrid** — A as the containment-preserving default for the common 80%; B
   (provisioning mode) only for explicitly-trusted hosts that need the full module
   ecosystem.

## Recommendation

Re-decide the mechanism before building Phase B. **Phase A (introspect-on-adopt,
read-only) is unaffected and remains the right first build** regardless of the
A-vs-B-vs-hybrid outcome. Whichever way the trust decision goes, the transport work is
small — the connection plugin is proven trivial, and native actions are thin wrappers
over the agent's existing exec/write primitives.

## Reproduction

ansible-core 2.21 in a venv; a `connection: homepilot_agent` plugin subclassing
`ansible.plugins.connection.ConnectionBase` implementing the three primitives (stand-in
that logs + runs locally); `ansible.cfg` pointing `connection_plugins` at it; a playbook
with `ping` + `command` + `copy` tasks. Run with `-vvv` (or a logging plugin) and observe
the `/bin/sh -c` command stream.
