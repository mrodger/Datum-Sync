"""The audit spine: one row per authorised action, joined by a server trace id.

Written alongside the existing per-surface logs (`mcp_call_log`, `proxy_log`),
not instead of them. See migrations/011_audit_log.sql for why, and
spec/datum-gate/14-audit.md for the shape.

The one idea here
-----------------
`new_trace()` is called once, at request entry, and the value is passed down
the call. Every row that request produces carries it, so the rows join.

The trace is passed as an ordinary argument rather than held in a ContextVar.
A ContextVar would need no signature changes, which is exactly the objection:
a new call site that forgets it would still write a plausible row, and the
mistake would be invisible in review and in the data. Passed explicitly with
no default, forgetting it is a TypeError at the call site. That is the same
trade `Principal.vault_scope` already makes in auth.py, for the same reason.

Failure policy
--------------
`write()` never raises. A logging failure must not turn a successful call into
a failed one -- both existing writers already take that line. But a swallowed
write is a hole in an audit trail, so failures are counted in `dropped()` and
reported on `/health` as `audit_dropped`. An audit gap should be visible; a
bare `except: pass` makes it the one kind of failure nobody can see.

Who writes rows
---------------
`011_audit_log.sql` says only `mcp` is written, and that stopped being true when
`api.trace_and_audit` was added: every authenticated non-GET request now writes
one coarse row (`via` of `rest`, `ui` or `oauth`), alongside the richer rows the
`/mcp` endpoint and the proxy write for themselves. The migration's comment is
left as it was -- it was true when it was applied, and an applied migration is a
record of what happened, not a description of the present. This is the present.

`/mcp` is excluded from the middleware row on purpose. That migration states
that `audit_log` and `mcp_call_log` agreeing row for row is the check on phase
one, and a second row per `/mcp` post would end that check without failing
anything.

Not implemented here (deliberately): the spec's bounded queue and background
batch drain, its retention sweeper, and its export CLI. `write()` awaits the
insert inline, which is what the writers it sits beside already do. Adding a
queue would change the failure and ordering behaviour of the existing logging
path in the same change that introduces the table -- and then a disagreement
between the old and new tables would have two possible causes instead of one.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

from datum_sync.auth import Principal

# Bounded so a caller cannot push an unbounded blob into the audit trail by
# provoking a big error. The spec's figure.
MAX_DETAIL_BYTES = 2048

_dropped = 0


def dropped() -> int:
    """How many audit rows failed to write since start. Reported on /health."""
    return _dropped


def drop() -> None:
    """Count a row that could not be written, for a caller that never reached
    `write()`.

    `write()` takes an open connection, so the one failure it cannot swallow is
    the pool being unavailable -- the caller fails while acquiring and never gets
    here. Without this the row would go missing and `dropped()` would still read
    zero, which is the single reading this counter must never give wrongly: it
    exists so an audit gap is visible, and a gap that reports none is worse than
    no counter at all.
    """
    global _dropped
    _dropped += 1


@dataclass(frozen=True)
class Trace:
    """The two trace values for one inbound request, kept together.

    `id` is ours and `client_id` is theirs, and the entire security property of
    this change is that only the first is ever a join key. They are both
    strings, so as two adjacent parameters threaded through six functions they
    are trivially swappable -- and a swap would silently promote the forgeable
    value to the join key while every test that checks "the rows share a trace"
    still passed. One frozen object cannot be swapped with itself.
    """

    id: str
    client_id: str | None

    @classmethod
    def mint(cls, client_id: str | None) -> Trace:
        """Mint a server trace id for one inbound request.

        uuid4, generated here rather than accepted from the caller. That is the
        point: `X-Trace-Id` is a client string, so a client can send one value
        on every call to merge unrelated work into a single trace, or send
        another principal's value to splice itself into theirs. It is still
        recorded -- as `client_trace_id`, never as `trace_id`.

        `id` is a `str`; asyncpg takes it for a UUID column (measured, not
        assumed -- a `dict` for the jsonb column in the same insert does not
        work, so the types here are not interchangeable by guesswork).
        """
        return cls(id=str(uuid.uuid4()), client_id=client_id)


def _bounded(detail: dict[str, Any] | None) -> str | None:
    """Serialise `detail`, truncating to MAX_DETAIL_BYTES.

    Truncation replaces the payload rather than slicing it: half a JSON
    document is not JSON, and a jsonb column would reject it -- which would
    take the row down with it, losing the trace as well as the detail.
    """
    if detail is None:
        return None
    encoded = json.dumps(detail, default=str)
    if len(encoded.encode()) <= MAX_DETAIL_BYTES:
        return encoded
    return json.dumps({"truncated": True, "keys": sorted(detail)})


async def write(
    conn,
    *,
    trace: Trace,
    principal: Principal,
    via: str,
    verb: str,
    target_kind: str | None,
    target: str | None,
    outcome: str,
    error_code: int | None = None,
    duration_ms: int | None = None,
    governance: bool = False,
    detail: dict[str, Any] | None = None,
) -> None:
    """Write one audit row on the caller's connection. Never raises.

    `conn` is taken rather than acquired because the proxy calls this from
    inside its own `pool().acquire()` block. Acquiring a second connection
    while holding one is how a pool deadlocks under load, and it would be the
    audit writer -- the thing that is supposed to be free -- that caused it.

    Every argument after `conn` is keyword-only. The row has five short string
    columns whose order is not memorable, and a positional swap of `verb` and
    `target` would write a row that inserts cleanly and reads as nonsense.
    """
    global _dropped
    if principal.agent_id is not None:
        actor_kind, actor_id, actor_name = "agent", principal.agent_id, (
            principal.agent_name or ""
        )
        # The account is not in a column, so it goes in detail -- an agent row
        # that cannot be traced back to its account answers half the question.
        detail = {**(detail or {}), "account": principal.name}
    else:
        actor_kind, actor_id, actor_name = "account", principal.account_id, principal.name

    try:
        await conn.execute(
            """
            INSERT INTO audit_log
                (trace_id, client_trace_id, actor_id, actor_name, actor_kind,
                 via, verb, target_kind, target, outcome, error_code,
                 duration_ms, governance, detail)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
            """,
            trace.id,
            trace.client_id,
            actor_id,
            actor_name,
            actor_kind,
            via,
            verb,
            target_kind,
            target,
            outcome,
            error_code,
            duration_ms,
            governance,
            _bounded(detail),
        )
    except Exception:
        _dropped += 1
