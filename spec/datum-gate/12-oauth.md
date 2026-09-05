# 12 — OAuth 2.1 Authorization Server

Purpose: let an interactive MCP client (Claude.ai, ChatGPT, Copilot, an IDE)
obtain an access token **bound to a principal**. The grant carries no
permissions of its own; the token resolves to a principal and the
principal's effective grant is what is enforced. Hand-rolled: one grant
type, one client type, and the parts that matter (audience binding,
refresh rotation with reuse detection, exact redirect matching) are the
parts that should be visible in the source.

## 1. Endpoints

| Endpoint | RFC | Purpose |
|---|---|---|
| `GET /.well-known/oauth-protected-resource` | 9728 | `{"resource": "{PUBLIC_URL}/mcp", "authorization_servers": ["{PUBLIC_URL}"], "bearer_methods_supported": ["header"], "scopes_supported": ["mcp"]}` |
| `GET /.well-known/oauth-authorization-server` | 8414 | issuer `{PUBLIC_URL}`, `authorization_endpoint`, `token_endpoint`, `registration_endpoint`, `revocation_endpoint`, `code_challenge_methods_supported: ["S256"]`, `grant_types_supported: ["authorization_code","refresh_token"]`, `response_types_supported: ["code"]`, `token_endpoint_auth_methods_supported: ["none","client_secret_post"]`, `resource_indicators_supported: true` |
| `POST /oauth/register` | 7591 | dynamic client registration, unauthenticated |
| `GET /oauth/authorize` | 6749 | login + consent screen |
| `POST /oauth/authorize` | | consent posted |
| `POST /oauth/token` | 6749/7636/8707 | code exchange, refresh rotation |
| `POST /oauth/revoke` | 7009 | |

`PUBLIC_URL` is the issuer and the audience and is **required at startup**;
a wrong value serves discovery happily and mints tokens no client can use,
so it is logged at startup and refused when absent.

## 2. Registration

Body per RFC 7591: `client_name`, `redirect_uris` (≥1, each `https://` or
loopback `http://127.0.0.1`/`http://localhost`), `token_endpoint_auth_method`
(`none` default → public client; `client_secret_post` → confidential,
secret returned once and stored as sha256). Returns `client_id` (32 random
bytes urlsafe) and echoes the metadata. Unauthenticated by necessity; a
client id grants nothing on its own.

Registrations that are never used are pruned after
`retention.unused_oauth_client_days` (default 30).

## 3. Authorize

`GET /oauth/authorize?response_type=code&client_id&redirect_uri&code_challenge&code_challenge_method=S256&state&resource&scope`

- `client_id` must exist; `redirect_uri` must **exactly** equal one
  registered (string equality after nothing more than trailing-slash
  normalisation is *not* done — exact). A mismatch renders an error page
  and does **not** redirect.
- `code_challenge_method` must be `S256`; `plain` is refused.
- `resource`, if present, must canonicalise to `{PUBLIC_URL}/mcp`; absent
  defaults to it.
- Renders a self-contained HTML form (no external assets): principal name,
  password, the client name, the scope, and a Consent button. Hidden fields
  carry the request parameters and a CSRF token bound to a short-lived
  signed cookie.

`POST /oauth/authorize`: verify CSRF; `passwords.authenticate(name, password)`
with lockout (`03 §9`); the principal must be `kind='human'`, not disabled,
and tier ≥1. On success: `INSERT oauth_codes` (10-minute expiry, challenge,
resource, scope) and 302 to `redirect_uri?code=…&state=…`. On failure:
re-render with a generic message (never "unknown user" vs "wrong password");
429 with `Retry-After` when locked out.

Consent is per authorization — there is no "remember this client". The
principal sees which client and which audience each time.

## 4. Token

`POST /oauth/token` (form-encoded):

**`grant_type=authorization_code`**: `code`, `client_id`, `redirect_uri`,
`code_verifier`, optional `client_secret`, optional `resource`.

```
row = SELECT … FROM oauth_codes WHERE code=$1 FOR UPDATE
if not row or row.client_id != client_id: invalid_grant
if row.used_at is not None:
    # replay — revoke the whole family and refuse
    revoke_family(client_id, principal_id)   # outside the transaction that is about to fail
    invalid_grant
if row.expires_at < now: invalid_grant
if row.redirect_uri != redirect_uri: invalid_grant
if b64url(sha256(code_verifier)) != row.code_challenge: invalid_grant
if resource given and canonical(resource) != canonical(row.resource): invalid_target
confidential client: verify client_secret hash
UPDATE oauth_codes SET used_at=now()
mint access (kind oauth_access, expires ACCESS_TOKEN_TTL_SECONDS=3600, audience=row.resource, scope)
mint refresh (kind oauth_refresh, expires REFRESH_TOKEN_TTL_SECONDS=30d)
return {access_token, token_type: "Bearer", expires_in, refresh_token, scope}
```

**`grant_type=refresh_token`**: `refresh_token`, `client_id`.

```
row = SELECT … FROM credentials WHERE secret_hash=sha256(token) AND kind='oauth_refresh' FOR UPDATE
if not row or row.client_id != client_id: invalid_grant
if row.rotated_to is not None:
    # reuse of a rotated refresh token: someone else has a copy
    revoke_family(row.client_id, row.principal_id)
    invalid_grant
if row.revoked_at or row.expires_at < now: invalid_grant
if principal disabled: invalid_grant
new_refresh = mint …; UPDATE old SET rotated_to=new.id, revoked_at=now()
new_access = mint …
return {access_token, refresh_token, …}
```

`revoke_family(client_id, principal_id)`: `UPDATE credentials SET revoked_at=now() WHERE client_id=$1 AND principal_id=$2 AND revoked_at IS NULL`.
Runs in its own transaction so the failure response cannot roll it back —
the original documents this exact bug and its fix, and it is registered as
a guard (`17`).

Responses set `Cache-Control: no-store`. Errors are RFC 6749 §5.2 JSON.

## 5. Revoke

`POST /oauth/revoke` with `token` and `client_id`: marks the matching
access or refresh row revoked; revoking a refresh token also revokes its
family. Always 200 (RFC 7009), even for unknown tokens.

## 6. Resolution

`03 §7`: an `oauth_access` credential resolves like a token, plus the
audience check against `{PUBLIC_URL}/mcp`. Access tokens work on every
surface (REST, MCP, service paths), not only `/mcp` — the audience is the
*server*, and the principal's grant does the rest.

## 7. Guards registered for this module

- replayed authorization code revokes the family
- replayed refresh token revokes the family
- family revocation survives the failing transaction
- `plain` challenge refused
- redirect URI prefix match refused
- token minted for another resource refused at resolve
- lockout keyed on name, checked before DB
- consent requires `kind='human'`
