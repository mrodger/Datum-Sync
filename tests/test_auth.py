"""Authentication, authorisation, OAuth and the MCP endpoint.

Every guard here is tested twice: once showing it refuses, and once showing the
*same request* succeeds when the thing it objected to is fixed. A test that only
asserts a 403 passes just as well against a route that is broken, misspelled or
returns 403 unconditionally, so on its own it proves the request failed and not
that the guard is what failed it.

`tests/conftest.py` mints an unscoped admin token and `client` sends it by
default. Tests that need a different credential override the header on the
individual request, which keeps them going through the real middleware rather
than around it.
"""
from __future__ import annotations

import base64
import hashlib
import json
import pathlib
import re
import secrets
import uuid
from dataclasses import fields
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import httpx
from starlette.datastructures import Address
import pytest
import pytest_asyncio

from datum_sync import auth, config, tokens
from datum_sync import db as db_module
from datum_sync.api import app
from datum_sync.manifest import Manifest

from conftest import TEST_ACCOUNT

PASSWORD = "correct horse battery staple"


@pytest_asyncio.fixture
async def client(db, token):
    """Authenticated by default; see tests/test_api.py for the same fixture.

    Duplicated rather than shared because these tests also need `db` open at the
    same time to move the account's scope around mid-test, and a fixture in
    conftest would have to be imported by both modules anyway.
    """
    await db_module.init_pool()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"authorization": f"Bearer {token}"},
    ) as c:
        yield c
    await db_module.close_pool()


async def set_scope(db, scope: list[str]) -> None:
    """Rescope the fixture account. `["*"]` is everything, `[]` is nothing.

    Not `None`: the column is NOT NULL since migration 013, so passing it now
    raises NotNullViolationError instead of restoring an unrestricted scope,
    which is what it used to mean.
    """
    await db.execute(
        "UPDATE service_accounts SET repo_scope = $2 WHERE name = $1",
        TEST_ACCOUNT,
        scope,
    )


def bearer(raw: str) -> dict[str, str]:
    return {"authorization": f"Bearer {raw}"}


# --------------------------------------------------------------------------
# the bearer guard
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_an_anonymous_request_is_refused_with_a_discovery_challenge(client):
    """401 alone is not enough: RFC 9728 says where to go next.

    A fresh MCP client has never seen this server and has nothing to go on but
    this header. Without it the connection fails with no route to recovery.

    Guard: AUTH-008.
    """
    r = await client.get("/rest/v1/repositories", headers={"authorization": ""})
    assert r.status_code == 401
    challenge = r.headers.get("www-authenticate", "")
    assert "resource_metadata=" in challenge
    assert auth.RESOURCE_METADATA_URL in challenge
    assert r.json()["code"] == "UNAUTHENTICATED"


@pytest.mark.asyncio
async def test_the_challenge_survives_the_middleware(client):
    """Regression: middleware runs *outside* Starlette's exception middleware.

    An ApiError raised there is not caught by the handler that adds headers, so
    the response degrades to a bare 500 and the challenge disappears -- which is
    invisible until an MCP client tries to connect. The envelope is therefore
    built in the middleware, not raised.
    """
    r = await client.get("/rest/v1/repositories", headers={"authorization": ""})
    assert r.status_code == 401, r.text
    assert r.headers["content-type"].startswith("application/json")


@pytest.mark.asyncio
async def test_an_unknown_token_is_refused(client):
    # Guard: AUTH-009.
    r = await client.get("/rest/v1/repositories", headers=bearer(auth.new_token()))
    assert r.status_code == 401
    assert r.json()["code"] == "UNAUTHENTICATED"


@pytest.mark.asyncio
async def test_a_malformed_authorization_header_is_refused(client):
    for header in ("Basic abc", "Bearer", "Bearer    ", "token abc"):
        r = await client.get(
            "/rest/v1/repositories", headers={"authorization": header}
        )
        assert r.status_code == 401, header


@pytest.mark.asyncio
async def test_health_and_discovery_stay_public(client):
    """The allowlist is real, not decorative."""
    for path in (
        "/health",
        "/.well-known/oauth-protected-resource",
        "/.well-known/oauth-authorization-server",
    ):
        r = await client.get(path, headers={"authorization": ""})
        assert r.status_code == 200, path


@pytest.mark.asyncio
async def test_a_disabled_account_cannot_use_a_valid_token(client, db, token):
    await db.execute(
        "UPDATE service_accounts SET disabled = true WHERE name = $1", TEST_ACCOUNT
    )
    r = await client.get("/rest/v1/repositories")
    assert r.status_code == 401
    assert r.json()["code"] == "ACCOUNT_DISABLED"

    # The same request, same token, once the account is back: proves the refusal
    # was the disabled flag and not something incidental.
    await db.execute(
        "UPDATE service_accounts SET disabled = false WHERE name = $1", TEST_ACCOUNT
    )
    assert (await client.get("/rest/v1/repositories")).status_code == 200


@pytest.mark.asyncio
async def test_an_expired_token_is_refused(client, db):
    # Expiry lives on the token row, not the account: one credential can lapse
    # while the account's others keep working.
    await db.execute(
        "UPDATE account_tokens SET expires_at = now() - interval '1 second' "
        "WHERE account_id = (SELECT id FROM service_accounts WHERE name = $1)",
        TEST_ACCOUNT,
    )
    r = await client.get("/rest/v1/repositories")
    assert r.status_code == 401
    assert r.json()["code"] == "TOKEN_EXPIRED"

    await db.execute(
        "UPDATE account_tokens SET expires_at = NULL "
        "WHERE account_id = (SELECT id FROM service_accounts WHERE name = $1)",
        TEST_ACCOUNT,
    )
    assert (await client.get("/rest/v1/repositories")).status_code == 200


# --------------------------------------------------------------------------
# repository scope
# --------------------------------------------------------------------------

def test_scope_patterns_are_two_literal_forms():
    """`SCIMAC/*` is a spec spelling, not a glob.

    fnmatch would also accept `S*` and `*C*`, so a typo in an admin form widens
    access instead of failing.
    """
    p = auth.Principal(
        account_id=1, name="x", max_tier=1, repo_scope=["SCIMAC/*"],
        is_admin=False, vault_scope=None,
        source="token",
    )
    assert p.allows_repo("SCIMAC")
    assert not p.allows_repo("SCIMAC_OTHER")
    assert not p.allows_repo("OTHER")

    glob = auth.Principal(
        account_id=1, name="x", max_tier=1, repo_scope=["S*"],
        is_admin=False, vault_scope=None,
        source="token",
    )
    assert not glob.allows_repo("SCIMAC")

    everything = auth.Principal(
        account_id=1, name="x", max_tier=1, repo_scope=["*"],
        is_admin=False, vault_scope=None,
        source="token",
    )
    assert everything.allows_repo("anything")

    nothing = auth.Principal(
        account_id=1, name="x", max_tier=1, repo_scope=[],
        is_admin=False, vault_scope=None,
        source="token",
    )
    # The only two ways to hold everything are `*` and naming them; an empty
    # list is not a third.
    assert not nothing.allows_repo("SCIMAC")


