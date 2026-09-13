# Report — WP4: OAuth elevation

**Built:**

| File | Change |
|---|---|
| `migrations/020_device_flow.sql` | `oauth_device_codes` (hash, user code, scope, decision, approved scope, poll bookkeeping, consumed); `audit_log` outcome check widened to `denied` |
| `datum_sync/device.py` (new) | `POST /oauth/device` (RFC 8628 §3.1, bearer required, JSON or form); the device grant on `/oauth/token` (`authorization_pending`, `slow_down` with a growing interval, `expired_token`, `access_denied`; a consumed code raises `_Reuse` and the family is revoked); `/rest/v1/elevations` list, approve (narrowing only), deny (reason required); `validate_scope`, `clamp_scope`; the gateway's own public client `datum-sync-elevate`, created on first use; `elevate()` for the built-in |
| `datum_sync/oauth.py` | scope validated at authorize (`invalid_scope` on the redirect), never at register; consent page scope picker (`POLICY_CONSENT_SCOPE_PICKER`, on by default, and always when the request named no scope) with what each option unlocks; `on_behalf_of` binds the code to an agent the signer sponsors (or any agent at tier 4), clamped to `min(agent tier, signer tier)`; `oauth.consent` audit row naming both; registration limited to `OAUTH_REGISTER_PER_HOUR` (429 `too_many_registrations`); refresh accepts `scope` and refuses a widening; discovery advertises `device_authorization_endpoint`, `scopes_supported`, `resource_indicators_supported` |
| `datum_sync/lifecycle.py` | `daily()` prunes clients older than `RETENTION_UNUSED_OAUTH_CLIENT_DAYS` with no token, device request or code; `pruned_last_run` |
| `datum_sync/mcp.py` | `elevate` built-in, listed only when `elevation_hints` is non-empty; returns the user code, the verification URL and the polling instructions |
| `datum_sync/api.py` | `device.router`; `/rest/v1/auth/clients` adds `total`, `unused`, `pruned_last_run`, `retention_days` and per-client `device_requests`, `last_grant_at`, `unused` |
| `datum_sync/config.py` | `POLICY_ELEVATION_AUTO_APPROVE_SCOPES`, `POLICY_CONSENT_SCOPE_PICKER`, `DEVICE_CODE_TTL_SECONDS`, `DEVICE_POLL_INTERVAL_SECONDS`, `OAUTH_REGISTER_PER_HOUR`, `RETENTION_UNUSED_OAUTH_CLIENT_DAYS` |
| `datum_sync/static-v2/app.js` | Approvals screen (`#/approvals`, tier 3): Elevations tab with pending requests (scope narrowing select, approve, deny with reason, `?code=` highlight) and recent decisions; Calls tab is the seat for WP6; Authentication Services shows the hygiene counts and per-client usage; Connect panel on the Principal screen, filled in beside a freshly minted token |
| `datum_sync/static-v2/connect-snippets.js` (new) | the onboarding snippets from `12 §4` as data |
| `datum_sync/static-v2/icons.js`, `tools/gen_icons.py` | `approvals` icon |
| tests | `test_elevation.py` (20), conformance cases 3, 4, 6; `browser_smoke.py` elevates the enrolled agent by device code, approves it on the Approvals screen, polls, replays; `conftest.py` resets the registration limiter between tests as it does the login one |

**Acceptance (09 WP4):**

| Item | Evidence |
|---|---|
| device request → pending → approve in UI → token → `effective_tier == 3`; replay → `invalid_grant`, family revoked | `test_the_device_flow_end_to_end`, `test_a_device_code_is_single_use_and_reuse_revokes_the_family`; `browser_smoke.py` principals section |
| scope `mcp` cannot submit a job whatever the tier | TIER-003 (`test_an_oauth_scope_caps_the_effective_tier`); `test_a_pinned_scope_flows_to_the_token` |
| `on_behalf_of` by a non-sponsor → 403, no code | `test_on_behalf_of_needs_the_sponsor_or_tier_4` |
| 31st registration → 429 | `test_registration_is_rate_limited` (limit lowered to 3 by monkeypatch; the window is the same code) |
| Codex-shaped and Claude-Code-shaped logins; replayed refresh | conformance cases 3, 4, 6 |
| Guards ELEV-001…012 | ELEV-002…012 in `break_the_guard.py`; ELEV-001 *is* TIER-003 and is registered once under that id (D-34) |

**Deviations:**

- ELEV-001 is not a second registry entry. The scope cap was built with the tier model in WP1 as TIER-003, and the registry refuses duplicate ids by test; the spec's guard table is satisfied by the earlier id.
- The device poll records itself even when the answer is an error. `exchange_device_code` collects the outcome inside its transaction and raises after it commits, because an `authorization_pending` raised inside the transaction rolled back `last_polled_at` and `slow_down` could never fire (the first version did exactly that, and the ELEV-008 test caught it).
- The gateway's own client `datum-sync-elevate` is created on first use by the REST route as well as the built-in, so a script (or the smoke) needs no registration step to elevate.
- `unused` on a client row and the prune predicate use the same three references (tokens, device requests, codes); a client with an unexchanged code is neither shown as unused nor pruned.
- The registration limiter is module state like the login lockout; the test suite resets both in the same autouse fixture, otherwise the OAuth tests would trip it in collection order.
