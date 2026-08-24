# The guest portal (#442): a friend's window into the lab

Same backend, different client. A cert-holding friend gets `web-guest/index.html`
— one static file, no build step — and it talks to `/guest/*` (their machines,
their budget, power buttons) and `/invite/*` (redeeming a new machine). Nothing
else of HomePilot faces them: not the admin UI, not the API, not MCP.

```
friend's browser ──mTLS──> front nginx ──┬── /            serves web-guest/index.html
                                          ├── /guest/*     proxied to backend :8000
                                          └── /invite/*    proxied to backend :8000
```

## What goes where

- **`web-guest/index.html`** → the front server, e.g. `/var/www/guest-portal/`.
  Copy the one file; there is nothing to install or build.
- **The nginx vhost** below → the front server's existing nginx (the one that
  already terminates the friends' client certificates).
- **Backend env** (control-plane box): the four portal variables must be set —
  `HP_PORTAL_TRUSTED_PROXY` (the front server's address as the backend sees it),
  `HP_PORTAL_PROXY_SECRET`, `HP_PORTAL_VERIFY_HEADER=ssl-client-verify`,
  `HP_PORTAL_CN_HEADER=ssl-client-subject-dn`. Unset = every guest route
  answers 503, fail-closed.

## The vhost

```nginx
server {
    listen 443 ssl;
    server_name guests.example.net;

    ssl_certificate         /etc/nginx/certs/guests.crt;
    ssl_certificate_key     /etc/nginx/certs/guests.key;

    # The friends' CA. `optional` so the page itself can render a clear
    # "no certificate" message instead of a browser-level TLS failure;
    # the backend refuses anything unverified regardless.
    ssl_client_certificate  /etc/nginx/certs/friends-ca.crt;
    ssl_verify_client       optional;

    # The static client.
    root /var/www/guest-portal;
    location = / { try_files /index.html =404; }

    # ONLY these two prefixes reach HomePilot. Everything else 404s here.
    location ~ ^/(guest|invite)/ {
        proxy_pass http://<backend-host>:8000;
        proxy_set_header X-Hp-Portal-Secret     "<value of HP_PORTAL_PROXY_SECRET>";
        proxy_set_header ssl-client-verify      $ssl_client_verify;
        proxy_set_header ssl-client-subject-dn  $ssl_client_s_dn;
        # Never forward auth material a client might try to smuggle.
        proxy_set_header Authorization "";
        proxy_set_header Cookie "";
    }
}
```

Three factors, all required by the backend on every request: the request must
come from the address in `HP_PORTAL_TRUSTED_PROXY`, carry the shared secret,
and carry `ssl-client-verify: SUCCESS` with exactly one `CN=` in the subject.
Anything less renders "no client certificate".

## Per-guest budgets

`hp quota set --cn <friend> --max-vms 2 --max-cores 8 --max-memory-mb 16384 --max-disk-gb 100`

Totals across ALL the friend's machines. Redemption stops at the line (the
invite stays open so they can free resources and retry), and the portal shows
usage-vs-budget meters so the line is never a surprise. No quota row = no
quota; unset axes are unlimited. `hp quota list` shows every budget next to
real usage.

## What a guest can and cannot do

Can: see their machines (name, address, size, state), start / stop / reboot
them, watch their budget, redeem invites. Cannot: see anyone else's machines
(another guest's id answers exactly like a typo), see nodes / templates /
tasks / topology, read hypervisor error text, destroy or resize anything, or
reach any other HomePilot surface.