@pytest.mark.asyncio
async def test_an_unset_scope_grants_nothing(db):
    """A scope column nobody filled in holds no repositories.

    Guard: AUTHZ-002.

    This is the whole of migration 013. `repo_scope` used to default to NULL
    and NULL meant EVERY repository, while `vault_scope` -- on the same table,
    for the same account -- defaults to NULL meaning NO access. So "never
    configured" was the most dangerous state one column could be in and the
    safest state the other could be in, with nothing in either name to say
    which way round it went.

    Asserted through the database default rather than by constructing a
    Principal, because the default is the thing that was wrong. Code that reads
    the column can be inspected; a column that grants everything to a row
    written by an INSERT that never mentioned it cannot.
    """
    await db.execute("DELETE FROM service_accounts WHERE name = $1", "_pytest_unset")
    try:
        row = await db.fetchrow(
            """
            INSERT INTO service_accounts (name, max_tier)
            VALUES ($1, 1)
            RETURNING repo_scope, vault_scope
            """,
            "_pytest_unset",
        )
        assert row["repo_scope"] == []
        assert row["vault_scope"] is None

        unset = auth.Principal(
            account_id=1, name="x", max_tier=1, repo_scope=row["repo_scope"],
            is_admin=False, vault_scope=auth.vault_scope_of(row),
            source="token",
        )
        # Both directions the same: what was never granted is not held.
        assert not unset.allows_repo("SCIMAC")
        assert not unset.allows_vault_path("read", "anything.md")
    finally:
        await db.execute("DELETE FROM service_accounts WHERE name = $1", "_pytest_unset")


def test_the_wildcard_is_not_passed_to_sql_as_a_repository_name():
    """`*` is a pattern, and the scope filter compares literal names.

    Guard: AUTHZ-003.

    `_scope_sql` builds `repository = ANY($n::text[])` out of the scope list.
    Hand it `["*"]` and it asks Postgres for a repository *called* `*`, finds
    none, and an unrestricted caller is shown an empty list of schedules and
    automations -- no error, no 403, just nothing. That is why the wildcard has
    to be caught before the list is used, and why the check is `all_repos()`
    rather than the `is None` it replaced.
    """
    from datum_sync import api

    everything = auth.Principal(
        account_id=1, name="x", max_tier=1, repo_scope=["*"],
        is_admin=False, vault_scope=None, source="token",
    )
    assert api._scope_sql(everything, "repository", 1) == ("", [])

    scoped = auth.Principal(
        account_id=1, name="x", max_tier=1, repo_scope=["SCIMAC/*"],
        is_admin=False, vault_scope=None, source="token",
    )
    fragment, args = api._scope_sql(scoped, "repository", 1)
    assert fragment == " AND repository = ANY($1::text[])"
    assert args == [["SCIMAC"]]


@pytest.mark.asyncio
async def test_a_repository_outside_scope_is_refused(client, db, workspace):
    repo, ws = workspace
    await set_scope(db, ["SOMETHING_ELSE"])
    r = await client.get(f"/rest/v1/repositories/{repo}/workspaces/{ws}")
    assert r.status_code == 403
    assert r.json()["code"] == "FORBIDDEN"

    # Widen the scope to include it: the identical request now succeeds, so the
    # 403 was the scope check and not a missing route or a broken fixture.
    await set_scope(db, [f"{repo}/*"])
    assert (
        await client.get(f"/rest/v1/repositories/{repo}/workspaces/{ws}")
    ).status_code == 200


@pytest.mark.asyncio
async def test_scope_is_enforced_on_submit_not_only_on_reads(client, db, workspace):
    # Guard: AUTH-007.
    repo, ws = workspace
    await set_scope(db, ["SOMETHING_ELSE"])
    r = await client.post(
        f"/rest/v1/transformations/submit/{repo}/{ws}",
        json={"params": {"WHO": "world"}},
    )
    assert r.status_code == 403

    await set_scope(db, ["*"])
    r = await client.post(
        f"/rest/v1/transformations/submit/{repo}/{ws}",
        json={"params": {"WHO": "world"}},
    )
    assert r.status_code == 202


@pytest.mark.asyncio
async def test_a_listing_hides_repositories_outside_scope(client, db, workspace):
    # Guard: AUTH-010.
    repo, _ = workspace
    await set_scope(db, ["*"])
    body = (await client.get("/rest/v1/repositories")).json()
    names = [r["name"] for r in body["items"]]
    assert repo in names

    # A listing is the one place an out-of-scope repository should simply not
    # appear -- 403 for a whole collection tells the caller it exists.
    await set_scope(db, ["SOMETHING_ELSE"])
    r = await client.get("/rest/v1/repositories")
    assert r.status_code == 200
    assert repo not in [x["name"] for x in r.json()["items"]]


@pytest.mark.asyncio
async def test_the_flat_catalogue_hides_workspaces_outside_scope(client, db, workspace):
    """The same rule as the repository listing, on the screen that flattens it.

    Flattening is where the rule is easiest to lose: the per-repository route
    is only reachable through a repository the caller already passed a scope
    check to see, so an unfiltered query there is invisible. This route is
    reachable directly and reads every repository on the server, which makes
    the filter the only thing between an out-of-scope caller and the full
    catalogue.
    """
    repo, ws = workspace
    await set_scope(db, ["*"])
    body = (await client.get("/rest/v1/workspaces")).json()
    assert ws in [w["name"] for w in body["items"] if w["repository"] == repo]

    await set_scope(db, ["SOMETHING_ELSE"])
    r = await client.get("/rest/v1/workspaces")
    assert r.status_code == 200
    assert repo not in [w["repository"] for w in r.json()["items"]]


@pytest.mark.asyncio
async def test_a_job_in_another_repository_is_not_readable(client, db, workspace):
    # Guard: AUTH-011.
    repo, ws = workspace
    r = await client.post(
        f"/rest/v1/transformations/submit/{repo}/{ws}",
        json={"params": {"WHO": "world"}},
    )
    assert r.status_code == 202
    job_id = r.json()["id"]

    job = f"/rest/v1/transformations/jobs/id/{job_id}"
    assert (await client.get(job)).status_code == 200

    # Scope is checked against the job's own denormalised repository, so it
    # keeps working after the workspace is unpublished.
    await set_scope(db, ["SOMETHING_ELSE"])
    assert (await client.get(job)).status_code == 403
    assert (await client.delete(job)).status_code == 403
    assert (await client.get(f"{job}/log")).status_code == 403


@pytest.mark.asyncio
async def test_the_submitter_is_recorded(client, db, workspace):
    repo, ws = workspace
    r = await client.post(
        f"/rest/v1/transformations/submit/{repo}/{ws}",
        json={"params": {"WHO": "world"}},
    )
    job_id = uuid.UUID(r.json()["id"])
    who = await db.fetchval("SELECT submitted_by FROM jobs WHERE id = $1", job_id)
    assert who == TEST_ACCOUNT


# --------------------------------------------------------------------------
# OAuth: discovery and registration
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_protected_resource_metadata_points_at_this_server(client):
    body = (await client.get("/.well-known/oauth-protected-resource")).json()
    assert body["resource"] == auth.MCP_RESOURCE
    assert config.PUBLIC_URL in body["authorization_servers"][0]


@pytest.mark.asyncio
async def test_authorization_server_metadata_advertises_pkce(client):
    body = (await client.get("/.well-known/oauth-authorization-server")).json()
    assert body["code_challenge_methods_supported"] == ["S256"]
    assert "authorization_code" in body["grant_types_supported"]
    assert "refresh_token" in body["grant_types_supported"]


@pytest.mark.asyncio
async def test_registration_refuses_a_redirect_uri_it_cannot_trust(client):
    for uri in (
        "http://evil.test/cb",           # plaintext, not loopback
        "https://ok.test/cb#fragment",   # fragments are forbidden by RFC 6749
        "ftp://ok.test/cb",
    ):
        r = await client.post("/oauth/register", json={"redirect_uris": [uri]})
        assert r.status_code == 400, uri
        assert r.json()["error"] == "invalid_redirect_uri"

    # http on loopback is the one plaintext case that is allowed, because that
    # is how a desktop MCP client receives its callback.
    r = await client.post(
        "/oauth/register", json={"redirect_uris": ["http://127.0.0.1:9999/cb"]}
    )
    assert r.status_code == 201


# --------------------------------------------------------------------------
# OAuth: the flow
# --------------------------------------------------------------------------

REDIRECT_URI = "https://claude.ai/api/mcp/auth_callback"


@pytest_asyncio.fixture
async def account_password(db):
    await db.execute(
        "UPDATE service_accounts SET password_hash = $2 WHERE name = $1",
        TEST_ACCOUNT,
        auth.hash_password(PASSWORD),
    )
    return PASSWORD


