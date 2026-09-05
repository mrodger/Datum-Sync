# 10 — REST API

Prefix `/rest/v1/`. Service paths at root. All JSON. All timestamps
ISO-8601 with offset.

## 1. Authentication and middleware

Order of middleware, outermost first:

1. **Trace**: mint `trace_id = uuid4()`, read `X-Trace-Id` into
   `client_trace_id`, set `X-Trace-Id` response header to the server value.
2. **Auth**: fail closed. Public paths: `/health`, `/.well-known/*`,
   `/oauth/*`, `/ui/`, `/ui/static/*`, `/hooks/*` (webhook trigger — signed,
   not principal-authenticated), and `/serve/{name}` when the service is
   `public`. Cookie accepted only on `/rest/v1/`, `/ui/`, `/serve/`. Bearer
   wins over cookie. Errors here are rendered to the envelope directly
   (middleware sits outside exception handlers) and carry `WWW-Authenticate`.
3. **Audit**: after the handler, write one `audit_log` row for every
   non-GET request and for GETs on `/stream`, `/download`, `/serve`,
   `/rest/v1/vault/*`, `/rest/v1/jobs/*/artifacts/*` (reads that hand out
   content). Plain listing GETs are not audited.
4. **Rate limit**: from the principal's effective limits (`03 §8`).

## 2. Error envelope

```json
{"status": 403, "code": "TIER_DENIED", "message": "submit requires tier 3; caller is tier 2",
 "detail": {"verb": "job.submit", "required_tier": 3, "tier": 2}, "trace_id": "…"}
```

Codes are stable strings; clients branch on `code`, never on `message`.
Full list in `errors.py`; the ones referenced across this corpus:
`UNAUTHENTICATED TOKEN_EXPIRED TOKEN_REVOKED WRONG_AUDIENCE PRINCIPAL_DISABLED
ANCESTOR_DISABLED FORBIDDEN TIER_DENIED SCOPE_DENIED PROXY_DENIED BLOCKED_ADDRESS
NOT_FOUND CONFLICT ALREADY_TERMINAL INVALID_PARAMETER INVALID_MANIFEST
GRANT_NOT_NARROWER LIMIT_EXCEEDED RATE_LIMITED NO_WORKER TIMEOUT JOB_FAILED
SERVICE_NOT_ENABLED SECRETS_UNAVAILABLE PUBLISH_FAILED INTERNAL_ERROR`.

OAuth endpoints use RFC 6749 error bodies; MCP uses JSON-RPC errors.

## 3. Pagination

List endpoints accept `?limit=` (default 50, max 500) and `?cursor=`
(opaque, base64 of the sort key of the last row). Responses:
`{"items":[…], "next_cursor": "…"|null, "total": n|null}` (`total` only
when cheap).

## 4. System

| Method & path | Auth | Purpose |
|---|---|---|
| `GET /health` | public | `{"status":"ok","db":"ok","workers":[{id,heartbeat_at,concurrency}],"secrets":"ok|unavailable","version":"…"}` — 503 if db down |
| `GET /rest/v1/whoami` | any | the caller: `{name, kind, tier, effective_grant, credential_kind, client_id}` |
| `GET /rest/v1/policy` | tier ≥4 | the loaded policy.yaml (read-only) |
| `POST /rest/v1/policy/reload` | tier 5 | re-read policy.yaml |
| `GET /openapi.json`, `GET /docs` | public | generated |

## 5. Catalogue

| Method & path | Tier | Notes |
|---|---|---|
| `GET /rest/v1/repositories` | 1 | filtered by scope |
| `GET /rest/v1/repositories/{repo}` | 1 | with workspace count |
| `POST /rest/v1/repositories/{repo}/sync` | 4 | body `{"dry_run":bool,"prune":bool,"force":bool}`; returns per-workspace `{name, result: published|unchanged|failed|withdrawn, version, error}` |
| `GET /rest/v1/repositories/{repo}/workspaces` | 1 | current versions only |
| `GET /rest/v1/workspaces` | 1 | flat, all in scope, `?service=mcp` filter |
| `GET /rest/v1/workspaces/{repo}/{ws}` | 1 | manifest, doc, current version, recent jobs count |
| `GET /rest/v1/workspaces/{repo}/{ws}/versions` | 1 | history |
| `POST /rest/v1/workspaces/{repo}/{ws}/versions/{id}/activate` | 4 | roll back/forward: set `current_version_id` if the version's status is not `withdrawn` and its content hash still matches disk (else 409) |
| `GET /rest/v1/workspaces/{repo}/{ws}/schema` | 1 | JSON Schema of parameters + outputs list (builder seam, `05 §9`) |
| `GET /rest/v1/workspaces/{repo}/{ws}/doc` | 1 | `MANIFEST.md`, `text/markdown` |

