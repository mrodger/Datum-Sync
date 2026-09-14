# Skills — provider-agnostic, governed, authorised by default

Status: draft for review · September 2026 · builds on migrations 016–023

## 1. What a skill is

A skill is a named, versioned piece of *instruction* — how to do a task well, which
tools to use, in what order, what to check — that an agent loads when the task
comes up. It is not code and it is not a grant. It never widens what an agent may
do; it tells the agent how to use what it already may do.

Datum Sync already treats skills as governed content: writes under `skills/` in
the vault are classified `is_governance=true` in the MCP call log
(`datum_sync/mcp.py`), and the vault design gives agents read access to `skills/**`
while writes need elevation. This spec turns that convention into a registry with
scoping, delivery to any client, and an operator surface.

**Provider-agnostic** means a skill written once works in the Codex worker, Claude
Code, Claude.ai, Hermes, OpenClaw or any MCP client, without per-client files.
Delivery is through the MCP endpoint the client already uses, in the standard MCP
primitives (prompts, resources, tools), with one convenience path for workers that
want the effective skill set at connect time.

The on-disk form is the open Agent Skills layout so skills can be authored and
exchanged as plain files:

```
skills/<slug>/SKILL.md          frontmatter + markdown instructions
skills/<slug>/references/*.md   optional supporting documents (read on demand)
```

```yaml
---
name: auckland-orders-brief
description: Summarise regional order performance from the operations database and produce a short brief.
version: 3
requires:
  tools: [postgres__orders_summary, postgres__sample_orders, resources_create]
scope: shared           # fleet | shared | agent   (registry decides; this is the author's intent)
---
When asked for an orders brief:
1. Call postgres__orders_summary first; only sample rows if the summary is ambiguous.
2. Report by region, then call out the two largest deviations from the fleet mean.
3. Save the brief with resources_create as HTML, path briefs/<region>-<date>.html.
```

`requires.tools` is declarative. It is used to show the operator what the skill
needs and to hide a skill from an agent whose grant cannot satisfy it; it never
grants anything.

## 2. Principles

1. **Authorised by default.** A skill in an agent's scope is available unless an
   operator excludes it, disables it, or the agent's grant cannot satisfy its
   required tools. Skills are the opposite of credentials: they should flow easily.
2. **Skills never widen grants.** Tool calls a skill recommends still pass the full
   policy check (agent · session · server · tool · tier · credential grant).
3. **Three scopes, one precedence rule.** `fleet` (every active agent of this
   operator), `shared` (a named set of agents or personas), `agent` (one
   principal). An explicit exclusion always wins over any inclusion.
4. **Deliver through MCP, not through files.** Clients discover skills with the
   primitives they already implement; no client-side plugin format is required.
5. **Governed like everything else.** Versions are immutable, changes are audited,
   reads leave payload-free evidence, and content never enters the flow log.

## 3. Data model — migration 024

> Number note: this branch's 016–023 already share numbers with another branch's
> migrations. Whichever branch renumbers, `024` here means "the next free number".

```sql
CREATE TABLE plane_skills (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    slug          TEXT NOT NULL UNIQUE,                  -- [a-z0-9-]{1,64}
    name          TEXT NOT NULL,
    description   TEXT NOT NULL CHECK (length(description) <= 500),
    scope_kind    TEXT NOT NULL CHECK (scope_kind IN ('fleet','shared','agent')),
    owner_id      INTEGER NOT NULL REFERENCES service_accounts(id),  -- the operator
    state         TEXT NOT NULL DEFAULT 'active'
                  CHECK (state IN ('proposed','active','disabled','retired')),
    current_version INTEGER NOT NULL DEFAULT 1,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE plane_skill_versions (
    skill_id      UUID NOT NULL REFERENCES plane_skills(id) ON DELETE CASCADE,
    version       INTEGER NOT NULL,
    content       TEXT NOT NULL CHECK (length(content) <= 65536),   -- SKILL.md body
    frontmatter   JSONB NOT NULL DEFAULT '{}'::jsonb,               -- parsed, incl. requires
    references    JSONB NOT NULL DEFAULT '[]'::jsonb,               -- [{name, resource_id}]
    content_hash  TEXT NOT NULL,
    created_by    INTEGER REFERENCES service_accounts(id),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (skill_id, version)
);

-- Who a shared/agent skill reaches, and who is excluded from any skill.
CREATE TABLE plane_skill_bindings (
    id            BIGSERIAL PRIMARY KEY,
    skill_id      UUID NOT NULL REFERENCES plane_skills(id) ON DELETE CASCADE,
    kind          TEXT NOT NULL CHECK (kind IN ('include','exclude')),
    target_kind   TEXT NOT NULL CHECK (target_kind IN ('principal','persona')),
    target        TEXT NOT NULL,          -- service_accounts.name or persona id
    granted_by    INTEGER REFERENCES service_accounts(id),
    expires_at    TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (skill_id, kind, target_kind, target)
);

-- Payload-free lifecycle events, same shape as credential_events / plane_resource_events.
CREATE TABLE plane_skill_events (
    id            BIGSERIAL PRIMARY KEY,
    skill_id      UUID NOT NULL,
    kind          TEXT NOT NULL,          -- created, versioned, scoped, bound, unbound,
                                          -- disabled, enabled, retired, proposed, approved
    actor_id      INTEGER, actor_name TEXT,
    detail        JSONB NOT NULL DEFAULT '{}'::jsonb,   -- slug, version, scope, target; never content
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX plane_skill_events_skill_cursor ON plane_skill_events(skill_id, id);
```