async def register_client(client, uris=(REDIRECT_URI,)) -> str:
    r = await client.post(
        "/oauth/register",
        json={"redirect_uris": list(uris), "client_name": "pytest"},
    )
    assert r.status_code == 201, r.text
    return r.json()["client_id"]


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


async def get_code(client, client_id: str, challenge: str, password: str) -> str:
    r = await client.post(
        "/oauth/authorize",
        data={
            "username": TEST_ACCOUNT,
            "password": password,
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "code_challenge": challenge,
            "resource": auth.MCP_RESOURCE,
            "state": "xyz",
            "scope": "",
        },
    )
    assert r.status_code == 302, r.text
    params = parse_qs(urlparse(r.headers["location"]).query)
    assert params["state"] == ["xyz"]
    return params["code"][0]


@pytest.mark.asyncio
async def test_the_consent_screen_refuses_a_wrong_password(client, account_password):
    client_id = await register_client(client)
    _, challenge = pkce()
    r = await client.post(
        "/oauth/authorize",
        data={
            "username": TEST_ACCOUNT,
            "password": "wrong",
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "code_challenge": challenge,
            "resource": auth.MCP_RESOURCE,
        },
    )
    # Re-rendered with an error, not redirected: a failed login must not send
    # anything to the client's callback.
    assert r.status_code == 401
    assert "Incorrect account or password" in r.text


@pytest.mark.asyncio
async def test_an_unregistered_redirect_uri_is_not_redirected_to(client, account_password):
    """The open-redirector case.

    If the requested callback is not one we know, it is not a place to send an
    error either -- an attacker would receive the code. The person at the
    browser is the only safe audience.

    Guard: AUTH-004.
    """
    client_id = await register_client(client)
    _, challenge = pkce()
    r = await client.get(
        "/oauth/authorize",
        params={
            "client_id": client_id,
            "redirect_uri": "https://claude.ai.evil.test/cb",
            "response_type": "code",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
    )
    assert r.status_code == 400
    assert "evil.test" not in r.headers.get("location", "")
    assert "not registered" in r.text

    # A prefix match would have accepted the hostile URI above; the registered
    # one still works, so the refusal is the exact-match rule.
    r = await client.get(
        "/oauth/authorize",
        params={
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
    )
    assert r.status_code == 200
    assert "Sign in" in r.text or "<form" in r.text


@pytest.mark.asyncio
async def test_the_full_flow_mints_a_token_that_works(client, account_password):
    client_id = await register_client(client)
    verifier, challenge = pkce()
    code = await get_code(client, client_id, challenge, account_password)

    r = await client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "code_verifier": verifier,
            "client_id": client_id,
            "resource": auth.MCP_RESOURCE,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["token_type"] == "Bearer"
    assert body["expires_in"] > 0
    assert r.headers["cache-control"] == "no-store"

    # The point of the whole exercise: this token authenticates a real request.
    r = await client.get("/rest/v1/repositories", headers=bearer(body["access_token"]))
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_a_wrong_pkce_verifier_is_refused(client, account_password):
    # Guard: AUTH-005.
    client_id = await register_client(client)
    verifier, challenge = pkce()
    code = await get_code(client, client_id, challenge, account_password)

    other, _ = pkce()
    r = await client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "code_verifier": other,
            "client_id": client_id,
        },
    )
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_grant"


@pytest.mark.asyncio
async def test_replaying_an_authorization_code_revokes_the_whole_family(
    client, db, account_password
):
    """OAuth 2.1 §4.14.2.

    A code presented twice means someone else has a copy. Refusing the second
    presentation is not enough -- the *first* one already produced a token, and
    whoever holds it is still in. Everything that grant produced has to go.

    This is also the regression test for revoking inside the transaction that
    is about to roll back: the revocation would be undone on the way out and
    this assertion would find the access token still working.

    Guard: AUTH-001, AUTH-002.
    """
    client_id = await register_client(client)
    verifier, challenge = pkce()
    code = await get_code(client, client_id, challenge, account_password)

    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "code_verifier": verifier,
        "client_id": client_id,
    }
    first = (await client.post("/oauth/token", data=form)).json()
    access = first["access_token"]
    assert (
        await client.get("/rest/v1/repositories", headers=bearer(access))
    ).status_code == 200

    replay = await client.post("/oauth/token", data=form)
    assert replay.status_code == 400
    assert replay.json()["error"] == "invalid_grant"

    # The token minted by the legitimate first exchange is now dead.
    r = await client.get("/rest/v1/repositories", headers=bearer(access))
    assert r.status_code == 401
    assert r.json()["code"] == "TOKEN_REVOKED"


@pytest.mark.asyncio
async def test_a_rotated_refresh_token_cannot_be_replayed(client, account_password):
    # Guard: AUTH-006.
    client_id = await register_client(client)
    verifier, challenge = pkce()
    code = await get_code(client, client_id, challenge, account_password)
    first = (
        await client.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT_URI,
                "code_verifier": verifier,
                "client_id": client_id,
            },
        )
    ).json()

    refreshed = await client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": first["refresh_token"],
            "client_id": client_id,
        },
    )
    assert refreshed.status_code == 200, refreshed.text
    second = refreshed.json()
    assert second["refresh_token"] != first["refresh_token"]
    assert (
        await client.get("/rest/v1/repositories", headers=bearer(second["access_token"]))
    ).status_code == 200

    # Replaying the spent refresh token: recognised as reuse, not as an unknown
    # token, which is why the row records its replacement instead of being
    # deleted.
    replay = await client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": first["refresh_token"],
            "client_id": client_id,
        },
    )
    assert replay.status_code == 400
    assert replay.json()["error"] == "invalid_grant"

    # ...and the family it belongs to is gone, including the token that the
    # legitimate rotation had just issued.
    assert (
        await client.get("/rest/v1/repositories", headers=bearer(second["access_token"]))
    ).status_code == 401


@pytest.mark.asyncio
async def test_a_code_cannot_be_redeemed_by_a_different_client(
    client, db, account_password
):
    client_id = await register_client(client)
    other_id = await register_client(client)
    verifier, challenge = pkce()
    code = await get_code(client, client_id, challenge, account_password)

    r = await client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "code_verifier": verifier,
            "client_id": other_id,
        },
    )
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_grant"


@pytest.mark.asyncio
async def test_a_token_for_another_audience_is_refused(client, db, account_password):
    """RFC 8707.

    The token is valid, unexpired and belongs to a live account -- it was just
    minted for somewhere else. Without this check, a compromised downstream
    server replays its tokens against this one.

    Guard: AUTH-003.
    """
    client_id = await register_client(client)
    verifier, challenge = pkce()
    code = await get_code(client, client_id, challenge, account_password)
    body = (
        await client.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT_URI,
                "code_verifier": verifier,
                "client_id": client_id,
            },
        )
    ).json()
    access = body["access_token"]
    assert (
        await client.get("/rest/v1/repositories", headers=bearer(access))
    ).status_code == 200

    await db.execute(
        "UPDATE oauth_tokens SET resource = $2 WHERE token_hash = $1",
        auth.hash_token(access),
        "https://elsewhere.test/mcp",
    )
    r = await client.get("/rest/v1/repositories", headers=bearer(access))
    assert r.status_code == 401
    assert r.json()["code"] == "WRONG_AUDIENCE"


@pytest.mark.asyncio
async def test_the_token_endpoint_refuses_a_foreign_resource(client, account_password):
    client_id = await register_client(client)
    verifier, challenge = pkce()
    code = await get_code(client, client_id, challenge, account_password)
    r = await client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "code_verifier": verifier,
            "client_id": client_id,
            "resource": "https://elsewhere.test/mcp",
        },
    )
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_target"