## 6. Jobs

| Method & path | Tier | Notes |
|---|---|---|
| `POST /rest/v1/jobs/submit/{repo}/{ws}` | 3 | body = params; headers `Idempotency-Key`, `X-Priority` (−10..10, tier ≥4 for >0); 202 + `Location`; 200 + `Location` when the key matched an existing job |
| `POST /rest/v1/jobs/submit-bulk` | 3 | `[{repository, workspace, params, idempotency_key?}]` ≤100; per-item results |
| `GET /rest/v1/jobs` | 2 | filters `status repository workspace submitted_by since until triggered_by trace_id`; scope-filtered |
| `GET /rest/v1/jobs/summary` | 2 | counts by status for the last 24h/7d in scope |
| `GET /rest/v1/jobs/{id}` | 2 | |
| `DELETE /rest/v1/jobs/{id}` | 3 or submitter | cancel |
| `POST /rest/v1/jobs/{id}/resubmit` | 3 | `?version_id=` optional |
| `GET /rest/v1/jobs/{id}/log` | 2 | `?after=<log id>`, `?level=` |
| `GET /rest/v1/jobs/{id}/events` | 2 | SSE (`06 §10`); cookie allowed |
| `GET /rest/v1/jobs/{id}/artifacts` | 2 | descriptors with URLs |
| `GET /rest/v1/jobs/{id}/artifacts/{name}` | 2 | bytes; `?download=1` sets attachment |
| `POST /rest/v1/uploads` | 3 | multipart → `{"upload_id","filename","size"}` |

The path family is `/rest/v1/jobs/…` rather than `/transformations/…`; the
latter is kept as a **301 alias** for the four original routes so existing
clients and the reference MCP configurations keep working.

## 7. Connections

| Method & path | Tier | Notes |
|---|---|---|
| `GET /rest/v1/connections` | 2 | `config` and `has_secret`; never `secret` |
| `POST /rest/v1/connections` | 4 | `{name,type,tier,scope,scope_targets,access,description,config,secret}`; tier ≤ caller tier; 503 if no secret key and a secret is given |
| `GET /rest/v1/connections/{name}` | 2 | |
| `PATCH /rest/v1/connections/{name}` | 4 | partial; `secret` write-only |
| `DELETE /rest/v1/connections/{name}` | 4 | 409 if declared by any active version |
| `POST /rest/v1/connections/{name}/test` | 4 | `{ok, error}` |
| `POST /rest/v1/connections/{name}/proxy` | 3 | credential proxy (`07 §6`) |

## 8. Principals and credentials

| Method & path | Tier | Notes |
|---|---|---|
| `GET /rest/v1/principals` | 4 | `?parent=`, `?kind=`; tier-4 callers see only principals whose effective grant ⊆ theirs |
| `POST /rest/v1/principals` | 3 (agents, ≤ tier 2, own children) / 4 (humans and agents ≤ tier 4) / 5 | `{name, kind, parent?, grant, description}`; `parent` defaults to the caller for tier-3 callers; 400 `GRANT_NOT_NARROWER` with the failing field |
| `GET /rest/v1/principals/{name}` | 4, or self | includes `effective_grant`, children names, live credential summary |
| `PATCH /rest/v1/principals/{name}` | per `03 §5.3` | `{grant?, description?, disabled?}`; refuses if any descendant would stop narrowing, naming them |
| `DELETE /rest/v1/principals/{name}` | 5 | hard delete; 409 if it has children (disable them first) |
| `POST /rest/v1/principals/{name}/tokens` | 4, or self (tier ≥3) | `{label, expires_in_seconds?}` → `{token, id}` — raw value returned once |
| `GET /rest/v1/principals/{name}/credentials` | 4, or self | kinds, labels, expiry, last use — never hashes |
| `DELETE /rest/v1/principals/{name}/credentials/{id}` | 4, or self | revoke one |
| `DELETE /rest/v1/principals/{name}/credentials` | 4 | revoke all (sign out everywhere, kill every token) |
| `POST /rest/v1/principals/{name}/password` | 4, or self | `{password}`; humans only |

The **first principal** is created by CLI only (`python -m datumgate principals create`),
never by an HTTP route: there is nobody to authenticate the request that
would create it.

## 9. Vault

