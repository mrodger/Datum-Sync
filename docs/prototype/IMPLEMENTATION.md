# Local Auth Portal prototype

The supplied snapshot is committed as the baseline. This file tracks implementation against `docs/review/REVIEW.md`.

## Prototype contract

Extend the existing Python package with a dedicated portal entry point, PostgreSQL migration and auth screens in the supplied `static-v2` UI shell. Run that entry point, a private read-only legacy MCP adapter, and two private demonstration providers on loopback; the legacy workspace runner and unrestricted coding harness are not launched. Use an isolated local database and generated operator credentials. Demonstrate real enrolment, baseline authentication, bounded OAuth elevation, human approval, MCP sessions, guarded resource calls, restriction/revocation, durable audit events and deterministic MCP request timelines.

The first resource provider is a local, persistent document/release fixture. It executes real reads/writes under the same authorization checks as MCP calls. The gateway also exposes nine exactly granted, read-only tools for the legacy repository registry and principal- and repository-scoped job observability. Two additional operator-registered providers demonstrate a constrained OfficeCLI command surface and structured PostgreSQL reads. Arbitrary shell tools and third-party federation profiles are not exposed. This is a runnable proof of the auth plane, not a claim that agent code is sandboxed or that every v2 work package is complete.

## Decisions correcting v2

- Keep existing account admin flags and legacy agent identities unchanged. New portal credentials are explicitly issued; existing credentials are not silently migrated into stronger rights.
- Only an authenticated human operator can mint durable agent credentials. Agent credentials cannot mint credentials, approve their own elevation, or create durable schedules.
- Every OAuth authorization has an absolute deadline and family ID. Refresh rotates within that deadline, never beyond it.
- Restriction is an overlay preserving deny lists. Restore removes the overlay; it does not restore obsolete permissions.
- Pending calls preserve requested arguments and authority as a ceiling; execution also checks current principal, parent and resource policy.
- Session admission is locked per principal. Reconnect requires closing the old lease; a shared token does not imply a shared running conversation. External side effects are excluded from the fixture so execution and audit can commit atomically.
- Gateway scope controls only gateway resources. The local prototype exposes no subprocess, arbitrary shell, host filesystem or third-party credentials.

## Work tracking

| Review | Work | Status |
|---|---|---|
| 1 | Durable issuance limited to operator sessions | implemented in prototype |
| 2 | Additive migration without privilege upgrades | implemented in prototype |
| 3 | Strip all redirect credentials / constrain forwarding | implemented in prototype |
| 4 | Fail-closed harness configuration | implemented in prototype |
| 5 | Per-server bridge credentials and uniform allowlist | implemented in prototype |
| 6 | Absolute OAuth expiry and family revocation | implemented in prototype |
| 7 | Structured resource operations; no shell guard claims | implemented in prototype |
| 8 | Revalidate pending calls before atomic execution | implemented in prototype |
| 9 | Monotone restriction with deny preservation | implemented in prototype |
| 10 | Pin proxy destinations and bound streamed responses | implemented in prototype |
| 11 | Loopback portal only; no untrusted code execution | implemented in prototype |
| 12 | Atomic session admission and concurrency tests | implemented in prototype |
| Portal | Enrolment, principals, approvals, access lab, activity UI | implemented in prototype |
| MCP federation | Private legacy adapter, persistent catalogue, exact grants and gateway forwarding | increment 1 implemented |
| Job observability | Stable principal ownership, repository scope, summary/list/status, bounded logs, durable events and safe artifact metadata | increment 2 implemented |
| MCP observability | Payload-free flow summaries and ordered auth/session/tool/provider event timelines | increment 3 implemented |
| MCP credential access | Versioned write-only service identities, agent requests, narrow operator approval, live effective-access inventory, rotation and immediate revocation | increment 4 implemented |
| MCP fleet governance | Operator-owned server registry, discovery, global server/tool controls, per-agent tool revocation, lifecycle events, and OfficeCLI/PostgreSQL demos | increment 5 implemented |
| Delivery | Local Git commits, launch scripts, tests and browser verification | implemented; verification in delivery report |

Security rules implemented in the dedicated portal do not retrofit every legacy endpoint. Existing proxy/harness repairs and remaining legacy migrations are distinguished in `docs/review/DELIVERY.md`.

The original v2 files remain a historical proposal. This document and the implementation describe the corrected prototype; incomplete production work will be recorded in the delivery report rather than marked complete.