@pytest.mark.asyncio
async def test_revocation_stops_the_token(client, account_password):
    client_id = await register_client(client)
    verifier, challenge = pkce()
    code = await get_code(client, client_id, challenge, account_password)
    body = (
        await client.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT_URI,
                "code_verifier": verifier,
                "client_id": client_id,
            },
        )
    ).json()

    r = await client.post(
        "/oauth/revoke", data={"token": body["access_token"], "client_id": client_id}
    )
    assert r.status_code == 200
    assert (
        await client.get("/rest/v1/repositories", headers=bearer(body["access_token"]))
    ).status_code == 401

    # RFC 7009: a token that never existed is indistinguishable from one that
    # did, or the endpoint becomes an oracle for guessing tokens.
    r = await client.post(
        "/oauth/revoke", data={"token": auth.new_token(), "client_id": client_id}
    )
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_one_client_cannot_revoke_anothers_token(client, account_password):
    client_id = await register_client(client)
    other_id = await register_client(client)
    verifier, challenge = pkce()
    code = await get_code(client, client_id, challenge, account_password)
    body = (
        await client.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT_URI,
                "code_verifier": verifier,
                "client_id": client_id,
            },
        )
    ).json()

    r = await client.post(
        "/oauth/revoke", data={"token": body["access_token"], "client_id": other_id}
    )
    assert r.status_code == 200  # RFC 7009 says 200 regardless
    # But it must not have worked.
    assert (
        await client.get("/rest/v1/repositories", headers=bearer(body["access_token"]))
    ).status_code == 200


# --------------------------------------------------------------------------
# MCP
# --------------------------------------------------------------------------

def rpc(method: str, params: dict | None = None, id_: int | None = 1) -> dict:
    body: dict = {"jsonrpc": "2.0", "method": method}
    if id_ is not None:
        body["id"] = id_
    if params is not None:
        body["params"] = params
    return body


@pytest.mark.asyncio
async def test_mcp_requires_a_token_and_says_where_to_get_one(client):
    r = await client.post(
        "/mcp", json=rpc("tools/list", {}), headers={"authorization": ""}
    )
    assert r.status_code == 401
    assert auth.RESOURCE_METADATA_URL in r.headers.get("www-authenticate", "")


@pytest.mark.asyncio
async def test_initialize_reports_the_protocol_version(client):
    r = await client.post(
        "/mcp",
        json=rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}}),
    )
    assert r.status_code == 200, r.text
    result = r.json()["result"]
    assert result["protocolVersion"] == "2025-06-18"
    assert "tools" in result["capabilities"]


@pytest.mark.asyncio
async def test_a_get_is_not_a_stream(client):
    """No SSE channel is offered, so say so rather than hanging the client."""
    r = await client.get("/mcp")
    assert r.status_code == 405


@pytest.mark.asyncio
async def test_a_batch_is_refused(client):
    """JSON-RPC batching was removed in the 2025-06-18 revision.

    HTTP 200 with an `error` member, not an HTTP error: a JSON-RPC fault is a
    successful HTTP transaction carrying a failed RPC, and a client reading the
    status code instead of the body would see a transport failure and retry.
    """
    r = await client.post("/mcp", json=[rpc("tools/list", {})])
    assert r.status_code == 200
    assert r.json()["error"]["code"] == -32600  # invalid request


@pytest.mark.asyncio
async def test_a_notification_gets_no_body(client):
    r = await client.post("/mcp", json=rpc("notifications/initialized", {}, id_=None))
    assert r.status_code == 202
    assert not r.content


async def publish(db, repo: str, name: str, manifest: dict) -> None:
    repo_id = await db.fetchval("SELECT id FROM repositories WHERE name = $1", repo)
    await db.execute(
        """
        INSERT INTO workspaces (repository_id, name, version, manifest)
        VALUES ($1, $2, $3, $4)
        """,
        repo_id,
        name,
        manifest["version"],
        json.dumps(manifest),
    )


STREAMING = {
    "name": "streamer",
    "version": "1.0.0",
    "parameters": [{"name": "WHO", "type": "STRING", "required": True}],
    "outputs": [{"name": "out", "type": "text/plain", "primary": True}],
    "services": ["job_submitter", "data_streaming"],
    "timeout_seconds": 20,
}


@pytest.mark.asyncio
async def test_tools_list_offers_only_workspaces_that_publish_the_service(
    client, db, workspace
):
    repo, _ = workspace
    await set_scope(db, ["*"])

    # The conftest fixture publishes job_submitter only.
    names = [
        t["name"]
        for t in (await client.post("/mcp", json=rpc("tools/list", {}))).json()["result"][
            "tools"
        ]
    ]
    assert "_pytest__fixture" not in names

    await publish(db, repo, "streamer", STREAMING)
    names = [
        t["name"]
        for t in (await client.post("/mcp", json=rpc("tools/list", {}))).json()["result"][
            "tools"
        ]
    ]
    assert "_pytest__streamer" in names


@pytest.mark.asyncio
async def test_tools_list_hides_a_workspace_that_could_only_fail(client, db, workspace):
    """A required FILE parameter needs an upload id an MCP client cannot obtain.

    Advertising it would trade a missing tool for one that always fails, which
    is worse: the model would keep trying.

    Guard: AUTH-012.
    """
    repo, _ = workspace
    await set_scope(db, ["*"])
    needs_file = dict(STREAMING, name="needsfile")
    needs_file["parameters"] = [{"name": "DATA", "type": "FILE", "required": True}]
    await publish(db, repo, "needsfile", needs_file)

    optional_file = dict(STREAMING, name="optfile")
    optional_file["parameters"] = [{"name": "DATA", "type": "FILE", "required": False}]
    await publish(db, repo, "optfile", optional_file)

    result = (await client.post("/mcp", json=rpc("tools/list", {}))).json()["result"]
    names = [t["name"] for t in result["tools"]]
    assert "_pytest__needsfile" not in names
    # The optional one is offered, with the unusable parameter left out of the
    # schema rather than the whole tool withdrawn.
    assert "_pytest__optfile" in names
    schema = next(t for t in result["tools"] if t["name"] == "_pytest__optfile")
    assert "DATA" not in schema["inputSchema"]["properties"]


@pytest.mark.asyncio
async def test_tools_list_respects_repository_scope(client, db, workspace):
    # Guard: AUTH-013.
    repo, _ = workspace
    await publish(db, repo, "streamer", STREAMING)

    await set_scope(db, ["*"])
    names = [
        t["name"]
        for t in (await client.post("/mcp", json=rpc("tools/list", {}))).json()["result"][
            "tools"
        ]
    ]
    assert "_pytest__streamer" in names

    await set_scope(db, ["SOMETHING_ELSE"])
    names = [
        t["name"]
        for t in (await client.post("/mcp", json=rpc("tools/list", {}))).json()["result"][
            "tools"
        ]
    ]
    assert "_pytest__streamer" not in names


@pytest.mark.asyncio
async def test_an_out_of_scope_tool_looks_exactly_like_an_unknown_one(
    client, db, workspace
):
    """Otherwise the error message enumerates repositories the caller cannot see."""
    repo, _ = workspace
    await publish(db, repo, "streamer", STREAMING)
    await set_scope(db, ["SOMETHING_ELSE"])

    hidden = await client.post(
        "/mcp", json=rpc("tools/call", {"name": "_pytest__streamer", "arguments": {}})
    )
    absent = await client.post(
        "/mcp", json=rpc("tools/call", {"name": "_pytest__nosuchthing", "arguments": {}})
    )
    assert hidden.status_code == absent.status_code
    assert hidden.json()["error"]["code"] == absent.json()["error"]["code"]
    # Identical wording, differing only in the name the caller itself supplied.
    # Comparing the two messages directly would fail for that reason alone and
    # prove nothing, so compare each against the one template.
    assert hidden.json()["error"]["message"] == "unknown tool '_pytest__streamer'"
    assert absent.json()["error"]["message"] == "unknown tool '_pytest__nosuchthing'"


