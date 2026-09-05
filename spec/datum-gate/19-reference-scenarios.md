# 19 — Reference Scenarios

The build is done when every scenario here runs end to end. They are
written against a small geotechnical firm (kept from the original's case
study so the domain stays familiar) plus the agent fleet the proposal
describes.

## 1. Cast

| Principal | kind | tier | repositories | notes |
|---|---|---|---|---|
| `marcus` | human | 5 | `*` | superuser; bootstraps |
| `simone` | human | 4 | `SCIMAC` | builds and publishes |
| `robert` | human | 3 | `SCIMAC` | consultant on Claude.ai via OAuth |
| `datum-main` | agent | 3 | `SCIMAC`, `Testing` | long-lived agent; vault read `dev/**, shared/**, logs/**`, write `dev/**`, promote `quarantine/research/** -> shared/long_term/**`, proxy `[openai]` |
| `openclaw` | agent | 2 | `SCIMAC` | read `shared/**` |
| `hermes` | agent | 1 | — | read `shared/long_term/**` |
| `drone-*` | agent | 2 | — | children of `datum-main`, created per task |
| `system:worker` | system | 3 | `*` | built-in |

Connections: `StratumDB` (database, tier 2, scope `SCIMAC`, read),
`SMTPStratum` (email_smtp, tier 1, global, write), `openai` (http, tier 3,
global, `auth_inject: bearer`), `SharePoint` (http, tier 2, scope `SCIMAC/reports`).

## 2. Scenarios

### S1 — Bootstrap and first publish

1. Ops runs the bootstrap in `16 §5`: `marcus` exists with a password and a
   token.
2. `marcus` creates `simone` (tier 4, `SCIMAC`) via REST and mints her a
   token; creates the four connections; `StratumDB` test passes.
3. `simone` runs `sync --as simone` on `SCIMAC`: `site_plan`, `core_log`,
   `soil_test_download`, `site_plan_viewer` publish; `soil_report_draft`
   fails the gate because `SharePoint` is scoped to `SCIMAC/reports` and
   the workspace lives in `SCIMAC/core` — the error names the connection and
   the scope. Nothing else is affected.
4. Audit shows `workspace.publish` ×4 and one `denied` `workspace.publish`
   with the reason, all `via=cli`, actor `simone`.

### S2 — A consultant on Claude.ai

1. `robert` adds `{PUBLIC_URL}/mcp` to Claude.ai; the client hits 401,
   follows `WWW-Authenticate` to discovery, registers, redirects him to the
   consent screen; he signs in; a code is exchanged; the access token is
   bound to `robert`.
2. `tools/list` shows exactly the four `SCIMAC__*` tools with `mcp` in
   their services, plus `whoami`, `job_*`, `connection_list`,
   `schedule_*`. No vault tools (his vault scope is empty). No
   `proxy_request`.
3. "Run the site plan for job 70045" → `tools/call SCIMAC__site_plan` with
   `_wait_seconds` default 45 → completes in 12s → HTML inline, JPEG as a
   `resource_link`; `structuredContent.job_id` present.
4. "Get the soil tests for 70023 as a zip" → completes → `resource_link` to
   the zip; he fetches it with the same token via `resources/read`.
5. "Is the core log for 70012 done yet?" → `job_list` filtered by
   workspace → status.
6. An hour later the access token expires; the client refreshes silently.
   Thirty-one days later the refresh token has expired; he re-consents.
7. Audit: every call under a distinct trace; the job rows carry
   `triggered_by = mcp:{client_id}`.

### S3 — Email-only recipients

1. `simone` writes the `site-plan-delivery` automation (`09 §3`): on
   `SCIMAC/site_plan` complete → email `andrew@`, `david@` with the PDF
   attached via `SMTPStratum`; then `run_workspace SCIMAC/soil_report_draft`.
2. Robert's run in S2.3 completes → the worker considers it → one run, two
   deliveries; the email goes out (test SMTP sink captures it with the
   `Message-ID` derived from the dedupe key); the second delivery submits
   the draft job with `parent_job` set.
3. The draft job completes → `report-draft-notify` fires → Robert gets an
   email with `{{ artifact_url.report_docx }}` (an absolute `/rest/v1/…`
   link that requires his token — clicking it in mail opens the UI sign-in
   with `next=`).
4. Kill the worker after the first delivery; restart: the second delivery
   runs; the first is not repeated (status `done`).

### S4 — A long-lived agent, and a drone

1. `marcus` creates `datum-main` with the grant in §1 and a token; the
   agent's MCP config carries it.
2. `datum-main`: `tools/list` includes `vault_read/write/list/search`,
   `quarantine_promote`, `proxy_request`, `delegate_create`, and the
   `Testing__*` and `SCIMAC__*` tools.
3. It reads `dev/config/foo.md` (ok), tries `secrets/key` (403, audit
   `denied`, `governance: false`), tries `skills/planner.md` write (403,
   `governance: true` — policy tier 4 required despite `dev/**` not
   matching anyway).
4. It calls `proxy_request` on `openai` → `POST /v1/chat/completions`; the
   audit row is `proxy.request` with target `openai:POST:/v1/chat/completions`
   and no body.
5. It dispatches a drone: `delegate_create({name: "drone-7f2a", grant: {tier: 2,
   repositories: [], vault: {read: ["shared/**", "quarantine/research/**"],
   quarantine: ["quarantine/research/7f2a/**"], deny: ["secrets/**", "private/**"]}},
   expires_in_seconds: 7200})` → child created, token returned once.
