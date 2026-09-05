# 06 — Job Engine

## 1. Overview

```
 submit ──► jobs(queued) ──NOTIFY jobs──► worker.claim ──► jobs(running, lease)
                                              │
                                              ▼
                                        runner.Runner ──spawn──► child harness ──import──► main.py
                                              ▲   NDJSON events on stdout        │
                                              └──────────────────────────────────┘
                                              │
                                    heartbeats lease every 10s
                                              │
                                              ▼
                           jobs.finish(complete|failed|cancelled) ──NOTIFY──► SSE readers,
                                                                             automations tick
```

Everything durable is in Postgres. `pg_notify` accelerates; polling
guarantees.

## 2. Submit

`jobs.submit(conn, principal, repo, ws, params, *, triggered_by, parent_job,
idempotency_key, priority)`:

1. Resolve the workspace's **current version**; 404 if none.
2. `principal.allows_repo(repo)` else 403; `principal.requires_tier(3)` for
   direct submission (the worker acting for a schedule uses `system:worker`
   intersected with the schedule owner's frozen grant — `09 §2`).
3. Validate and coerce `params` against the version's manifest (400 on any
   unknown, missing or malformed parameter — never silently dropped).
4. Enforce `limits.jobs_per_hour` and `limits.concurrent_jobs` by counting
   rows → 429 `LIMIT_EXCEEDED` with `Retry-After`.
5. If `idempotency_key`: look up `(key, principal_id)`; if found, return the
   existing job (200 semantics at the API layer, `Location` to it).
6. In one transaction: `INSERT jobs` with `effective_grant = principal.effective`,
   `version_id`, `trace_id = principal.trace_id`; insert the idempotency row
   with `ON CONFLICT DO NOTHING RETURNING` and roll back on a lost race
   (then return the winner).
7. `NOTIFY jobs, '{"job_id":…,"status":"queued"}'` (delivered on commit).
8. audit `job.submit`.

## 3. Claim

```sql
WITH next AS (
  SELECT id FROM jobs
   WHERE status = 'queued'
   ORDER BY priority DESC, submitted_at
   FOR UPDATE SKIP LOCKED
   LIMIT 1
)
UPDATE jobs j
   SET status = 'running', started_at = now(), attempt = attempt + 1,
       lease_worker = $1, lease_until = now() + $2::interval
  FROM next WHERE j.id = next.id
RETURNING j.*;
```

Inside the same transaction: `NOTIFY jobs … "running"`. If the claimed job's
`version_id` is NULL or its version is `withdrawn`, finish it `failed` with
`workspace version withdrawn before the job ran` and continue.

`concurrent_jobs` for the submitting principal is not re-checked at claim —
it is a submission-time admission control.

## 4. Leases, heartbeats, reaper

- Lease length `JOB_LEASE_SECONDS` (default 60). The worker heartbeats each
  running job every `JOB_LEASE_SECONDS/6` seconds: `UPDATE jobs SET lease_until = now() + … WHERE id = $1 AND lease_worker = $2`.
  If the update affects 0 rows the worker has lost the job (another worker
  reaped it): it kills the child and does not write a terminal status.
- The worker also heartbeats its `workers` row every 10s.
- **Reaper** (runs on every worker's poll tick, guarded by a short advisory
  lock so only one runs at a time):

```sql
UPDATE jobs SET status = 'queued', lease_worker = NULL, lease_until = NULL,
                started_at = NULL
 WHERE status = 'running' AND lease_until < now() AND attempt < max_attempts
RETURNING id;
-- and, for those out of attempts:
UPDATE jobs SET status = 'failed', completed_at = now(),
                lease_worker = NULL, lease_until = NULL,
                error = 'worker lease expired after ' || attempt || ' attempt(s)'
 WHERE status = 'running' AND lease_until < now() AND attempt >= max_attempts
RETURNING id;
```

  Each requeued/failed job gets a `job_log` warn line and a NOTIFY.
  `DELETE FROM workers WHERE heartbeat_at < now() - interval '5 minutes'`.

This replaces a single-worker advisory lock with a design that allows N
workers. With one worker it behaves identically to the original.

## 5. Worker process

`python -m datumgate worker [--concurrency N] [--once] [--tags a,b]`

Loop (poll interval `WORKER_POLL_SECONDS`, default 5, woken early by NOTIFY
on `jobs` and `deliveries`):

1. heartbeat `workers` row
2. reaper (§4) — advisory-locked
3. schedules tick (`09 §2`)
4. automations tick: consider finished jobs with `automations_at IS NULL`
   (`09 §5`)
5. deliveries: claim and execute due rows up to concurrency (`09 §6`)
6. jobs: claim while `running < concurrency`
7. wait for NOTIFY / task completion / poll timeout

Signals: `SIGTERM`/`SIGINT` → stop claiming, wait up to
`WORKER_DRAIN_SECONDS` (default 30) for running jobs, then cancel them
(they will be requeued by the reaper on the next worker's tick, as
`attempt` permits).

`--tags`: reserved for routing (a job may carry `tags` in a future manifest
key); accepted and ignored in this build.

## 6. Runner and child protocol

`runner.Runner(job_row, version, params, connections, artifact_dir, sink)`:

- spawns `[<interpreter>, "-m", "datumgate_child"]` — the child harness is a
  **separate top-level package** (`datumgate_child/`) so that `PYTHONPATH`
  can point at it alone and the gateway package is not importable from the
  workspace. Interpreter = workspace venv python if present, else
  `sys.executable`.
