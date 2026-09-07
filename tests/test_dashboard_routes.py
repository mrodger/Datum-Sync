"""The nine read/write routes added for the dashboard.

These landed as 855 lines of live API surface with no tests at all, which is a
particular kind of invisible: the suite stays green because it does not know
the routes exist. Every route here is reachable by anyone holding any valid
token, so the assertions that matter are the ones about who is refused.

Six of the nine call `auth.require_admin`. Three did not, and three of those
three return rows belonging to other principals -- the audit trail, other
repositories' failed jobs, other callers' MCP targets. The tests named
`..._is_not_readable_by_a_scoped_account` are the ones that found that; they
fail against the code as first written, which is the only reason to trust them.

Scoping note: `analytics/summary` and `mcp-servers` are admin-gated rather than
filtered, because neither has a repository to filter on -- an audit row's
`target` is a free-text string and `mcp_call_log.target` is a URL. Filtering
them would mean inventing an ownership model per row. `notifications` does have
a repository per failed job, so that half is filtered by `allows_repo` and the
audit half is admin-gated, which keeps the route useful to a non-admin without
showing it anyone else's activity.
"""
from __future__ import annotations

import pytest
import pytest_asyncio

from datum_sync import db as db_module, tokens
from datum_sync.api import app

pytestmark = pytest.mark.asyncio

ADMIN = "_pytest_dash_admin"
SCOPED = "_pytest_dash_scoped"
OTHER_REPO = "_pytest_dash_other"


@pytest_asyncio.fixture
async def accounts(db):
    """One admin, one tier-2 account scoped to a repository it owns.

    The scoped account is the interesting one: it holds a valid token, so every
    route below authenticates it. What it must not get is anything belonging to
    anyone else.
    """
    await db.execute(
        "DELETE FROM service_accounts WHERE name = ANY($1)", [ADMIN, SCOPED]
    )
    admin_id = await db.fetchval(
        """
        INSERT INTO service_accounts (name, max_tier, is_admin, repo_scope)
        VALUES ($1, 5, true, ARRAY['*']) RETURNING id
        """,
        ADMIN,
    )
    scoped_id = await db.fetchval(
        """
        INSERT INTO service_accounts (name, max_tier, is_admin, repo_scope)
        VALUES ($1, 2, false, ARRAY['_pytest_dash_mine']) RETURNING id
        """,
        SCOPED,
    )
    _, admin_tok = await tokens.create(db, admin_id, "fixture")
    _, scoped_tok = await tokens.create(db, scoped_id, "fixture")
    try:
        yield {
            "admin_id": admin_id,
            "admin": admin_tok,
            "scoped_id": scoped_id,
            "scoped": scoped_tok,
        }
    finally:
        await db.execute(
            "DELETE FROM service_accounts WHERE name = ANY($1)", [ADMIN, SCOPED]
        )


@pytest_asyncio.fixture
async def client(db):
    """Unauthenticated. Each test presents the token it wants to test with."""
    import httpx

    await db_module.init_pool()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as c:
        yield c
    await db_module.close_pool()


@pytest_asyncio.fixture
async def foreign_rows(db, accounts):
    """Activity belonging to somebody the scoped account is not.

    Written as the admin account and against a repository outside the scoped
    account's `repo_scope`, so any of it appearing in a scoped response is a
    leak rather than a coincidence.
    """
    await db.execute(
        "INSERT INTO repositories (name, path) VALUES ($1, '/nonexistent')",
        OTHER_REPO,
    )
    job_id = await db.fetchval(
        """
        INSERT INTO jobs (repository, workspace, params, status, submitted_by,
                          submitted_at, completed_at, error)
        VALUES ($1, 'secret-workspace', '{}', 'failed', $2, now(), now(),
                'LEAKCANARY: connection string rejected')
        RETURNING id
        """,
        OTHER_REPO,
        ADMIN,
    )
    await db.execute(
        """
        INSERT INTO audit_log (trace_id, actor_id, actor_name, actor_kind, via,
                               verb, target_kind, target, outcome, error_code)
        VALUES (gen_random_uuid(), $1, $2, 'account', 'rest',
                'POST', 'connection', 'LEAKCANARY-connection', 'error', 500)
        """,
        accounts["admin_id"],
        ADMIN,
    )
    await db.execute(
        """
        INSERT INTO mcp_call_log (account_id, account_name, method, tool_name,
                                  target, is_governance, outcome)
        VALUES ($1, $2, 'tools/call', 'proxy_request',
                'https://LEAKCANARY.internal/api', false, 'ok')
        """,
        accounts["admin_id"],
        ADMIN,
    )
    try:
        yield job_id
    finally:
        await db.execute("DELETE FROM jobs WHERE repository = $1", OTHER_REPO)
        await db.execute("DELETE FROM repositories WHERE name = $1", OTHER_REPO)
        await db.execute(
            "DELETE FROM audit_log WHERE target = 'LEAKCANARY-connection'"
        )
        await db.execute(
            "DELETE FROM mcp_call_log WHERE target LIKE '%LEAKCANARY%'"
        )