6. The drone (its own MCP session) can `vault_read shared/x`, can
   `quarantine_write quarantine/research/7f2a/notes.md`, **cannot**
   `vault_write dev/anything` (not in its grant), **cannot**
   `quarantine_promote` (no edge), and its `tools/list` reflects that.
7. `datum-main` requests promotion `quarantine/research/7f2a/notes.md ->
   shared/long_term/notes-7f2a.md` → `pending` (policy requires a second
   principal for tier < 5).
8. `simone` sees it on the Promotions screen and approves; the file moves;
   sidecars written; audit has request/approve/apply under two traces.
9. `datum-main` calls `delegate_revoke drone-7f2a` → the drone's token is
   dead; its audit history remains.
10. `marcus` narrows `datum-main` to remove `shared/**` read. The drone
    (if it still existed) would lose it on its next request without any
    write to the drone's row.

### S5 — Scheduled and geospatial

1. `simone` creates a schedule: `SCIMAC/core_log` every weekday 07:00
   `Pacific/Auckland`; the UI preview shows the next five fires; across the
   September DST change the UTC instants shift by one hour.
2. `SCIMAC/site_plan` with a `GEOMETRY` param `AREA`: a WKT polygon is
   accepted; an invalid ring is refused at submit with the PostGIS message.
3. The worker is down for a day; on restart the schedule fires **once**,
   `next_run` is the following weekday 07:00.

### S6 — A hosted viewer

1. `SCIMAC/site_plan_viewer` returns a `service/static` directory named
   `scimac-viewer` with visibility `tier`, `min_tier: 2`.
2. `/serve/scimac-viewer/` answers for `robert` (cookie, tier 3) and for
   `openclaw` (bearer, tier 2); refuses `hermes` (tier 1) with 403;
   refuses anonymous with a redirect to sign-in.
3. Re-running swings the path; "revert" swings it back; the artifact sweep
   after 90 days leaves both directories because the row points at one and
   the other was pointed at within retention (the sweeper only deletes
   directories no service points at).
4. `marcus` registers a proxy service `catalogue` → `http://192.168.88.102:3027`
   public; `/serve/catalogue/` proxies; `Authorization` is not forwarded.

### S7 — Operational failure modes

1. The secret key is removed from the environment: the API starts,
   `/health.secrets == unavailable`, creating a connection with a secret →
   503 `SECRETS_UNAVAILABLE`, the Connections screen shows a banner, jobs
   declaring connections fail at claim with a message naming the key.
2. Postgres restarts: the API returns 503 on `/health` for the interval,
   the worker's LISTEN reconnects, no job is lost.
3. A workspace's `main.py` is edited to raise, `sync` publishes a new
   version (the gate does not run code), the next job fails with the
   traceback in its log, `activate` on the previous version restores
   service without touching disk.
4. An automation's webhook target starts returning 500: deliveries back
   off to hourly, go `dead` after five attempts, `automations.last_error`
   is set, the Dashboard counter shows 1 dead; after the target recovers,
   Retry succeeds.

### S9 — A barebones agent becomes capable

1. A fresh agent process on `vm111` has a model, a loop, and one MCP URL
   with a registration code. It calls `POST /register` with its metadata
   and a requested grant → `pending`, claim code returned.
2. `marcus` sees it in the Review queue with the requested grant beside
   the code's template, trims `compute.hosts` to `vm111`, approves.
3. The agent claims its token, calls `initialize` → `tools/list`: it now
   has `memory_*` on `org/**` (read) and `agents/hermes-2/**` (read/write),
   `gh__*` read tools on `datum/*`, `vm__run_command` on `vm111` with the
   read-only command allow-list, `drive__search`/`drive__get_file` on one
   folder, and the `Testing__*` workspaces. `whoami.capabilities` describes
   exactly that.
4. It searches memory for "site plan conventions", reads two entries,
   reads `datum/gateway/README.md` via `gh__get_file_contents`, runs
   `journalctl -u datumgate-worker -n 50` on `vm111`, and writes a summary
   to `agents/hermes-2/notes/2026-09-05`. Five audit rows, one trace,
   arguments summarised to repo/host/namespace only.
5. It tries `gh__push_files` to `datum/gateway` → denied (`write: false`),
   `vm__run_command "sudo systemctl restart …"` → denied (deny regex),
   `drive__share_file` → not listed. Three `denied` rows; a `denied_burst`
   signal does **not** fire (three < five).
6. Thirty days idle → auto-restricted → its next `tools/list` is
   `whoami`, `memory_get` on its own namespace, and nothing else; it can
   read `agents/hermes-2/_status` to learn why.

### S10 — Reviewing a team's month

1. `simone` (team `geo` admin) opens Review: two overdue reviews, one
   `new_resource` signal (`datum-main` touched `vm102` for the first time),
   one pending approval (`vm__restart_service`), one pending promotion.
2. She approves the restart after reading the arguments; the gateway
   forwards it under `datum-main`'s frozen grant; result stored;
   `datum-main` fetches it with `pending_result`.
3. She reviews `datum-main`: the activity summary shows 400 proxy calls to
   `openai`, 6 code writes to `datum/gateway`, 0 governance touches; she
   narrows `code.write` to false for the next quarter and records the
   review.
4. `GET /rest/v1/review/report?team=geo&month=2026-08` produces the summary
   her manager asked for.

### S8 — Break the guards

`python tests/break_the_guard.py` reports every guard in `17 §2` as PROVEN
on a clean checkout. Deleting any one guard line by hand and running
`pytest -q` fails at least the named test.
