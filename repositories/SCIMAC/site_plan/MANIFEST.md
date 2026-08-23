# site_plan

## Purpose
Produces a geotechnical site plan summary for a Stratum job: an HTML report
(primary) and a JSON summary. It does **not** render maps, query LiDAR, or
export PDFs — those arrive once the connection store lands and this workspace
can reach StratumDB and the GIS toolchain.

## Dependencies
None. Outputs are derived from parameters alone. This is deliberate: it lets
the loader and job engine be exercised before build step 8 (Connections)
exists. A `StratumDB` read connection will be declared here at that point.

## Dependents
Called by the job submitter, data streaming and data download services. The
contract they rely on is the `report` output being marked `primary` — that is
the body `/stream/` and `/download/` return.

## Failure modes
- Missing `JOB_ID` → rejected at parameter validation, before the job is queued.
- `OUTPUT_FORMAT` outside `HTML`/`JSON` → rejected at parameter validation.
- The workspace itself has no failure path: with valid parameters it always
  produces both outputs.

## Behavioral contracts
- No side effects. Writes nothing, sends nothing, mutates no external state.
- Safe to retry: identical parameters always produce identical output.
- `OUTPUT_FORMAT` currently labels the summary rather than switching the report
  body; the report is HTML either way. Honest note — not yet a real branch.
