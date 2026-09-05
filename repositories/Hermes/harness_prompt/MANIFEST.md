# harness_prompt

## Purpose
Drives **one prompt through one Hermes governance test harness** and returns the
turn: a plain-text transcript (primary) and a JSON record carrying the session
id, the token/cost accounting and the gateway event types observed.

The point is not chat — the harness already has a chat PWA. The point is that a
turn becomes **submittable**, so an N-way concurrency test across N harness
instances is N jobs with rows, logs, artifacts and per-turn `cost_usd`, rather
than N browser tabs somebody watched.

It does **not** manage the harness: it does not start, stop, configure or scale
containers, and it does not choose the persona. The persona is fixed inside the
harness at boot (`HARNESS_PERSONA`); this workspace only talks to whichever agent
is answering on the connection's `base_url`.

## Dependencies
- **Connection `hermes-researcher`** (`http`, tier 1, read, repository-scoped to
  `Hermes`). Supplies `base_url` only. It carries **no secret** — see Behavioral
  contracts; the credential is scraped per run, deliberately.
- **A live Hermes harness** at that URL, serving `GET /` (for the session token)
  and `/api/ws` (JSON-RPC: `session.create` / `session.resume` /
  `prompt.submit`). Unreachable → `HarnessError`, job fails, no partial artifacts.
- **The harness's own upstream model provider** (OpenRouter, DeepSeek v4 Flash).
  This workspace never talks to it; a provider outage surfaces as a turn that
  never completes, i.e. as a timeout here.
- `httpx` and `websockets`, both already in the datum-sync environment.

## Dependents
The job submitter, data streaming and data download services. The contract they
rely on is `transcript` being marked `primary` — that is the body `/stream/` and
`/download/` return. `turn` is the machine-readable sibling for anyone
aggregating a concurrency run.

Nothing depends on `RESUME_SESSION` returning the same session; a caller that
wants a multi-turn conversation must thread the `session_id` out of `turn` and
back in itself.

## Failure modes
| Condition | Behaviour |
|---|---|
| `base_url` unreachable / not a Hermes instance | `HarnessError` naming the URL, the HTTP status and the body size. Job fails. |
| Index page served but no `__HERMES_SESSION_TOKEN__` | Same error. This is the signature of a **non-SPA path or a changed injection point**, not of a down server — Hermes injects the token only into the SPA fallback HTML. |
| Bad/expired token | The WS **handshake is refused (403)**. It is never a close code, so this surfaces as a `websockets` connect exception, not as an empty transcript. |
| Gateway never answers `session.create` | `HarnessError` after 25s. |
| `session.resume` with an unknown id | `HarnessError` quoting the gateway's own `error` object. |
| No `message.complete` within `TIMEOUT_SECONDS` | `HarnessError` reporting how many partial chunks arrived — **the job fails rather than returning a truncated transcript as if it were an answer.** |
| Harness answers but the model refuses/errors | Job **succeeds**; the refusal is the transcript and `turn.status` records it. A governance test needs a refused turn to be a result, not an outage. |

There is no retry. A retried prompt is a second turn against a stateful agent
with a memory system, so silently re-running it would corrupt the very thing a
concurrency test measures.

## Behavioral contracts
- **The session token is scraped every run and never stored.** Hermes generates
  it at import time (`_SESSION_TOKEN = secrets.token_urlsafe(32)`, "generated
  fresh on every server start"). Putting it in the connection store would make
  it stale the first time the container restarted, and the failure would present
  as an auth bug rather than a stale credential. The connection therefore holds
  `base_url` and nothing else, and `has_secret` is false by design.
- **Only `message.*` is the answer.** The same socket also carries
  `reasoning.delta` and `thinking.delta`; `thinking.delta` is decorative spinner
  text, and `reasoning.available` has been observed carrying the *message* text
  rather than the reasoning. A reader that accumulates every payload with a
  `text` key returns the answer three times over — measured. Deltas are
  collected per event type, and `message.complete.text` wins over our
  reassembly of them, because it is the server's own final answer.
- **Not idempotent, and not safe to retry blindly.** Each run creates a session
  (or extends one), spends the harness's provider budget, and writes to the
  harness's Mnemosyne memory. Identical parameters do not produce identical
  output.
- **Read-only with respect to datum-sync.** Touches no datum-sync table and no
  other host. Its entire external effect is one conversation on one harness.
- **The transcript is the turn, not the conversation.** Resuming a session gives
  the agent its history, but the artifact only ever contains the prompt sent and
  the answer received.
- `turn.events_seen` and `turn.delta_counts` are diagnostics, not a contract:
  they exist so a run that produced a thin answer can be told apart from one
  where the gateway's event vocabulary changed under us.
