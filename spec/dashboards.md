# Live dashboards — persistent, governed, streamed into worker cards

Status: draft for review · September 2026 · builds on 006 (hosted services), 018 (job events), 022 (resources), `spec/skills.md`, `spec/transcripts.md`

## 1. What this adds

A **dashboard** is a persistent, named surface in Datum Sync that agents and jobs
*stream data into* and that people and workers *watch live*. It outlives any
session, any job and any worker. The operator decides who may write to it and who
may see it; every write is a governed MCP call with the same policy, evidence and
revocation as any other.

Three pieces, all reusing machinery that already exists:

| Piece | Reuses |
|---|---|
| **Streams** — append-only, typed channels of points/rows/events with a durable cursor | the `job_events` pattern (table + `pg_notify`), the SSE replay-then-tail reader in `datum_sync/events.py` |
| **Dashboards** — a layout of cards bound to streams, hosted at a stable URL | hosted services (`service/dashboard`, CSP already defined in `services.py`) and governed resources (022) for the layout document |
| **Cards in the worker** — the worker subscribes to a dashboard's streams and renders live cards beside the conversation | the artifact pane, the sandboxed iframe, the `data-card` sidebar pattern |

The model never renders a dashboard by regenerating HTML every turn. It emits
*data*; Datum Sync keeps it; the cards draw it. That is what makes the dashboard
persistent, cheap and honest.

## 2. Principles

1. **Data in, pixels out.** Agents write typed points to streams. Rendering is a
   declared card type, not agent-authored code, so nothing an agent emits can run
   in a viewer's browser.
2. **Streams are governed resources.** Writing needs a grant (`streams_append`
   is a tool like any other); reading needs visibility on the dashboard. Both
   leave payload-free evidence.
3. **Durable first, live second.** Every point is a row before it is a
   notification (`jobs.notify` already works this way). A viewer that reconnects
   replays from its cursor and misses nothing.
4. **Bounded.** Streams have retention (points and age), rate limits per
   principal, and size caps per point, so a runaway agent fills a window, not a
   disk.
5. **One dashboard, many viewers.** The operator portal, a hosted URL, and every
   worker card show the same rows from the same cursor.

## 3. Data model — migration 026

