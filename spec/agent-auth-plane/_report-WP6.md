# Report — WP6: approval-gated calls, the review queue, activity

**Built:**

| File | Change |
|---|---|
| `migrations/022_pending_calls.sql` | `pending_calls` per `06 §022`, plus `reason` and a requester index |
| `datum_sync/pending.py` (new) | `request` (snapshot taken, `federate.pending` audited), `approve` (sponsor-or-4, pending-and-unexpired, forwards **once** under the snapshot, stores the result, `federate.approve` + `federate.execute`), `reject` (`federate.reject`, outcome `denied`), `expire` for the tick, `for_requester`; REST `GET /rest/v1/pending-calls[?status=decided]`, `POST …/{id}/approve`, `POST …/{id}/reject`; the MCP handle (`status: pending_approval`, `pending_id`, `label`, `expires_at`, `isError: false`) |
| `datum_sync/mcp.py` | a guarded call whose approval label the block lists is parked instead of refused; `execute_approved` re-runs the guards against the snapshot's block and forwards; built-ins `pending_status`, `pending_result` (requester only; rejected or expired → `access_denied`) |
| `datum_sync/review.py` (new) | `GET /rest/v1/review`: pending enrolments, pending elevations, pending calls, overdue reviews, inactivity restrictions, stale upstreams, name clashes — each `{kind, age, principal, label, link, at}`, oldest first, a sponsor's subtree only below tier 4 (upstream rows are tier 4); `GET /rest/v1/principals/{name}/activity?window=7d|30d`: totals, denied, governance, sessions, last seen, by surface, by resource kind; top targets at tier 4 only |
| `datum_sync/lifecycle.py` | `daily()` expires pending calls (`expired_calls`) |
| `datum_sync/api.py` | routers; `/health` adds `pending_approvals` (elevations + calls) and `sessions_live` |
| `datum_sync/config.py` | `POLICY_PENDING_CALL_TTL_HOURS` (72) |
| `datum_sync/static-v2/app.js` | Approvals › Calls (arguments shown, approve-and-run, reject with reason, recent decisions); Review screen (`#/review`, tier 3, kind chips, Open links); dashboard "Needs review" card; Activity panel on the Principal screen (7d / 30d) |
| tests | `test_pending.py` (6); `test_federation.py`'s held-call test now expects the handle; smoke renders Review and the dashboard card |

**Acceptance (09 WP6):**

| Item | Evidence |
|---|---|
| `vm__restart_service` → pending; approve → forwarded once under the snapshot after the requester was narrowed; result for the requester only; reject → `access_denied`; expiry by the tick | `test_a_held_call_is_not_forwarded_until_approved_and_then_once`, `test_an_approved_call_runs_under_the_requesters_snapshot`, `test_an_expired_call_cannot_be_approved` |
| `/rest/v1/review` lists a pending enrolment, elevation, call, an overdue review and a stale upstream with resolving links | `test_the_review_queue_lists_every_kind_with_a_link` |
| Guards FED-009, FED-010, REV-001, REV-002, REV-003 | registered and proven |

**Deviations:**

- Approval forwards the call from the approver's request (the REST route), not from a worker: the approver waits for the upstream, which is the honest place for the wait, and the result is on the row before the response returns. A slow upstream is bounded by the call timeout.
- The activity rollup is computed live from `audit_log` (`09 WP6` allows a rollup table only if the 90-day query exceeds 500 ms on demo data; it does not).
- `restricted_reason` is free text written by the tick (`inactive for N days`); the queue matches its prefix rather than adding a column.
- `sessions_live` on `/health` counts sessions across every principal within the idle window.
