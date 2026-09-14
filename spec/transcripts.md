# Session transcripts — Postgres system of record, row-level isolation, nothing durable on the worker

Status: draft for review · September 2026 · builds on 016–023 and `spec/skills.md`

## 1. Problem

Today a session's conversation lives in three places, none of them right for a
fleet:

| Where | What | Lifetime |
|---|---|---|
| Worker process memory (`worker/app.py` `_sessions`) | Live turns, trace, tokens | Until disconnect or worker restart |
| Codex's own home (`~/.codex/sessions/*.jsonl`) | Full rollout of every turn, plaintext | Forever, unmanaged |
| Datum Sync Postgres | Payload-free flow evidence only | Retained, governed |

The second row is the problem: months of plaintext transcripts accumulate on the
worker host under the operator's home directory, outside every control Datum Sync
has. The first row is the right *live* behaviour but gives an agent no memory of
its own past sessions.

Goal: the worker holds only the live session; the transcript system of record is
Postgres, encrypted at rest, isolated per agent by row-level security so an agent
can read only its own sessions; nothing durable stays on the worker host.

## 2. Principles

1. **Transcripts are content, evidence is not.** They live in their own tables with
   their own retention, never in `mcp_flows` or `plane_audit`. A flow may reference
   a transcript turn by id; it never embeds text.
2. **One agent, one view.** Row-level security in Postgres, not application
   filtering, decides which rows an agent's session can see. The application sets
   the principal on every transaction; the database enforces it.
3. **Sealed at rest.** Turn content is encrypted with the existing multi-key
   AES-GCM sealer (`datum_sync/crypto.py`), bound to the transcript id as AAD, so a
   copied table is ciphertext and a rotated key still opens history.
4. **The worker is a cache, not a store.** It streams turns to Datum Sync as they
   complete, keeps the live session in memory, and leaves no transcript on disk.
5. **Operators see metadata by default, content by approval.** Reading another
   principal's transcript is an explicit, audited action, not a side effect of
   being an operator.

## 3. Data model — migration 025

```sql
CREATE TABLE plane_transcripts (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    principal_id  INTEGER NOT NULL REFERENCES service_accounts(id),
    owner_id      INTEGER NOT NULL REFERENCES service_accounts(id),   -- the operator (tenant seam)
    session_id    UUID REFERENCES plane_sessions(id),                 -- the consented worker session
    client_name   TEXT NOT NULL,                                       -- 'datum-worker', 'claude-code', ...
    model         TEXT,
    title         TEXT,                                                -- first user turn, 120 chars, secret-redacted (the only plaintext)
    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at      TIMESTAMPTZ,
    turn_count    INTEGER NOT NULL DEFAULT 0,
    retain_until  TIMESTAMPTZ,                                         -- from the retention policy
    state         TEXT NOT NULL DEFAULT 'live' CHECK (state IN ('live','closed','sealed','purged'))
);
CREATE INDEX plane_transcripts_principal_time ON plane_transcripts(principal_id, started_at DESC);

CREATE TABLE plane_transcript_turns (
    transcript_id UUID NOT NULL REFERENCES plane_transcripts(id) ON DELETE CASCADE,
    seq           INTEGER NOT NULL,
    role          TEXT NOT NULL CHECK (role IN ('user','assistant','tool','system')),
    kind          TEXT NOT NULL,                 -- message, tool_call, tool_result, skill_read, error
    sealed        BYTEA NOT NULL,                -- crypto.seal(transcript_id, {text, meta})
    content_hash  TEXT NOT NULL,                 -- sha256 of plaintext, for integrity and dedupe
    bytes         INTEGER NOT NULL,
    trace_id      UUID,                          -- mcp_flows.trace_id when kind is tool_call/tool_result
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (transcript_id, seq)
);

-- Explicit, time-bounded read grants to someone other than the owner (operator review,
-- hand-over to another agent, support). Same shape as plane_resource_grants.
CREATE TABLE plane_transcript_grants (
    id            BIGSERIAL PRIMARY KEY,
    transcript_id UUID NOT NULL REFERENCES plane_transcripts(id) ON DELETE CASCADE,
    principal_id  INTEGER NOT NULL REFERENCES service_accounts(id),
    purpose       TEXT NOT NULL,
    granted_by    INTEGER REFERENCES service_accounts(id),
    expires_at    TIMESTAMPTZ NOT NULL,
    revoked_at    TIMESTAMPTZ
);

CREATE TABLE plane_transcript_events (          -- payload-free: opened, closed, read, granted, revoked, purged
    id BIGSERIAL PRIMARY KEY, transcript_id UUID NOT NULL, kind TEXT NOT NULL,
    actor_id INTEGER, actor_name TEXT, detail JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

`title` is the only plaintext derived from content: the first user turn, 120
characters, passed through the same secret-pattern redaction the credential review
uses. Everything else content-bearing is in `sealed`.

## 4. Row-level security

The service connects as one database role today. RLS needs the caller's identity
inside the transaction, so:

```sql
ALTER TABLE plane_transcripts       ENABLE ROW LEVEL SECURITY;
ALTER TABLE plane_transcript_turns  ENABLE ROW LEVEL SECURITY;
ALTER TABLE plane_transcripts       FORCE  ROW LEVEL SECURITY;   -- applies to the table owner too
ALTER TABLE plane_transcript_turns  FORCE  ROW LEVEL SECURITY;