```sql
CREATE TABLE plane_streams (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    slug          TEXT NOT NULL UNIQUE,                  -- 'auckland-orders', 'fleet-health'
    owner_id      INTEGER NOT NULL REFERENCES service_accounts(id),   -- operator (tenant seam)
    kind          TEXT NOT NULL CHECK (kind IN ('series','table','events','geo','kv')),
    schema        JSONB NOT NULL DEFAULT '{}'::jsonb,   -- JSON Schema for one point; enforced on append
    retention     JSONB NOT NULL DEFAULT '{"max_points":10000,"max_age_hours":720}'::jsonb,
    rate_limit    INTEGER NOT NULL DEFAULT 60,          -- appends per minute per principal
    state         TEXT NOT NULL DEFAULT 'active' CHECK (state IN ('active','paused','retired')),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE plane_stream_points (
    id            BIGSERIAL PRIMARY KEY,                 -- the cursor
    stream_id     UUID NOT NULL REFERENCES plane_streams(id) ON DELETE CASCADE,
    principal_id  INTEGER NOT NULL REFERENCES service_accounts(id),   -- who appended
    source        TEXT NOT NULL,                         -- 'mcp', 'job', 'schedule', 'automation'
    trace_id      UUID,                                  -- mcp_flows.trace_id or jobs.id
    ts            TIMESTAMPTZ NOT NULL DEFAULT now(),    -- point time (client may supply, bounded ±24h)
    key           TEXT,                                  -- kv upsert key / table row key / series name
    payload       JSONB NOT NULL,                        -- validated against plane_streams.schema, ≤ 8 KB
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX plane_stream_points_cursor ON plane_stream_points(stream_id, id);
CREATE INDEX plane_stream_points_time   ON plane_stream_points(stream_id, ts DESC);
-- kv and table streams upsert by key: the append path does INSERT ... ON CONFLICT on this index.
CREATE UNIQUE INDEX plane_stream_points_key ON plane_stream_points(stream_id, key) WHERE key IS NOT NULL;

-- Who may append to a stream. Reading is governed by dashboard visibility.
CREATE TABLE plane_stream_writers (
    stream_id     UUID NOT NULL REFERENCES plane_streams(id) ON DELETE CASCADE,
    target_kind   TEXT NOT NULL CHECK (target_kind IN ('principal','persona','workspace')),
    target        TEXT NOT NULL,                         -- agent name, persona id, or repo/workspace
    granted_by    INTEGER REFERENCES service_accounts(id),
    expires_at    TIMESTAMPTZ,
    PRIMARY KEY (stream_id, target_kind, target)
);

CREATE TABLE plane_dashboards (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    slug          TEXT NOT NULL UNIQUE,
    owner_id      INTEGER NOT NULL REFERENCES service_accounts(id),
    title         TEXT NOT NULL,
    layout        JSONB NOT NULL,                        -- cards: see §4; validated, no code
    layout_version INTEGER NOT NULL DEFAULT 1,
    visibility    TEXT NOT NULL DEFAULT 'fleet' CHECK (visibility IN ('operator','fleet','shared','public-link')),
    state         TEXT NOT NULL DEFAULT 'active' CHECK (state IN ('active','paused','retired')),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE plane_dashboard_viewers (                   -- for visibility = 'shared'
    dashboard_id  UUID NOT NULL REFERENCES plane_dashboards(id) ON DELETE CASCADE,
    target_kind   TEXT NOT NULL CHECK (target_kind IN ('principal','persona')),
    target        TEXT NOT NULL,
    expires_at    TIMESTAMPTZ,
    PRIMARY KEY (dashboard_id, target_kind, target)
);

CREATE TABLE plane_dashboard_links (                     -- for visibility = 'public-link'
    dashboard_id  UUID NOT NULL REFERENCES plane_dashboards(id) ON DELETE CASCADE,
    token_hash    TEXT NOT NULL UNIQUE,                  -- sha256; the token is shown once
    label         TEXT NOT NULL,
    expires_at    TIMESTAMPTZ NOT NULL,
    revoked_at    TIMESTAMPTZ,
    created_by    INTEGER REFERENCES service_accounts(id)
);

CREATE TABLE plane_stream_events (                       -- payload-free: created, writer_granted, writer_revoked,
    id BIGSERIAL PRIMARY KEY, stream_id UUID NOT NULL,   -- paused, resumed, retired, purged, rate_limited
    kind TEXT NOT NULL, actor_id INTEGER, actor_name TEXT,
    detail JSONB NOT NULL DEFAULT '{}'::jsonb, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

Stream kinds and what a point is:

| kind | point | card types that read it |
|---|---|---|
| `series` | `{name?, value, ts}` — numeric time series, one or many named lines | line, area, sparkline, stat (latest / delta) |
| `table` | `{key, ...columns}` — rows keyed for upsert | table, ranked list |
| `events` | `{level, message, ...}` — append-only feed | feed, counter |
| `geo` | GeoJSON Feature with `key` | map (Leaflet, already in the design system) |
| `kv` | `{key, value}` — latest wins | stat tiles, status pills, gauge |

## 4. Dashboard layout — declared, not coded

A layout is a JSON document validated against a fixed schema. There is no
template language and no script. Cards reference streams by slug and pick a
card type from the fixed set.

```json
{
  "grid": {"columns": 12, "row_height": 96},
  "cards": [
    {"id": "orders", "type": "line", "title": "Orders by region",
     "stream": "auckland-orders", "series": ["Auckland", "Canterbury"], "window": "24h",
     "x": 0, "y": 0, "w": 8, "h": 3},
    {"id": "total", "type": "stat", "title": "Orders today",
     "stream": "orders-kv", "key": "orders_today", "format": "number", "x": 8, "y": 0, "w": 4, "h": 1},
    {"id": "health", "type": "status", "title": "Fleet health",
     "stream": "fleet-health", "x": 8, "y": 1, "w": 4, "h": 2},
    {"id": "sites", "type": "map", "title": "Active sites",
     "stream": "sites-geo", "basemap": "nz-aerial", "x": 0, "y": 3, "w": 12, "h": 4},
    {"id": "log", "type": "feed", "title": "Agent activity",
     "stream": "agent-events", "limit": 50, "x": 0, "y": 7, "w": 12, "h": 3}
  ]
}
```

Card types (v1): `stat`, `status`, `line`, `area`, `bar`, `sparkline`, `table`,
`feed`, `map`, `markdown` (static text from the layout, for headings and notes).
Each has a small options schema (format, window, thresholds for `status`,
series filter). Thresholds colour with the design system's governance colours
(`#4ade80` / `#fbbf24` / `#f87171`).

