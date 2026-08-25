# slow

## Purpose
Sleeps for `SECONDS`, split into at most ten steps, emitting a `log` and a
`progress` event at each one. A test fixture whose only job is to still be
running when something else needs to look at it — cancellation, timeout
handling, the live progress bar, and the worker's claim of an in-flight job all
need a job that does not finish immediately. It computes nothing.

## Dependencies
None. No connections, no filesystem, no network. Depends only on the event loop
being able to sleep.

## Dependents
The job engine, cancellation and UI tests, plus `job_submitter` and
`data_streaming`. What they rely on is that `progress` climbs monotonically from
0 to 1 and that the run lasts about `SECONDS` — so it is the fixture to use for
anything timing-dependent, and the wrong one for anything asserting on output.
The `out` output is `primary`.

## Failure modes
- `SECONDS` not an integer → rejected at parameter validation, before queueing.
- `SECONDS` above `timeout_seconds` (120) → SIGTERM then SIGKILL from the
  worker; the job ends `failed` having emitted partial progress.
- Cancellation mid-run leaves the job `cancelled`, not `failed`, and the
  artifact is never produced.

## Behavioral contracts
- No side effects. Writes nothing, sends nothing, mutates no external state.
- Safe to retry: identical parameters produce identical output.
- Step count is `max(1, min(SECONDS, 10))`, so `progress` has at most ten
  increments however long the run is. Do not use it to test fine-grained
  progress resolution.