def test_the_input_schema_mirrors_the_manifest():
    from datum_sync import mcp

    manifest = Manifest.model_validate(
        dict(
            STREAMING,
            parameters=[
                {"name": "WHO", "type": "STRING", "required": True},
                {"name": "LOUD", "type": "BOOLEAN", "default": False},
                {"name": "N", "type": "INTEGER"},
                {
                    "name": "MODE",
                    "type": "LOOKUP_CHOICE",
                    "choices": ["a", "b"],
                },
            ],
        )
    )
    schema = mcp.input_schema(manifest)
    assert schema["required"] == ["WHO"]
    assert schema["properties"]["LOUD"]["type"] == "boolean"
    assert schema["properties"]["N"]["type"] == "integer"
    assert schema["properties"]["MODE"]["enum"] == ["a", "b"]


# -- login rate limiting ---------------------------------------------------
#
# The consent screen is the only place a guessable secret is accepted, and it is
# reachable by anyone who can reach the server. argon2 makes each guess slow,
# which bounds the guessing rate but also makes the form a cheap way to burn
# CPU -- an unknown account still pays for a full hash, deliberately, so that
# the form does not enumerate names.


async def wrong_login(
    client, client_id, username=TEST_ACCOUNT, password="wrong", challenge=None
):
    if challenge is None:
        _, challenge = pkce()
    return await client.post(
        "/oauth/authorize",
        data={
            "username": username,
            "password": password,
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "code_challenge": challenge,
            "resource": auth.MCP_RESOURCE,
        },
    )


@pytest.mark.asyncio
async def test_repeated_wrong_passwords_lock_the_account_out(client, account_password):
    """And the *right* password is refused too, which is what proves it.

    A test that only counts 401s cannot tell a lockout from a form that was
    always going to reject. The tell is that the same request which succeeds in
    `test_a_login_below_the_limit_is_not_refused` returns 429 here.

    Guard: AUTH-014.
    """
    client_id = await register_client(client)
    for i in range(config.PASSWORD_MAX_ATTEMPTS):
        r = await wrong_login(client, client_id)
        assert r.status_code == 401, f"attempt {i} was not a plain refusal: {r.text}"

    r = await wrong_login(client, client_id)
    assert r.status_code == 429
    assert "Too many failed attempts" in r.text
    # Without Retry-After the client has to guess, and guessing means retrying,
    # which extends the lockout.
    assert int(r.headers["Retry-After"]) > 0

    _, challenge = pkce()
    r = await client.post(
        "/oauth/authorize",
        data={
            "username": TEST_ACCOUNT,
            "password": account_password,
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "code_challenge": challenge,
            "resource": auth.MCP_RESOURCE,
        },
    )
    assert r.status_code == 429, "the correct password got through a lockout"


@pytest.mark.asyncio
async def test_a_login_below_the_limit_is_not_refused(client, account_password):
    """The other half: the limiter does not break an ordinary fumbled login."""
    client_id = await register_client(client)
    for _ in range(config.PASSWORD_MAX_ATTEMPTS - 1):
        assert (await wrong_login(client, client_id)).status_code == 401

    _, challenge = pkce()
    code = await get_code(client, client_id, challenge, account_password)
    assert code


@pytest.mark.asyncio
async def test_a_successful_login_clears_the_counter(client, account_password):
    """Otherwise failures accumulate across weeks and lock out a real user.

    Guard: AUTH-016.
    """
    client_id = await register_client(client)
    for _ in range(config.PASSWORD_MAX_ATTEMPTS - 1):
        assert (await wrong_login(client, client_id)).status_code == 401

    _, challenge = pkce()
    await get_code(client, client_id, challenge, account_password)

    # If the counter had survived the success, the first of these would be the
    # one that trips the limit.
    for i in range(config.PASSWORD_MAX_ATTEMPTS - 1):
        r = await wrong_login(client, client_id)
        assert r.status_code == 401, f"counter was not cleared (attempt {i})"


@pytest.mark.asyncio
async def test_the_lockout_does_not_reveal_whether_an_account_exists(
    client, account_password
):
    """The limit is checked before the database, so it cannot be an oracle.

    A limiter that only counted real accounts would answer 429 for a name that
    exists and 401 for one that does not -- turning a defence into account
    enumeration.

    Guard: AUTH-017.
    """
    client_id = await register_client(client)
    unknown = f"no-such-account-{uuid.uuid4().hex[:8]}"
    # One challenge for every request here: it is echoed into the form as a
    # hidden field, so a fresh one per call would make the two pages differ for
    # a reason that has nothing to do with the account. Holding it equal leaves
    # the username as the only difference, which is the point of the comparison.
    _, challenge = pkce()

    for _ in range(config.PASSWORD_MAX_ATTEMPTS):
        r = await wrong_login(client, client_id, username=unknown, challenge=challenge)
        assert r.status_code == 401

    locked_unknown = await wrong_login(
        client, client_id, username=unknown, challenge=challenge
    )
    assert locked_unknown.status_code == 429

    for _ in range(config.PASSWORD_MAX_ATTEMPTS):
        await wrong_login(client, client_id, challenge=challenge)
    locked_real = await wrong_login(client, client_id, challenge=challenge)

    assert locked_real.status_code == locked_unknown.status_code

    # The countdown is normalised out before comparing. It legitimately differs
    # -- the two lockouts started a few hundred milliseconds apart, and the
    # value is truncated to whole seconds, so the pages disagree whenever a
    # second boundary falls between them. That is a function of when guessing
    # began, which the caller already knows; it says nothing about the account.
    # Comparing the raw bodies made this test fail about one run in five.
    countdown = re.compile(r"\d+ seconds")
    assert countdown.search(locked_unknown.text), "no countdown to normalise"
    assert countdown.sub("N seconds", locked_real.text) == countdown.sub(
        "N seconds", locked_unknown.text
    )


@pytest.mark.asyncio
async def test_the_lockout_expires(client, account_password, monkeypatch):
    """A lockout that never lifts is a denial of service on the real user.

    Guard: AUTH-015.
    """
    client_id = await register_client(client)
    for _ in range(config.PASSWORD_MAX_ATTEMPTS):
        await wrong_login(client, client_id)
    assert (await wrong_login(client, client_id)).status_code == 429

    # Rather than sleep for the real window: the failures already recorded fall
    # outside a zero-length window, so the next attempt is judged on an empty
    # history -- the same state the passage of time produces.
    monkeypatch.setattr(config, "PASSWORD_WINDOW_SECONDS", 0)
    r = await wrong_login(client, client_id)
    assert r.status_code == 401, "the lockout outlived its window"


# -- PUBLIC_URL ------------------------------------------------------------


def test_an_unset_public_url_refuses_to_start(monkeypatch):
    """It is the issuer, the advertised origin and the token audience.

    A guessed default serves discovery happily and mints tokens no client can
    use, so the failure appears at the client as an opaque authorization error
    with nothing in the log pointing back here. Better to refuse at startup.

    Guard: AUTH-018.
    """
    monkeypatch.setattr(config, "PUBLIC_URL_CONFIGURED", False)
    with pytest.raises(RuntimeError, match="PUBLIC_URL is not set"):
        config.require_public_url()


def test_a_configured_public_url_is_returned(monkeypatch):
    monkeypatch.setattr(config, "PUBLIC_URL_CONFIGURED", True)
    monkeypatch.setattr(config, "PUBLIC_URL", "https://sync.example.com")
    assert config.require_public_url() == "https://sync.example.com"


# --------------------------------------------------------------------------
# browser sessions (web UI)
# --------------------------------------------------------------------------

@pytest_asyncio.fixture
async def browser(db, token):
    """An *unauthenticated* client that keeps cookies.

    Deliberately not the `client` fixture: that one sends a bearer token on
    every request, which would satisfy the middleware before the cookie was ever
    consulted and make every test below pass whether sessions work or not. It
    still depends on `token`, because that fixture is what creates the account
    `account_password` then gives a password to -- without it the UPDATE matches
    no rows and sign-in fails for a reason with nothing to do with sessions.
    """
    await db_module.init_pool()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as c:
        yield c
    await db_module.close_pool()


