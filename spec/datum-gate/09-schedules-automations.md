# 09 — Schedules and Automations

## 1. Two mechanisms, one worker

**Schedules** turn time into jobs. **Automations** turn events into ordered
actions. Both are driven by the worker's poll (`06 §5`), both are rows, and
neither keeps state outside the database.

## 2. Schedules

A schedule row names a workspace, params, and either `cron` or `interval_s`.
`next_run` is the **only** place a due time lives.

### 2.1 Semantics

- Cron is evaluated in the schedule's `timezone` and converted to UTC, so
  "07:00 every weekday" survives DST.
- Intervals are durations; they do not shift with clocks.
- A missed schedule fires **once**; the due time is walked forward past
  now, preserving an interval's phase.
- Re-enabling re-times from now.
- Params are validated against the **current** manifest at write time (400
  on failure) and again at fire time (a mismatch after a republish records
  `last_error` and still advances).
- A schedule whose workspace has been withdrawn records `last_error` and
  still advances; it does not stop, and it does not retry every poll.

### 2.2 Ownership and authority

A schedule has an `owner` — the principal who created it (tier ≥3, repo in
scope). Jobs it submits run as `submitted_by = "schedule:{name}"`,
`principal_id = owner`, and `effective_grant = intersect(owner.effective_now, system:worker.grant)`
computed at fire time. If the owner is disabled, the schedule records
`last_error = 'owner disabled'` and advances. Editing a schedule you do not
own requires tier ≥4.

### 2.3 Firing

```sql
WITH due AS (
  SELECT id FROM schedules
   WHERE enabled AND next_run <= now()
   ORDER BY next_run FOR UPDATE SKIP LOCKED LIMIT 20)
SELECT s.* FROM schedules s JOIN due USING (id);
```

For each: submit (`06 §2`) with `triggered_by = "schedule:{name}"`,
`trace_id = uuid4()`; then `UPDATE schedules SET last_run=now(), last_job=$job, next_run=$next, last_error=$err`.
The submit and the update share one transaction; a crash in between leaves
the row due and re-fires — acceptable, and the reason `Idempotency-Key`
`"schedule:{id}:{next_run}"` is used on the submit so a re-fire returns the
same job.

## 3. Automations

A YAML document, stored verbatim (comments and ordering preserved) alongside
its validated parse.

```yaml
name: site-plan-delivery
enabled: true
trigger:
  type: job_complete            # job_complete | schedule | webhook | email
  repository: SCIMAC
  workspace: site_plan
  status: complete              # complete | failed | cancelled | any
actions:
  - type: email
    connection: SMTPStratum
    to: ["andrew@scimac.co.nz", "david@scimac.co.nz"]
    subject: "Site plan ready — Job {{ params.JOB_ID }}"
    body: |
      Please find the site plan for job {{ params.JOB_ID }} attached.
    attach: [site_plan_pdf]     # artifact names from the trigger job
  - type: http_request
    url: https://hooks.example.com/site-plan
    method: POST
    headers: {Content-Type: application/json}
    body: '{"job": "{{ job.id }}", "status": "{{ job.status }}"}'
  - type: run_workspace
    repository: SCIMAC
    workspace: soil_report_draft
    params: {JOB_ID: "{{ params.JOB_ID }}"}
  - type: vault_write
    path: "dev/reports/{{ params.JOB_ID }}/summary.json"
    from_artifact: summary
```

### 3.1 Triggers

| Type | Fields | Fires when |
|---|---|---|
| `job_complete` | `repository?`, `workspace?`, `status?` (default `any`), `submitted_by?` | a job in scope reaches a terminal status. Absent repository = every repository the **owner** can see (never "all"). |
| `schedule` | `cron` or `interval_s`, `timezone?` | on the clock; the automation gets its own `next_run` in `config` and is ticked like a schedule |
| `webhook` | `secret_connection` (an `http` connection whose secret holds `hmac_key`), `path` (`^[a-z0-9-]{3,63}$`) | `POST /hooks/{path}` arrives with a valid `X-DG-Signature: sha256=<hmac>` over the raw body and a fresh `X-DG-Nonce` (replay window 10 min, `webhook_receipts`) |
| `email` | `connection` (`email_imap`), `folder`, `subject_pattern?`, `from_pattern?`, `poll_seconds` (≥60) | the worker polls the mailbox; each new UID matching the patterns fires once; the UID is recorded in `config._seen_uids` (bounded ring of 5000) |

### 3.2 Actions

| Type | Fields | Delivery kind |
|---|---|---|
| `run_workspace` | `repository`, `workspace`, `params` (templated) | `run_workspace` |
| `http_request` | `url`, `method` (GET/POST/PUT/PATCH/DELETE), `headers`, `body` (templated string), `connection?` (an `http` connection: if given, `url` is a path under its `base_url` and auth is injected — the same proxy path as `07 §6`) | `http_request` |
| `email` | `connection` (`email_smtp`), `to`, `cc?`, `subject`, `body`, `attach?` (artifact names), `html?` bool | `email` |
| `vault_write` | `path` (templated), `content` (templated) **or** `from_artifact` (artifact name), `mode?` | `vault_write` |

### 3.3 Validation (at save, inside `parse()`)

- `yaml.safe_load` only
- unknown keys anywhere are errors
- trigger and every action fully typed; templates checked (§4)
- **self-trigger refused**: an automation with a `job_complete` trigger
  that could match a workspace it also runs
- **owner scope**: the owner must be able to see every repository the
  trigger names and every repository the actions run; a trigger naming no
  repository is scoped to the owner's repositories at fire time
