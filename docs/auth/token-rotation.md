# Token Rotation

## Generating a New Token

Use the `POST /auth/tokens` endpoint with the admin secret:

```bash
curl -X POST http://localhost:8000/auth/tokens \
  -H "Content-Type: application/json" \
  -H "x-hp-admin-secret: $HP_VAULT_PASSPHRASE" \
  -d '{"label": "ci-deploy", "scope": "full"}'
```

Response:

```json
{"token": "hp_<hex>", "scope": "full"}
```

Store the returned token securely. The prefix (first 8 characters) is stored in the database for lookup.

## Revoking a Token

Use the `DELETE /auth/tokens/{prefix}` endpoint with an admin-scoped token:

```bash
curl -X DELETE http://localhost:8000/auth/tokens/hp_abc12 \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

- Returns `204 No Content` on success.
- Returns `404 Not Found` if the prefix does not match any token.
- Returns `403 Forbidden` if the calling token lacks the `admin` scope.

## Cookie Secure Flag

Set `HP_COOKIE_SECURE=true` in your environment or `.env` file to mark session cookies with the `Secure` attribute. This is required when serving HomePilot over HTTPS.

Default: `false` (homelab deployments commonly use HTTP).

## Future: Synchronizer Token CSRF Pattern

The current CSRF defense uses the double-submit cookie pattern. A future iteration will upgrade to the synchronizer token pattern, storing a server-side CSRF secret per session and validating it against an encrypted token in the request header. This provides stronger guarantees against sub-domain attacks at the cost of server-side state.