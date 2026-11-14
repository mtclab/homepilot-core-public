# The `hp` CLI

`hp` runs in two places:

- **On the control-plane host** (the server itself, or `docker compose exec`
  into its container). It reaches the backend on loopback with the host's own
  admin credential, and some commands work on the host's files directly
  (`hp vault`, `hp db`, `hp export`). Nothing to set up.
- **On any other machine**, as a client of the server's API: an operator's
  workstation, a jump box, the machine an assistant runs on. This page is about
  that.

## Install

One command, at the release your server runs (`hp whoami` or the console footer
shows it):

```bash
uv tool install git+https://github.com/mtclab/homepilot-core-public@vX.Y.Z
```

`pipx install git+https://github.com/mtclab/homepilot-core-public@vX.Y.Z` works
the same way. Either puts `hp` on your PATH in an environment of its own. It
installs the whole `homepilot` package (the server's code is the CLI's code),
from the release tag on the public mirror; git must be installed.

## Log in

```bash
hp login --profile prod --url https://homepilot.example.com
API token (input hidden):
```

Paste an API token (Settings -> Tokens mints one; give it the scope you want
the CLI to have). It is read from the prompt without echo, or from stdin:

```bash
hp login --profile prod --url https://homepilot.example.com < ~/prod-token.txt
```

It is never taken from the command line, so it does not end up in shell history
or a process listing. `hp login` checks the token against the server first and
saves nothing if the server refuses it.

What it writes:

| File | Mode | Holds |
|---|---|---|
| `~/.config/homepilot/` | 0700 | the client configuration (`$XDG_CONFIG_HOME` is honoured) |
| `~/.config/homepilot/client.toml` | 0600 | the profiles: URL, token file, CA file. No token. |
| `~/.config/homepilot/tokens/prod.token` | 0600 | the token (`--token-file` puts it elsewhere) |

A token file that other users can read or write is refused, with the `chmod`
that fixes it.

**TLS** is always verified. A server with a self-signed certificate is trusted
by naming its CA: `hp login ... --ca-file ~/homepilot-ca.pem` (stored in the
profile as `ca_file`). There is no switch that turns verification off. Plain
`http://` is refused to anything but loopback, because the token would cross the
network in clear; `--allow-http` opts a profile in when you accept that.

## Profiles

```toml
default_profile = "prod"

[profiles.prod]
url = "https://homepilot.example.com"
token_file = "~/.config/homepilot/tokens/prod.token"

[profiles.dev]
url = "https://homepilot-dev.example.com"
token_file = "~/.config/homepilot/tokens/dev.token"
ca_file = "~/.config/homepilot/dev-ca.pem"
```

Which server a run talks to, first match wins:

1. `--url URL` or `HP_URL`, with the token from `HP_TOKEN` (for scripts and CI;
   nothing on disk needed), or from `--profile`'s token file;
2. `--profile NAME` / `-p NAME`, or `HP_PROFILE`;
3. `default_profile` (the first profile `hp login` saves; `--default` changes it);
4. none: local mode, the on-host behaviour.

An unknown profile is an error, never a quiet fallback to some other server.

```bash
hp whoami                 # server URL, server and CLI versions, token label and scope
hp -p dev whoami --json
```

## Keeping up with the server

The first call `hp` makes to a server in a run reads the server's version. When
the CLI is older, it prints one line on stderr:

```
hp 3.12.0 is older than the server (3.13.0): run `hp self-update`
```

It never blocks the command and never writes to stdout, so `--json` output is
unaffected.

```bash
hp self-update             # reinstall at the server's release
hp self-update --dry-run   # print the install command only
hp self-update --version 3.13.0
```

`hp self-update` works for a `uv tool` or pipx install: it runs
`uv tool install --force git+...@vX.Y.Z` (or `pipx install --force ...`), then
checks the version that is installed. It does not downgrade past the server.
Elsewhere (the server image, a development checkout) it says why it cannot and
prints the install command; on the control-plane host the CLI updates with the
server.

## Everyday commands

Readable tables by default, `--json` for scripts. Exit codes: 0 done; 1 refused
or failed, with the server's reason on stderr; 2 a usage mistake; 3 a run whose
outcome could not be read (follow it with `hp tasks follow`). They work the same
on the control-plane host and against a profile.