The layout document is stored as a governed resource (`text/json`, owner =
operator) as well as in `plane_dashboards.layout`, so an agent with a grant can
*propose* a layout via `resources_create` and the operator can adopt it — the same
proposal path skills use.

## 5. Writing — how data gets in

Three producers, one table:

**Agents (MCP).** Two baseline tools, granted per stream through
`plane_stream_writers` (the grant JSON gains `streams: {append: [...]}` beside
`mcp` and `skills`):

- `streams_append(stream, points: [...])` — up to 100 points per call, each
  validated against the stream schema, rate-limited per principal, recorded as a
  flow with `streams.appended {stream, count}` (never payload). Returns the
  cursor of the last point.
- `streams_describe(stream)` — kind, schema, retention, current cursor, last
  point time, and the caller's write permission. Lets the model discover the
  contract before writing.

**Jobs.** A workspace declares an output `stream/<slug>` in its manifest, the
publish gate checks the stream exists and the workspace is a writer, and the
runner forwards NDJSON lines the child prints on a `stream:` prefix into
`plane_stream_points` with `source='job'` and `trace_id = job id`. Schedules and
automations therefore stream for free — a scheduled workspace that emits a point
every five minutes is a live dashboard with no agent involved.

**Operators.** `POST /api/streams/{slug}/points` for backfills and integrations,
same validation.

Writes are `INSERT` then `pg_notify('stream_points', {stream_id, id})`, exactly
`jobs.notify`'s discipline: durable row first, announcement second.

## 6. Reading — one live protocol for every viewer

`GET /api/streams/{slug}/events?after=<cursor>&window=24h` is an SSE endpoint
built on `events.py`'s replay-then-tail: LISTEN first, replay rows after the
cursor, then tail. Each event carries `id:` so `Last-Event-ID` resumes exactly.
A viewer subscribes once per stream; a dashboard with five cards on three streams
opens three connections (or one multiplexed `GET /api/dashboards/{slug}/events`
that fans them in — v1 ships the multiplexed form).

Access is decided per dashboard:

| visibility | who can read |
|---|---|
| `operator` | the owner only |
| `fleet` | every active agent of the owner, and the owner |
| `shared` | principals/personas in `plane_dashboard_viewers` |
| `public-link` | anyone holding an unexpired link token; the token is the credential, shown once, revocable |

A worker reading on behalf of an agent uses the agent's session token, so a
dashboard the agent cannot see does not exist to that worker.

## 7. Hosting — the persistent URL

A dashboard is served at `/serve/dash/{slug}/` by the existing hosted-service
path with `service/dashboard`'s CSP (`default-src 'self'; connect-src 'self'`).
The page is one static renderer (`datum_sync/dashboard_static/`) that fetches
the layout and opens the event stream; nothing per-dashboard is generated. It
uses the Datum design system: DM Sans / Space Grotesk / JetBrains Mono, navy and
amber, Phosphor icons, the same card chrome as the portal, and the governance
colours for thresholds. Leaflet for `map` cards. No CDN — the renderer vendors
what it needs, as the worker does.

Because the layout is data and the renderer is fixed, the same dashboard renders
identically in the hosted page, the operator portal (a **Dashboards** page that
embeds the renderer) and the worker (§8).

## 8. Cards in the worker — streaming into the agent's environment

The worker gains a **Dashboards** pane view (`data-view="dashboards"`) beside
Artifacts, Run trace and Authority:

1. On connect it calls `GET /api/dashboards` (visible to this agent) and lists
   them in the pane.
2. Selecting one renders its cards with the same fixed renderer, inside the
   existing sandboxed iframe (`sandbox="allow-scripts"`), fed by a single
   multiplexed event stream that the worker proxies through its relay
   (`/internal/mcp/{sid}/…` already does this for MCP) so the agent token never
   reaches the iframe.
3. A card can be **pinned** to the sidebar as a compact tile (`data-card`
   pattern) — a stat, a status, a sparkline — so the agent's operator sees the
   live number without opening the pane.
