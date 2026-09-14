> Snapshot review: findings below describe the supplied baseline. See [DELIVERY.md](DELIVERY.md) for implemented repairs, prototype scope and remaining legacy work. `checks.py` is historical defect evidence, not a post-fix test suite.

# Datum-Sync review — 13 September 2026

The v1 tree is a substantial workspace execution platform with authentication, a credential proxy, vault access, OAuth, auditing, and a meaningful security-test structure. The v2 documents make a coherent proposal for an authority server and MCP gateway. I would **not implement v2 unchanged or treat the supplied system as production-ready for mutually untrusted agents**. Several proposed rules permit privilege escalation, and some current credential boundaries need repairs first.

This is a source and design review, not a penetration test or deployment certification. I reviewed all 13 top-level v2 documents and selected security-critical v1/harness modules, migrations, and tests. I did not exhaustively review the older Datum-Gate corpus, UI assets, every workspace, or every harness endpoint. Documentation describing a live deployment is not evidence of its current configuration.

## Highest-priority findings

### 1. Critical — v2 lets an elevated caller issue a stronger, durable token

**Design defect.** [Token API](../Datum-sync%20v2/07-api-surface.md#1-principals-replaces-restv1accounts-which-stay-as-aliases-for-one-release), [credential ceilings](../Datum-sync%20v2/03-principals-lifecycle-sessions.md#3-credentials).

Self-service token creation requires tier 3, but the requested cap is checked against the principal's stored tier. A tier-4 principal using an OAuth token scoped to `mcp:operate` has effective tier 3 and can therefore request a non-expiring account token capped at 4, or omit the cap. That bypasses both the OAuth scope ceiling and the intended temporary elevation. Even a tier-3 principal can turn temporary operate access into durable operate access.

**Required change:** distinguish permission to use authority from permission to mint durable credentials. Minted credentials must not exceed the caller's effective authority or authorized lifetime; issuing a broader or longer-lived credential needs separate approval. Apply the same rule to delegated principals, enrolment codes, schedules, and automation creation. Test a tier-4 principal holding only `mcp:operate` attempting to mint tier-4 and unlimited-lifetime credentials.

### 2. Critical — the proposed migration upgrades existing agent tokens to admin

**Design/SQL defect.** [Migration 016/017](../Datum-sync%20v2/06-schema.md), [current agent resolution](../Datum-sync%20v1/datum_sync/auth.py#L324).

V1 explicitly sets `is_admin=False` for agents, even when their parent has tier 4 or 5. Migration 017 copies that tier, migration 016 derives admin from it, and the token migration preserves the old token without a cap. An old non-admin agent token thus gains admin access. Existing tier-4 non-admin accounts are also promoted. Showing this in a report or UI does not preserve existing permissions. The paragraph after migration 017 even contradicts itself about whether the trigger sets admin false.

There are two further migration problems: v1 agent names and account names occupy separate unique namespaces, so valid existing name collisions can abort the insert; and parents start with empty `proxy_grants`, while migrated children retain theirs. Ancestor intersection then removes the children's proxy access, or strict narrowing validation rejects the data.

**Required change:** perform a preflight and explicit identity mapping; preserve operation-level permissions and cap migrated credentials before making them usable. Define how existing proxy grants map into parent/child authority without broadening either. Test non-admin high-tier accounts, high-tier agents, cross-table name collisions, and nonempty proxy grants.

### 3. High — custom credentials leak across proxy redirects

**Current code defect; isolated check confirmed.** [Proxy redirect loop and stripping](../Datum-sync%20v1/datum_sync/proxy.py#L255).

`inject_auth` supports arbitrary credential headers. `_strip_auth` removes only `Authorization`. An `X-API-Key` credential is therefore sent to a redirected public origin. A connection exposing a caller-influenced redirect endpoint can disclose its key while passing the SSRF checks. The v2 assessment incorrectly recommends keeping this redirect handling unchanged.

**Required change:** reject cross-origin redirects for credentialed requests, or rebuild outgoing headers from an explicit safe set and re-inject credentials only for an authorized origin. Include custom headers and credentials embedded in redirect URLs in the tests. Header handling must be case-insensitive.

### 4. High — harness authentication has unsafe deployment defaults

**Current code defect; configuration check confirmed.** [Harness authentication](../Datum-harness/auth.py#L114), [session configuration](../Datum-harness/auth.py#L142), [server binding](../Datum-harness/server.py#L1432).

Authentication defaults off while the server defaults to `0.0.0.0`. Enabling authentication without setting `DATUM_SESSION_SECRET` still uses the public string `dev-insecure-change-me`. Despite the documentation calling that setting required, there is no startup rejection. A known signing key allows a forged owner session; the fingerprint check accepts a session with no `fp`. Authenticated owners can reach the agent execution surface, making this a host-access concern rather than just a chat-history concern.

**Required change:** fail startup without a strong signing key and configured password when auth is enabled; default to authenticated operation, with an explicit loopback-only development mode. Add missing-key/known-key and unauthenticated remote-access tests. Whether the actual deployment supplies safe settings was not inspected.

### 5. High — the MCP bridge sends the gateway token to every configured HTTP server

**Current code defect; isolated check confirmed.** [Bridge HTTP client](../Datum-harness/datum_mcp/mcp_bridge.py#L87).

`_make_http_client()` has no server or origin argument. It reads the same `DATUM_MCP_AUTH_TOKEN` for every HTTP discovery and tool call. Adding another HTTP MCP server exposes the gateway bearer token to that server. The project-local `mcp_desktop.json` entries are also loaded before, and without, the `DATUM_MCP_SERVERS` filter used for the home config.

**Required change:** bind credentials to individual server configurations and validated origins. Apply the allowlist to all config sources. Test that a second HTTP server never receives Datum-Sync's token. The v2 client document notices the global-token behavior but treats the repair as a small future bridge task; it is a prerequisite for safe multi-server use.

### 6. High — elevation is not bounded by the stated approval duration

**Current behavior conflicts with v2's intended elevation model; isolated mint check confirmed.** [OAuth minting](../Datum-sync%20v1/datum_sync/oauth.py#L518), [refresh](../Datum-sync%20v1/datum_sync/oauth.py#L681), [elevation design](../Datum-sync%20v2/04-oauth-elevation.md).

Every refresh calls `_mint`, which creates another refresh token expiring 30 days from the current time. Refreshing regularly can maintain approved scope indefinitely. A one-hour access-token lifetime therefore does not enforce the UI's one-hour elevation, nor does it guarantee reapproval after 30 days. There is no persisted absolute elevation expiry. Also, `_revoke_family` revokes by client/account rather than by an actual authorization-family ID, affecting separate authorizations for that pair.

**Required change:** persist an authorization/elevation record with absolute expiry, approver, approved authority, and family ID. Cap access and refresh issuance at that expiry and check current principal/ancestor state. Decide explicitly whether approval covers one hour or a renewable grant; display that actual duration.

### 7. High — federation's example guards do not safely constrain shell or filesystem access

**Design defect; example matching checks confirmed.** [Command/path policy](../Datum-sync%20v2/05-mcp-gateway-federation.md#2-the-federation_scope-blocks), [guard evaluation](../Datum-sync%20v2/05-mcp-gateway-federation.md#4-argument-guards).

The example allow regex accepts `ls; printf SECOND_COMMAND`; none of the deny regexes rejects it. This string was matched only, not executed. More fundamentally, the `run_command` guard checks host and command but never the path grant: `cat /etc/passwd` passes the stated policy despite the allowed paths being `/home/ubuntu/**` and `/var/log/**`.

The separate file guards use glob matching without specifying traversal normalization or upstream symlink containment. The actual vault matcher accepts `/home/ubuntu/../../etc/passwd` against `/home/ubuntu/**` when used directly. Local vault access has a separate normalizer; reusing only its matching semantics does not transfer that protection to remote paths.

**Required change:** use structured operations with validated arguments and upstream-enforced filesystem roots. Do not present regex-approved arbitrary shell commands as a confinement boundary. Reject traversal and require canonical-path/symlink enforcement at the machine that opens the file. Validate every relevant resource argument, not merely whether some guard matched the tool.

### 8. High — pending approval can execute authority that has since been withdrawn

**Explicit design flaw.** [Pending-call execution](../Datum-sync%20v2/05-mcp-gateway-federation.md#5-approval-gated-calls), [WP6 acceptance](../Datum-sync%20v2/09-build-plan.md#wp6--approvals-and-review).

The spec requires execution under the request-time grant snapshot and explicitly tests successful execution after the requester was narrowed. A pending deploy can therefore execute after an operator withdrew its compute grant. The flow does not require rechecking requester/ancestor state, current policy, or the binding between the approved operation and the current upstream definition.

**Required change:** retain the snapshot as the maximum originally requested authority and as audit evidence; at execution intersect it with current authority and revalidate the operation. Disabling or retiring the requester must cancel pending work. Define policy/tool version binding and an atomic execution claim, plus retry behavior when the upstream succeeds but recording the result fails. A database status alone cannot guarantee exactly-once external side effects.

### 9. High — restriction can remove deny rules and restore can revive obsolete grants

**Design defect.** [Lifecycle rules](../Datum-sync%20v2/03-principals-lifecycle-sessions.md#4-lifecycle).

Restriction reduces `vault_scope` to its `read` block. For `{read:["**"], deny:["private/**"]}`, that wording drops the exclusion and widens access to private files. This matters for an agent whose exclusion is narrower than its parent's. Restore requires the saved tuple to be reinstated exactly, without reconciling it with changed parent authority or newer restrictions. Elsewhere the spec requires every write to preserve narrowing, making those rules inconsistent.

**Required change:** retain deny rules during restriction. Treat restriction as a monotone overlay; restore removes that overlay only after validating against current authority. Store history without making it a source of irrevocable permission.

### 10. High — proxy SSRF validation and response limits do not constrain the actual connection adequately

**Current code defects, established by source inspection; no network exploit attempted.** [DNS validation](../Datum-sync%20v1/datum_sync/proxy.py#L65), [request and response handling](../Datum-sync%20v1/datum_sync/proxy.py#L255).

The validator resolves DNS, checks addresses, and returns the original URL. The HTTP client independently resolves it again; the validated address is not bound to the socket. DNS rebinding can therefore invalidate the earlier decision. The code also buffers the full response through `client.request` before truncating `response.content` to 1 MiB. The advertised limit bounds the returned payload, not memory consumption. The database connection remains held across the upstream call.

**Required change:** bind destination validation to the actual transport connection, including redirects and private-origin policy. Stream and stop reading at an enforced byte budget, apply a total deadline, and release DB connections before network waits where possible. Test changing DNS answers and an oversized streamed response.

### 11. High — the supplied harness and job runner are not confinement boundaries

**Architectural limitation with concrete code evidence.** [Harness built-ins](../Datum-harness/harness_tools.py#L136), [runner](../Datum-sync%20v1/datum_sync/runner.py#L66), [config loading](../Datum-sync%20v1/datum_sync/config.py#L9).

Harness built-ins run arbitrary shell and read/write arbitrary paths using the harness process's authority. V1 workspace children run as the worker user. Stripping `DATABASE_URL` from the child environment does not prevent a workspace from reading the worker's `.env` or importing configuration that loads it, where normal deployment permissions allow that access. Local tools can also use inherited credentials and direct network routes outside the gateway.

V2 correctly disclaims workspace sandboxing. Consequently the security claim must be limited to resources accessible exclusively through the gateway, and workspace code must be fully trusted at worker privilege. An auth plane alone cannot make the supplied harness a restricted agent runtime.

**Required change:** for untrusted agent code, use separate OS identities or isolated runtimes with restricted mounts, no control-plane secrets, and deliberate network egress. Otherwise document trusted-code assumptions explicitly in the product and deployment model.

### 12. Medium — session and quota rules need atomicity and one consistent meaning

**Design gaps.** [Sessions and job quotas](../Datum-sync%20v2/03-principals-lifecycle-sessions.md#5-sessions-and-the-concurrency-limit), [client sessions](../Datum-sync%20v2/12-client-harnesses.md#2-session-supersession).

Count-then-insert operations can race across concurrent async requests even with one API process. The spec does not prescribe per-principal locking or another atomic admission mechanism. Same-token supersession also cannot distinguish a reconnect from two processes sharing the same token. Ending a session row does not cancel an upstream call already admitted under it. Session count is therefore not a bound on concurrent actions.

The acceptance criteria disagree: WP3 both refuses and supersedes a second initialize on the same token. Client conformance case 2 expects a second credential to hit the limit after all five earlier sessions were closed. The claim that two instances with the same pasted token are different credentials is incorrect.

**Required change:** distinguish session leases, credential identity, and active-operation limits. Define atomic DB admission, in-flight cancellation semantics, and shared ancestor budgets where needed; test simultaneous requests, not just sequential ones. Rewrite the contradictory cases before implementation.

## Other issues to resolve before building

- **Frozen jobs must carry credential-effective authority.** `03 §7` and WP3 tell the worker to use `grant_snapshot.max_tier`, while the resolver keeps stored tier separate from `effective_tier`. Explicitly freeze the credential-capped grant so tier-3 OAuth cannot authorize tier-4 connections through a job. Scheduled work also needs an explicit durable authorization rather than silently reconstituting an owner's full authority after temporary elevation expires.
- **Parent narrowing is both refused and required to succeed.** `03 §2` refuses edits that invalidate descendants; WP1 requires a parent tier reduction to flow through to the child without writing it. Choose stored-grant versus effective-grant invariants and specify emergency revocation separately.
- **Migration ordering is incomplete.** WP2 applies 019 before WP3's 020; deferred 018 is described as one release later, while 019+ can already be in service. Assign an executable release/migration sequence. Dropping `disabled` also requires updating the supposedly unchanged OAuth queries.
- **Federation profiles are not complete resource authorization.** The sample `delete_*` and `share_*` guards check tier/share without checking target repo/folder. Resource guard syntax refers to a named regex group without clearly specifying its regex. Drive ancestry caching needs a policy for moves, shortcuts, and stale authorization decisions. These are profile/spec gaps, not demonstrated live exploits.
- **Blocking password hashing affects availability.** `auth.authenticate_password` calls Argon2 synchronously inside the async request path. Its semaphore does not move CPU work off the event loop. Use bounded off-thread verification and an admission limit; check limiter state again after waiting.
- **Audit durability is a stated tradeoff.** `audit.write` swallows insert failures and increments a process-local counter. That is useful visibility but cannot guarantee a durable record for every privileged action. Decide which sensitive operations require a committed audit intent/outbox before execution. Avoid command text or sensitive path/query values in fields described as identifiers only.
- **Harness compatibility remains unverified.** I checked the supplied bridge source, not installed Claude Code/Codex versions or current external documentation. The spec's compatibility matrix and precise configuration snippets need real-client conformance tests; recorded request shapes are not sufficient evidence on their own.

## What is worth retaining

- Opaque, high-entropy credentials stored as hashes, with password hashing treated separately.
- Central principal resolution and explicit grant checks; vault deny-first semantics and path normalization.
- Explicit server-generated trace threading rather than trusting caller trace IDs as authoritative identity.
- A guard registry and mutation-test approach that treats skipped tests as unproven.
- A delta migration strategy, persisted federation mapping, and clear separation between stored upstream secrets and client-facing tool calls.

These are useful foundations. The recurring problem is preserving authority across transitions: migration, token minting, refresh, delegation, restriction, approval, and delayed execution. Those transitions deserve stronger tests than simply deleting individual guard lines.

## Validation and next build order

`python3 review/checks.py` completed successfully. Six probes reproduced custom-header retention, shell-policy acceptance, raw path traversal matching, sliding refresh expiry, the default session key, and global bridge token injection. The script extracts selected functions from source and uses synthetic inputs/stubs; it does not import or start the applications. Its assertions demonstrate existing defects, so a successful run is **not** a security pass.

The environment's Python lacks pytest and the core runtime dependencies (including asyncpg, FastAPI, httpx, Argon2, and dotenv). I did not install packages, run the application suites, execute migrations, contact live upstreams, or access the deployed database. Browser and geometry gates were not run. No application code or spec was changed; the only additions are this review and its offline probes.

Suggested sequence:

1. Repair current credential forwarding and harness startup defaults; establish the runtime trust boundary.
2. Rewrite v2's authority-transition rules: durable issuance, absolute elevation expiry, monotone restriction, current-authority execution, and atomic admission.
3. Build a disposable PostgreSQL migration/conformance environment and prove no existing credential gains an operation during migration.
4. Implement the principal/token core first, then test one narrowly defined federated provider end to end before expanding the profiles and UI.
