# Report — WP2: lifecycle and enrolment

**Built:**

| File | Change |
|---|---|
| `migrations/018_registration.sql` | `registration_codes`, `registration_claims`; `audit_log.actor_kind` gains `system`. Numbered 018 rather than the spec's 019 (see below) |
| `datum_sync/lifecycle.py` (new) | the state machine (`TRANSITIONS`), `approve/reject/restrict/restore/retire/review` and their routes, `daily()` housekeeping, `reviews_overdue`, `pending_count` |
| `datum_sync/enrol.py` (new) | codes (mint/list/revoke), `POST /enrol`, `POST /enrol/claim`, per-code lockout reusing `auth._locked_for`, anonymous audit rows |
| `datum_sync/auth.py` `audit.py` | `SYSTEM_PRINCIPAL` (kind `system`) for the worker's housekeeping rows |
| `datum_sync/worker.py` | `_tick_lifecycle`, once per `LIFECYCLE_TICK_SECONDS` on the ordinary poll |
| `datum_sync/api.py` | public paths `/enrol`, `/enrol/claim`; routers; `/health.reviews_overdue`, `pending_enrolments` |
| `datum_sync/config.py` | `POLICY_REVIEW_INTERVAL_DAYS`, `POLICY_INACTIVITY_RESTRICT_DAYS`, `POLICY_PENDING_TTL_DAYS`, `POLICY_CLAIM_TTL_HOURS`, `LIFECYCLE_TICK_SECONDS` |
| `datum_sync/static-v2/app.js` | Principals list (tabs by kind/state), Principal detail (details, effective authority, grant editor with the parent's values beside each field, tokens with caps, lifecycle verbs by state), New principal, Enrolment (Codes / Pending); nav and router gate by `effective_tier` (`minTier`) |
| `tests/test_lifecycle.py` `tests/test_enrol.py` `tests/browser_smoke.py` | 18 tests; the smoke enrols, approves, claims, restricts, restores and retires an agent through the v2 screens |

**Acceptance (09 WP2):**

| Item | Evidence |
|---|---|
| enrol → pending; approve → active; claim → token `max_tier=2`; second claim → 401 | `test_enrol_approve_claim`, `test_a_claim_code_is_single_use_and_expires`, smoke |
| restrict then restore returns the tuple byte-equal | `test_restore_puts_back_exactly_what_was_stored` (the live row is edited in between and the edit does not survive) |
| retire with children → 409; `?cascade` retires all, revokes every token | `test_retire_needs_the_children_first_unless_cascade`, `test_retire_revokes_every_credential_and_keeps_the_row` |
| a pending row older than 7 days is rejected by the tick with reason `expired` | `test_the_daily_tick_expires_pending_and_restricts_idle_agents` (also: idle agents restricted, humans untouched, rows are `system` actor, tick idempotent) |

**Gates:** `pytest -q` all passed · `break_the_guard.py PRIN` and `ENRL` proven · `browser_smoke.py` PASS including the new principals section · `flow_geometry.py` PASS.

**Guards added:** PRIN-008…011, ENRL-001…005.

**Deviations, recorded in `11-decision-log.md`:**

- Migration numbering: registration is `018`, sessions will be `019`, device flow `020`, federation `021`; the `agents`/`disabled` drop the spec called 018 is a later release and will take the next free number then. A migration file that sorts before an applied one is what `migrate.py`'s checksum check exists to refuse.
- Enrolment names: `^[a-z0-9][a-z0-9_.-]{1,63}$`, not starting with `system`, `admin`, `superuser` or `_`. Fixture names in this repository start with `_`, which is why the reserved prefix is there.
- A tier-3 sponsor's code template must satisfy the same table as creating a child directly (tier ≤ 2, within its own grant), by calling `principals._may_edit`.
- `retire` is tier 4 (the spec's sponsor-or-4 rule applies to approve/reject/restrict/restore/review); retirement is the terminal state and a sponsor narrowing its agent has `restrict`.