4. When the agent itself appends (its own `streams_append` call), the card
   updates within the same turn; the run trace shows `streams.appended` like
   any other tool call.
5. The pane is a viewer. The model gets data *into* a card only by calling the
   tool, and gets data *out* only by calling `streams_read` (a bounded read tool,
   last N points or a window) — it never scrapes the rendered card.

Persistence follows from the design: close the worker, reopen it tomorrow, the
dashboard is where it was, with every point since.

## 9. Operator surface

- **Dashboards page**: list, visibility badge, state, streams used, viewers,
  public links (label, expiry, revoke), "open hosted URL".
- **Editor**: card grid with add/remove/resize; card options forms generated
  from the card schema; stream picker; live preview. Saves bump `layout_version`.
- **Streams page**: kind, schema (editable JSON Schema with validation), retention,
  rate limit, writers (add agent/persona/workspace, expiry), state, point count,
  last write, per-writer volume in the last hour. **Pause** stops writes and
  reads with a clear status; **Retire** freezes; **Purge** applies retention now.
- **Evidence**: `plane_stream_events` plus the MCP flows for every agent write;
  a rate-limited writer shows as `rate_limited` events, not silent drops.

## 10. Security stance

- **No agent-authored code reaches a browser.** Layouts are validated data;
  card types are fixed; the renderer is shipped by Datum Sync. This is stronger
  than the artifact sandbox, which has to tolerate agent HTML.
- **Writes are tool calls.** Same policy chain, same evidence, same revocation
  (remove the writer, pause the stream, retire the dashboard).
- **Reads are scoped.** Dashboard visibility decides; the worker sees only what
  its agent can; public links are short-lived hashed tokens with a label and a
  kill switch.
- **Points are validated and bounded.** JSON Schema per stream, ≤ 8 KB per point,
  ≤ 100 points per call, rate limit per principal, retention by count and age.
  `ts` may be client-supplied within ±24 h, so a backfill is possible but a
  point cannot claim to be from last year.
- **Payload-free evidence.** Flows record stream and count; point content lives
  only in `plane_stream_points` under its own retention.
- **CSP as already specified.** `service/dashboard` keeps `connect-src 'self'`;
  the renderer talks only to Datum Sync.

## 11. Build plan

| Increment | Scope | Proof |
|---|---|---|
| **D1 · Streams** | Migration 026 (streams, points, writers, events); `datum_sync/streams.py`; `streams_append/describe/read` tools; SSE reader on `events.py`; rate limit and retention job | `tests/test_streams.py`: schema rejection, rate limit, replay-then-tail resumes at cursor, writer revocation denies next append, flows carry no payload |
| **D2 · Dashboards + hosted page** | `plane_dashboards`, layout schema, fixed renderer under `/serve/dash/{slug}/`, multiplexed `dashboards/{slug}/events`, visibility rules and public links | `test_dashboards.py`: visibility matrix, invalid layout refused, link expiry/revoke; `browser_smoke.py` scene renders three card types live |
| **D3 · Worker pane** | Dashboards view, relay-proxied event stream, pinned tiles, `streams_read` in the model's toolset | Worker smoke: append from the model, card updates in-turn |
| **D4 · Jobs as producers** | `stream/<slug>` manifest output, publish-gate check, runner forwarding | `test_runner.py`: NDJSON `stream:` lines land as points with the job id |
| **D5 · Editor + portal** | Dashboards and Streams pages, grid editor, adopt-proposed-layout | `test_portal.py` additions |

D1 + D2 give a persistent hosted dashboard that jobs and agents can stream into;
D3 puts it in the worker.

## 12. Open questions

1. **Downsampling.** `series` streams with 10 k points render fine; should
   retention also keep an hourly rollup so a 30-day window stays cheap? Recommended
   for v2, with the rollup written by the retention job.
2. **Cross-tenant dashboards.** `public-link` covers "show a client"; a
   dashboard shared *between* two operators' fleets waits for tenancy.
3. **Bidirectional cards.** Should a card carry a control (a button that calls a
   workspace)? It is a natural next step, but it turns a viewer into an actor;
   keep v1 read-only and route actions through the worker's normal tool path.
4. **Point provenance in the UI.** Show which agent wrote each point on hover
   (available from `principal_id`), or keep cards anonymous by default?