| Method & path | Tier | Notes |
|---|---|---|
| `GET /rest/v1/vault/read?path=` | 1 | `?offset=&limit=`; JSON `{path,content,size,truncated,sha256}`; `Accept: text/plain` returns raw |
| `GET /rest/v1/vault/list?path=&depth=` | 1 | |
| `GET /rest/v1/vault/stat?path=` | 1 | |
| `GET /rest/v1/vault/search?q=&root=&limit=` | 1 | |
| `PUT /rest/v1/vault/write` | 3 | `{path, content, mode}` |
| `DELETE /rest/v1/vault/delete?path=` | 3 | |
| `PUT /rest/v1/vault/quarantine` | 2 | `{path, content}` |
| `POST /rest/v1/vault/promote` | 3 | `{source, destination, reason}` → `{status, promotion_id}` |
| `GET /rest/v1/vault/promotions` | 3 | `?status=pending`; own requests, or all for tier ≥4 |
| `POST /rest/v1/vault/promotions/{id}/approve` | 4 | |
| `POST /rest/v1/vault/promotions/{id}/reject` | 4 | `{reason}` |

## 10. Schedules

| Method & path | Tier | Notes |
|---|---|---|
| `GET /rest/v1/schedules` | 2 | scope-filtered |
| `POST /rest/v1/schedules` | 3 | `{name,repository,workspace,params,cron|interval_s,timezone,enabled}`; validated (`09 §2.1`) |
| `GET /rest/v1/schedules/{id}` | 2 | includes `next_run`, `last_run`, `last_job`, `last_error` |
| `PATCH /rest/v1/schedules/{id}` | 3 owner / 4 | any field; re-times on enable |
| `DELETE /rest/v1/schedules/{id}` | 3 owner / 4 | |
| `POST /rest/v1/schedules/{id}/run-now` | 3 | submit immediately, does not change `next_run` |

## 11. Automations

| Method & path | Tier | Notes |
|---|---|---|
| `GET /rest/v1/automations` | 2 | |
| `POST /rest/v1/automations` | 3 | body is the YAML (`Content-Type: application/yaml`) or `{"yaml": "…"}` |
| `GET /rest/v1/automations/{id}` | 2 | `yaml` verbatim + `config` |
| `PUT /rest/v1/automations/{id}` | 3 owner / 4 | replace document |
| `PATCH /rest/v1/automations/{id}` | 3 owner / 4 | `{enabled}` only — no reparse |
| `DELETE /rest/v1/automations/{id}` | 3 owner / 4 | |
| `POST /rest/v1/automations/validate` | 3 | parse only, return config or errors |
| `GET /rest/v1/automations/{id}/runs` | 2 | with nested deliveries |
| `GET /rest/v1/automations/{id}/runs/{run_id}` | 2 | full context + deliveries |
| `POST /rest/v1/automations/{id}/runs/{run_id}/deliveries/{d}/retry` | 3 owner / 4 | |
| `POST /hooks/{path}` | signed | webhook trigger (`09 §3.1`); 202 on accept, 401 on bad signature, 409 on replay |

## 12. Hosted services

| Method & path | Tier | Notes |
|---|---|---|
| `GET /rest/v1/services` | 2 | scope-filtered for static; all proxy services visible to tier ≥4 |
| `GET /rest/v1/services/{name}` | 2 | |
| `POST /rest/v1/services` | 4 | proxy kind only: `{name, origin_url, visibility, min_tier, insecure_tls}` |
| `PATCH /rest/v1/services/{name}` | 4 | visibility, min_tier, origin_url (proxy) |
| `DELETE /rest/v1/services/{name}` | 4 | static services may also be deleted; the job's artifacts remain |
| `GET /serve/{name}/{path}` | per visibility | `13-hosted-services.md` |

## 13. Audit

| Method & path | Tier | Notes |
|---|---|---|
| `GET /rest/v1/audit` | 1 (own rows) / 4 (scope) / 5 (all) | filters `actor via verb target_kind target outcome governance since until trace_id`; cursor-paged |
| `GET /rest/v1/audit/trace/{trace_id}` | as above | everything under one trace: audit rows, jobs, deliveries |

## 14. Service paths (no prefix, bearer only — never cookie)

| Method & path | Tier | Behaviour |
|---|---|---|
| `GET /stream/{repo}/{ws}?PARAM=…` | 3 | requires `data_streaming`; runs synchronously (`06 §11`), returns the primary output body with its MIME type; `?wait=` seconds ≤ timeout; 504 with `job_id` on timeout |
| `GET /download/{repo}/{ws}?PARAM=…` | 3 | as stream, `Content-Disposition: attachment` |
| `POST /upload/{repo}/{ws}` | 3 | multipart; file fields map to `FILE` params, text fields to others; requires `data_upload`; 202 + job |

These execute a workspace from a URL, so `SameSite=Lax` cookies are refused
here on purpose: a link on any page could otherwise run a workspace as the
signed-in viewer.

## 15. UI shell

| Method & path | Auth | |
|---|---|---|
| `GET /ui/` and `/ui/static/*` | public | shell and assets; no data |
| `POST /ui/signin` | public, lockout | `{name,password}` → sets cookie, returns whoami |
| `POST /ui/signout` | cookie | revokes the session |