- every named connection must exist, be `global` or scoped to a repository
  the owner can see, and have tier ≤ owner tier
- `vault_write.path` must be permitted by the owner's `vault.write` for the
  *literal* prefix before the first `{{` (templated tails are checked at
  fire time against the rendered path)
- `owner_grant` is frozen at save: actions execute under
  `intersect(owner.effective_now, owner_grant)` — a later widening of the
  owner does not widen an automation written earlier; a narrowing applies
  immediately

### 3.4 Loops

- A job carries `triggered_by = "automation:{name}"`. `consider()` skips an
  automation for a job it caused itself.
- **Chain depth**: a job also carries `parent_job`; `consider()` walks the
  `parent_job` chain up to `policy.automation.max_chain_depth` (default 8)
  and refuses to fire if the same automation name appears anywhere in it.
  This closes the A→B→C→A gap the original documented as known.
- A job that finished before the automation was created is never delivered
  (`automations.created_at <= job.completed_at`).

## 4. Templates

Placeholders `{{ name }}` over a fixed namespace, a dict lookup with no
attribute traversal:

| Namespace | Keys |
|---|---|
| `job.*` | `id repository workspace version status error submitted_by submitted_at completed_at duration_seconds` |
| `params.*` | the trigger job's params |
| `artifact_url.*` | per artifact name, an absolute `PUBLIC_URL` link to it |
| `metrics.*` | values from `metric` events, last-wins by name |
| `trigger.*` | for `webhook`: `body_json.<key>` (one level), `headers.<name>`; for `email`: `subject from date uid` |
| `automation.*` | `name run_id trace_id` |
| `now` | ISO-8601 UTC at fire time |

Unknown placeholders are refused at save. An absent `params.X` renders as
`""`. Rendering is plain substitution; there is no filter syntax and no
expression evaluation. The rendered namespace is stored on
`automation_runs.context` so a delivery can be reproduced and a run can be
debugged after the fact.

## 5. Firing

`consider(conn, job_id)` on the worker's automations tick:

1. `UPDATE jobs SET automations_at = now() WHERE id=$1 AND automations_at IS NULL RETURNING id` — claim first, at-most-once *consideration*
2. for each enabled automation with a `job_complete` trigger that matches,
   created before the job completed, not looping (§3.4), and whose owner is
   not disabled:
   - `INSERT automation_runs (…, context)`
   - for each action index `i`: render into `payload`, `INSERT deliveries
     (run_id, action_index, kind, payload, dedupe_key = "{automation}:{job_id}:{i}") ON CONFLICT DO NOTHING`
3. `NOTIFY deliveries`

A crash between 1 and 2 loses consideration of that job — the same trade
the original makes, chosen because re-consideration would re-fire actions
that already ran. A crash between inserting deliveries loses nothing: the
dedupe key makes the insert idempotent and the claim in step 1 is only
taken after the run row exists (steps 1–2 are one transaction).

Webhook, email and schedule triggers create the run and deliveries the same
way, with `trigger_job = NULL` and `trigger_kind` set; their dedupe key uses
the webhook nonce / email UID / schedule due time.

## 6. Deliveries: the outbox

Deliveries are executed by the worker, at-least-once with backoff:

```sql
WITH next AS (
  SELECT id FROM deliveries
   WHERE status IN ('pending','running')
     AND next_attempt_at <= now()
     AND (lease_until IS NULL OR lease_until < now())
   ORDER BY next_attempt_at FOR UPDATE SKIP LOCKED LIMIT $1)
UPDATE deliveries d SET status='running', attempts=attempts+1,
       lease_worker=$2, lease_until=now()+interval '2 minutes'
  FROM next WHERE d.id=next.id RETURNING d.*;
```

Execute by `kind` under the automation's `intersect(owner.effective_now, owner_grant)`:

| kind | Execution | Idempotency |
|---|---|---|
| `run_workspace` | `jobs.submit` with `Idempotency-Key = dedupe_key`, `triggered_by = "automation:{name}"`, `parent_job = trigger_job` | the key: a retry returns the same job |
| `http_request` | `egress.fetch()` — public-address check on every hop, 15s timeout, ≤64 KiB response stored; success = 2xx/3xx | header `X-DG-Delivery-Id: {dedupe_key}` so receivers can dedupe; otherwise none — HTTP is not idempotent and the doc says so |
| `email` | SMTP via the connection; `Message-ID` derived from the dedupe key | `Message-ID` |
| `vault_write` | `vault.fs.write` under the owner's scope, mode from payload (default `overwrite`) | naturally idempotent |

Outcomes:

- success → `status='done', completed_at=now(), result={…}`
- failure with attempts < max → `status='pending', next_attempt_at = now() + min(2^attempts * 30s, 1h), last_error`
- failure at max → `status='dead'`, `automations.last_error` set, audit `delivery.dead`

Actions in one run are independent: a dead email does not block the
webhook after it. The UI shows each delivery's state under the run.

`POST /rest/v1/automations/{id}/runs/{run_id}/deliveries/{d}/retry` (tier ≥3,
owner or admin) resets a `dead` row to `pending` with `attempts = 0`.

## 7. Egress rules (shared by http_request, proxy, connection tests)

`triggers/egress.py`:

- scheme `http`/`https` only
- resolve host; every address must be global unicast, or the connection
  carries `allow_private_origin` (tier-5-created only)
- manual redirects, max 3 for automations / 5 for proxy, re-checked per hop
- auth headers stripped on cross-origin redirect
- response body capped
- one shared `httpx.AsyncClient` per process with connection limits, no
  proxies from the environment (`trust_env=False`)
