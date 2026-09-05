# 07 — Connections and the Credential Proxy

## 1. Shape

A connection has two halves that are never rejoined on any read path:

| Half | Column | Contents | Returned by |
|---|---|---|---|
| Identity | `config` JSONB | host, port, database, base_url, region, non-secret headers | every read |
| Authentication | `secret` BYTEA | password, token, key, private key | nothing |

The caller declares the split when writing. The store refuses obvious
secret names in `config` (`password passwd pass secret token access_token
refresh_token client_secret api_key apikey private_key passphrase credentials
key secret_key`) with a message saying where they belong.

Every read SQL uses one `_COLUMNS` string that excludes `secret`. The only
function that reads the column is `_sealed()`, called by `resolve()`, `test()`
and `proxy.forward()` — none of which return to an HTTP client.

## 2. Types

| Type | Required config | Secret keys (typical) | Testable |
|---|---|---|---|
| `database` | `host`, `database`; `port` default 5432; `user` | `password` | yes — `SELECT 1` with 10s timeout |
| `http` | `base_url`; optional `headers`, `auth_inject` | per `auth_inject` | yes — `GET base_url`, 401/403 = fail |
| `email_smtp` | `host`, `port` default 587, `from`, `starttls` bool | `username`, `password` | yes — EHLO + AUTH, no send |
| `email_imap` | `host`, `port` default 993, `mailbox` | `username`, `password` | yes — LOGIN + SELECT |
| `file` | `root` (absolute) | — | yes — `is_dir` |
| `oauth_client` | `token_url`, `client_id`, `scope` | `client_secret` | yes — client-credentials token fetch |
| `s3` | `endpoint_url`, `bucket`, `region` | `access_key_id`, `secret_access_key` | yes — HEAD bucket |

`auth_inject` (http only):

```json
{"type": "bearer", "secret_field": "token"}
{"type": "basic",  "user_field": "username", "pass_field": "password"}
{"type": "header", "header": "X-API-Key", "secret_field": "api_key"}
{"type": "query",  "param": "key", "secret_field": "api_key"}
```

## 3. Tier, scope, access

- **Tier** (1–5): sensitivity. A workspace may declare the connection only if
  the publisher's tier ≥ connection tier; an agent may proxy through it only
  if its tier ≥ connection tier.
- **Scope**: `global` | `repository` (targets = repo names) | `workspace`
  (targets = `Repo/Workspace`). An addressing rule: two repositories may each
  have a `MainDB`. Enforced at publish and at resolve with one predicate,
  `matches_scope(row, repo, ws)`.
- **Access**: `read` | `write`. A workspace declaring `write` on a `read`
  connection is refused at publish.

Tier is checked **at publish against the publisher** and **at proxy against
the agent**. It is not re-checked at job run time: a workspace runs with its
own authority, chosen once by whoever published it.

## 4. Sealing

AES-256-GCM. Sealed value layout:

```
byte 0        key_id   (1..255)  which DG_SECRET_KEY_<id> sealed this
bytes 1..12   nonce    (96-bit random)
bytes 13..    ciphertext || 16-byte tag
```

- AAD = connection name (UTF-8). Moving a sealed blob between rows fails to
  open.
- Keys come from the environment as `DG_SECRET_KEY_1`, `DG_SECRET_KEY_2`, …
  (base64url, 32 bytes each). `DG_ACTIVE_KEY_ID` names which one seals new
  values. All present keys may open. This is what makes rotation possible:
  add key 2, set active to 2, run `python -m datumgate keys reseal` which
  opens every secret with its `secret_key_id` and reseals with the active
  key, then remove key 1.
- No key configured → the server starts, and every write carrying a secret
  is refused with `503 SECRETS_UNAVAILABLE`; the UI shows why before the
  form is filled in.
- `open_()` returns one error for wrong key / tampered / wrong AAD; it does
  not say which.

## 5. Resolution for a job

`resolve(conn, repo, ws, refs) -> dict[name, ConnectionObject]`:

For each declared connection: fetch, check scope, open the secret, and build
the object the workspace receives:

```python
{
  "name": "StratumDB", "type": "database", "access": "read", "tier": 2,
  # config keys spread in…
  "host": "...", "port": 5432, "database": "...", "user": "...",
  # …and secret keys spread in
  "password": "...",
}
```

Raises rather than omits: a missing or out-of-scope connection fails the
job at claim time with an error naming the connection, before the child is
spawned.

The object is a dict on purpose: the workspace decides which client library
to use. The child harness additionally offers helpers
`datumgate_child.clients.pg(conn_obj)`, `.http(conn_obj)`, `.smtp(conn_obj)`
that build an `asyncpg` connection / `httpx.AsyncClient` with auth injected /
`smtplib` session from the object. Helpers are conveniences; the dict is the
contract.

## 6. Credential proxy

Lets an agent use a key it must never hold.

Surface: MCP tool `proxy_request` and `POST /rest/v1/connections/{name}/proxy`
(same body).

```json
{
  "method": "POST",
  "path": "/v1/chat/completions",
  "query": {"k": "v"},
  "headers": {"Content-Type": "application/json"},
  "body": {...} | "raw string",
  "timeout_seconds": 30
}
```

Checks, in order:

1. connection exists (404) and `type == 'http'` (400)
2. `name in principal.effective.connections.proxy` (403 `PROXY_DENIED`)
3. `principal.effective.tier >= connection.tier` (403 `TIER_DENIED`)
4. method in `GET POST PUT PATCH DELETE HEAD`
5. upstream URL = `base_url + path` (path forced to start with `/`; a path
   containing `://` or `..` segments is refused)
6. **SSRF guard**: resolve the host; every A/AAAA answer must be global
   unicast (`ipaddress.is_global and not is_multicast`). Reject otherwise
   with 403 `BLOCKED_ADDRESS`. Exception: connections with
   `config.allow_private_origin: true` may target private ranges — such a
   connection can only be created by a tier-5 principal, and the audit row
   flags `governance: true`.
7. Inject auth from the secret. **Caller-supplied `Authorization`,
   `Cookie`, and any header named in `auth_inject` are dropped** before
   injection.
8. Follow redirects manually (max 5), re-running the SSRF check on each
   hop, stripping injected auth on cross-origin hops, converting to GET on
   301/302/303.
9. Response: ≤ `PROXY_MAX_RESPONSE_BYTES` (1 MiB, truncated flag), text
   types inlined, binary summarised. `isError` when status ≥ 400.
10. Audit `proxy.request` with `target = "{connection}:{METHOD}:{path}"`,
    upstream status in `detail`. Bodies are never stored.

Rate limit: `limits.calls_per_minute` already applies; additionally
`policy.proxy.per_connection_per_minute` (default 60) per principal per
connection.

## 7. Connection tests

`POST /rest/v1/connections/{name}/test` (tier ≥4). Runs the type's tester
with a 10s timeout, records `last_test_at/ok/error` on the row. The error
string is truncated to 500 chars and can contain host and user but never
the secret — the testers construct errors from exceptions whose args never
include the password.

## 8. API shape

See `10-rest-api.md §7`. `secret` is write-only: present replaces, `null`
clears, absent leaves untouched. Deleting a connection that any active
workspace version declares is refused with 409 naming the workspaces.
