# echo_file

## Purpose
Reads the file behind its `INPUT_FILE` parameter and returns the contents as a
text artifact, prefixed with `LABEL`. It is deliberately the worst case for a
`FILE` parameter: if that parameter carried a filesystem path rather than an
upload id, a caller could name any file the worker can read and get it back in
the response. This workspace is what proves the id is resolved against the
upload store instead of trusted. It does not validate, convert or inspect the
file beyond decoding it as UTF-8.

## Dependencies
The upload store, reached indirectly — the worker resolves `INPUT_FILE` to a
path before `run()` is called, so this workspace never addresses the store
itself. If the id does not resolve, the job fails before this code runs.

## Dependents
The upload and parameter-handling tests, and the `data_upload` service. Two
properties are relied on: the body is returned verbatim, and `LABEL` is echoed
in the first line — the latter proves ordinary text fields of a multipart
request reach `run()`, not merely that the API stored them. The `echoed` output
is `primary`.

## Failure modes
- `INPUT_FILE` missing → rejected at parameter validation; it is `required`.
- An id that does not resolve to a stored upload → the job fails before `run()`,
  with the error naming the parameter.
- Non-UTF-8 content does **not** fail. It is decoded with `errors="replace"`, so
  binary input returns replacement characters rather than an error — a
  deliberate choice to keep the fixture's failure surface about *paths*, not
  encodings.

## Behavioral contracts
- No side effects. Reads one file; writes nothing, sends nothing, deletes
  nothing. The upload is left in place.
- Safe to retry: the same upload id and label always produce the same output.
- Whole-file read into memory, with no size limit of its own — it inherits
  whatever cap the upload endpoint enforces.
