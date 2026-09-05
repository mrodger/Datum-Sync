# 23 — Tenancy and Activity Review

## 1. Teams

An organisation has teams; a team owns agents, connections, repositories,
memory namespaces and a vault subtree together. Migration `012_teams.sql`:

```sql
CREATE TABLE teams (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE CHECK (name ~ '^[a-z][a-z0-9-]{1,31}$'),
    description TEXT,
    vault_root  TEXT,              -- e.g. "teams/geo"; the team's default vault subtree
    memory_root TEXT,              -- e.g. "team/geo"
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE principals   ADD COLUMN team_id INTEGER REFERENCES teams(id) ON DELETE SET NULL;
ALTER TABLE connections  ADD COLUMN team_id INTEGER REFERENCES teams(id) ON DELETE SET NULL;  -- NULL = org-wide
ALTER TABLE repositories ADD COLUMN team_id INTEGER REFERENCES teams(id) ON DELETE SET NULL;
ALTER TABLE hosted_services ADD COLUMN team_id INTEGER REFERENCES teams(id) ON DELETE SET NULL;
```

Rules:

- A principal belongs to at most one team; children inherit the parent's
  team and cannot be moved out of it.
- A **team admin** is a tier-4 principal in that team: `03 §5.3` powers
  apply to principals, connections and repositories **of that team** plus
  org-wide (`team_id IS NULL`) read. Tier 5 spans teams.
- Connections with a `team_id` are usable (publish, proxy, federate) only
  by principals of that team, in addition to the grant's own lists — a
  second, structural fence that a grant typo cannot cross.
- `GET` listings are filtered by team for tier ≤4; the grant's
  `repositories` list is then applied within that.
- A team's `vault_root` and `memory_root` are the defaults offered when
  creating grants in the UI; they are not themselves permissions.

Cross-team sharing is explicit: an org-wide connection, a memory namespace
under `org/**`, or a vault path outside any team root, each granted by
tier 5.

## 2. What tenancy is not

Not separate databases, not separate processes, not separate URLs. One
gateway, one audit spine, teams as a labelling and fencing dimension. That
is deliberate: the question "what did every agent in the company do
yesterday" must have one answer.

## 3. Activity review

The audit spine (`14`) holds everything; review is the set of views that
make it usable by a person deciding whether an agent should keep its
authority.

### 3.1 Per-agent activity summary

`GET /rest/v1/principals/{name}/activity?window=7d` (tier ≥4 in team, or self):

```json
{
  "window": "7d",
  "requests": 1842, "denied": 17, "errors": 4,
  "by_surface": {"mcp": 1790, "rest": 52},
  "by_resource": {
    "workspaces": {"calls": 31, "top": ["SCIMAC__site_plan"]},
    "vault":      {"reads": 412, "writes": 38, "denied": 9, "governance_writes": 0, "top_paths": ["dev/notes/**"]},
    "memory":     {"searches": 610, "puts": 44, "namespaces": ["team/geo/**", "agents/datum-main/**"]},
    "code":       {"calls": 120, "repos": ["datum/gateway"], "writes": 6, "denied": 3},
    "compute":    {"calls": 22, "hosts": ["vm102"], "denied": 5, "pending_approvals": 1},
    "documents":  {"calls": 60, "folders": ["1AbC…"]},
    "proxy":      {"calls": 400, "connections": ["openai"]}
  },
  "delegations": {"created": 3, "active": 1, "names": ["drone-7f2a"]},
  "jobs": {"submitted": 31, "failed": 2},
  "first_seen": "…", "last_seen": "…",
  "anomalies": [
    {"kind": "denied_burst", "at": "…", "count": 9, "target_kind": "vault", "sample": "secrets/**"},
    {"kind": "new_resource", "at": "…", "detail": "first compute call on vm111"}
  ]
}
```

Computed from `audit_log` with rollups; the worker maintains a daily
`audit_rollups(actor_id, day, resource, verb, outcome, count)` table so a
90-day summary does not scan the spine.

### 3.2 Anomaly signals (deterministic, no model)

The worker's daily tick flags, per agent, from rollups:

| Signal | Rule |
|---|---|
| `denied_burst` | ≥5 denied outcomes on one `target_kind` within 10 minutes |
| `new_resource` | first ever call on a connection/host/repo/namespace |
| `off_hours` | >20% of calls outside `policy.review.working_hours` for that team |
| `volume_spike` | daily request count > 3× the trailing 14-day median |
| `delegation_fanout` | > `policy.review.max_children_per_day` children created |
| `governance_touch` | any governance-flagged write |
| `approval_pressure` | > 3 pending approvals requested in a day |

Signals are rows in `review_signals(principal_id, day, kind, detail, acknowledged_by)`,
shown on the Principal screen and the Review queue, and cleared by an
admin acknowledging them. They are hints for a human, never an automatic
restriction — except `denied_burst` on `secrets/**`-class deny patterns,
which `policy.review.auto_restrict_on` may list.

### 3.3 Review queue

`GET /rest/v1/review` (tier ≥4): agents with `review_due_at` passed,
unacknowledged signals, pending registrations, pending promotions, pending
approval-gated calls, dead deliveries — one inbox, ordered by age. Each
item links to the trace or principal. The UI's Dashboard shows the count;
the Review screen is the queue.

### 3.4 Trace view, extended

`GET /rest/v1/audit/trace/{id}` now also joins `memory_history`,
`pending_calls`, `promotions` and federated `federate.call` rows, so one
agent turn ("summarise the client folder and push a note") reads as a
timeline: Drive search → Drive get ×3 → memory_put → gh__push_files
(denied: repo not in scope) → memory_put `agents/…/_last_error`.

### 3.5 Exports for the organisation

`python -m datumgate audit export --team geo --since … --format jsonl` and
`GET /rest/v1/review/report?team=&month=` (tier ≥4) produce a monthly
per-team summary: agents, grants changed, denied counts, governance
touches, approvals granted, retired agents — the artefact a review
meeting or an auditor asks for.

## 4. UI additions (`15-web-ui.md`)

| Screen | Contents |
|---|---|
| Review | the queue from §3.3 |
| Principal (extended) | activity summary card, signals with acknowledge, review button with "narrow grant" inline, delegation subtree, capabilities |
| Teams | list; team admin assignment; team's connections/repositories/roots |
| Memory | namespace browser, search, entry view with history diff, edit (tier ≥2 in scope) |
| Approvals | pending calls with full arguments, requester, guard label; approve / reject |
| Federation | per `mcp` connection: cached tool list, guard coverage (which tools are guarded/unguarded/denied), last refresh, availability |

## 5. Guards (TEAM / REV)

| id | Guard |
|---|---|
| TEAM-001 | team connection unusable outside the team regardless of grant |
| TEAM-002 | child inherits parent's team and cannot leave it |
| TEAM-003 | tier-4 listings filtered by team |
| TEAM-004 | cross-team grant requires tier 5 |
| REV-001 | activity summary visible only to team admins, self, or tier 5 |
| REV-002 | rollups match the spine (property test on a seeded day) |
| REV-003 | signals never auto-restrict unless policy lists the kind |
| REV-004 | export requires tier ≥4 and is audited |
