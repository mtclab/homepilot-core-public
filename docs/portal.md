# The invite portal (`/invite/*`)

A friend needs a VM. You do not want to hand them the admin UI, an API token, or
a shell. The invite portal is the whole of what they get: three server-rendered
HTML pages behind your existing client-certificate vhost, where they type a
username and an SSH key and receive one machine with caps **you** chose.

Everything else - the admin UI, the API, MCP, the agent hub - stays internal.
The friend's browser never loads a line of admin JavaScript.

## The security model in plain words

The portal decides who you are from your **client certificate**, and it believes
the certificate only because your reverse proxy did the TLS handshake and told
it so. HTTP headers alone prove nothing, so the portal demands three things at
once, on every single request:

1. **The request came from the proxy.** Its source address must be inside
   `HP_PORTAL_TRUSTED_PROXY`. A request from anywhere else is refused even if
   every header is perfect.
2. **The proxy signed it.** The proxy sets `X-Hp-Portal-Secret` to the value of
   `HP_PORTAL_PROXY_SECRET`; the portal compares it in constant time. This is
   what stops anything else that happens to sit on the trusted network.
3. **The certificate actually verified.** The header named by
   `HP_PORTAL_VERIFY_HEADER` must be exactly `SUCCESS`, and the header named by
   `HP_PORTAL_CN_HEADER` must carry a subject DN with exactly one `CN=`.

Miss any one of the three and the request gets a plain "no client certificate"
page. Leave any one of the four settings unset and **every** `/invite/*` route
returns 503 with the missing variable named - the portal fails closed, never
open.

On top of that:

- **An invite is bound to one CN.** A leaked link is useless to anyone else: the
  CN in the certificate must equal the CN the invite was minted for.
- **The token is never stored.** The database holds a 16-character prefix (the
  lookup key) and a SHA-256 hash, exactly like API tokens. A database copy does
  not let you redeem anything.
- **Single use, atomically.** Redemption claims the row with
  `UPDATE ... WHERE redeemed_at IS NULL`, and only a rowcount of 1 proceeds. Two
  simultaneous posts produce exactly one machine.
- **Caps come from the invite, never from the form.** The redeemer chooses a
  username, an SSH key, optionally a hostname and optionally their own tailscale
  auth key. Cores, memory, disk, template and node come from the invite row. A
  post carrying `cores=64` changes nothing.
- **The pages carry no operator data.** No node names, no template ids, no task
  ids, no error text from Proxmox. A build that fails says so and nothing more.
- **Failure pages are uniform.** A wrong CN cannot tell whether a token exists,
  has expired, was revoked, or was already used - all four render the same page.
- **Redemption is rate-limited** per (source, CN), on top of the global per-IP
  HTTP limiter.

## The nginx location block

The point of the split is that **only** `/invite/` is public. Put this on the
client-certificate vhost; leave every other path off it.

```nginx
server {
    listen 443 ssl;
    server_name portal.example.net;

    ssl_certificate     /etc/ssl/portal/fullchain.pem;
    ssl_certificate_key /etc/ssl/portal/privkey.pem;

    # Client certificates: your own CA, and verification is mandatory. The
    # portal re-checks the result, but a failed handshake should never reach it.
    ssl_client_certificate /etc/ssl/portal/friends-ca.pem;
    ssl_verify_client on;
    ssl_verify_depth 2;

    # Nothing but the portal is published here.
    location / {
        return 404;
    }

    location /invite/ {
        proxy_pass http://127.0.0.1:8000;

        proxy_set_header Host              $host;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # STRIP anything a client tried to send under these names, then set them
        # ourselves. Without the strip, a client could simply send its own
        # ssl-client-verify: SUCCESS.
        proxy_set_header ssl-client-verify     $ssl_client_verify;
        proxy_set_header ssl-client-subject-dn $ssl_client_s_dn;

        # The shared secret. Must equal HP_PORTAL_PROXY_SECRET.
        proxy_set_header X-Hp-Portal-Secret "REPLACE-WITH-HP_PORTAL_PROXY_SECRET";
    }
}
```

Notes that matter:

- `proxy_set_header` **replaces** a client-supplied header of the same name, so
  the two `ssl-client-*` lines are both the assertion and the strip. Do not use
  `underscores_in_headers on;` with underscore-named variants - keep hyphens.