Supporting `references/` files are stored as governed resources
(`plane_resources`, migration 022) owned by the operator and readable through the
skill, so the existing size caps, hashes and sandboxing apply.

The persona/agent grant JSON gains one optional key, parallel to `mcp`:

```json
"skills": { "exclude": ["legacy-job-triage"], "include": ["auckland-orders-brief"] }
```

`include` here is the `agent`-scope binding expressed inside the grant, so
`demo/provision_personas.py` can ship a persona with its skills; `exclude` is the
per-agent opt-out. The bindings table is the system of record; the grant key is
imported into it when a grant is saved.

## 4. Authorisation — the effective skill set

For principal **P** (active agent, valid session), the effective set is computed at
`skills_list`, `prompts/list` and at worker connect:

```
candidates = active skills where
              scope_kind = 'fleet'
           or scope_kind = 'shared' and an include binding matches P or P's persona
           or scope_kind = 'agent'  and an include binding matches P
effective  = candidates
           - skills with an exclude binding matching P or P's persona
           - skills whose frontmatter.requires.tools contain a tool P's grant does not allow
             (reported to the operator as "unavailable to <agent>: needs <tool>")
```

Rules:

- Exclusion beats inclusion at every level. A fleet skill can be switched off for one
  agent without touching the skill.
- `disabled` hides a skill from every agent immediately; `retired` also hides it
  from the portal's default list and freezes versions. Both take effect on the
  next `skills_list`/`prompts/list`, exactly like tool disablement (§ revocation in
  `docs/architecture.md`).
- Tier is not a skill concept. If a skill needs a tool whose `min_tier` exceeds the
  agent's, the tool rule above already hides it.
- `proposed` skills (see §7) are visible only to the operator.
- Expired bindings are ignored; the nightly cleanup deletes them.

The effective set carries a `catalogue_version` — a hash of (slug, version) pairs —
so clients can detect change cheaply.

## 5. Delivery — the same skill to any client

All delivery goes through the portal's existing `/mcp` endpoint under the agent's
session token, so every read is authenticated, session-checked and recorded.

| Primitive | What it returns | Who uses it |
|---|---|---|
| `prompts/list` / `prompts/get` | One prompt per effective skill: `name = slug`, `description`, arguments from `frontmatter.arguments` if declared. `prompts/get` returns the SKILL.md body as the user message. | Claude Code, Claude.ai, Cursor, any client with prompt support — the skill appears in the client's own skill/command UI. |
| `resources/list` / `resources/read` | `datum://skills/<slug>` (current version) and `datum://skills/<slug>@<n>`; `datum://skills/<slug>/references/<name>` for supporting files. | Clients that load context by resource; also how references are fetched on demand. |
| `skills_list`, `skills_read` (built-in tools) | `skills_list` → `[{slug, name, description, version, scope, requires}]` + `catalogue_version`. `skills_read(slug, version?)` → content. | Clients that only speak tools (the Codex worker today). Added to `BASELINE_TOOLS` so every agent has them. |
| `notifications/prompts/list_changed` | Sent on the MCP session when the effective set changes. | Long-lived clients; the worker also polls `catalogue_version` per turn as a fallback. |
| `GET /api/session/skills` | The effective set for the session's principal, JSON, same shape as `skills_list`. | Workers that assemble instructions at connect time before any MCP call. |

A skill is therefore *one* row in Datum Sync and *zero* files in any client.

## 6. The worker — adding authorised skills the easy way

The federated worker gains skills with no per-skill code:

1. **Connect.** After consent, `worker/app.py` calls `GET /api/session/skills`
   alongside the persona it already loads.
2. **Instruct.** `codex_bridge.developer_instructions()` appends a compact index —
   one line per skill, `name — description`, capped at 4 KB — and the sentence
   *"Before starting a task that matches a skill, call `skills_read` with its slug
   and follow it."* Skills under 1.5 KB whose frontmatter sets `inline: true` are
   included in full. Nothing else about the worker changes; the model pulls the
   rest through the tool it already has.
3. **Show.** A **Skills** card in the worker sidebar (`data-card="skills"`) lists
   the effective skills with their scope badge (Fleet / Shared / Agent) and a
   "read this session" marker, fed by the session-local trace. Read-only, like the
   other cards.
4. **Refresh.** On `notifications/prompts/list_changed`, or when a turn's
   `skills_list` reports a new `catalogue_version`, the worker rebuilds the index
   for the *next* turn. A disabled skill disappears without a reconnect, matching
   tool revocation.

Because the worker consumes the same MCP surface, swapping Codex for another model
client changes nothing about skills.

## 7. Operator surface

