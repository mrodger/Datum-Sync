# site

## Purpose
Builds a two-file static site and publishes it as a `service/static` output, so
that the one artifact shape nothing else produces — a *directory* — is exercised
end to end. Every other Testing workspace returns a file, and a file is all the
artifact path was ever asked to carry until hosted services existed. This one
returns a built tree and is therefore the only fixture that runs
`child._store`'s copytree, `runner._reconcile`'s service⇔directory check,
`services.register`'s containment check and a GET on `/serve/`.

It writes a nested `assets/site.css` as well as an `index.html`. A static server
that only ever handles `/` looks entirely correct right up until someone links a
stylesheet, so the fixture links one.

## Dependencies
None. No connections, no network, no filesystem beyond a temporary directory of
its own making. It builds into `tempfile.mkdtemp()` rather than the job's
artifact directory deliberately: a real workspace builds wherever its toolchain
puts things, and the copy *out* of that place into the job's artifacts is the
behaviour under test.

## Dependents
`tests/test_services.py`, and the publish gate's service checks. Three properties
are relied on:
- the output is named `_fixture-site`, which is the path segment `/serve/` is
  requested at;
- `HEADING` reaches the rendered page, proving parameters survive the trip into a
  directory artifact and not merely into a text one;
- `assets/site.css` exists, so a request for a subpath can be distinguished from
  a request that fell back to the index.

## Failure modes
- `HEADING` missing → it has a default, so this cannot happen through the API.
- The returned directory is not published as `service/static` in the manifest →
  `_reconcile` fails the job. That is the correspondence check, and this fixture
  is the only thing that can trip it.
- A service name already held by another workspace → refused at publish, not at
  run time; `sync` reports it and the workspace is not registered.

## Behavioral contracts
- Side effects: creates a temporary directory per run and does not remove it.
  Left deliberately — deleting it would race the copy that is the point of the
  fixture, and the tree it leaves is small and lives under the system temp dir.
- Safe to retry, and *meant* to be re-run: re-running is how a hosted site is
  updated, so the second run's `register` takes the upsert branch rather than the
  insert branch. The URL swings to the new job's artifacts; the old job's
  artifacts are left alone.
- Not idempotent in the strict sense: each run writes a new directory and moves
  the service's `path` to it. The *served content* is identical for identical
  parameters.
