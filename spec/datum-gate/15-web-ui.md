# 15 — Web UI

A single-page application in vanilla JavaScript ES modules, no build step,
no CDN, talking only to `/rest/v1/`. Served from `/ui/`.

## 1. Non-negotiables

- **No `innerHTML`, `outerHTML`, `insertAdjacentHTML`, `document.write`,
  or `eval`/`Function`.** Every node is built with `h(tag, attrs, ...children)`
  where string children become text nodes. A test greps the source.
- **No external origins.** Fonts are named with system fallbacks; icons
  are inline SVG symbols in `icons.js`. The page must render on a host
  with no internet route, and no third party can serve script into a
  session.
- **CSP** on the shell: `default-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'`.
- The shell is public and contains no data; it asks `/rest/v1/whoami` and
  renders sign-in or the app.
- Every screen waits on `data-ready` (set by the router after the screen's
  fetch resolves and passes the generation check), never on rendered
  content — the pattern the original documents for its browser smoke test.

## 2. Files

```
datumgate/static/
  index.html        shell: sidebar, topbar, #view, sign-in dialog
  app.js            boot, router, generation counter
  api.js            fetch wrapper (envelope → thrown ApiError with code), SSE helper
  h.js              element builder, fmt helpers (dates in DEFAULT_TIMEZONE, bytes, durations)
  nav.js            sidebar, collapse state (in-memory), active item
  table.js          sortable/filterable table, cursor pager, selection
  forms.js          form builder from a manifest / JSON schema
  icons.js          inline SVG symbol set
  screens/
    dashboard.js repositories.js workspace.js jobs.js job.js
    schedules.js automations.js connections.js services.js
    vault.js promotions.js principals.js principal.js audit.js trace.js
  style.css
```

## 3. Chrome

- **Left sidebar**, collapsible: Dashboard, Repositories, Jobs, Schedules,
  Automations, Connections, Vault, Services, Principals, Audit. Items the
  caller's tier cannot use are hidden, not disabled.
- **Top bar**: product mark, worker health dot (from `/health`, polled
  every 30s), principal badge (name, tier chip, "NO AUTH" red badge when
  `credential_kind == 'dev'`), sign-out.
- **Main view** `#view`, one screen at a time, hash-routed
  (`#jobs`, `#jobs/{id}`, `#workspaces/{repo}/{ws}`, …).

## 4. Screens

| Screen | Contents | Tier |
|---|---|---|
| Dashboard | job counters (24h/7d by status), recent jobs, quick-run tiles for the 8 most-run workspaces in scope, pending promotions count (tier ≥4), dead deliveries count | 1 |
| Repositories | list → repository → workspace cards (name, version, services badges, last run) | 1 |
| Workspace | manifest summary, `MANIFEST.md` rendered (a minimal safe markdown → DOM renderer in `md.js`, no HTML passthrough), parameters form (from `/schema`), Run button (tier ≥3), version history with Activate (tier ≥4), recent jobs | 1 |
| Jobs | table with filters (status, repo, workspace, submitted_by, since), status badges, duration, trace link; cancel on running (tier ≥3) | 2 |
| Job | status header driven by SSE; progress bar only while progress frames arrive; live log with level filter; artifacts (download / open service); params; resubmit; cancel; "open trace" | 2 |
| Schedules | table: enabled toggle, next run (in schedule tz and local), last run, last error; new/edit form with cron helper (next 5 fires preview from a `POST /rest/v1/schedules/preview` endpoint) | 2 / edit 3 |
| Automations | table with enabled toggle; detail: YAML editor (`<textarea>`, monospace, verbatim round-trip), Validate button, runs list with per-delivery state and Retry | 2 / edit 3 |
| Connections | table: type icon, tier chip, scope, last test; detail/form: config fields per type, secret fields write-only with "set/replace/clear", Test button; banner when `/health.secrets == unavailable` | 2 / edit 4 |
| Vault | tree browser over `vault/list` (depth 1, lazy), file view (`vault/read`, paged), write/append editor (tier ≥3), quarantine badge on `.dgq.json` siblings, promote dialog | 1 |
| Promotions | pending list with source/destination/requester/age, Approve/Reject (tier ≥4), history | 3 |
| Services | list: name, kind, type, visibility, owner or origin, last updated, Open link; proxy registration form (tier ≥4); static: "revert to previous run" | 2 |
| Principals | tree (indented by delegation), kind icon, tier chip, disabled state; create form with grant editor (structured: tier select, repo picker, connection pickers, vault pattern lists, promote edges, limits) that validates via `POST /rest/v1/principals/validate` and shows `GRANT_NOT_NARROWER` inline | 4 |
| Principal | effective grant (with the ancestor that narrowed each field, if any), credentials list (mint token → shown once in a copy dialog, revoke), children, recent audit rows | 4 / self |
| Audit | table with filters; governance and denied quick-filters; row click → Trace | 1 |
| Trace | timeline of everything under one trace id | 1 |

## 5. Visual tokens

Kept from the original's brief because they are the product's identity:

| Token | Value |
|---|---|
| Primary | Navy `#1D3A5C` |
| Accent | Amber `#C89632` |
| Background | Dark `#0F1923` |
| Body font | DM Sans, system fallback |
| Heading font | Space Grotesk, system fallback |
| Mono | JetBrains Mono, system fallback |
| Status: queued / running / complete / failed / cancelled | grey / amber / green / red / muted red |
| Tier chips 1–5 | grey, blue, green, amber, red |

## 6. Behavioural rules

- Optimistic updates never: every mutation re-fetches the affected screen.
- All mutations show the envelope `message` on failure, with `code` as a
  chip, and a "copy trace id" affordance.
- The job screen subscribes to SSE with `Last-Event-ID`; on reconnect it
  re-reads status from the first frame rather than trusting local state.
- Tables keep sort/filter state in the URL hash so links are shareable.
- Destructive actions (delete principal, delete connection, reject
  promotion, delete service) require a typed confirmation of the name.

## 7. Tests

- `test_ui_source.py`: greps for the banned APIs and for `http://`/`https://`
  in `static/` other than `PUBLIC_URL`-relative links.
- `browser_smoke.py` (Playwright, against a running server): sign in,
  each screen reaches `data-ready`, run the `Testing/site` fixture, watch
  the job complete over SSE, open the resulting `/serve/`, create and revoke
  a token, create a schedule and see `next_run`, submit and approve a
  promotion, view the trace.
