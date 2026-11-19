# Spike: bundling ntfy with HomePilot (#822)

Date: 2026-11-18. Every fact below was read from the pinned release's source at
its tag, and the ones the design rests on were also run against the real
binary (last section). Line links are to `v2.37.2`
(`https://github.com/binwiederhier/ntfy/blob/v2.37.2/<file>#L<n>`), commit
`05109571fd0931affb6f92b17bbfd62c4d2ea68a`.

## The pin

| | |
|---|---|
| Release | **v2.37.2**, published 2026-11-04 - the newest stable release at least two weeks old on 2026-11-18 (v2.38.0 is 2026-11-14, v2.38.1 2026-11-16) |
| Image | `binwiederhier/ntfy:v2.37.2@sha256:1875ddcf815c8a3c981d6d78c67a93452d0abba79b1ab9520b1f5f5238902bd7` (multi-arch index digest, from the Docker Hub registry API) |
| Binary for the journey test | `ntfy_2.37.2_linux_amd64.tar.gz`, sha256 `e67f53d7fbed529b83faf3993b584110921ef3567b0ac0b033c6910879a4ffd1` - matches the release's `checksums.txt` |
| Image base | `alpine` with `ntfy` on PATH, runs as root, `ENTRYPOINT ["ntfy"]` (`Dockerfile`) - so `sh`, `sha256sum`, `wget` (busybox) exist for the service script and healthcheck |

**Known upgrade to take:** v2.38.1 is a security release hardening *message
templating* against denial-of-service. Templating is applied to what a
publisher sends. On the bundled server only HomePilot can publish (anonymous
`deny-all`, phones `read-only`), so the exposure is limited to HomePilot's own
publisher token - but the pin should move to v2.38.1 once it is two weeks old
(2026-11-30).

## Declarative users, access and tokens in server.yml

Keys (all also take `_` for `-`): `auth-users`, `auth-access`, `auth-tokens`,
`auth-default-access` - `cmd/serve.go` L63-L66, read at L193-L197, parsed at
L469-L485. Documented in `server/server.yml` L171-L200 and `docs/config.md`
"Users via the config" / "Tokens via the config".

| Key | Entry format | Validated by |
|---|---|---|
| `auth-users` | `<username>:<bcrypt-hash>:<role>`, role `admin` or `user` | `parseUsers`, `cmd/serve.go` L694-L719: `user.AllowedUsername` (`^[-_.+@a-zA-Z0-9]+$`, `user/util.go` L20), `user.ValidPasswordHash(hash, DefaultUserPasswordBcryptCost)`, `user.AllowedRole` |
| `auth-access` | `<username>:<topic-pattern>:<permission>` | `parseAccess`, L721-L762: user must be a PROVISIONED one (or `*`), must have role `user` (admins cannot have ACL entries), topic pattern `^[-_*A-Za-z0-9]{1,64}$` (`user/util.go` L22), permission via `user.ParsePermission` |
| `auth-tokens` | `<username>:<token>[:<label>]` | `parseTokens`, L764-L798: user must be provisioned; `user.ValidToken` |
| `auth-default-access` | `read-write` (default), `read-only`, `write-only`, `deny-all` | `user.ParsePermission`, L469-L473 |

- **Token format:** `^tk_[-_A-Za-z0-9]{29}$` (`user/util.go` L24, `allowedTokenRegex`),
  32 characters with the prefix (`user/manager.go` L32-L33). ntfy's own
  `GenerateToken` (L73-L75) uses lower case only "to support
  `<topic>+<token>@<domain>` email addresses"; HomePilot does the same.