-- The application sets these per transaction; the policies read them.
--   SET LOCAL datum.principal_id = '<id>';
--   SET LOCAL datum.mode = 'agent' | 'operator';

CREATE POLICY transcript_owner ON plane_transcripts
    USING (principal_id = current_setting('datum.principal_id', true)::int);

CREATE POLICY transcript_granted ON plane_transcripts
    USING (EXISTS (SELECT 1 FROM plane_transcript_grants g
                   WHERE g.transcript_id = plane_transcripts.id
                     AND g.principal_id = current_setting('datum.principal_id', true)::int
                     AND g.revoked_at IS NULL AND g.expires_at > now()));

-- Operators see metadata for their fleet through a view that excludes `sealed`;
-- content needs a grant to themselves (self-granted, but audited) like anyone else.
CREATE POLICY transcript_operator_metadata ON plane_transcripts
    USING (current_setting('datum.mode', true) = 'operator'
           AND owner_id = current_setting('datum.principal_id', true)::int);

CREATE POLICY turns_follow_transcript ON plane_transcript_turns
    USING (EXISTS (SELECT 1 FROM plane_transcripts t WHERE t.id = transcript_id));
    -- the transcript policies already filtered t; a turn is visible iff its transcript is
```

Application side (`datum_sync/transcripts.py`):

- Every transcript query runs inside `async with conn.transaction():` after
  `SET LOCAL datum.principal_id = $1` and `SET LOCAL datum.mode = $2`, taken from
  the already-resolved `caller()`; no query path reaches these tables without it.
- Operator content reads go through a separate role, `datum_transcript_reader`,
  granted `SELECT` on the tables and nothing else, used only by the review
  endpoint, so a bug in the main service cannot leak content without also using
  that role. Both roles are subject to the same policies because of `FORCE`.
- `tests/test_transcripts.py` includes a **negative RLS test**: with the principal
  set to agent B, a direct `SELECT *` on agent A's turns returns zero rows even
  though the row exists; and with no principal set, every policy denies.

## 5. Write path — from the worker to Postgres, nothing left behind

The worker already sees every event it needs in `codex_bridge.stream_turn()`:
`item/agentMessage/delta`, `item/started`/`item/completed` for `mcpToolCall`, and
`turn/completed`. It gains a `TranscriptSink`:

1. On connect, `POST /api/transcripts` with the agent token → a `live` transcript
   bound to the consented session. The id is kept in `_sessions[sid]`.
2. On each completed user turn and assistant turn, `POST /api/transcripts/{id}/turns`
   with `{seq, role, kind, text, meta, trace_id?}`. Datum Sync seals it, hashes it,
   stores it, bumps `turn_count`. Tool calls are stored as `kind=tool_call` with the
   flow's `trace_id` so the evidence and the conversation line up without either
   containing the other.
3. Writes are batched per turn and retried; if Datum Sync is unreachable the
   worker marks the turn `unsynced` in memory and refuses to start a new turn after
   a bounded backlog (default 3), so an outage cannot silently produce an
   unrecorded conversation.
4. On disconnect, `POST /api/transcripts/{id}/close`; the in-memory session is
   dropped; the per-session workspace directory is deleted.

**Stopping the local plaintext copy.** The bridge currently passes `HOME` through
to Codex (`codex_bridge.py:84`), so Codex writes its rollout under the operator's
`~/.codex`. The change:

- Give each session its own `CODEX_HOME` inside the per-session workspace
  (`.runtime/worker-workspaces/<sid>/codex-home`), seeded with a read-only copy of
  `auth.json` and the worker's `config.toml` (`[history] persistence = "none"`),
  and delete the whole workspace on disconnect and on worker start-up (orphan
  sweep). Rollouts that Codex insists on writing therefore live seconds to hours,
  never months, and never in the operator's home.
- The worker's own logs (`.runtime/worker.log`) must not contain turn text; the
  sink logs ids and byte counts only. `tests/test_transcript_sink.py` asserts the
  log stays content-free.

Verify on the Ubuntu host before relying on it: that the pinned Codex build honours
`CODEX_HOME` and `history.persistence`, and that `auth.json` is sufficient for a
signed-in app-server. If a build ignores them, the orphan sweep is the fallback and
the gap is documented.

## 6. Read path — the agent's own memory

Two baseline MCP tools, so any client gets memory the same way it gets skills:

- `transcripts_list(limit, before)` → `[{id, title, started_at, ended_at, turn_count, client_name}]`
  for the calling principal (RLS does the filtering; the tool adds nothing).
- `transcripts_read(id, from_seq, limit)` → turns, unsealed, for a transcript the
  principal owns or holds a grant to. Reads emit `transcripts.read` flow events
  with id and range only.

The worker uses them for **continuity, not auto-injection**: on connect it shows the
agent's last sessions in a **Sessions** card; the operator or the model can pull one
into context deliberately with `transcripts_read`. Nothing from past sessions
enters the prompt unless asked — the same stance skills take.

`GET /api/session/transcript` returns the live transcript for the current worker
session so the sidebar can render from Datum Sync rather than from process memory
once the sink is in place; the in-memory copy becomes a cache.

## 7. Operator surface

- **Sessions page**: per agent, transcripts with title, client, model, duration,
  turn count, state, retention date. No content.
- **Read**: requires a self-grant with a purpose ("incident 2026-09-14",
  "quality review"), default 24 hours, recorded in `plane_transcript_events` and
  in `plane_audit`. The read view shows turns with tool calls linked to their MCP
  flow timeline.
- **Grant to another agent**: hand-over of a session to a successor agent, time-bounded,
  revocable, same UI as resource sharing.
- **Retention**: per-owner policy (`transcript_retention_days`, default 90) applied
  by the worker's periodic job: `sealed` is nulled and state set to `purged`;
  metadata and events stay so the evidence trail is unbroken. Legal hold flag
  exempts a transcript.
- **Export**: JSONL of a transcript, unsealed, only through the review grant path,
  with the export itself recorded.

## 8. Security stance

- **Isolation is in the database.** The application sets identity; Postgres
  enforces it; `FORCE ROW LEVEL SECURITY` means even the owning role cannot bypass.
- **No plaintext at rest anywhere.** Sealed in Postgres; no rollouts left in home
  directories; workspaces deleted; logs content-free.
- **Keys rotate without re-encrypting history.** The key-id prefix the sealer
  already writes handles it; old keys stay available for opening until a rewrap
  job has re-sealed everything under the current key.
- **Reads are evidence.** Every content read — by the agent, an operator, or a
  grantee — is an event with actor, transcript and range.
- **Operators are not silently omniscient.** Metadata by right; content by
  recorded self-grant. This is the property that makes the transcript store
  acceptable to the people whose conversations are in it.
- **Backpressure over silence.** The worker will pause rather than converse
  unrecorded when the sink is down.

## 9. Build plan

| Increment | Scope | Proof |
|---|---|---|
| **T1 · Store + RLS** | Migration 025, `transcripts.py`, `SET LOCAL` discipline, seal/unseal, `transcripts_list/read` tools, `/api/transcripts*` | Negative RLS tests; sealed bytes never equal plaintext; flows contain no content |
| **T2 · Worker sink** | `TranscriptSink`, per-session `CODEX_HOME`, orphan sweep, content-free logging, backlog pause | `worker/tests`: turns reach the store in order; workspace gone after disconnect; log has no turn text |
| **T3 · Operator surface** | Sessions page, self-grant read, grants to agents, retention job, export | `test_portal.py` additions; `browser_smoke.py` scene |
| **T4 · Continuity** | Sessions card in the worker; deliberate recall via `transcripts_read` | Worker smoke |

T1 + T2 achieve the stated goal: Postgres is the system of record, each agent sees
only its own sessions, and nothing durable stays on the worker host.

## 10. Open questions

1. Should the worker also seal client-side with a session key before sending, so
   Datum Sync stores content it cannot read without the worker's cooperation? It
   defeats operator review by design — probably a per-tenant policy, not a default.
2. Retention default: 90 days, or tie it to the session-length choices (demo
   sessions purge in 24 hours)?
3. Does a `tool_result` turn store the result text at all, or only the hash and the
   `trace_id`? Storing it makes recall complete; omitting it keeps upstream data out
   of the transcript store. Recommended: store, sealed, with a per-server
   `record_results` switch in the MCP server registry.
4. Multi-tenant: `owner_id` is the seam; when tenants arrive the operator policy
   becomes `owner_id = current tenant's operator`, unchanged in shape.
