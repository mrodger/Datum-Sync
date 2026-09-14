# Demo script — Datum Sync agent auth plane

Twenty minutes: two of setup framing, twelve of live product, five of production
considerations, one to close. Every click, every prompt, every sentence. The
production points are woven into the scenes rather than left for the end, so the
audience hears *why* at the moment they see *what*. Fallback for every scene is the
matching clip in `docs/walkthrough/video/`.

---

## Before the room

**Thirty minutes before**

```bash
cd <repository root>
python demo/manage.py restart
python demo/provision_personas.py --clean-test-artifacts --register
codex login status                      # must say signed in
```

Open three browser tabs at 100 % zoom on the presenting display, in this order:

1. **Portal** — <http://127.0.0.1:8210> — sign in as `operator` (password from `.runtime/operator.txt`, then close that file; it must not be on screen)
2. **Worker** — <http://127.0.0.1:8220>
3. **Portal → MCP Activity** — a second portal tab parked on MCP Activity for the evidence beats

Warm-up so the first live turn is not the slow one: in the worker, connect as `researcher` (15 minutes), ask **"List the Office documents I can access."**, wait for the answer, **Disconnect**. Then in Secrets & Proxies confirm every tool shows `active` (scene 7 leaves one disabled if a rehearsal was interrupted).

Close DevTools, terminal windows, and anything showing environment variables.

**Have ready**: `docs/walkthrough/video/` clips in a player, the brochure PDF, this script on a second screen.

---

## 0 · Framing — 2 min

*Portal tab, Dashboard visible.*

> "Every agent deployment eventually meets the same question: what can this thing actually do, and what happens when we need it to stop? Most answers today are a shared API key in a prompt and a hope.
>
> Datum Sync is an authority plane for a fleet of agents. It contains no model. It gives each agent an identity, a permission bundle and one governed route to tools — and it keeps the credentials behind those tools where the agent can never reach them.
>
> I'll show you the running system for about twelve minutes, then talk about what it takes to run something like this in production."

Point at the nav: *Principals, Approvals, Secrets & Proxies, MCP Activity, Resources.* "Those five surfaces are the whole story."

---

## 1 · The problem — 1 min

**Do**: Principals → find `researcher` → **MCP access**.

**Say**:
> "The Researcher is a first-class principal. It has an owner, a lifecycle state, and this grant — exact tools on exact servers. What it does *not* have is an Office credential, a database password or an upstream token. It has permission to ask; Datum Sync holds everything else."

**Point at**: effective grants, live sessions (none yet), recent flows.

**Production point** — *Identity*: "An agent is a principal with an audit trail, not a key pasted into a prompt. That's the foundation everything else stands on."

**Do**: Escape to close.

---

## 2 · The service boundary — 1 min

**Do**: Secrets & Proxies. Scroll slowly once to the tools table.

**Say**:
> "Operators register MCP servers once. Tools are discovered, and each one is individually enabled with a minimum tier. Upstream credentials are stored write-only — versioned, rotated only after a live probe, and injected at dispatch. The value never goes to a browser, a worker, or a model."

**Point at**: the three servers (OfficeCLI demo, PostgreSQL orders demo, Datum Harness tools), a tool row with its `active` badge and `Disable tool` button, the managed identity fingerprint.

**Production point** — *Least privilege*: "Grants name tools, not servers. Nothing is inherited."

---

## 3 · Connect the worker — 1.5 min

**Do**: Worker tab. Pause on the four status cards.

**Say**:
> "This is the worker — deliberately small. Codex owns the conversation loop. Datum Sync owns every operational capability. Shell, browser, web search, computer use — all disabled in its profile. Its only route to the world is the MCP endpoint you just saw."

**Do**: **Connect agent** → consent dialog → Agent `researcher`, Session length **15 minutes · Demo** → **Approve connection**.

**Say** (while the dialog is up):
> "Consent, not configuration. I choose the agent and how long. Fifteen minutes for a demo; two, four or eight hours for real work. That deadline is absolute — refresh can't extend it."

**Point at** (after return): the green **GOVERNED** pill, *Researcher* in the header, the three starter prompts, the Authority Boundary footer in the sidebar.

**Production point** — *Bounded sessions*: "When the deadline passes, the worker doesn't crash — it just stops being able to ask."

---

## 4 · A real task, and the evidence — 2.5 min

