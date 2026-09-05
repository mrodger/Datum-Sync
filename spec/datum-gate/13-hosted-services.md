# 13 — Hosted Services

A hosted service is a URL under `/serve/{name}/` that keeps answering after
the job that produced it has ended, or that fronts an application running
somewhere else.

## 1. Two kinds

| Kind | How it is created | What `/serve/` does |
|---|---|---|
| `static` | a job completes with a `service/static`, `service/pwa` or `service/dashboard` output — never by hand | serves files from that job's artifact directory |
| `proxy` | an admin registers `origin_url` — never by a job | reverse-proxies to the origin |

Supervised subprocess services (the original's `service/interactive` and
`service/notebook`) are **not** a kind. A workspace that wants a long-lived
process runs it elsewhere (a container, a systemd unit) and an admin fronts
it with a `proxy` service. The gateway's process model stays one API and N
workers.

## 2. Naming

`service_name` in a manifest output (static) or the request body (proxy):
`^[a-z0-9][a-z0-9-]{0,62}$`, globally unique because the URL contains
nothing else. Collisions are refused at **publish** for static (the name is
owned by `repo/ws` or free) and at **registration** for proxy.

## 3. Static registration

On job completion (`06 §7`), for each `service/*` artifact:

```sql
INSERT INTO hosted_services (name, kind, type, repository, workspace, source_job, path, visibility, min_tier, created_by)
VALUES ($name, 'static', $type, $repo, $ws, $job, $abs_dir, $vis, $tier, $by)
ON CONFLICT (name) DO UPDATE
   SET type=EXCLUDED.type, source_job=EXCLUDED.source_job, path=EXCLUDED.path, updated_at=now()
 WHERE hosted_services.kind = 'static'
   AND hosted_services.repository = EXCLUDED.repository
   AND hosted_services.workspace  = EXCLUDED.workspace
RETURNING name;
```

No row returned ⇒ the name is held by someone else ⇒ the job is finished
`failed` with a reason. `path` is `job_dir/{artifact}` resolved and checked
`is_relative_to(job_dir)` and `is_dir()` before it is stored.

**The artifact is the site.** Nothing is copied to a webroot; re-running
swings `path` to the new job's directory; the previous job's directory is
untouched (rollback = `PATCH` `source_job` back, exposed in the UI as
"revert to previous run"). The retention sweeper never deletes an artifact
directory a service points at.

Visibility defaults come from the manifest output:
`{"name":"viewer","type":"service/static","service_name":"scimac-viewer","visibility":"tier","min_tier":2}`
(default `principal`). Only an admin can later change it.

Deregistration: `--prune` removing the workspace deletes its services.
`DELETE /rest/v1/services/{name}` removes the row and leaves the artifacts.

## 4. Proxy registration

`POST /rest/v1/services` (tier ≥4):

```json
{"name": "catalogue", "origin_url": "http://192.168.88.102:3027", "visibility": "public",
 "insecure_tls": false}
```

- `origin_url` scheme http/https, no path, no query.
- Private-range origins are **allowed** for proxy services (that is their
  purpose — fronting LAN apps); this is the one egress path exempt from
  the public-address rule, and it is admin-only and audited.
- `insecure_tls: true` skips certificate verification for that origin and
  logs a warning per request.

## 5. Serving

`GET /serve/{name}` → 308 to `/serve/{name}/`. `GET /serve/{name}/{path:path}`:

1. look up the row; 404 if none
2. visibility:
   - `public` → no auth
   - `principal` → any authenticated principal (bearer **or** cookie —
     this is the one service-shaped path that accepts the cookie, because
     it executes nothing) whose scope includes the owning repository (static)
     or any principal (proxy)
   - `tier` → as `principal` plus `tier >= min_tier`
   - unauthenticated → 401 with `WWW-Authenticate` for API clients, or a
     302 to `/ui/#signin?next=` when `Accept` prefers HTML