**Skills page** (portal nav, under Authentication with Principals and Secrets & Proxies):

- List: name, slug, scope badge, state, current version, "reaches N agents",
  "unavailable to M (needs …)", last read.
- **Add skill**: paste markdown, upload `SKILL.md`, or upload a `skills/<slug>/`
  zip with references. Frontmatter is parsed; `requires.tools` is validated against
  the registered tool catalogue and unknown tools are flagged, not rejected.
- **Scope**: Fleet / Shared (pick agents and/or personas) / Agent (pick one).
  Changing scope writes a `scoped` event and rebinds.
- **Versions**: a new save creates version n+1; old versions remain readable at
  `@n`; "pin agent X to version n" is an include binding with a version field
  (deferred — see open questions).
- **State**: Disable / Enable / Retire. Disable is the fast revoke.
- **Exclusions**: from Principals → MCP access → Skills tab, toggle any effective
  skill off for that agent; from the skill page, "exclude persona".

**Proposals.** An agent may call `skills_propose(name, description, content)`
(tier 3, `mcp:operate`). It creates a `proposed` skill owned by the operator,
visible only in Approvals, with the proposing agent recorded. Approve → `active`
with the chosen scope; reject → `retired`. This is how a fleet can learn from its
agents without agents authoring each other's instructions.

**Import/export.** `demo/skills.py import skills/ --scope fleet` loads a directory
of Agent Skills; `export` writes the registry back to the same layout, so a skill
library can live in git and be reviewed like configuration.

## 8. Evidence and audit

- MCP flow events: `skills.listed` (count, catalogue_version) and `skills.read`
  (slug, version) are recorded in `mcp_flow_events` through `mcp_observe.mark` —
  identifiers only, never content, consistent with every other flow.
- `plane_skill_events` records create / version / scope / bind / unbind / disable /
  enable / retire / propose / approve with actor and target.
- Operator-side writes to skills are governance-class actions, the same class the
  vault already assigns to `skills/**`.
- Deterministic answer to "what instructions was this agent following at 14:02?":
  the flow shows `skills.read auckland-orders-brief@3`; version 3 is immutable.

## 9. Security stance

- **Skills cannot escalate.** Every recommended call is policy-checked at call
  time; `requires.tools` is advisory.
- **Instructions are a trust boundary.** Only operators create `active` skills.
  Agent-authored content enters as `proposed` and needs human approval. Shared
  resources cannot be promoted to skills without that step.
- **No secrets in skills.** Save rejects content matching the credential patterns
  already used by the credential-access review (bearer/API-key shapes, PEM blocks)
  and warns on URLs with embedded credentials.
- **Bounded.** 64 KB per version, 4 KB inline budget in the worker, references
  served through the sandboxed resource path with the resource size cap.
- **Payload-free evidence.** Skill content never appears in `mcp_flows`,
  `plane_audit` or the worker's trace; slugs and versions do.
- **Content is data to the client.** Workers present skills as developer
  instructions from Datum Sync, not as user turns, so a client can attribute them
  correctly.

## 10. Build plan

| Increment | Scope | Tests |
|---|---|---|
| **S1 · Registry + tools** | Migration 024; `datum_sync/skills.py` (CRUD, effective-set, versioning); `skills_list`/`skills_read` in `BASELINE_TOOLS`; `GET /api/session/skills`; flow events | `tests/test_skills.py`: scope precedence, exclusion wins, requires-tools hiding, disable takes effect next list, versions immutable, content absent from flows |
| **S2 · Worker** | `datum_sync_client.skills()`; instruction index in `codex_bridge`; Skills card; refresh on catalogue change | `worker/tests/test_skills_index.py`: index budget, inline rule, refresh on version change |
| **S3 · Portal** | Skills page, scope/state/exclusion controls, SKILL.md and zip import, Principals → Skills tab | `test_portal.py` additions; `browser_smoke.py` scene |
| **S4 · MCP primitives** | `prompts/list`/`get`, `resources/list`/`read` for `datum://skills/*`, `list_changed` notification | `test_mcp_federation.py` additions; a Claude Code client smoke |
| **S5 · Proposals + library** | `skills_propose`, Approvals entry, `demo/skills.py import/export` | `test_skills.py` proposal lifecycle |

S1 and S2 together are enough for the worker to use fleet-wide skills; S3 makes it
operable; S4 makes it provider-agnostic in the standard sense.

## 11. Open questions

1. **Version pinning per agent** — needed for reproducible long-running agents, or
   is "current version, immutable history" enough for v1?
2. **Persona as a binding target** — personas are demo bundles today
   (`persona_profiles.py`), not a table. Either promote them to a `plane_personas`
   table or bind to principals only until that exists.
3. **Tenancy** — `owner_id` is the operator; when tenants arrive, `fleet` means
   "this tenant's fleet". Nothing else changes.
4. **Skills that need files** — should `references/` also allow small datasets
   (CSV/GeoJSON) or only documents? The resource layer already supports both.
5. **Should a skill be able to declare `deny.tools`** to *narrow* what the agent
   uses while following it? It would be advisory only, but useful for the model.