**Do**: Click the starter prompt **"Review the quarterly brief and test its claims against the available operations data."** Wait for the answer (20–60 s). Keep the run trace pane visible.

**Say** (while it runs):
> "Codex is choosing an MCP tool. Watch the trace on the right: server, tool, state, duration. That's all the worker ever records — no arguments, no document text."

**Do** (after the answer): sidebar → **MCP activity** card. Show the session-local list. Close.

**Do**: switch to the **MCP Activity portal tab** → **Refresh** → top row → **Timeline**.

**Say**:
> "Same call, operator's view. Receipt, authentication, session validation, tool authorization, upstream dispatch, completion. Deterministic, replayable — and payload-free. No prompt, no argument, no result, no secret. Denied calls land in this same stream, which is what makes it safe to retain and export."

**Production point** — *Evidence without payloads*: "You can prove what happened without keeping what was said."

**Do**: Escape.

---

## 5 · The credential never moves — 1.5 min

**Do**: Worker → type **"Summarize the available operations data and call out the regional differences."** → Send. Wait.

**Say**:
> "The PostgreSQL provider accepts fixed, parameterised reads — not SQL. Before dispatch, Datum Sync checked the agent, the session, the server, the exact tool, the tier, and that a credential grant exists. Then it unsealed the database credential *server-side* and injected it. The worker process never held it."

**Do**: sidebar → **Secrets & proxies** card.

**Point at**: label, fingerprint, version. "That's the agent's entire view of a credential."

**Production point** — *Credential brokering*: "Write-only, versioned, rotated after a live probe, injected at the edge. Keys go to an HSM or KMS in production; the shape doesn't change."

---

## 6 · Real capabilities, bounded egress — 1.5 min

**Do**: type **"Resolve Crassostrea gigas and Perna canaliculus with WoRMS, compare their classifications, and use the persona catalogue to recommend who should continue the work."** → Send. Then **"Create a Wellington Leaflet map and explain which evidence you would add next."**

**Say**:
> "These are real harness tools behind the same plane. Persona discovery reads real configuration. The taxonomy lookup is a bounded, read-only call to the public WoRMS API — only the two names leave the machine. The map is rendered locally: no credential, no file write."

**Production point** — *Egress control*: "Every credentialed outbound call goes through a pinned-IP transport: DNS resolved once, private ranges refused, redirects refused, response capped. In production that's also enforced outside the process — a container per worker with its own egress policy."

---

## 7 · Live revocation — 2 min · *the scene that lands*

**Do**: Portal → Secrets & Proxies → row `postgres__orders_summary` → **Disable tool**. Badge turns `disabled`.

**Say**:
> "I'm taking one tool away. Not the server, not the agent — one tool."

**Do**: Worker → repeat **"Summarize the available operations data and call out the regional differences."** → Send.

**Say** (as the denial or the changed catalogue appears):
> "Refused, or simply absent — on the very next call. No worker restart. No credential to rotate inside the agent, because it was never given one."

**Do**: MCP Activity tab → **Refresh** → point at the `denied` row.

> "And the refusal is recorded next to the successes."

**Do**: Secrets & Proxies → **Enable tool**.

**Say**:
> "Tool, server, grant and agent are four independent levers. In production they become reviewed configuration — a policy file with a diff and a signature — not portal clicks."

**Production point** — *Revoke immediately, separately.*

> Fallback: if the worker's turn is slow, narrate over the clip `07-revocation.webm`.

---

## 8 · Fleet review — 45 s

**Do**: Principals → `researcher` → **MCP access**.

**Say**:
> "The operator's three questions, on one screen: which servers can this agent reach, which grants make that effective, and what has it called in the last hour — including the call I just refused."

**Production point** — *Tenancy is a seam*: "Every record already attaches to a principal and a grant. Tenant ownership and operator roles apply to the same rows — that's the next increment, not a rewrite."

---

## 9 · Governed outputs — 1.5 min

**Do**: Worker → **"Create a small HTML artifact that summarizes the Auckland results and save it for this agent."** → Send. When done: pane → **Artifacts** → click the new item → **⛶** (focus). Escape to restore.

**Say**:
> "Output is saved through Datum Sync as a versioned, private resource — owner, path, hash, source trace, expiry. HTML renders in a sandbox with no network. Content stays out of the audit trail; ownership and shares stay in it."

**Do**: **"Share this artifact with designer."** → Send. Portal → **Resources** → point at the share row.