```bash
hp status                          # Needs you (one line per thing to act on, with the
                                   #   command that acts on it), machines, agents,
                                   #   alerts, changes waiting, version, update
hp hosts list [--gone] [--running] # the inventory; --gone: machines Proxmox no longer reports
hp hosts show NAME
hp hosts forget NAME...            # names every host first and asks; --yes to skip
hp hosts forget --gone             # every gone machine, listed before anything is removed

hp agents list
hp agents upgrade NAME... | --all  # one host at a time, progress per host, stops at the
                                   #   first failure and says which host and why
hp agents install NAME             # install + enrol through qemu-guest-agent
hp agents forget ID                # forget a decommissioned agent (revokes its credential)

hp changes list [--inbox]          # --inbox: what waits for review or a run
hp changes show ID
hp changes propose FILE            # a spec in YAML or JSON (- = stdin)
hp changes approve ID
hp changes apply ID [--no-wait]    # follows the run to its outcome unless --no-wait
hp changes approve-and-apply ID [--no-wait]
hp changes withdraw ID [--reason ...]
hp changes reject ID --reason ...
hp tasks list [--change ID]
hp tasks follow TASK_ID            # prints each change of status until it ends
hp tasks cancel TASK_ID

hp kb list [--kind K] [--target T]
hp kb show ID
hp kb search WORDS
hp kb add FILE --target HOST --kind note|policy|decision   # FILE or - for stdin

hp alerts                          # what is firing now
hp channels list                   # notification channels (endpoints redacted)
hp channels test ID                # exit 1 when the message was not delivered
hp guests list                     # friends, their machines, budgets, open invites

hp power NAME start|shutdown|reboot|stop
```

`hp power` acts directly, as the operator (admin token), with the console's
guards: shutdown and reboot ask first (`--yes` skips the question); a hard stop,
or any action on the machine HomePilot runs on or a protected guest, needs the
machine's name typed back - at the prompt, or `--confirm-name NAME` in a script.
`--yes` never replaces the typed name.

`hp changes`, `hp hosts` and `hp agents` were called `hp artifacts`,
`hp inventory` and `hp agent`; the old names still work.

## Every API operation: `hp api`

`hp api` is generated at run time from the server's own OpenAPI document, so it
covers everything the API can do - including routes a newer server added after
this CLI was installed. The document is cached per server URL and version under
`~/.cache/homepilot/openapi/`.

```bash
hp api --list                                    # every operation: method, path, command
hp api inventory --help                          # one group
hp api inventory get-host --help                 # one operation: arguments and options
hp api inventory list-inventory --q web --table  # query parameters are options
hp api inventory get-host HOST_ID                # path parameters are positional
hp api monitoring create-rule --name "cpu hot" --metric cpu.percent \
    --comparison gt --threshold 90               # simple body fields are options
hp api kb create-note --body-file note.json      # or a whole JSON body (--body - = stdin)
hp api raw GET '/audit?actor=people'             # any path on the server
```

- **Groups** are the API's tags (`agents`, `artifacts`, `inventory`, `kb`,
  `monitoring`, ...); **operations** are the handler names.
- **Output** is JSON on stdout. `--table` prints a list of flat objects as a
  table (and falls back to JSON, saying so, when the answer is not one).
- **Exit codes**: 0 done; 1 the server refused or the call failed (its reason on
  stderr); 2 a usage mistake.
- **Destructive operations** - every DELETE, and anything the server marks
  `x-hp-destructive` (revoke, reject, withdraw, cancel a task, power) - say what
  they will affect and ask first. Without a terminal to ask on they refuse
  unless `--yes`.
- The guest and invite portals and the first-run claim are not offered: they
  authenticate with a friend's client certificate or the claim code, not an API
  token.

## Commands that only run on the server

These work on the control-plane host's own files, database or processes, and
refuse in remote mode with the reason (and the `hp api` command that does the
same over the API, where there is one): `hp init`, `hp claim-code`,
`hp mcp-serve`, `hp doc`, `hp export`, `hp import`, `hp db ...`, `hp vault ...`,
`hp changes reset-approval|edit|push|pull|sync-status`, `hp kb reindex`,
`hp token create|list|revoke`, `hp hosts refresh`.

`hp status`, `hp changes list` and `hp changes show` answer from the server in
remote mode and from this host's files on the host, as before.
