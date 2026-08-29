# Datum Sync — Holonic Architecture and SHACL Analysis

> **Date:** 2026-08-29
> **Author:** Researcher persona, Datum
> **Status:** Reference — informs PROPOSAL.md Phase 1+ revisions
> **Sources:** W3C Holon CG research (`~/vault/research/holons-shacl-w3c-community-group-cited.md`),
>   PROPOSAL.md, live :5435 schema

---

## 1. Purpose

This document maps the Datum Sync Agent Hub proposal (PROPOSAL.md) against two
architectural paradigms — holonic multi-agent systems and SHACL-enforced boundary graphs
— and identifies where each adds value, where it is overhead, and what concrete changes
(if any) are warranted.

The conclusion is: **the architecture is already holonic**. The framing adds two things
of substance: a correction to the drone authority model, and a motivation to reorder
the build phases. SHACL is valuable in one specific place — vault_scope validation at
account write time — and nowhere else in the current build.

---

## 2. Background: Holons and SHACL

A **holon** (Koestler, 1967) is an entity that is simultaneously an autonomous whole and
a dependent part of a larger system. The W3C Holon Graph Community Group (June 2026,
chair: Kurt Cagle) formalises this as four interrelated graph components per holon:

| Component | Role |
|---|---|
| Knowledge graph | What the holon knows — entities, relationships, state |
| Context/event graph | What has happened — history of actions and changes |
| Boundary graph | What is permitted — constraints on what can enter or exit |
| Projections | What it exposes — views of internal state filtered for external callers |

A **holarchy** is a recursive nesting of holons. The Moderated Group pattern defines
a Head holon (governs the boundary, brokers inter-member communication) and Member holons
(full local autonomy within their boundary).

**SHACL** (W3C Shapes Constraint Language) is the enforcement mechanism for boundary
graphs. Where OWL defines what is logically possible, SHACL defines what is structurally
permitted: cardinalities, value ranges, required properties, forbidden patterns, and
cross-property constraints expressible via SPARQL.

---

## 3. The Current Architecture Is Already Holonic

Map the four holon components onto the live :5435 schema:

| Holon component | Datum Sync implementation |
|---|---|
| **Knowledge graph** | `repositories` + `workspaces` + `workspaces.manifest` — the interface catalogue, what the hub knows how to do |
| **Context/event graph** | `jobs` + `job_log` + `automation_runs` — the history of all execution |
| **Boundary graph** | `service_accounts` — `max_tier`, `repo_scope[]`, `connection_grants[]` — what each agent is allowed |
| **Projections** | `mcp.py → catalogue()` — `tools/list` output per principal, filtered by boundary |

The hub/agent relationship is a Moderated Group holarchy:

- **Datum Sync (VM112) = Head holon.** Holds the job engine, boundary enforcement, vault
  gate, and projection layer (MCP endpoint). Authority is structural: the :5435 database
  and vault NFS mount are only accessible through it (once vault gate is live).
- **Each agent VM = Member holon.** Datum (VM102), research drones (VM111) retain full
  local autonomy for operations that do not cross the boundary. A `service_accounts` row
  IS the membership record for that member holon in the hub registry.

The holonic framing does not change the code. It clarifies why the design is correct and
surfaces two gaps described in §4.

---

## 4. What the Holonic Frame Fixes

### 4.1 Drone sub-holon authority (design correction)

The handoff note flags this: "drone sub-holons need correct modelling — authority derived
at dispatch, not independently granted — privilege escalation path via drone promote scope."

The correct model: a research drone is a **transient member holon**, instantiated at
dispatch and dissolved on completion. Its authority is a **delegated slice** of the
dispatching agent's vault scope, not an independent grant to the drone's own service
account.

Implication for the schema: `jobs.params` should carry the delegated vault scope for that
job, and the worker validates that the drone's actions stay within it. A drone that tries
to write outside its delegated slice gets 403 from the vault gate, not from its own
account-level scope.

This prevents a privilege-escalation path: a drone service account granted `quarantine/**`
write scope could not be exploited to promote beyond its parent's `promote` scope, because
the job-level scope is the intersection of account scope and dispatching agent's delegation.

**Schema change required:**
```sql
ALTER TABLE jobs ADD COLUMN delegated_vault_scope JSONB;
```

The vault gate checks `delegated_vault_scope` when set, falling back to the account-level
`vault_scope` for non-delegated jobs (direct agent calls).

### 4.2 Phase ordering (build correction)

The current PROPOSAL orders phases: Vault Gate → Hosted Proxy → Production → Onboarding.

The holonic framing surfaces the error: **you cannot onboard member holons before the
boundary exists.** Vault Gate is the boundary graph. Without it, adding agents to the hub
adds members without a membrane.

The hosted service proxy is a projection (convenience, not structural). Doing it second
implies it is load-bearing for governance. It is not.

Revised phase order:

| Phase | Goal | Reason |
|---|---|---|
| 1 | Vault Gate | Establish the boundary graph — the structural prerequisite for everything else |
| 2 | Production Deployment | Make the Head holon persistent before onboarding any members |
| 3 | Agent Onboarding | Register member holons against a stable, running boundary |
| 4 | Hosted Service Proxy | Add projections once the core is solid |

---

## 5. Where SHACL Adds Value

### 5.1 vault_scope structural validation (recommended — high payoff)