async def sign_in(browser, password: str, name: str = TEST_ACCOUNT):
    return await browser.post("/ui/login", json={"name": name, "password": password})


@pytest.mark.asyncio
async def test_signing_in_returns_a_session_that_authenticates_a_rest_call(
    browser, account_password
):
    """Both halves: the same request is refused before sign-in and served after."""
    before = await browser.get("/rest/v1/whoami")
    assert before.status_code == 401

    r = await sign_in(browser, account_password)
    assert r.status_code == 200, r.text
    assert r.json()["name"] == TEST_ACCOUNT
    assert browser.cookies.get(auth.SESSION_COOKIE)

    after = await browser.get("/rest/v1/whoami")
    assert after.status_code == 200
    assert after.json() == {**r.json(), "source": "session"}


@pytest.mark.asyncio
async def test_a_wrong_password_at_the_ui_mints_nothing(browser, account_password):
    r = await sign_in(browser, "not the password")
    assert r.status_code == 401
    assert r.json()["code"] == "INVALID_CREDENTIALS"
    assert browser.cookies.get(auth.SESSION_COOKIE) is None
    assert (await browser.get("/rest/v1/whoami")).status_code == 401


@pytest.mark.asyncio
async def test_the_ui_login_is_rate_limited_too(browser, account_password):
    """The limiter lives in authenticate_password, so this route inherits it.

    Worth asserting rather than assuming: it is exactly the kind of protection
    that gets added to one caller and forgotten on the second.
    """
    for _ in range(config.PASSWORD_MAX_ATTEMPTS):
        assert (await sign_in(browser, "wrong")).status_code == 401
    locked = await sign_in(browser, account_password)
    assert locked.status_code == 429
    assert int(locked.headers["retry-after"]) > 0


@pytest.mark.asyncio
async def test_the_session_cookie_is_refused_on_the_service_paths(
    browser, account_password, workspace, token
):
    """CSRF. `GET /stream/...` runs a workspace, and SameSite=Lax sends the
    cookie on top-level navigation, so any page that links here would execute
    someone's workspace as them.

    The bearer half proves the route is reachable at all: without it a 401 from
    a misspelled path would pass just as well.

    Guard: AUTH-019.
    """
    repo, ws = workspace
    assert (await sign_in(browser, account_password)).status_code == 200

    with_cookie = await browser.get(f"/stream/{repo}/{ws}?WHO=x")
    assert with_cookie.status_code == 401
    assert with_cookie.json()["code"] == "UNAUTHENTICATED"

    with_token = await browser.get(f"/stream/{repo}/{ws}?WHO=x", headers=bearer(token))
    assert with_token.status_code != 401


@pytest.mark.asyncio
async def test_a_session_is_not_usable_as_a_bearer_token(browser, account_password):
    """The two channels query different `kind`s and must not cross.

    Presenting the cookie's value in an Authorization header is what an attacker
    who has read it out of a proxy log would try first, and it would sidestep
    the service-path restriction above.

    Guard: AUTH-020.
    """
    assert (await sign_in(browser, account_password)).status_code == 200
    raw = browser.cookies.get(auth.SESSION_COOKIE)

    r = await browser.get("/rest/v1/whoami", headers=bearer(raw))
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_signing_out_revokes_the_session_and_not_only_the_cookie(
    browser, account_password
):
    """Clearing the cookie is cosmetic: a session captured before sign-out has
    to stop working, so the test re-presents the raw value by hand.
    Guard: AUTH-021.
"""
    assert (await sign_in(browser, account_password)).status_code == 200
    raw = browser.cookies.get(auth.SESSION_COOKIE)

    out = await browser.post("/ui/logout")
    assert out.status_code == 200
    assert browser.cookies.get(auth.SESSION_COOKIE) is None

    # Sent as a literal header rather than through the cookie jar: sign-out has
    # just written an expiring Set-Cookie for this name, and putting the value
    # back into the jar competes with that entry instead of replacing it -- the
    # request then carries nothing and the test passes for the wrong reason. A
    # replayed cookie is a header an attacker constructs by hand anyway.
    browser.cookies.clear()
    replayed = await browser.get(
        "/rest/v1/whoami", headers={"cookie": f"{auth.SESSION_COOKIE}={raw}"}
    )
    assert replayed.status_code == 401
    assert replayed.json()["code"] == "TOKEN_REVOKED"


@pytest.mark.asyncio
async def test_disabling_an_account_kills_a_session_already_in_flight(
    browser, db, account_password
):
    """Otherwise the control does nothing for up to SESSION_TTL_SECONDS.

    Guard: AUTH-022.
    """
    assert (await sign_in(browser, account_password)).status_code == 200
    assert (await browser.get("/rest/v1/whoami")).status_code == 200

    await db.execute(
        "UPDATE service_accounts SET disabled = true WHERE name = $1", TEST_ACCOUNT
    )
    r = await browser.get("/rest/v1/whoami")
    assert r.status_code == 401
    assert r.json()["code"] == "ACCOUNT_DISABLED"


@pytest.mark.asyncio
async def test_an_expired_session_is_refused(browser, db, account_password):
    # Guard: AUTH-023.
    assert (await sign_in(browser, account_password)).status_code == 200
    await db.execute(
        """
        UPDATE oauth_tokens SET expires_at = now() - interval '1 second'
         WHERE kind = 'session'
           AND account_id = (SELECT id FROM service_accounts WHERE name = $1)
        """,
        TEST_ACCOUNT,
    )
    r = await browser.get("/rest/v1/whoami")
    assert r.status_code == 401
    assert r.json()["code"] == "TOKEN_EXPIRED"


@pytest.mark.asyncio
async def test_the_shell_is_public_but_the_data_behind_it_is_not(browser):
    """The shell has to be reachable signed-out or there is nowhere to sign in."""
    assert (await browser.get("/ui")).status_code == 200
    assert (await browser.get("/rest/v1/whoami")).status_code == 401


# --------------------------------------------------------------------------
# the job listing and the admin routes
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_job_listing_hides_jobs_outside_scope(client, db, workspace):
    """Both halves: the job is listed when in scope and absent when not.

    The repository the caller is narrowed to holds a job of its own, so the
    allowed set is non-empty and the SQL filter is what hides the fixture's
    job. Narrowing to an empty repository instead is answered by the early
    return for an empty allowed set, which leaves the filter untested -- that
    is what the first version of this test did, and break_the_guard caught it:
    the filter could be replaced by `OR true` and this still passed.

    Guard: AUTH-024.
    """
    repo, ws = workspace
    submit = await client.post(
        f"/rest/v1/transformations/submit/{repo}/{ws}", json={"params": {"WHO": "x"}}
    )
    job_id = submit.json()["id"]

    listed = await client.get("/rest/v1/transformations/jobs")
    assert job_id in {j["id"] for j in listed.json()["items"]}

    # Inserted directly: what is needed is a row in another repository, not a
    # second published workspace, and the listing reads `jobs.repository` as
    # text regardless of how it got there. Removed by hand -- the db fixture
    # only cleans up jobs in TEST_REPO.
    other_id = await db.fetchval(
        """
        INSERT INTO jobs (repository, workspace, submitted_by)
        VALUES ($1, $2, $3) RETURNING id
        """,
        "somewhere-else", "elsewhere", "someone",
    )
    try:
        await set_scope(db, ["somewhere-else"])
        hidden = await client.get("/rest/v1/transformations/jobs")
        assert hidden.status_code == 200
        ids = {j["id"] for j in hidden.json()["items"]}
        assert job_id not in ids
        # The listing was filtered, not merely empty.
        assert str(other_id) in ids
    finally:
        await db.execute("DELETE FROM jobs WHERE repository = $1", "somewhere-else")


