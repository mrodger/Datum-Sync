# 01 — Vision

## 1. The problem, as inferred from the original

A small organisation runs a growing set of AI agents alongside a handful of
people. The agents need to do real work: read and write a shared knowledge
vault, run data-processing scripts against production databases, call
third-party APIs that need keys, and leave results where humans and other
agents can find them. The people need to do the same things, and they need
to let external collaborators in at a distance (a consultant on Claude.ai, an
engineer who only reads email).

Every one of those actions crosses a trust boundary. The original's answer
is a single deterministic gateway that sits on the boundary and holds all of
the authority: credentials, scopes, the queue, the audit log. Agents reason
elsewhere; the gateway enforces. Governance is structural — a scope, a tier,
a rate limit, a log row — and never a judgement about intent.

That is the problem Datum-Gate solves too.

## 2. One sentence

Datum-Gate is the deterministic infrastructure layer every principal — human
or agent — connects to for vault access, job execution, tool discovery,
credential use and hosted-service delivery. It contains no model, calls no
model, and evaluates no intent. It enforces grants, runs subprocesses, and
records everything.

## 3. Principles

1. **One identity model.** A human at a browser, a script with a token, an
   MCP client with an OAuth grant, and an agent dispatched by another agent
   are all *principals*. They differ in how they authenticate and in what
   they are granted, never in what kind of thing they are to the system.

2. **Authority is a document, and it only narrows.** Every principal holds a
   single grant document. A principal created by another principal cannot
   hold more than its creator. A job carries a frozen copy of the grant it
   was submitted under, so the authority a run executes with is the authority
   it was given, not the authority its owner has by the time it finishes.

3. **The database is the system of record for everything, including time.**
   Queues, leases, schedules, deliveries and sessions are rows. `pg_notify` is
   a hint that a row changed, never the only record of it. Any process may be
   killed at any instant and nothing is lost, only delayed.

4. **Workspaces declare their interface before they run.** A workspace is
   callable only after its manifest, documentation, connections and outputs
   have been checked at publish time. Publishing is versioned and atomic; a
   broken publish leaves the previous version live.

5. **Secrets have exactly one door.** Credentials are sealed at rest, never
   returned by any read path, and reach workspace code only as a resolved
   object in a subprocess whose environment has been stripped. Agents that
   need a key never see it — they proxy through the connection.

6. **The gateway never guesses.** Scope patterns are literal. Deny wins.
   A 403 is never softened to a 404 to be polite, and a 404 is never
   upgraded to a 403 to be safe — each is chosen for the enumeration risk it
   carries and documented where it is chosen.

7. **MCP is the primary agent surface; REST is the primary programmatic
   surface; both are projections of one catalogue.** The list of tools an
   agent sees is the list of workspaces its grant reaches, and nothing else.

8. **Every guard has a test that fails when the guard is deleted.** Security
   behaviour is registered, not just implemented (`17-security-guards.md`).

9. **Connection is capability.** An agent on its own has a model and a local
   loop. Registering with the gateway and being authorised is what gives it
   the company's memory, code, machines and documents — through one
   endpoint, filtered by one grant, recorded in one log. Onboarding is one
   principal, one grant, one token, one URL.

## 4. What Datum-Gate is not

| It is not | It is |
|---|---|
| An agent, orchestrator or LLM wrapper | A job engine and authority server agents call into |
| A reasoning or routing layer | A rule enforcer: grants, schemas, limits, logs |
| A visual workflow builder | A runner with a stable seam a builder can target |
| A file server | A scoped, audited gate in front of a filesystem |
| A full MCP relay | A tool and resource broker: it federates upstream MCP servers' tools and resources under grant and argument guards, and does not relay prompts, sampling or notifications |
| A replacement for local agent autonomy | The boundary local autonomy stops at |

## 5. The holonic framing, kept

The original describes itself as the **Head holon** in a moderated-group
holarchy. Datum-Gate keeps that framing because it maps cleanly onto the
data model and tells the implementer what each table is *for*:

| Holon component | Datum-Gate implementation |
|---|---|
| Knowledge graph — what the holon can do | `repositories`, `workspaces`, `workspace_versions` (the published catalogue) |
| Context/event graph — what has happened | `jobs`, `job_log`, `audit_log`, `deliveries`, `promotions` |
| Boundary graph — what is permitted | `principals.grant` and the delegation tree (`principals.parent_id`) |
| Projections — what is exposed to whom | MCP `tools/list` and `resources/list`, REST listings, `/serve/` — every one filtered through the caller's effective grant |

Each connected agent is a **member holon**: it retains full local autonomy for
anything that does not cross the boundary, and it exists in the hub only as a
`principals` row. A transient sub-agent (a research drone) is a member holon
whose row has a `parent_id` and whose grant is a strict narrowing of its
parent's. When the parent is narrowed or disabled, the child follows.

## 6. Who uses it

Inferred from the reference case study and the account tables:

| Persona | How they arrive | What they need |
|---|---|---|
| **Builder** (in-house technical staff) | Web UI, CLI on the box, Claude.ai over MCP | Publish workspaces, manage connections and principals, watch jobs |
| **Operator** (technical collaborator) | Claude.ai / other MCP client with OAuth | Run workspaces by asking, get results inline, check status |
| **Recipient** (non-technical collaborator) | Email only | Receive deliveries; never touch the system |
| **Long-lived agent** (Datum, OpenClaw, Hermes) | Bearer token in MCP config | Vault tools, job tools, proxied API access, hosted-service publishing |
| **Transient agent** (research drone) | Bearer token minted by its dispatcher | A delegated slice of the dispatcher's vault and job authority, for the life of one task |
| **Superuser** | CLI + UI | Bootstrap, key rotation, cross-scope audit |

## 7. Success looks like

- A new agent is onboarded by creating one principal with one grant and
  handing it one token. It sees exactly the tools its grant reaches.
- A workspace goes from a directory to a callable tool with one `sync`, and
  a broken edit never takes a working version down.
- Any action by any principal is answerable from one table: who, via what
  surface, on what target, with what outcome, under which trace.
- Killing any process at any moment loses nothing except wall-clock time.
- Deleting any security check in the source makes at least one named test
  fail.