The PROPOSAL specifies `vault_scope` as a JSONB column with `read`, `write`, `quarantine`,
`promote`, and `deny` keys containing glob arrays. Runtime enforcement is handled by the
Python path validation module in `datum_sync/vault.py`.

The gap: Python code validates that calls conform to the scope. Nothing validates that the
scope itself is internally consistent. An account could be created with `promote` scope but
no `quarantine` scope, which is structurally incoherent (you cannot promote what you cannot
write to quarantine). This failure surfaces at runtime, not at account creation.

SHACL closes this gap. A `VaultScopeShape` validates the scope at write time — in
`accounts.py` before the INSERT/UPDATE — rather than failing at call time.

Example constraints the shape encodes:

- `promote` requires `quarantine` (cannot promote without quarantine write access)
- `write` on a path implies `read` on the same path (write-only is incoherent)
- `deny` patterns are disjoint from `read`/`write` (deny wins — so overlapping them is
  a configuration error, not a runtime branch)
- A `max_tier=1` account cannot be granted `promote` scope (tier enforcement in the shape)

**Implementation:**
```python
# In accounts.py, before DB write:
from pyshacl import validate as shacl_validate

def _check_vault_scope(scope: dict) -> None:
    """Raises ValueError if scope is structurally incoherent."""
    graph = _scope_to_rdf(scope)          # thin converter, ~30 lines
    conforms, _, report = shacl_validate(
        graph, shacl_graph=VAULT_SCOPE_SHAPE
    )
    if not conforms:
        raise ValueError(f"Invalid vault_scope: {report}")
```

Shape file: `spec/shapes/vault_scope.ttl` (Turtle, stored in the repo, loaded once at
startup). No schema change required. One `pip install pyshacl` dependency.

**Do not run pySHACL on the job submission hot path.** 50–100ms per call is acceptable at
account creation; it is not acceptable at `tools/call` time.

### 5.2 Workspace manifest as a latent shape (low effort, future value)

`manifest.py` uses Pydantic for parameter validation at workspace registration time. This
validates that parameters conform to their declared types. It cannot reason across
workspaces: does the output of `site_plan` satisfy the input schema of `scimac_summary_report`?

Extending `manifest.py` to emit a SHACL shape representation alongside the normalised
JSONB costs little now and enables cross-workspace reasoning later (the deferred pipeline
feature, port 5274). The shape would be stored as a second key in `workspaces.manifest`,
not executed at runtime.

**Not recommended for the current build.** Flag for the pipeline phase.

### 5.3 Agent capability declarations (future, when agents > 5)

Each `service_account` currently declares permissions (what it is allowed). A SHACL
capability shape would declare capabilities (what it IS): produces `scimac-report` output,
consumes `vault/dev/**` reads, emits `job_log` entries. The hub validates at onboarding
that declared capabilities are consistent with granted scopes.

With 2 service accounts this is overhead. At 10+ agents (Hermes, OpenClaw, multiple drone
types) it becomes necessary. Flag for Phase 3 (Agent Onboarding), not before.

---

## 6. Where Neither Adds Value

| Approach | Why not |
|---|---|
| Full RDF/SPARQL stack for all data | The existing Postgres schema is well-designed and working. SPARQL over SQL is overhead with no benefit at this scale. |
| SHACL on the job submission hot path | pySHACL is 50–100ms. Pydantic manifest validation already runs here. Do not stack them. |
| Dynamic SHACL shape inference | The SHACL/xLAM-60k prototype (researcher tasks) is about inferring constraints from observed tool calls. For Datum Sync, constraints are declared at workspace registration time. Inference not needed. |
| Replacing `job_log` with an RDF event graph | `job_log` + `jobs` IS a context/event graph already. No benefit in re-encoding it. |

---

## 7. Summary of Recommended Changes

### Immediate (Phase 1 — Vault Gate)

1. **Add `delegated_vault_scope JSONB` to `jobs`** — drone authority model (§4.1).
   Migration: `migrations/007_vault_scope.sql` (extend the migration the PROPOSAL already
   planned for `service_accounts.vault_scope`).

2. **Add pySHACL validation to `accounts.py`** — vault_scope coherence at write time (§5.1).
   - Add `pyshacl` to `requirements.txt`
   - Write `spec/shapes/vault_scope.ttl`
   - Call `_check_vault_scope()` in account create/update before DB write

3. **Reorder build phases** per §4.2 — Vault Gate → Production → Onboarding → Proxy.

### Deferred (Phase 3 — Agent Onboarding)

4. **Capability shape column on `service_accounts`** — when agent count justifies it.
   `ALTER TABLE service_accounts ADD COLUMN capability_shape TEXT` (Turtle, nullable).

### Not recommended

- RDF store, SPARQL queries, full holon ontology, runtime SHACL on hot paths.

---

## 8. Reference

- W3C Holon Graph Community Group: https://www.w3.org/community/holon/
- SHACL W3C Recommendation: https://www.w3.org/TR/shacl/
- Research digest: `~/vault/research/holons-shacl-w3c-community-group-cited.md`
- Holonic MAS paper digest: `http://192.168.88.102:8190/static/holonic-mas.html`
- Adaptive Holonic Architecture paper: `~/vault/_inbox/2026-08-29/Adaptive Holonic Architecture.pdf`
- Datum Sync PROPOSAL: `PROPOSAL.md`
