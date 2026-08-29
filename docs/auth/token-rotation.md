# Token Rotation

## Generating a New Token

Tokens are minted by admins. `POST /auth/tokens` takes either credential - an
admin-scope token (bearer, or the console session cookie) or the admin secret:

```bash
curl -X POST http://localhost:8000/auth/tokens \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -d '{"label": "ci-deploy", "scope": "read,write"}'
```

```bash
# Equivalent, with the admin secret the CLI resolves from the vault
curl -X POST http://localhost:8000/auth/tokens \
  -H "Content-Type: application/json" \
  -H "x-hp-admin-secret: $HP_ADMIN_SECRET" \
  -d '{"label": "ci-deploy", "scope": "read,write"}'
```

### Which scope to ask for

The ladder is `read` < `write` < `admin`, and `scope` takes a comma-separated
list. Give a token the smallest rung that does its job:

| `scope` you send | what it grants | MCP tool tier |
|---|---|---|
| `read_only` (or `read`) | reads only | `read_only` |
| `read,write` | reads plus the standard mutators | `full` |
| `admin` | the above plus tokens, secrets and fleet administration | `admin` |
| `all` | **superuser (`*`)** - everything, for ever. What `hp init` mints for the box itself | `admin` |

Two traps, both real (#579, #614):

* **`full` is not the write tier here.** As an API scope, `full` is a legacy
  alias for `all` - the SUPERUSER scope. It is accepted for ever so nothing
  breaks, and it is advertised nowhere. `"scope": "full"` on a CI credential
  mints `*`. The write tier's API scope is spelled `read,write`.
* **`full` IS the write tier on the MCP side.** The tool tiers are
  `read_only` < `full` < `admin`, and an API `write` token resolves to the
  `full` tier. Same word, two ladders; the console never says it, and neither
  should you when you mean the API scope.

On the box, `hp token create` does the same thing with whichever of the two
credentials this instance holds. Its direct-DB fallback is bootstrap only:
allowed while the instance has zero live tokens, refused the moment one exists.
It needs an admin credential of its own to reach the API - `HP_ADMIN_TOKEN`, or
`<data dir>/api-token`, which `hp init` and the browser claim write. An instance
whose first admin token came from the console has neither, and the command says
so rather than falling through to the schema guard.

Response:

```json
{"token": "hp_<hex>", "scope": "read,write"}
```

Store the returned token securely. The **first 16 characters** are stored in the
database as the lookup prefix (`homepilot.auth.tokens.PREFIX_LENGTH`); only a
sha256 of the whole token is kept, so a lost token is revoked and replaced
rather than recovered.

## Revoking a Token

Use the `DELETE /auth/tokens/{prefix}` endpoint with an admin-scoped token. The
prefix is the token's first 16 characters, which is what `hp token list` and
Settings -> Tokens display:

```bash
curl -X DELETE http://localhost:8000/auth/tokens/hp_0123456789abc \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

- Returns `204 No Content` on success.
- Returns `404 Not Found` if the prefix does not match any token.
- Returns `403 Forbidden` if the calling token lacks the `admin` scope.

## Cookie Secure Flag

Set `HP_COOKIE_SECURE=true` in your environment or `.env` file to mark session cookies with the `Secure` attribute. This is required when serving HomePilot over HTTPS.

Default: `false` (homelab deployments commonly use HTTP).

## CSRF

Cookie-authenticated **unsafe** methods (anything but GET/HEAD/OPTIONS) must
carry all three of:

* the `hp_csrf` cookie, set at login;
* an `X-CSRF-Token` header equal to it (double-submit);
* any `X-Requested-With` header, which a cross-site form post cannot set without
  a preflight the browser will not grant.

Both cookies are `SameSite=Lax`, so a cross-site form post does not carry the
session in the first place. Bearer-token callers (the CLI, MCP, CI) are exempt:
they are not a browser flow and there is no ambient credential to ride.

This was previously written up as a stopgap awaiting "the synchronizer token
pattern". It is not a stopgap. The one thing double-submit is genuinely weaker
at is an attacker who can *write cookies on a sibling subdomain* of the
instance - and the `X-Requested-With` requirement blocks the form-post shape
that attack needs anyway. Reviewed 2026-08-29 (review #648): **no change
planned.** If HomePilot ever serves the console from a host that shares a
registrable domain with untrusted content, revisit it then, and set
`HP_COOKIE_SECURE=true` at the same time.