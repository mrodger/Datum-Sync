# Report — WP8: consolidation

**Built:**

| File | Change |
|---|---|
| `datum_sync/ui.py`, `api.py` | one shell: `/ui` answers 307 → `/ui/v2` (bookmarks and old verification URLs still land; the fragment survives); the `/ui/static` mount and `PUBLIC_PREFIXES` entry are gone |
| `datum_sync/static/` | deleted (v1 shell: 2,886 lines) |
| `datum_sync/static-v2/style.css` | the Flow chrome numbers moved in from v1: topbar 64, gutter 40, search 40, sidebar padding 18, tile 56, counter 72 with 10 px gaps, ws-card 210 × 90 (D-41) |
| `tests/flow_geometry.py` | measures `/ui/v2`; `.action-bar` selectors |
| `tests/browser_smoke.py` | every pass drives `/ui/v2`; the principals pass also checks `whoami.elevate`, that a baseline token's `tools/list` offers `elevate`, and that every built-in carries `datumMinTier` |
| `tests/test_ui.py` | the shell tests target the one shell; `/ui` is asserted as a redirect; the v1 innerHTML test is gone (UI-001 now anchors the v2 test, which cites it) |
| `tests/break_the_guard.py` | UI-001, UI-005, SERVICE-014 re-anchored |
| `CLAUDE.md` | status, the one-process constraint, the gates note on tier 5 for the smoke's federation pass, the key file map, what is deliberately not built (teams, memory, OAuth-to-upstream, stdio) |
| `README.md` | status and the auth-plane concepts |
| `spec/agent-auth-plane/00-README.md` | status: implemented |

**Acceptance (09 WP8):**

| Item | Evidence |
|---|---|
| all four gates on a clean checkout | `pytest -q`: 804 passed; `browser_smoke.py`: PASS (every pass on `/ui/v2`, federation and review included); `flow_geometry.py`: PASS on `/ui/v2` |
| `break_the_guard.py` with no argument: every id proven or UNPROVABLE with a reason | 242 of 242 proven; 5 UNPROVABLE (AGENT-001, AUDIT-007, AUTHZ-001, AUTHZ-002, SCHEMA-001), each with its reason |
| a fresh database built from `migrations/` matches the demo database | `tests/test_schema_drift.py` in the suite |

**Deviations:**

- `static-v2/` keeps its name and the `/v2/` asset prefix: it is what every tag and import inside the shell already says, and `/ui/v2` is the address in every device-code `verification_uri` already issued. Renaming would change URLs for nothing.
- Teams are not built (D-14); the delegation tree is the fence. The seam is `service_accounts.parent_id` and `grants.narrows`.
- The smoke's federation pass needs a tier-5 account (FED-018); below that it prints a note.