def _h(token: str) -> dict:
    return {"authorization": f"Bearer {token}"}


ALL_ROUTES = [
    ("GET", "/rest/v1/accounts/_pytest_dash_admin/tokens"),
    ("GET", "/rest/v1/analytics/summary"),
    ("GET", "/rest/v1/queue"),
    ("GET", "/rest/v1/system/config"),
    ("GET", "/rest/v1/notifications"),
    ("GET", "/rest/v1/mcp-servers"),
    ("GET", "/rest/v1/auth/clients"),
]

# `json` is carried alongside because POST has a required body: sending none
# gets a 422 from request validation, which happens before the handler runs and
# so proves nothing about who the route lets in. The body has to be valid for
# the refusal to be an authorisation refusal.
ADMIN_ONLY = [
    ("GET", "/rest/v1/accounts/_pytest_dash_admin/tokens", None),
    ("POST", "/rest/v1/accounts/_pytest_dash_admin/tokens", {"label": "nope"}),
    ("DELETE", "/rest/v1/accounts/_pytest_dash_admin/tokens/fixture", None),
    ("GET", "/rest/v1/analytics/summary", None),
    ("GET", "/rest/v1/queue", None),
    ("GET", "/rest/v1/system/config", None),
    ("GET", "/rest/v1/mcp-servers", None),
    ("GET", "/rest/v1/auth/clients", None),
]


# -- authentication ----------------------------------------------------------


@pytest.mark.parametrize("verb,path", ALL_ROUTES)
async def test_every_dashboard_route_refuses_an_anonymous_caller(
    accounts, client, verb, path
):
    """No token, no answer -- on all of them.

    Parametrised rather than written out so a route added to the list is
    covered by construction. A new route that forgets `Caller` fails here.

    No guard of its own: AUTH-008 already breaks the bearer path this
    parametrisation rides on, and the case a new route would fail on --
    forgetting `Caller` -- is an omission, which the harness cannot make by
    removing a string.
    """
    r = await client.request(verb, path)
    assert r.status_code == 401, f"{verb} {path} answered {r.status_code}"


# -- authorisation -----------------------------------------------------------


@pytest.mark.parametrize("verb,path,body", ADMIN_ONLY)
async def test_admin_routes_refuse_a_non_admin_holding_a_valid_token(
    accounts, client, verb, path, body
):
    """A valid credential is not an administrative one.

    The scoped account authenticates fine; that is the point. It must still be
    refused by everything that reads across accounts.

    Guard: DASH-001.
    """
    r = await client.request(
        verb, path, headers=_h(accounts["scoped"]), **({"json": body} if body else {})
    )
    assert r.status_code == 403, f"{verb} {path} answered {r.status_code}"


async def test_the_audit_trail_is_not_readable_by_a_scoped_account(
    accounts, client, foreign_rows
):
    """`analytics/summary` returns the last 20 audit rows for the whole platform.

    Written without a guard, so any tier-1 account could read every other
    principal's verbs and targets -- the governance log readable by the parties
    it governs. Fails against the original route, which answered 200.

    Guard: DASH-002.
    """
    r = await client.get("/rest/v1/analytics/summary", headers=_h(accounts["scoped"]))
    assert r.status_code == 403
    assert "LEAKCANARY" not in r.text


async def test_another_repositorys_failed_jobs_are_not_readable(
    accounts, client, foreign_rows
):
    """`notifications` selected failed jobs with no `allows_repo` filter.

    `jobs.error` carries whatever the workspace printed on the way down, which
    is exactly where a connection string or a path ends up. The existing jobs
    routes at api.py:545 and api.py:1817 filter on `allows_repo`; this one did
    not. Fails against the original route.

    Guard: DASH-003.
    """
    r = await client.get("/rest/v1/notifications", headers=_h(accounts["scoped"]))
    assert r.status_code == 200
    assert "LEAKCANARY" not in r.text, "a foreign repository's failure text leaked"
    assert r.json()["audit_errors"] == [], "audit errors leaked to a non-admin"