- **Password hash format:** must start `$2a$`, `$2b$` or `$2y$` and have bcrypt
  cost >= 10 (`ValidPasswordHash`, `user/util.go` L53-L65;
  `DefaultUserPasswordBcryptCost = 10`, `user/manager.go` L42). The hash is
  parsed with `bcrypt.Cost` only, so any well-formed bcrypt string passes.
  HomePilot gives its two token-only accounts a well-formed hash of **no known
  password** (random 16-byte salt + random 23-byte digest in bcrypt's base64) -
  no bcrypt dependency, and password login to them is impossible (proven below).
- **Lifecycle:** provisioning runs when the user manager is created at server
  start (`user/manager.go` L87 -> `maybeProvisionUsersAccessAndTokens`
  L1753-L1786, enabled by `ProvisionEnabled: true`, `server/server.go` L322).
  Provisioned users no longer in the config are DELETED (L1807-L1849, and
  `docs/config.md`: "Removing users from the config file will delete them from
  the database the next time ntfy is restarted"); provisioned grants are all
  re-created (L1855-L1880); provisioned tokens no longer listed are deleted
  (L1882-L1913). Non-provisioned users - the phones - are untouched.

## The admin API (creating phone logins)

Routes, all behind `ensureAdmin` (`server/server_middleware.go` L125-L132: the
authenticated user must have role `admin`; a bearer token of an admin user
authenticates as that user) - `server/server.go` L782-L801:

| Method + path | Body | Handler |
|---|---|---|
| `GET /v1/users` (`?username=`) | - | list users with role, `provisioned` and grants (`server_admin.go`) |
| `POST /v1/users` | `{"username","password"}` or `{"username","hash"}` | `handleUsersAdd`, `server_admin.go` L127: creates role `user`; a `role` field is refused off the admin socket (`checkRoleChange` L685-L692) - **the network admin API cannot create admins** |
| `DELETE /v1/users` | `{"username"}` | `handleUsersDelete` L238: refuses `*`; non-`user` roles only over the admin socket |
| `PUT`/`POST /v1/users/access` | `{"username","topic","permission"}` | `handleAccessAllow` L264: refuses admin users |
| `DELETE /v1/users/access` | `{"username","topic"}` | `handleAccessReset`; kills that user's subscribers |
| `GET /v1/version` | - | `ensureAdmin(handleVersion)`, `server.go` L774-L775 - an admin-only read, which is what HomePilot uses to prove ntfy loaded ITS config |

Request types: `server/types.go` L170-L207. Tokens of an admin user cannot be
created or listed over the network (`handleUsersTokensAdd` L350, "only over the
admin socket") - irrelevant here: HomePilot's admin token is provisioned in the
config. The experimental admin unix socket (`server.yml` L33-L36,
`docs/config.md` "Admin socket") is not used: it is experimental and would have
to cross container boundaries.

## How a config change is picked up

**A restart.** SIGHUP is handled (`cmd/serve_unix.go` L16-L30, started at
`cmd/serve.go` L645) but reloads ONLY the log level (`reloadLogLevel`). Users,
access and tokens are provisioned only at start (above). Hence the compose
service's script: it starts `ntfy serve` when `server.yml` exists, watches the
file's sha256 every 5 s, and restarts ntfy when it changes or stops it when it
disappears; SIGTERM is forwarded (ntfy drains on SIGTERM,
`sigHandlerShutdown`).

Environment variables beat the config file (urfave/cli altsrc: every flag has
`EnvVars`, e.g. `NTFY_LISTEN_HTTP`, `NTFY_AUTH_FILE`; verified below). The
journey test uses that to point the container paths at scratch ones while
running the very config HomePilot writes.

## Behind nginx: websockets and long polls

From `docs/config.md` "nginx/Apache2/caddy" (L1303 onward) and the options
table:

- subscriptions are long-lived (`/<topic>/json`, `/sse`, `/ws`; `server.go`
  L102-L106): pass `Upgrade` / `Connection "upgrade"`, `proxy_http_version 1.1`,
  `proxy_buffering off`, `proxy_request_buffering off`;
- ntfy writes a keepalive every `keepalive-interval` = 45 s (options table,
  L2611: "the Android app has a hardcoded timeout at 77s"), so nginx's read and
  send timeouts must exceed 45 s - the docs use `3m`;
- `behind-proxy` (L1200-L1210, option L2592): without it every visitor behind
  the proxy is ONE visitor for rate limits and the failed-login ban. With it,
  the right-most `X-Forwarded-For` address is used, falling back to the remote
  address when the header is missing (`server/util.go` L86-L99) - so a LAN
  client that bypasses the proxy still works;
- `/v1/health` answers anyone (`{"healthy":true,...}`, "Health checks"
  L2424-L2447).

The reference vhost (`deploy/ntfy/nginx-ntfy.conf`) also refuses
`/v1/(users|tiers|version|stats)` and `/metrics` from outside: HomePilot uses the
admin API over the compose network only.

## Other settings used

`enable-signup` / `enable-login` / `enable-reservations` default `false`
(options L2637-L2639; `EnableLogin` only feeds the web app's config,
`server_web.go` L80 - Basic-auth subscribing works without it, verified below),
`web-root: disable` (L2636), attachments are off unless `attachment-cache-dir`
is set (L2595). The Android app's deep link is `ntfy://<host>/<topic>`,
`?display=`, `&secure=false` for http (`docs/subscribe/phone.md` L140-L157); it
carries no credentials, so the app's login is added by hand (Settings -> Manage
users). iOS instant delivery for a self-hosted server needs `base-url` +
`upstream-base-url` (poll requests via ntfy.sh, L2616) - deliberately not set:
an owner decision if wanted.

## Run against the real binary

`ntfy serve` v2.37.2 with a config of exactly this shape (`deny-all`, two
provisioned users with random well-formed hashes, a write-only ACL for the
publisher, both tokens provisioned):

| Action | Result |
|---|---|
| `GET /v1/health` | `{"healthy":true,"role":"active"}` |
| anonymous publish | 403 |
| admin token: `POST /v1/users` phone + `PUT /v1/users/access` read-only | `{"success":true}` both |
| admin token: `POST /v1/users` with `"role":"admin"` | 403, code 40302 "only allowed over the local admin socket" |
| publisher token: publish to `homepilot` / to another topic / read `homepilot` | 200 / 403 / 403 |
| phone login: poll `homepilot` / publish | message received / 403 |
| anonymous poll | 403 |
| Basic auth `hp-admin:password` on `/v1/users` | 401 (no password exists) |
| admin token: `DELETE /v1/users` phone, then phone poll | success, then 401 |
| admin token: `GET /v1/version` | `{"version":"2.37.2","commit":"05109571...",...}` |
| `NTFY_LISTEN_HTTP` set, config says `:80` | listens on the env address |

The same facts are asserted on every run by
`tests/test_phone_alerts_journey_822.py` (needs `HP_NTFY_BIN`).

## Fetching the binary for the journey test

```bash
v=2.37.2
curl -sSLO https://github.com/binwiederhier/ntfy/releases/download/v$v/checksums.txt
curl -sSLO https://github.com/binwiederhier/ntfy/releases/download/v$v/ntfy_${v}_linux_amd64.tar.gz
grep "ntfy_${v}_linux_amd64.tar.gz" checksums.txt | sha256sum -c -
tar xzf ntfy_${v}_linux_amd64.tar.gz
export HP_NTFY_BIN=$PWD/ntfy_${v}_linux_amd64/ntfy
```