@pytest.mark.asyncio
async def test_the_job_listing_applies_scope_before_the_limit(client, db, workspace):
    """A scoped caller must not be paged out of their own jobs.

    Filtering the *result* of a LIMITed query is the natural way to write this
    and is wrong: the rows the caller may not see still consume the page, so a
    busy queue elsewhere hides their jobs entirely and the row count leaks how
    much is going on outside their scope. limit=1 makes that difference
    observable -- with the filter in SQL the one row returned is theirs.

    Guard: AUTH-025.
    """
    repo, ws = workspace
    mine = (
        await client.post(
            f"/rest/v1/transformations/submit/{repo}/{ws}",
            json={"params": {"WHO": "mine"}},
        )
    ).json()["id"]
    # A newer job in a repository the caller will not be scoped to. Inserted
    # directly: submitting it would need a second published workspace, and the
    # listing reads `jobs.repository` as text regardless of how it got there.
    await db.execute(
        "INSERT INTO jobs (repository, workspace, submitted_by) VALUES ($1, $2, $3)",
        "other-repo", "elsewhere", "someone",
    )
    try:
        await set_scope(db, [repo])
        r = await client.get("/rest/v1/transformations/jobs?limit=1")
        items = r.json()["items"]
        assert len(items) == 1
        assert items[0]["id"] == mine
    finally:
        await db.execute("DELETE FROM jobs WHERE repository = $1", "other-repo")


@pytest.mark.asyncio
async def test_the_accounts_listing_requires_admin(client, db):
    """Both halves: the same request is served for an admin and refused without.

    `token` is admin by default, so the refusal has to be created rather than
    assumed. Not restored afterwards because it does not need to be: `token`
    drops and recreates the account for every test.

    Guard: AUTH-026.
    """
    assert (await client.get("/rest/v1/accounts")).status_code == 200

    await db.execute(
        "UPDATE service_accounts SET is_admin = false WHERE name = $1", TEST_ACCOUNT
    )
    r = await client.get("/rest/v1/accounts")
    assert r.status_code == 403
    assert r.json()["code"] == "FORBIDDEN"


@pytest.mark.asyncio
async def test_the_accounts_listing_never_returns_a_hash(client):
    """Storing sha256(token) is pointless if a route hands it back: the hash is
    the whole credential for a lookup keyed on it."""
    r = await client.get("/rest/v1/accounts")
    body = r.text
    assert "token_hash" not in body and "password_hash" not in body
    mine = next(a for a in r.json()["items"] if a["name"] == TEST_ACCOUNT)
    assert mine["has_token"] is True


@pytest.mark.asyncio
async def test_revoking_grants_ends_a_live_session(browser, client, account_password):
    """The Admin screen's one destructive control. Two-sided: the session works
    before the revoke and not after."""
    assert (await sign_in(browser, account_password)).status_code == 200
    assert (await browser.get("/rest/v1/whoami")).status_code == 200

    r = await client.delete(f"/rest/v1/accounts/{TEST_ACCOUNT}/grants")
    assert r.status_code == 200
    assert r.json()["revoked"] >= 1

    after = await browser.get("/rest/v1/whoami")
    assert after.status_code == 401
    assert after.json()["code"] == "TOKEN_REVOKED"


@pytest.mark.asyncio
async def test_revoking_grants_leaves_the_service_account_token_working(
    browser, client, account_password
):
    """Sessions and OAuth grants live in oauth_tokens; the account's own token
    is a column on service_accounts and must survive. Otherwise 'sign out
    everywhere' quietly locks every script out too."""
    assert (await sign_in(browser, account_password)).status_code == 200
    await client.delete(f"/rest/v1/accounts/{TEST_ACCOUNT}/grants")
    assert (await client.get("/rest/v1/whoami")).status_code == 200


@pytest.mark.asyncio
async def test_revoking_grants_for_an_unknown_account_is_a_404(client):
    r = await client.delete("/rest/v1/accounts/no-such-account/grants")
    assert r.status_code == 404


# -- development mode ------------------------------------------------------
#
# `DATUM_SYNC_AUTH=off` serves every request as an administrator. The tests that
# matter here are not that it works -- that is one line of middleware -- but
# that it is off unless asked for in exactly one way, and that a server running
# with it on cannot be reached from another machine.


def test_authentication_is_on_unless_the_environment_says_otherwise():
    """The default, read from the real environment this suite runs in.

    Everything else in this section patches the flag on, so without this one the
    suite would never assert the shipped configuration."""
    assert config.AUTH_DISABLED is False


@pytest.mark.parametrize("raw", [None, "", "on", "false", "0", "no", "of", "offf"])
def test_anything_but_the_word_off_leaves_authentication_on(raw):
    """`false`, `0` and `no` are the trap: under a truthiness test each of them
    would turn authentication *off*, which is the reverse of what anyone typing
    them means. `of` and `offf` are the typos, and a typo must fail safe.
    Guard: AUTH-030.
"""
    assert config._auth_disabled(raw) is False


@pytest.mark.parametrize("raw", ["off", "OFF", " off ", "Off"])
def test_the_word_off_disables_authentication(raw):
    """The other half. Without it the test above would pass just as well against
    a function that returned False unconditionally, and the flag would simply
    never work. Case and surrounding whitespace are forgiven: `OFF ` in an .env
    is a typo with an unambiguous intention."""
    assert config._auth_disabled(raw) is True


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.88.102", "::"])
def test_a_reachable_server_refuses_to_start_without_authentication(
    monkeypatch, host
):
    # Guard: AUTH-027.
    monkeypatch.setattr(config, "AUTH_DISABLED", True)
    monkeypatch.setattr(config, "HOST", host)
    with pytest.raises(RuntimeError, match="DATUM_SYNC_AUTH=off"):
        config.require_safe_auth()


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_the_same_server_starts_when_only_this_machine_can_reach_it(
    monkeypatch, host
):
    """The other half of the guard. Without this the refusal above would pass
    just as well against a function that raised unconditionally."""
    monkeypatch.setattr(config, "AUTH_DISABLED", True)
    monkeypatch.setattr(config, "HOST", host)
    config.require_safe_auth()


def test_the_host_check_does_not_apply_when_authentication_is_on(monkeypatch):
    """A normal server binds 0.0.0.0 -- that is the default. The loopback
    requirement is a consequence of disabling auth, not a rule about binding."""
    monkeypatch.setattr(config, "AUTH_DISABLED", False)
    monkeypatch.setattr(config, "HOST", "0.0.0.0")
    config.require_safe_auth()


@pytest.mark.asyncio
async def test_with_auth_off_an_unauthenticated_request_is_served_as_admin(
    monkeypatch, client
):
    """Two-sided: the same credential-free request 401s with the flag off."""
    bare = {"Authorization": ""}
    refused = await client.get("/rest/v1/whoami", headers=bare)
    assert refused.status_code == 401

    monkeypatch.setattr(config, "AUTH_DISABLED", True)
    allowed = await client.get("/rest/v1/whoami", headers=bare)
    assert allowed.status_code == 200
    body = allowed.json()
    assert body["is_admin"] is True
    # Not "token" or "oauth": a caller branching on source should see something
    # it does not recognise rather than a plausible-looking lie.
    assert body["source"] == "auth-disabled"
    assert body["name"] == "auth-disabled"