async def test_other_callers_mcp_targets_are_not_readable(
    accounts, client, foreign_rows
):
    """`mcp-servers` groups the whole call log by target and returns the URLs.

    A proxy target names an internal host. Fails against the original route.

    Guard: DASH-004.
    """
    r = await client.get("/rest/v1/mcp-servers", headers=_h(accounts["scoped"]))
    assert r.status_code == 403
    assert "LEAKCANARY" not in r.text


async def test_a_scoped_account_still_sees_its_own_failed_jobs(
    accounts, client, foreign_rows, db
):
    """The fix must not turn `notifications` into an admin-only route.

    Without this, deleting the whole `failed_jobs` block would pass every other
    test in this file. The scoped account owns `_pytest_dash_mine`, so a failure
    there is its own business and must survive the filter.

    Guard: DASH-005.
    """
    await db.execute(
        "INSERT INTO repositories (name, path) VALUES ('_pytest_dash_mine', '/nonexistent')"
    )
    await db.execute(
        """
        INSERT INTO jobs (repository, workspace, params, status, submitted_by,
                          submitted_at, completed_at, error)
        VALUES ('_pytest_dash_mine', 'w', '{}', 'failed', $1, now(), now(),
                'MINECANARY: my own failure')
        """,
        SCOPED,
    )
    try:
        r = await client.get(
            "/rest/v1/notifications", headers=_h(accounts["scoped"])
        )
        assert r.status_code == 200
        assert "MINECANARY" in r.text, "the filter hid the caller's own job"
    finally:
        await db.execute("DELETE FROM jobs WHERE repository = '_pytest_dash_mine'")
        await db.execute(
            "DELETE FROM repositories WHERE name = '_pytest_dash_mine'"
        )


# -- shape -------------------------------------------------------------------


async def test_analytics_summary_reports_the_three_sections(accounts, client):
    """Shape check for the dashboard's main read.

    No guard: a shape assertion, not a property. Nothing in the route can be
    deleted to make it false that is not simply the route.
    """
    r = await client.get("/rest/v1/analytics/summary", headers=_h(accounts["admin"]))
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"jobs", "audit", "mcp"}
    assert set(body["jobs"]) == {"total", "by_status"}
    assert set(body["audit"]) == {"total", "by_verb", "recent"}
    assert set(body["mcp"]) == {"total", "top_tools"}


async def test_system_config_lists_applied_migrations(accounts, client):
    """The config view is how an operator confirms which migrations ran.

    No guard: a shape assertion, as above.
    """
    r = await client.get("/rest/v1/system/config", headers=_h(accounts["admin"]))
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"server", "limits", "paths", "migrations"}
    names = [m["filename"] for m in body["migrations"]]
    assert any("014" in n for n in names), names


async def test_the_queue_shows_only_unfinished_jobs(accounts, client, foreign_rows):
    """`/queue` is the live view: queued and running, never completed.

    `foreign_rows` leaves a *failed* job behind, so if the status filter is
    dropped this test sees it.

    Guard: DASH-006.
    """
    r = await client.get("/rest/v1/queue", headers=_h(accounts["admin"]))
    assert r.status_code == 200
    assert all(i["status"] in ("queued", "running") for i in r.json()["items"])


# -- token routes ------------------------------------------------------------


async def test_minting_returns_the_raw_token_once_then_never_again(
    accounts, client
):
    """The raw value is readable on create and absent from every later list.

    No guard: `tokens.public` omits the raw value unless one is passed in, so
    making this false means ADDING an argument at the list call site, and the
    harness proves a guard by removing one.
    """
    r = await client.post(
        f"/rest/v1/accounts/{ADMIN}/tokens",
        json={"label": "ci"},
        headers=_h(accounts["admin"]),
    )
    assert r.status_code == 201
    assert r.json()["token"]

    listed = await client.get(
        f"/rest/v1/accounts/{ADMIN}/tokens", headers=_h(accounts["admin"])
    )
    assert listed.status_code == 200
    labels = [t["label"] for t in listed.json()["items"]]
    assert "ci" in labels
    assert all("token" not in t for t in listed.json()["items"])


async def test_a_minted_token_authenticates_and_stops_when_revoked(
    accounts, client
):
    """The round trip the route exists for.

    No guard of its own: TOKEN-001 breaks the revocation check in
    `auth.py` that this round trip depends on.
    """
    r = await client.post(
        f"/rest/v1/accounts/{ADMIN}/tokens",
        json={"label": "rotate-me"},
        headers=_h(accounts["admin"]),
    )
    raw = r.json()["token"]
    assert (
        await client.get("/rest/v1/repositories", headers=_h(raw))
    ).status_code == 200

    gone = await client.delete(
        f"/rest/v1/accounts/{ADMIN}/tokens/rotate-me", headers=_h(accounts["admin"])
    )
    assert gone.status_code == 200
    assert (
        await client.get("/rest/v1/repositories", headers=_h(raw))
    ).status_code == 401