- If your vhost already exports the client-certificate identity under different
  header names, do not rename them: set `HP_PORTAL_CN_HEADER` and
  `HP_PORTAL_VERIFY_HEADER` to what you already have. If your nginx emits the
  legacy OpenSSL oneline DN (`/C=FI/O=MTC/CN=friend-a`), that is understood too.
- HomePilot's backend must not be reachable from the internet on any other
  vhost. The portal is the exception, not a new front door.
- Set `HP_TRUSTED_PROXIES` to the same proxy address if you want the global rate
  limiter to see real client IPs rather than the proxy's.
- The invite token is part of the URL, so your **access log records it**. That is
  survivable (the token is useless without the matching certificate), but treat
  the vhost's logs as sensitive or drop the request line for `/invite/`.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `HP_PORTAL_CN_HEADER` | `ssl-client-subject-dn` | Header carrying the client-certificate subject DN |
| `HP_PORTAL_VERIFY_HEADER` | `ssl-client-verify` | Header that must equal `SUCCESS` |
| `HP_PORTAL_TRUSTED_PROXY` | — | Address or CIDR(s) portal requests must arrive from |
| `HP_PORTAL_PROXY_SECRET` | — | Shared secret the proxy sets in `X-Hp-Portal-Secret` |
| `HP_PORTAL_BASE_URL` | — | Public origin, used only to print complete invite URLs |

The first four are all required. Any unset means 503 everywhere under
`/invite/`.

## Mint, send, redeem

**1. Mint an invite** (operator, on the box):

```bash
hp invite create --cn friend-a --template 9000 --node pve1 \
    --cores 2 --ram 4096 --disk 40 --expires 7d
```

It prints the URL **once**:

```
https://portal.example.net/invite/hpi_9f3c...
```

HomePilot keeps only the prefix and a hash, so there is no way to print it
again. If you lose it, revoke and mint a new one.

`--pool` puts the guest in a Proxmox pool; `--disk-device` picks the disk to
resize (default `scsi0`). Caps are validated against the same model that
provisioning uses, so a mint that would fail at build time fails at mint time
instead.

**2. Send the URL** to the holder of that certificate. Only they can use it.

**3. They redeem it.** `GET /invite/{token}` shows what they will get and asks
for a username, an SSH public key, an optional hostname and an optional
tailscale auth key of their own. `POST` claims the invite and starts the same
`ProvisionService` the admin API uses. They are redirected to
`/invite/{token}/status`, which refreshes itself until the machine is up and
then shows the VM id, the address and the ssh command. The page works with
JavaScript disabled - the refresh is a `<meta http-equiv="refresh">`, and the
portal ships no JavaScript at all.

**4. Watch or clean up:**

```bash
hp invite list                  # prefix, CN, caps, expiry, state - never tokens
hp invite revoke hpi_9f3c1a2b   # kills an unredeemed invite
```

States are `open`, `redeemed`, `expired` and `revoked`. A redeemed invite
records the task and the host it produced, so "which machine came from this
invite" is answerable from the invite row alone.

## The tailscale auth key

If the redeemer supplies one, HomePilot joins the new guest to **their** tailnet
with it, then forgets it: the key is never written to the invites table, the
task result, or the audit log.

The join runs through qemu-guest-agent after boot rather than through cloud-init,
because PVE's cloud-init drive exposes no free-form user-data field over the API -
the only key that could carry it, `cicustom`, points at a snippet file on node
storage that the API cannot write.

The key never appears on a command line. It is written to `/run/hp-tailscale.key`
(tmpfs) with the guest-agent file-write call, and the shell that runs
`tailscale up` reads it into an environment variable and deletes the file before
tailscale is invoked. This is deliberate: an argv is readable by every process in
the guest and PVE echoes the command back inside task errors, which is why
[tailscale's own guidance](https://tailscale.com/kb/1595/secure-auth-key-cli) is
to pass the key through the environment. A test asserts the key never reaches an
argv.
The template therefore needs `qemu-guest-agent` and `tailscale` installed. The
join is best-effort: if it fails, the machine is still built and the status page
says the join did not work.