**Say**:
> "A share is a named, time-bounded read grant to one agent in the fleet. The operator sees it here and can revoke it."

---

## 10 · Close the session — 30 s

**Do**: Worker → **Disconnect**. Then MCP Activity tab.

**Say**:
> "Disconnect terminates this browser's Codex process and discards its short-lived token. Upstream credentials never left Datum Sync. What remains is the evidence."

---

## 11 · Production considerations — 5 min

*Switch to the brochure PDF, page 6, or speak to the table below. Each row is something the audience just watched.*

| You saw | What it proves | What production adds |
|---|---|---|
| Principals, MCP access | **Identity** — agents are principals with owners and audit trails | Tenant ownership, operator roles on the same records |
| Per-tool enable, grants by name, session deadline | **Least privilege** — exact tools, tier ceilings, absolute expiry | Policy as reviewed configuration in git, applied with a diff |
| Fingerprint-only credential card, server-side injection | **Credential brokering** — write-only, versioned, probe-before-rotate | HSM / KMS-backed keys, per-tenant key sets |
| WoRMS call, refused-redirect transport | **Egress control** — pinned IP, private ranges refused, capped responses | Network policy outside the process: container or OS identity per worker |
| MCP Activity timeline, denied row | **Evidence** — deterministic, payload-free, denials included | Signed export, SIEM forwarding, retention and legal hold |
| Worker status cards, sandboxed artifact | **Isolation** — no secrets in the worker, local tools off | Attested client profile on connect; one worker per container |

**Say**:
> "The prototype already holds the shape of each answer. The right-hand column is the honest gap list, and none of it changes the schema — every row is an addition on the same principal, grant, session and evidence records."

Then the roadmap, one breath each:

- **Approval-gated calls** — a human yes on a specific tool call, with a bounded wait. The pending-call table already exists.
- **Budgets** — calls, bytes, spend per agent and session. "Stop it at fifty dollars" is a revocation control.
- **Skills** — provider-agnostic instruction bundles, fleet / shared / per-agent, authorised by default, delivered over MCP. Spec on the branch.
- **Transcripts** — sealed in Postgres, row-level isolated per agent, nothing durable on the worker host. Spec on the branch.
- **Live dashboards** — governed streams that jobs and agents write into, rendered as cards in the worker. Spec on the branch.

---

## 12 · Close — 30 s

> "Connect to a useful agent while identity, MCP access, credentials, evidence and artifacts remain governed in one place. That's Datum Sync. Questions?"

---

## Likely questions

**"Why not just put the key in the agent's environment?"**
Because then every prompt injection is a credential theft, and revocation means rotating a secret that's already been copied. Here the agent has nothing to copy.

**"Does this only work with Codex?"**
No. The worker is a demonstration client. Anything that speaks MCP — Claude Code, Claude.ai, Cursor, Hermes, OpenClaw — connects the same way, gets the same policy and the same evidence. The model client is the replaceable part.

**"What does the operator see of the conversation?"**
Nothing, by design. Datum Sync records who called what and when; it never stores what was said. The transcripts spec adds a sealed, per-agent-isolated store where operator reads are an explicit, audited action.

**"Is this multi-tenant?"**
Single operator today. Every record already carries an owner, so tenancy is applying roles and isolation to existing rows rather than redesigning them.

**"How does it fail?"**
Closed. If Datum Sync is unreachable the worker cannot call any tool; there is no cached credential to fall back on. Session expiry produces a denial, not a crash.

**"Are the demo providers real?"**
The PostgreSQL provider is a real database behind fixed reads. The OfficeCLI provider is OfficeCLI-shaped with three sample documents. The harness tools read real persona configuration and call the real WoRMS API. Say so if asked; don't oversell.

**"What about prompt injection through tool results?"**
The plane keeps tool results out of the evidence and never lets a result satisfy an authorization. Marking tool output as untrusted provenance for the client, and per-tool response filtering, are on the production list.

---

## If it goes wrong

| Symptom | Do |
|---|---|
| Worker won't answer | `codex login status` → `python demo/manage.py restart`; narrate over the clip |
| Tool missing from the catalogue | Secrets & Proxies — re-enable whatever a rehearsal left disabled |
| Consent dialog doesn't appear | Portal tab must be signed in; sign in, then retry **Connect agent** |
| Fleet looks messy | `python demo/provision_personas.py --clean-test-artifacts --register` |
| Anything else | Every scene exists as a standalone video; the story doesn't depend on the live run |