- `stdin` ← spec JSON: `{workspace_path, artifact_dir, params, connections, context}`
- `stdout` → NDJSON events; `stderr` → job log at `info`
- `start_new_session=True`, `preexec_fn` sets `PR_SET_PDEATHSIG`
- timeout = `manifest.timeout_seconds`; on expiry SIGTERM the group, wait
  `KILL_GRACE_SECONDS` (5), SIGKILL, reap.

Child harness (`datumgate_child/__main__.py`):

1. `event_fd = os.dup(1); os.dup2(2, 1); sys.stdout = sys.stderr` — before
   any workspace code runs
2. read spec from stdin, `os.chdir(workspace_path)`
3. load `main.py` by explicit `importlib.util.spec_from_file_location`
   (never `sys.path`), find `run`
4. `await run(params, emit, connections)`
5. store each returned artifact into `artifact_dir` (`content` written;
   `path` file copied; `path` directory copytree'd with `symlinks=False`);
   artifact names must be a bare filename, not start with `.`, ≤128 chars
6. write `{"event":"result","artifacts":[{name,file,dir,size,sha256}]}`
7. exit 0; on any exception write `{"event":"error","message":traceback}` and exit 1

Event lines: `{"event":"emit","type":…,"payload":…}` | `result` | `error`.

Runner reconciliation (`_reconcile`), before `finish`:

- every returned artifact name is a declared output; **undeclared → failed**
- a `service/*` output must be a directory; a non-service output must not be
- every `primary` output must be present if the job is `complete` — a
  missing primary is a failure (a `/stream` caller would otherwise get 200
  with nothing)
- descriptor `type` and `primary` come from the manifest, never from the
  child

## 7. Finish

`jobs.finish(conn, job_id, status, artifacts, error)`:

```sql
UPDATE jobs SET status=$2, completed_at=now(), artifacts=$3, error=$4,
                lease_worker=NULL, lease_until=NULL
 WHERE id=$1 AND status='running' AND lease_worker=$5
```

Zero rows affected means the job was reaped; the worker logs and moves on.
Then NOTIFY, then `hosted_services.register` if complete and any
`service/*` artifacts (`13 §3`) — a registration failure flips the status to
`failed` before commit, so a job is never `complete` with an unpublished
site. Audit `job.finish`.

## 8. Cancel

`DELETE /rest/v1/jobs/{id}` or MCP `job_cancel`:

- queued → `UPDATE … SET status='cancelled' WHERE status='queued'`; NOTIFY
- running → `NOTIFY jobs_control, '{"job_id":…,"action":"cancel","worker":<lease_worker>}'`;
  the leasing worker terminates the child and finishes `cancelled`. If the
  worker is dead, the reaper handles it on lease expiry; the API returns
  `{"result":"signalled"}` either way.
- terminal → 409 `ALREADY_TERMINAL`

Only principals who may see the job (repo in scope) and are tier ≥3, or the
submitting principal itself, may cancel.

## 9. Resubmit

`POST /rest/v1/jobs/{id}/resubmit` creates a new job with the same
`repository/workspace`, **the current version** (not the original's — a
resubmit is "run it again", and if the caller wants the old version they can
say so with `?version_id=`), the same params, `parent_job = original`,
`triggered_by = 'resubmit:{original}'`, and the **caller's** effective grant
(never the original's frozen one).

## 10. Streaming (SSE)

`GET /rest/v1/jobs/{id}/events` (cookie or bearer):

```
LISTEN jobs                       -- first, so nothing is missed
status frame from the row         -- {status, error, artifacts, started_at, completed_at}
replay job_log after Last-Event-ID (id: on each log frame)
if terminal: close
loop: queue.get(timeout=15s) → log | progress | status frames; ': keepalive' on timeout
```

Status frames are always built by re-reading the row, so one event name has
one shape. Progress frames are pass-through and not durable.

## 11. Synchronous execution helper

`execute.run_sync(principal, repo, ws, params, service, wait_seconds)`:
submit, then `await_job` on a dedicated LISTEN connection (`LISTEN` before
reading the status). Returns the row, or raises `504 TIMEOUT` carrying the
`job_id` so the caller can poll — the MCP surface turns that into a job
handle (`11 §5`). Refuses with `503 NO_WORKER` if `workers` has no row with
a heartbeat in the last 30s.

## 12. Artifacts

- Directory: `DATA_PATH/jobs/{job_id}/`
- `GET /rest/v1/jobs/{id}/artifacts/{name}` resolves with `Path.resolve()`
  and `is_relative_to` containment; 404 on anything else.
- `artifacts.sweep()` (worker, daily): delete artifact directories of
  terminal jobs older than `retention.job_artifact_days` **unless** a
  hosted service points into them; delete `job_log` rows older than
  `retention.job_log_days`; delete uploads older than `retention.upload_days`
  that were never consumed. Jobs rows themselves are never deleted by the
  sweeper.

## 13. Uploads

`POST /rest/v1/uploads` (multipart, tier ≥3, ≤`MAX_UPLOAD_BYTES`) writes
`DATA_PATH/uploads/{uuid}/{safe_name}` and an `uploads` row; returns the id.
A `FILE` parameter's value is that id. The worker resolves ids to paths just
before the run and marks `consumed_by`. An id belonging to another principal
is refused at submit (403) — the `uploads.principal_id` check is what stops
one principal feeding another's file into a workspace.

## 14. Invariants the tests must prove

- Killing the worker with SIGKILL mid-run leaves the child dead and the job
  requeued within one lease period, then completed by a fresh worker.
- Two workers never both run the same job.
- A job submitted while no worker was alive runs when one starts.
- A job whose workspace was re-published mid-queue runs the version it was
  submitted against.
- `finish` after reaping writes nothing.
- The child cannot open the gateway database (no URL in its environment;
  the test asserts the environment).
