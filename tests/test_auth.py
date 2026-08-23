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
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import pytest_asyncio

from datum_sync import auth, config
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


async def set_scope(db, scope: list[str] | None) -> None:
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
    await db.execute(
        "UPDATE service_accounts SET token_expires = now() - interval '1 second' "
        "WHERE name = $1",
        TEST_ACCOUNT,
    )
    r = await client.get("/rest/v1/repositories")
    assert r.status_code == 401
    assert r.json()["code"] == "TOKEN_EXPIRED"

    await db.execute(
        "UPDATE service_accounts SET token_expires = NULL WHERE name = $1", TEST_ACCOUNT
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
        connection_grants=None, is_admin=False, source="token",
    )
    assert p.allows_repo("SCIMAC")
    assert not p.allows_repo("SCIMAC_OTHER")
    assert not p.allows_repo("OTHER")

    glob = auth.Principal(
        account_id=1, name="x", max_tier=1, repo_scope=["S*"],
        connection_grants=None, is_admin=False, source="token",
    )
    assert not glob.allows_repo("SCIMAC")

    everything = auth.Principal(
        account_id=1, name="x", max_tier=1, repo_scope=None,
        connection_grants=None, is_admin=False, source="token",
    )
    assert everything.allows_repo("anything")

    nothing = auth.Principal(
        account_id=1, name="x", max_tier=1, repo_scope=[],
        connection_grants=None, is_admin=False, source="token",
    )
    # An empty array is not the same as NULL, and must not mean "all".
    assert not nothing.allows_repo("SCIMAC")


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
    repo, ws = workspace
    await set_scope(db, ["SOMETHING_ELSE"])
    r = await client.post(
        f"/rest/v1/transformations/submit/{repo}/{ws}",
        json={"params": {"WHO": "world"}},
    )
    assert r.status_code == 403

    await set_scope(db, None)
    r = await client.post(
        f"/rest/v1/transformations/submit/{repo}/{ws}",
        json={"params": {"WHO": "world"}},
    )
    assert r.status_code == 202


@pytest.mark.asyncio
async def test_a_listing_hides_repositories_outside_scope(client, db, workspace):
    repo, _ = workspace
    await set_scope(db, None)
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
async def test_a_job_in_another_repository_is_not_readable(client, db, workspace):
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
    await set_scope(db, None)

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
    """
    repo, _ = workspace
    await set_scope(db, None)
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
    repo, _ = workspace
    await publish(db, repo, "streamer", STREAMING)

    await set_scope(db, None)
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