async def test_a_duplicate_label_is_refused_as_a_conflict(accounts, client):
    """Two live tokens cannot share a label on one account.

    Guard: DASH-007.
    """
    body = {"label": "dupe"}
    first = await client.post(
        f"/rest/v1/accounts/{ADMIN}/tokens", json=body, headers=_h(accounts["admin"])
    )
    assert first.status_code == 201
    second = await client.post(
        f"/rest/v1/accounts/{ADMIN}/tokens", json=body, headers=_h(accounts["admin"])
    )
    assert second.status_code == 409
    assert second.json()["code"] == "LABEL_TAKEN"


async def test_a_missing_label_is_a_parameter_error_not_a_conflict(
    accounts, client
):
    """An empty label is the caller's mistake, and must not read as a collision.

    The route reports every failure from `tokens.create` as LABEL_TAKEN, so
    this distinguishes the two.

    Guard: DASH-008.
    """
    r = await client.post(
        f"/rest/v1/accounts/{ADMIN}/tokens", json={}, headers=_h(accounts["admin"])
    )
    assert r.status_code == 400
    assert r.json()["code"] == "INVALID_PARAMETER"


async def test_a_nonnumeric_expiry_is_refused_not_a_crash(accounts, client):
    """`int(expires_days)` on caller-supplied JSON.

    Written unguarded, so `{"expires_days": "soon"}` raised ValueError inside
    the handler and became a 500. A bad parameter is a 400.

    Guard: DASH-009.
    """
    r = await client.post(
        f"/rest/v1/accounts/{ADMIN}/tokens",
        json={"label": "bad-expiry", "expires_days": "soon"},
        headers=_h(accounts["admin"]),
    )
    assert r.status_code == 400
    assert r.json()["code"] == "INVALID_PARAMETER"


async def test_tokens_for_an_unknown_account_are_a_404(accounts, client):
    """Guard: DASH-010."""
    r = await client.get(
        "/rest/v1/accounts/_pytest_no_such_account/tokens",
        headers=_h(accounts["admin"]),
    )
    assert r.status_code == 404


async def test_revoking_a_label_that_is_not_live_is_a_404(accounts, client):
    """Revocation reports whether it changed anything.

    Guard: DASH-011.
    """
    r = await client.delete(
        f"/rest/v1/accounts/{ADMIN}/tokens/never-existed",
        headers=_h(accounts["admin"]),
    )
    assert r.status_code == 404


# -- bounded reads -----------------------------------------------------------


async def test_the_oauth_client_list_is_bounded_and_says_so(accounts, client, db):
    """`oauth_clients` grows without an operator, so the read must be bounded.

    Dynamic client registration means anything that speaks the protocol adds a
    row. The route was written against an empty table with no LIMIT; the dev
    instance had 3056 rows by the time the screen was first opened, and it
    rendered every one of them.

    This is the test that could not be written from the route's source. It took
    loading the page to see the size, so the fixture plants enough rows to make
    the same failure reachable from pytest -- 120, just over the limit, which is
    the smallest number that can tell a bound from its absence.

    `total` is asserted because a silently truncated list is the worse bug: an
    operator auditing which clients can reach the platform reads 100 rows and
    concludes there are 100.

    Guard: DASH-012.
    """
    made = [f"_pytest_dash_client_{i:03d}" for i in range(120)]
    await db.executemany(
        """INSERT INTO oauth_clients (client_id, client_name, redirect_uris,
                                      grant_types)
           VALUES ($1, '_pytest_dash', ARRAY['https://example.test/cb'],
                   ARRAY['authorization_code'])""",
        [(cid,) for cid in made],
    )
    try:
        r = await client.get("/rest/v1/auth/clients", headers=_h(accounts["admin"]))
        assert r.status_code == 200
        body = r.json()
        assert len(body["items"]) <= 100, f"returned {len(body['items'])} rows"
        assert body["total"] >= 120, body["total"]
        assert body["total"] > len(body["items"]), (
            "total must reveal the rows the list does not show"
        )
        # Newest first: a truncated list showing the oldest 100 is a list of the
        # clients least likely to be the reason anyone opened the screen.
        assert body["items"][0]["client_id"] in made
    finally:
        await db.execute(
            "DELETE FROM oauth_clients WHERE client_name = '_pytest_dash'"
        )
