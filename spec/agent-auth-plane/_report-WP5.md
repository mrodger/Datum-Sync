# Report — WP5: federation

**Built:**

| File | Change |
|---|---|
| `migrations/021_federation.sql` | `connections.type` gains `mcp`; `connections.federation_status`; `federated_tools`; `federated_resources` |
| `datum_sync/federation/client.py` (new) | streamable-HTTP MCP client on httpx: `initialize`, `tools/list`, `resources/list`, `resources/templates/list`, `tools/call`, `resources/read`; `Mcp-Session-Id` carried; SSE or JSON responses; auth injected from the connection alone (`proxy.inject_auth`, a static `Authorization` in `config.headers` is dropped); SSRF through `proxy.validate_upstream_url` unless `allow_private_origin`; 5 s list / 60 s call timeouts; results capped at 1 MiB and scrubbed of the upstream secret; upstream `_meta` dropped |
| `datum_sync/federation/guards.py` (new) | the grammar of `05 §4.1`: `tools` globs, `checks` with `path` / `join` / `const` values over a restricted JSONPath (`$`, `.name`, `[*]`, `[n]`), `optional`, `requires` (`write`, `share`, `tier`, `approval`), `commands` deny-then-allow with a 1 KiB regex cap, `resolve: drive_folder`; `resource_guards` with a regex group; validation against the cached schema (`schema_warnings`); pure, async only for the resolver |
| `datum_sync/federation/catalogue.py` (new) | `refresh` (initialize + lists → upsert; clashes with workspace tools and other upstreams dropped and audited `federate.name_clash`; guard mismatches audited `federate.guard_mismatch`; `federation_status` written; failures audited `federate.unavailable`); `refresh_due` for the worker tick; `block_for`, `visible_tools`, `visible_resources`, `lookup`; `health` (ok / stale / down) |
| `datum_sync/federation/routes.py` (new) | `GET /rest/v1/federation[?as=principal]` (status, coverage, clashes, warnings, tools with matching guards and, as a principal, visible / why not), `POST /rest/v1/federation/{name}/refresh`, `GET /rest/v1/federation/profiles` |
| `datum_sync/federation/profiles/*.json` (new) | `github-mcp`, `gitea-mcp`, `local-git-mcp`, `ssh-mcp`, `gdrive-mcp` |
| `datum_sync/connections.py` | type `mcp` (`url` required; `transport`, `resource_kind`, `tool_prefix`, `default`, `refresh_seconds`, `timeout_seconds` validated; guards compiled at save; `allow_private_origin` needs the saver at tier 5; `auth_inject` allowed); `federation_status` in `_COLUMNS` and `public()`; `test()` initialises the upstream |
| `datum_sync/mcp.py` | `tools/list` merges the cached catalogue for the principal's blocks (local names win); `tools/call` resolves a federated name after the workspace catalogue and runs `05 §4.2` (block, tier, allow/deny, guards, size cap, `federate.call` audited before forwarding with guarded values only, `federate.denied`, `federate.result`); `resources/list`, `resources/templates/list`, `resources/read` with `datum://{connection}/{uri}`; an approval-gated call answers `APPROVAL_REQUIRED` until WP6 |
| `datum_sync/worker.py` | `_tick_federation` every `FEDERATION_TICK_SECONDS`, refreshing upstreams past their `refresh_seconds` |
| `datum_sync/api.py` | an mcp connection is refreshed on save; `/health.federation`; connection writes pass the caller's tier |
| `datum_sync/grants.py` | `paths.read/write` narrow on every block, not only `compute` (a code block's `paths.write` governs `push_files`) |
| `datum_sync/config.py` | `FEDERATION_REFRESH_SECONDS`, `FEDERATION_TICK_SECONDS`, `FEDERATION_LIST_TIMEOUT_SECONDS`, `FEDERATION_CALL_TIMEOUT_SECONDS`, `FEDERATION_MAX_RESULT_BYTES`, `DRIVE_RESOLVE_TTL_SECONDS` |
| `datum_sync/static-v2/app.js` | Connections form: type `mcp` with a profile picker that fills `config`; connection detail shows the catalogue status; MCP Servers screen rebuilt over `/rest/v1/federation`: status badge, tool count, coverage, last refresh, refresh button, clashes, warnings, cached tools with their guards, and an "as principal" form that says why a tool is hidden |
| tests | `tests/mock_mcp_server.py` (github-, ssh- and drive-shaped tools, a request counter, a `DOWN` switch, runnable under uvicorn); `tests/test_federation.py` (22); `browser_smoke.py` saves a connection from a profile against the mock, reads the MCP Servers screen and the "as principal" reason (skipped with a note below tier 5) |

**Acceptance (09 WP5):**

| Item | Evidence |
|---|---|
| `gh__push_files` on `datum/gateway` forwarded; on `other/repo` denied with zero upstream requests; `write:false` denied; `gh__delete_repository` not listed | `test_a_value_outside_the_grant_is_denied_before_any_upstream_request`, `test_requires_write_is_enforced`, `test_tools_list_is_the_block_filtered_cache` |
| `vm__run_command` with `sudo ls` denied by the deny regex; `ls -la` allowed; missing `host` denied | `test_commands_and_hosts_on_the_compute_block`, `test_commands_are_denied_before_allowed` |
| `drive__get_file` outside `folders` denied; resolver failure denied | `test_drive_files_resolve_to_their_folder_and_failures_deny` |
| upstream stopped: `tools/list` from the cache within 5 s; a call returns `isError UPSTREAM_UNAVAILABLE`; `last_error` set | `test_an_upstream_that_is_down_lists_from_cache_and_fails_the_call_fast` |
| a federated tool named like a workspace tool is dropped with an audit row | `test_a_clash_with_a_workspace_tool_drops_the_upstream_tool` |
| `federate.call` detail carries `{"repos": "datum/gateway"}` and nothing else from the arguments | `test_a_value_outside_the_grant_is_denied_before_any_upstream_request` (the file body is asserted absent) |
| Guards FED-001…008, FED-011…020 | registered and proven; FED-009/010 are WP6 |

**Deviations:**

- `resource_guards` live on the connection's config beside `guards`, not on the principal's block (`05 §4.5` says "the block's"). Guards are the operator's description of the upstream's URI shapes; the block is what a principal may reach. Same split as tool guards. (D-37)
- A guard `check` against a grant field the block does not define denies — a code block with no `paths.write` refuses `push_files` with files. The github profile says so, and `grants.narrows` now narrows `paths` on every block so an operator can grant it. (D-38)
- An approval-gated call (`requires.approval` with the label in the block's `approval_required`) is refused as `APPROVAL_REQUIRED`, audited `federate.denied`, until WP6 builds `pending_calls`. Nothing is forwarded.
- The federated part of the browser smoke needs a tier-5 account, because the mock upstream is on loopback and `allow_private_origin` is tier 5 by FED-018; below that the pass prints a note and the denied-call path is left to `test_federation.py`.
- `resources/read` is a JSON-RPC error on a refusal rather than a tool result, because the method has no `isError` shape; the audit row is `federate.denied` either way.
- `tool_prefix` may start with `_` (the spec's pattern did not say); the test fixtures' naming convention needed it and nothing is lost.