@pytest.mark.asyncio
async def test_with_auth_off_the_oauth_endpoints_still_authenticate_themselves(
    monkeypatch, client
):
    """Disabling the request credential must not disable OAuth's own rules.

    The flag makes every route trust the caller; the endpoints that *mint*
    credentials authenticate by client secret and PKCE instead, and those are a
    separate mechanism that this must not reach into. Otherwise a dev server
    would hand out real tokens for the real audience to anyone who asked.

    Note what this does *not* prove: the middleware applies the flag after the
    PUBLIC_PATHS check, and that ordering is defensive rather than load-bearing
    -- no OAuth handler reads request.state.principal, so today the two orders
    are indistinguishable from outside. Do not read this test as covering it."""
    monkeypatch.setattr(config, "AUTH_DISABLED", True)
    r = await client.post(
        "/oauth/token",
        data={"grant_type": "authorization_code", "code": "nope"},
        headers={"Authorization": ""},
    )
    # An OAuth error object, not our envelope and not a 200 with a principal
    # attached: the endpoint reached its own rules and refused on them. The code
    # is invalid_request because no client_id was sent -- it never gets as far as
    # the credential.
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_request"


@pytest.mark.parametrize(
    "host", ["127.0.0.1", "127.0.0.53", "::1", "localhost", "::ffff:127.0.0.1"]
)
def test_a_local_peer_is_recognised_however_it_is_spelled(host):
    """A dual-stack listener reports an IPv4 loopback peer as ::ffff:127.0.0.1,
    and the loopback block is all of 127.0.0.0/8 rather than one address. Miss
    either and the flag is merely useless -- which is the safe direction, and so
    would go unnoticed until someone wondered why dev mode never worked."""
    assert config.is_loopback_client(Address(host, 54321)) is True


@pytest.mark.parametrize("host", ["192.168.88.102", "10.0.0.5", "::ffff:8.8.8.8"])
def test_a_remote_peer_is_not_local(host):
    """The half that matters. `::ffff:8.8.8.8` is the one to get wrong: strip
    the prefix carelessly and a mapped *public* address reads as loopback.
    Guard: AUTH-029.
"""
    assert config.is_loopback_client(Address(host, 54321)) is False


def test_no_peer_address_counts_as_local():
    """The in-process test transport supplies no client. That is not a network
    connection at all, so refusing it would break every test below rather than
    protect anything."""
    assert config.is_loopback_client(None) is True


@pytest.mark.asyncio
async def test_a_remote_caller_is_refused_even_with_auth_off(monkeypatch, client):
    """The guard the startup check cannot provide.

    `require_safe_auth()` reads config.HOST, and `uvicorn --host 0.0.0.0` never
    consults it -- so an auth-off server *can* end up bound to the network. This
    runs on the socket's own peer address, per request, and no start-up flag or
    forged header reaches it.
    Guard: AUTH-028.
"""
    monkeypatch.setattr(config, "AUTH_DISABLED", True)
    bare = {"Authorization": ""}

    # Local: served, as the test above establishes.
    assert (await client.get("/rest/v1/whoami", headers=bare)).status_code == 200

    monkeypatch.setattr(
        config, "is_loopback_client", lambda _client: False
    )
    remote = await client.get("/rest/v1/whoami", headers=bare)
    assert remote.status_code == 403
    assert remote.json()["code"] == "FORBIDDEN"


# --- vault_scope reaches the principal by every route ----------------------
# Four functions build a Principal from four different queries, and a fifth is
# the sign-in path in ui.py. Missing the column in any one of them yields
# vault_scope=None, which fails *closed*: no exception, no 500, and every
# pre-existing test still green. One shared test would not find that, so each
# route is asserted on its own.

VAULT_SCOPE_FIXTURE = {"read": ["dev/**"], "deny": ["dev/secrets/**"]}


@pytest_asyncio.fixture
async def vault_account(db):
    """An account carrying a vault_scope, plus its raw service token."""
    await db.execute("DELETE FROM service_accounts WHERE name = '_pytest_vault'")
    account_id = await db.fetchval(
        """
        INSERT INTO service_accounts (name, max_tier, vault_scope)
        VALUES ('_pytest_vault', 4, $1)
        RETURNING id
        """,
        json.dumps(VAULT_SCOPE_FIXTURE),
    )
    _, raw = await tokens.create(db, account_id, "fixture")
    try:
        yield account_id, raw
    finally:
        await db.execute("DELETE FROM service_accounts WHERE name = '_pytest_vault'")


async def test_a_service_token_carries_vault_scope(db, vault_account):
    _, raw = vault_account
    p = await auth.resolve(db, raw)
    assert p.vault_scope == VAULT_SCOPE_FIXTURE


async def test_an_oauth_token_carries_vault_scope(db, vault_account):
    account_id, _ = vault_account
    access = auth.new_token()
    await db.execute(
        """
        INSERT INTO oauth_tokens (token_hash, kind, account_id, expires_at)
        VALUES ($1, 'access', $2, now() + interval '1 hour')
        """,
        auth.hash_token(access),
        account_id,
    )
    p = await auth.resolve(db, access)
    assert p.source == "oauth"
    assert p.vault_scope == VAULT_SCOPE_FIXTURE


async def test_a_session_carries_vault_scope(db, vault_account):
    account_id, _ = vault_account
    raw = await auth.create_session(db, account_id)
    p = await auth.resolve_session(db, raw)
    assert p.source == "session"
    assert p.vault_scope == VAULT_SCOPE_FIXTURE


async def test_principal_by_name_carries_vault_scope(db, vault_account):
    p = await auth.principal_by_name(db, "_pytest_vault")
    assert p.source == "local"
    assert p.vault_scope == VAULT_SCOPE_FIXTURE


async def test_vault_scope_arrives_decoded_not_as_json_text(db, vault_account):
    """The trap this column walks into.

    No JSON codec is registered on the pool (db.py), so a JSONB column comes
    back as the *string* `{"read": [...]}`. A str is truthy and has no .get, so
    the failure would be an AttributeError inside a permission check rather than
    anything the query site could be blamed for.
    """
    _, raw = vault_account
    p = await auth.resolve(db, raw)
    assert isinstance(p.vault_scope, dict)
    assert p.allows_vault_path("read", "dev/notes.md")
    assert not p.allows_vault_path("read", "dev/secrets/key.md")


async def test_an_account_without_a_scope_gets_no_vault_access(db, token):
    """The conftest account is unscoped and admin -- and still has no vault.

    Admin is repository authority, not vault authority. If those were the same
    thing the column would not need to exist.
    """
    p = await auth.resolve(db, token)
    assert p.is_admin
    assert p.vault_scope is None
    assert not p.allows_vault_path("read", "dev/notes.md")


async def test_whoami_reports_vault_scope(client, vault_account):
    _, raw = vault_account
    r = await client.get(
        "/rest/v1/whoami", headers={"Authorization": f"Bearer {raw}"}
    )
    assert r.status_code == 200
    assert r.json()["vault_scope"] == VAULT_SCOPE_FIXTURE


def test_connection_grants_is_not_an_authority_field():
    """No Principal field may claim an authority nothing enforces.

    Guard: AUTHZ-001.

    `service_accounts.connection_grants` was carried on every Principal, and on
    every query that builds one, from migration 001 until it was removed. No
    code ever read it to decide anything: whether a workspace may use a
    connection is settled by the connection's own scope/scope_targets
    (`connections.matches_scope`) and by the publisher's `max_tier`. Migration
    008's own comment asserts the opposite -- "connection_grants controls which
    connections a workspace can resolve at job time" -- which is how it kept
    looking load-bearing.

    A dead permission field is worse than no field. It reads like a control in
    every review, and the first person to grant it will believe they restricted
    something.

    The SQL half of this matters as much as the dataclass half: re-adding the
    column to a query would put it back on the rows Principals are built from,
    where the next constructor to accept **row picks it up silently. The column
    itself still exists and is still populated -- dropping it is a later
    migration -- so this asserts what the code reads, not what the table holds.
    """
    assert "connection_grants" not in {f.name for f in fields(auth.Principal)}

    src = pathlib.Path(auth.__file__).parent
    offenders = sorted(
        p.name for p in src.glob("*.py") if "connection_grants" in
        # Comments are allowed to name it; a SQL column reference is not.
        re.sub(r"#.*", "", p.read_text())
    )
    assert offenders == [], f"connection_grants read back into: {offenders}"
