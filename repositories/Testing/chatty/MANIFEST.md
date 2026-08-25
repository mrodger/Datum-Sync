# chatty

## Purpose
Emits `COUNT` log lines as fast as it can, then returns a one-line artifact. It
is a test fixture, not a useful workspace: it exists so the SSE replay window
can be exercised deliberately. That window — between an event reader installing
its listener and finishing its read of `job_log` — is about a millisecond wide,
so a workspace logging once a second almost never puts an event inside it and a
reader with no de-duplication passes by luck. This one makes the window certain
to contain events arriving by both routes. It does no I/O and reaches nothing
outside the process.

## Dependencies
None. No connections, no filesystem, no network. `COUNT` alone determines what
it does, so it cannot fail for environmental reasons.

## Dependents
The event and streaming tests, and the `job_submitter` service. What they rely
on is the log volume being high enough to overlap a reader's setup, so lowering
`COUNT` to a small number quietly removes the property the fixture exists to
provide. The `out` output is `primary`, so it is the body `/stream/` returns.

## Failure modes
- `COUNT` not an integer → rejected at parameter validation, before queueing.
- A very large `COUNT` hits `timeout_seconds` (120) and is killed by the worker;
  the job ends `failed`, with the log lines emitted up to that point retained.
- No other failure path. With a valid `COUNT` it always completes.

## Behavioral contracts
- No side effects. Writes nothing, sends nothing, mutates no external state.
- Safe to retry: identical parameters produce identical output.
- Emits `log` events only — no `progress`, so a consumer that waits for progress
  before showing anything will show nothing for this workspace.
