# MCP credential access review

## Decision

The local MCP prototype meets the core credential-management requirements: operators can see which agents have effective access to each managed upstream identity, agents can request bounded tool access, operators can approve or deny those requests, and an operator can immediately revoke a grant or disable the upstream identity. Secret material remains write-only through the portal.

This conclusion applies to the three loopback MCP integrations in this prototype. It is not a claim that the credential store or proxy is ready for multi-host production use.

## Requirement results

| Core requirement | Result | Evidence |
|---|---|---|
| Keep upstream credentials out of agents and browser reads | Met | The encrypted version is read only inside the gateway immediately before forwarding. Dashboard and catalogue queries select metadata, field names and a short ciphertext fingerprint, never ciphertext or plaintext. Tests assert known secret values do not occur in responses. |
| Show each credential and the agents that can currently use it | Met | **Secrets & Proxies** shows status, active version, expiry/use metadata and a unique count of effective agents. The access table evaluates the current principal state and ancestry, tier, MCP policy, credential state, grant state, allowed tools and time bounds on every dashboard load. |
| Let an agent request access without receiving a secret | Met | The agent catalogue exposes logical credential metadata and requestable tool names. Requests require a purpose and a 5-minute to 30-day lifetime and cannot exceed the agent's current policy. |
| Give an operator a useful review decision | Met | **Approvals** shows requester, credential, purpose, tools and duration. The decision endpoint locks the request, verifies operator ownership and current authority, and permits approval only at or below the requested tools and duration. Denial is recorded. |
| Revoke access easily and promptly | Met | Operators can revoke a grant from **Secrets & Proxies** or disable the credential globally. Tool discovery and every tool call read live state, so subsequent calls are denied without reissuing agent tokens or restarting the service. Calls already accepted by an upstream cannot be recalled. |
| Rotate credentials without losing the working value on a bad replacement | Met | A replacement is encrypted as a pending version and tested with MCP `initialize` and `tools/list`. A failed test marks only the candidate failed. A passing candidate is activated transactionally and the previous version is retired. The UI never echoes the replacement or upstream failure text. |
| Leave an auditable, payload-free record | Met | Request, decision, authorization, revocation, disable/enable and rotation events are durable. Events contain IDs, tool names, bounded reasons and version metadata; they exclude secret values, MCP arguments and results. MCP Activity links tool authorization to the credential ID and version. |

## Security model now enforced

An MCP call succeeds only when all of these are true at call time:

1. the inbound agent credential and MCP session are valid;
2. the agent and every ancestor are active, and its effective tier is high enough;
3. every policy in the ancestry permits the exact connection and projected tool;
4. a live grant for that agent, credential and exact tool exists;
5. the connection belongs to the agent's operator, the server and tool are enabled, and their tier requirements are met;
6. the managed credential is active, unexpired and has an active encrypted version; and
7. the connection can be reached through the constrained federation transport.

The inbound agent bearer is stripped at the upstream boundary. The gateway injects the decrypted service credential into a newly built outbound header set. The decrypted value exists only in process memory for the outbound operation.

## Remaining production work

- Replace the local file-held encryption key with a KMS or external secret manager, add key rewrap, recovery and audited break-glass procedures.
- Complete multi-operator tenancy and explicit credential-owner roles. MCP servers and credential operations now have an operator-owner boundary, while the rest of the portal remains one local operator tree.
- Add generic connection onboarding and provider-specific validation. The UI currently manages the seeded `legacy-local` identity plus two vetted local demo templates.
- Remove the encrypted compatibility copy in the older `connections.secret` column after all legacy consumers migrate to versioned credentials.
- Add high-availability cache invalidation or event propagation if multiple gateway instances are introduced. This single instance checks PostgreSQL on every call.
- Add production TLS, service supervision, backups, retention policy, alerting and an independent security review.
- Define upstream-side revocation and incident response. Disabling a Datum grant stops new gateway calls but does not revoke the credential at the upstream service itself.

## Verification

The complete suite passes: **723 passed, 1 skipped**. Seven credential mutation cases prove that removing ciphertext exclusion, grant enforcement, immediate revocation, narrow approval, failed-rotation isolation, disabled-credential evaluation or error redaction breaks its named test. The visible Chromium workflow covers requests and approvals for all three providers, MCP use, operator inventory, global server/tool controls, per-agent tool revocation and grant revocation.