3. static: `target = (root / path).resolve()`; directory → `index.html`;
   `target.is_relative_to(root)` else 404; `FileResponse` with
   `Cache-Control: no-cache` and `ETag` from mtime+size. Never lists
   directories.
4. proxy: stream the request to `origin_url + path + query` via the shared
   httpx client (`trust_env=False`, `follow_redirects=False`, 30s connect /
   300s read). Forward method, body, and headers except hop-by-hop and
   `Authorization`/`Cookie` (the gateway's credentials must not leak to the
   origin). Add `X-Forwarded-For`, `X-Forwarded-Proto`, `X-Forwarded-Host`,
   and `X-DG-Principal: {name}` / `X-DG-Tier: {n}` when authenticated so the
   origin can do its own coarse authorisation. Stream the response back
   with status and headers (minus hop-by-hop). WebSocket upgrade is
   supported via the ASGI `websocket` scope with a bidirectional pump
   (needed for notebook-style origins).
5. audit `serve.read` (static) / `serve.proxy` (proxy) with `target=name`
   and the sub-path in `detail` — one row per request; the audit writer
   batches to keep this cheap.

## 6. PWA and dashboard types

`service/pwa` and `service/dashboard` serve exactly like `service/static`.
The type is recorded so the UI can badge them and so a dashboard (which
calls `/rest/v1/` from the browser) is understood to need the session
cookie — which `/serve/` and `/rest/v1/` both accept.

`service/dashboard` outputs get `Content-Security-Policy: default-src 'self'; connect-src 'self'`
injected on HTML responses so a dashboard cannot exfiltrate a session to a
third-party origin. `service/static` and `service/pwa` get
`default-src 'self' data: blob:`.

## 7. PWA and installable web apps

`service/pwa` exists so a workspace can publish an app that installs on a
phone's home screen. The serving rules that make that work, all enforced
by `serve.py`:

- `*.webmanifest` and `manifest.json` are served as
  `application/manifest+json`; `sw.js` / any `*.js` as `text/javascript`
  with `Service-Worker-Allowed: /serve/{name}/`.
- The publish gate warns (not fails) when a `service/pwa` output's
  manifest lacks `start_url` and `scope` equal to `/serve/{name}/`, or
  lacks a 192px and a 512px icon — the four things iOS needs to install
  rather than bookmark. A `service/pwa` workspace can ask the child helper
  `datumgate_child.pwa.write_manifest(dir, name, title, icons, theme)` to
  emit a correct one.
- No cookie-only content behind the install path: iOS installs from a
  standalone context, so a PWA meant for phones should be `public`
  visibility or use a bearer token stored by the app itself; `principal`
  visibility works in Safari and in the installed app on the same device,
  but the install flow must have been performed while signed in.
- **Secure context.** `localhost` counts as secure on the machine running
  the gateway, so a developer's Mac can install from
  `http://localhost:8200/serve/{name}/`. A phone on the LAN reaching
  `http://192.168.x.x` is *not* secure and will bookmark, not install.
  For phones use the HTTPS `PUBLIC_URL` (the tunnel); service workers and
  install both require it.
- Offline: the service worker is the app's own; the gateway serves
  `Cache-Control: no-cache` with `ETag` so the worker's fetch-then-cache
  strategy revalidates cheaply.

## 8. Dashboards

`service/dashboard` is a static bundle that calls `/rest/v1/` from the
browser. Because `/serve/` and `/rest/v1/` both accept the session cookie,
a signed-in person opening the dashboard needs nothing else; an agent or
script opening it programmatically sends its bearer token to the same
endpoints. The dashboard inherits the viewer's grant — it cannot show a
job or a vault path the viewer could not fetch directly. `connect-src
'self'` in its CSP means it cannot send that session anywhere else.

## 9. Guards

- a job cannot register a name owned by another workspace (WHERE clause)
- `/serve/` cannot escape its root (resolved containment, symlink-safe)
- proxy never forwards `Authorization`/`Cookie`
- `/stream` and `/download` refuse cookies; `/serve` accepts them
- a `tier`-visibility service refuses a lower tier with 403, not 404
