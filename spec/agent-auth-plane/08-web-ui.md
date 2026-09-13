# 08 — Web UI

Built on `datum_sync/static-v2/`. `STYLE.md` and `PORT.md` govern look and
structure; `CLAUDE.md`'s rules govern behaviour: wait on `data-ready`, no
`innerHTML`, no build step, no CDN, every screen reachable from the nav and
by hash. The v1 shell at `/ui` is retired in `09 WP8`; until then nothing
new is added to it.

## 1. Nav changes

| Section | Change |
|---|---|
| Admin › **Principals** | replaces "Admin" accounts list |
| Admin › **Enrolment** | new |
| Admin › **Approvals** | new — elevations and pending calls in one screen with two tabs |
| Admin › **Review** | new — the queue; the dashboard shows its count |
| **MCP Servers** | re-pointed at federation (`/rest/v1/federation`); stops reading `mcp_call_log` |
| Account (avatar menu) › **Sessions** | new, self-service |
| Authentication Services | gains device-flow and pruning columns |

`adminOnly` on these sections is replaced by `minTier: 4`, with `SECTIONS`
reading `me.effective_tier`. A tier-3 sponsor sees Principals, Enrolment
and Approvals filtered to their subtree; the API is the check, the nav is
the courtesy (as the existing comment in `app.js` says).

## 2. Principals

**List.** Columns: name, kind (icon), state (badge), tier / effective,
parent, scope summary, live sessions, last used, review due, flags. Filters:
kind, state, parent. Row actions by state: approve / reject (pending),
restrict / disable (active), restore (restricted), retire (disabled).
Every destructive action confirms and shows the audit verb it will write.

**Detail** (`#/principals/{name}`), replacing `screenAccount`:

- Header: name, kind, state badge, ancestors as breadcrumbs, children list.
- **Grant editor.** The authority tuple as a form: tier (select), repositories
  (chips with `*`), vault scope (five pattern lists), proxy grants
  (multi-select of connections ≤ tier), federation blocks (one panel per
  kind with connections, patterns, write/share toggles, tools allow/deny),
  limits, rate limit. Save calls PATCH; a `GRANT_NOT_NARROWER` or
  `DESCENDANTS_WOULD_WIDEN` error highlights the field and names the
  descendant. The parent's value is shown beside each field greyed, so the
  narrowing rule is visible while editing.
- **Effective** panel: the computed intersection, read-only, with a note
  when it differs from the stored tuple ("narrowed by parent `marcus`").
- **Tokens**: as today, plus a `max_tier` column and selector on mint.
- **Sessions**: live sessions with client, age, idle; end button.
- **Activity** (7d / 30d): counts by surface and by resource kind, denied
  count, last seen. Tier ≥4 sees top targets; sponsors see counts only.
- **Lifecycle**: review due, last reviewed by, restrict reason, a Review
  button opening the narrow-inline form.
- **Elevate on behalf** button (humans only): starts the PKCE flow with
  `on_behalf_of` for a registered client the operator picks.

## 3. Enrolment

Two tabs. **Codes**: mint form (template built with the same grant editor
component, pre-filled from the caller's own effective grant; max uses;
expiry; auto-approve when tier 4), list with uses/expiry/revoke, the raw
code shown once in a copy box. **Pending**: registrants with requested
tuple beside the template, metadata (model, host, purpose), approve with
edits / reject with reason. Approving shows the claim instruction to relay
to the agent's operator.

## 4. Approvals

**Elevations** tab: user code, principal, sponsor, requested scope, what it
unlocks for this principal, age, approve (with scope narrowing) / deny.
**Calls** tab: pending federated calls with connection, tool, label, full
arguments (this is the one screen that shows argument values, and the
screen says so), requester, age; approve / reject; after approval, the
result summary and status.

## 5. MCP Servers (federation)

Per `mcp` connection: status (ok / stale / down), tool count, last refresh,
refresh button, guard coverage bar (guarded / unguarded / hidden by
default-deny), name clashes. Expanding a row lists cached tools with their
matching guards and, for a chosen principal (picker), whether that principal
would see the tool and why not. The Connections form gains the `mcp` type
with a profile picker that fills `config`.

## 6. Sessions (self-service)

Under the avatar menu: the caller's live sessions, the limit, and an end
button. Same component as the principal detail panel.

## 7. Review

The queue from `07 §5`: one list, oldest first, kind chips, each row
linking to its screen. The dashboard tile "Needs review: N" links here.

## 8. Existing screens touched

- **Dashboard**: review count tile; "recent jobs" gains `submitted_by` (it
  is populated now).
- **Jobs / Job**: submitter shown; `grant_snapshot.kind` icon.
- **Connections**: `mcp` type; `allow_private_origin` visible only to tier 5.
- **Analytics**: reads `audit_log` only after `09 WP7`.
- **Authentication Services**: device codes count, prune stats.
- **Sign-in**: unchanged. `NO AUTH` badge unchanged.

## 9. Tests

`tests/browser_smoke.py` gains: enrol an agent through a code, approve it,
claim the token, open a second MCP session and see `SESSION_LIMIT`, elevate
via device code and approve it in Approvals, save a federation connection
against `tests/mock_mcp_server.py`, see the tool list and a denied call in
the principal's activity. Every new screen sets `data-ready` and the smoke
waits on it and nothing else.

`flow_geometry.py` is unaffected: no chrome changes.
