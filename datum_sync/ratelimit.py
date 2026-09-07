"""A per-account request rate limit: sliding window, in memory.

`service_accounts.rate_limit_per_min` has existed since 001_core.sql and was
read by nothing until this module. That is the whole of what C3 changes -- the
column becomes real, and the number an operator sets is the number enforced.

Why in memory and not a table
-----------------------------
`spec/datum-gate/03-authority.md` §8 puts the window in memory per process and
adds a `rate_windows` table only when there is more than one process. There is
one: `api.py` ends in `uvicorn.run(app, ...)` with no `workers` argument and no
setting that would add one. A table today would be a write on every request to
coordinate between processes that do not exist.

The hazard that buys is worth stating plainly, because it is silent: adding
`--workers N` multiplies every limit by N. Nothing here would fail, no test
would go red, and the only symptom is that a limit of 60 admits 240. If a
second process ever appears, this module is the thing that has to change, and
`test_the_window_is_per_process` exists to be read at that moment.

Why per account and not per credential
--------------------------------------
The key is `principal.account_id`. An account with three tokens and two agents
gets one window, not five. Keying on the credential would mean the limit could
be raised by minting another token -- something every account holder can do
unaided, which makes the limit advisory rather than enforced.

The consequence, which is intended: an account's own agents compete for its
budget. That is what "this account may make N requests per minute" means.
"""

from __future__ import annotations

import time

# Request timestamps per account id. Bounded by the number of accounts, which
# is bounded by who can create one -- unlike auth._attempts, whose keys come
# from an unauthenticated request body. The eviction pass below is therefore
# housekeeping rather than a defence, and the cap is deliberately generous.
_hits: dict[int, list[float]] = {}

_MAX_KEYS = 10_000

WINDOW_SECONDS = 60.0


def check(account_id: int, limit: int | None, now: float | None = None) -> int:
    """Record a request and return 0, or the seconds to wait and record nothing.

    A non-zero return means the caller is over its limit; the request must be
    refused. Recording nothing in that case is deliberate: counting rejected
    requests would let a client that keeps retrying extend its own lockout
    indefinitely, so a caller that backs off is never punished for the requests
    that were already refused.

    `limit is None` means no limit, which is what every existing account has --
    the column has always been nullable and has never been set. Fail-open is
    the right direction here only because the alternative is that deploying C3
    silently throttles every account that predates it.
    """
    if limit is None:
        return 0
    if now is None:
        now = time.monotonic()

    recent = [t for t in _hits.get(account_id, ()) if now - t < WINDOW_SECONDS]

    if len(recent) >= limit:
        _hits[account_id] = recent
        # Room appears when the oldest hit leaves the window. Rounded up so a
        # client that honours Retry-After exactly is not refused a second time
        # for arriving a fraction of a second early.
        return max(1, int(WINDOW_SECONDS - (now - recent[0])) + 1)

    if account_id not in _hits and len(_hits) >= _MAX_KEYS:
        for key, times in list(_hits.items()):
            if all(now - t >= WINDOW_SECONDS for t in times):
                del _hits[key]

    recent.append(now)
    _hits[account_id] = recent
    return 0


def reset() -> None:
    """Drop every recorded request. For tests; nothing in the app calls it."""
    _hits.clear()
