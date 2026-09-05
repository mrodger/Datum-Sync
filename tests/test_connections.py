"""Connections: sealing, scope, resolution, and the routes.

The tests that matter most here are the negative ones. A credential store that
stores and returns credentials is easy to write and easy to test; what makes
this one worth having is that certain things are impossible, and an impossible
thing has to be attempted to be shown impossible.
"""
from __future__ import annotations

import json
import os

import asyncpg
import httpx
import pytest
import pytest_asyncio

from datum_sync import auth, connections, crypto
from datum_sync import db as db_module
from datum_sync.api import app

PREFIX = "_pytest-conn"


@pytest.fixture(autouse=True)
def key(monkeypatch):
    """A throwaway key per test.

    Per test, not per session: a key that persists lets a test accidentally
    open a blob another test sealed, which is the very confusion the AAD
    binding exists to prevent.
    """
    monkeypatch.setenv(crypto.KEY_ENV, crypto.generate_key())


@pytest_asyncio.fixture
async def conns(db):
    await db.execute(f"DELETE FROM connections WHERE name LIKE '{PREFIX}%'")
    yield db
    await db.execute(f"DELETE FROM connections WHERE name LIKE '{PREFIX}%'")


async def _make(db, name=f"{PREFIX}-db", **kw):
    kw.setdefault("type_", "database")
    kw.setdefault("config", {"host": "localhost", "port": 5432, "database": "x"})
    kw.setdefault("secret", {"password": "hunter2"})
    return await connections.create(db, name, **kw)


# --------------------------------------------------------------------------
# the secret never comes back
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_read_path_returns_the_secret(conns):
    """Every read goes through _COLUMNS, and _COLUMNS has no `secret` in it.

    Asserted over each read path rather than once, because the guarantee is
    structural only for as long as nobody writes a sixth query with SELECT *.

    Guard: CONN-001.
    """
    await _make(conns)
    row = await connections.get(conns, f"{PREFIX}-db")
    listed = await connections.listing(conns)
    patched = await connections.update(conns, f"{PREFIX}-db", {"description": "x"})

    # Found by name, not `listed[0]`. `listing()` returns every connection in the
    # database ordered by name, and the fixture deletes only its own PREFIX rows,
    # so index 0 is whatever else happens to be registered. A real
    # `hermes-researcher` connection with no secret sorted first and failed the
    # has_secret assertion -- a leak-shaped failure caused by another row entirely.
    from_list = next(r for r in listed if r["name"] == f"{PREFIX}-db")

    for label, r in [("get", row), ("list", from_list), ("patch", patched)]:
        assert "secret" not in r.keys(), label
        assert "hunter2" not in json.dumps(connections.public(r), default=str), label
        assert r["has_secret"] is True, label


@pytest.mark.asyncio
async def test_a_credential_in_config_is_refused(conns):
    """`config` is rendered in the UI, so a password there is a password on screen.

    Guard: CONN-003.
    """
    with pytest.raises(connections.ConnectionStoreError, match="belongs in `secret`"):
        await connections.create(
            conns, f"{PREFIX}-leak", "http",
            config={"base_url": "http://x", "api_key": "sk-live-123"},
        )


@pytest.mark.asyncio
async def test_the_ciphertext_does_not_contain_the_plaintext(conns):
    await _make(conns)
    blob = await conns.fetchval(
        "SELECT secret FROM connections WHERE name = $1", f"{PREFIX}-db"
    )
    assert b"hunter2" not in blob


# --------------------------------------------------------------------------
# sealing
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_secret_sealed_for_one_connection_will_not_open_for_another(conns):
    """The row-swap. This is what the connection name as AAD buys.

    Without it, moving `secret` from one row to another with a single UPDATE
    hands the second connection the first one's credentials, and every read
    still succeeds -- there is nothing in the ciphertext that says which row it
    came from.

    Guard: CONN-002.
    """
    await _make(conns, name=f"{PREFIX}-a")
    await _make(conns, name=f"{PREFIX}-b", secret={"password": "different"})

    await conns.execute(
        f"UPDATE connections SET secret = "
        f"(SELECT secret FROM connections WHERE name = '{PREFIX}-a') "
        f"WHERE name = '{PREFIX}-b'"
    )

    with pytest.raises(crypto.CryptoError):
        await connections.resolve(conns, "Any", "ws", [f"{PREFIX}-b"])


@pytest.mark.asyncio
async def test_a_write_without_a_key_is_refused_rather_than_stored_in_clear(
    conns, monkeypatch
):
    monkeypatch.delenv(crypto.KEY_ENV, raising=False)
    assert crypto.available() is False
    with pytest.raises(crypto.KeyUnavailable):
        await _make(conns)
    assert await connections.get(conns, f"{PREFIX}-db") is None


@pytest.mark.asyncio
async def test_a_connection_with_no_secret_is_allowed(conns):
    """Not every connection has a credential -- a `file` root has none."""
    row = await _make(conns, name=f"{PREFIX}-file", type_="file",
                      config={"root": "/tmp"}, secret=None)
    assert row["has_secret"] is False
    resolved = await connections.resolve(conns, "Any", "ws", [f"{PREFIX}-file"])
    assert resolved[f"{PREFIX}-file"]["root"] == "/tmp"


# --------------------------------------------------------------------------
# scope
# --------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scope,targets,repo,ws,expected",
    [
        ("global", [], "Anything", "at-all", True),
        ("repository", ["SCIMAC"], "SCIMAC", "site_plan", True),
        ("repository", ["SCIMAC"], "Testing", "slow", False),
        ("workspace", ["SCIMAC/site_plan"], "SCIMAC", "site_plan", True),
        # The near miss: right repository, wrong workspace. A prefix match
        # would pass this.
        ("workspace", ["SCIMAC/site_plan"], "SCIMAC", "site_plan_v2", False),
    ],
)
async def test_scope_decides_who_can_resolve(
    conns, scope, targets, repo, ws, expected
):
    # Guard: CONN-004.
    await _make(conns, scope=scope, scope_targets=targets)
    if expected:
        got = await connections.resolve(conns, repo, ws, [f"{PREFIX}-db"])
        assert got[f"{PREFIX}-db"]["password"] == "hunter2"
    else:
        with pytest.raises(connections.ConnectionStoreError, match="scoped"):
            await connections.resolve(conns, repo, ws, [f"{PREFIX}-db"])


@pytest.mark.asyncio
async def test_the_scope_shape_is_refused_before_the_constraint_sees_it(conns):
    with pytest.raises(connections.ConnectionStoreError, match="no scope_targets"):
        await _make(conns, scope="global", scope_targets=["SCIMAC"])
    with pytest.raises(connections.ConnectionStoreError, match="at least one"):
        await _make(conns, scope="repository", scope_targets=[])
    with pytest.raises(connections.ConnectionStoreError, match="Repository/Workspace"):
        await _make(conns, scope="workspace", scope_targets=["SCIMAC"])


@pytest.mark.asyncio
async def test_the_database_refuses_the_same_shapes(conns):
    """The CHECK is not decoration: validate() could be bypassed by any INSERT."""
    with pytest.raises(asyncpg.CheckViolationError):
        await conns.execute(
            "INSERT INTO connections (name, type, scope, scope_targets) "
            f"VALUES ('{PREFIX}-raw', 'http', 'repository', '{{}}')"
        )


@pytest.mark.asyncio
async def test_resolving_a_missing_connection_names_it(conns):
    """Not a silent omission: the workspace would fail later with a bare KeyError."""
    with pytest.raises(connections.ConnectionStoreError, match="no connection named"):
        await connections.resolve(conns, "Any", "ws", ["nope"])


# --------------------------------------------------------------------------
# update
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_patch_that_does_not_mention_config_leaves_it_an_object(conns):
    """asyncpg returns jsonb as JSON *text*; re-encoding it stores a string.

    The same trap that bit schedules.update in step 7, where pausing a schedule
    turned its params into a string and 251 tests could not see it.

    Guard: CONN-005.
    """
    await _make(conns)
    await connections.update(conns, f"{PREFIX}-db", {"description": "one"})
    row = await connections.update(conns, f"{PREFIX}-db", {"description": "two"})
    assert json.loads(row["config"]) == {"host": "localhost", "port": 5432,
                                         "database": "x"}


@pytest.mark.asyncio
async def test_a_patch_that_does_not_mention_the_secret_keeps_it(conns):
    await _make(conns)
    await connections.update(conns, f"{PREFIX}-db", {"tier": 3})
    got = await connections.resolve(conns, "Any", "ws", [f"{PREFIX}-db"])
    assert got[f"{PREFIX}-db"]["password"] == "hunter2"


@pytest.mark.asyncio
async def test_the_secret_can_be_replaced_and_cleared(conns):
    await _make(conns)
    await connections.update(conns, f"{PREFIX}-db", {"secret": {"password": "new"}})
    got = await connections.resolve(conns, "Any", "ws", [f"{PREFIX}-db"])
    assert got[f"{PREFIX}-db"]["password"] == "new"

    row = await connections.update(conns, f"{PREFIX}-db", {"secret": None})
    assert row["has_secret"] is False


@pytest.mark.asyncio
async def test_the_name_is_not_patchable(conns):
    """It is the AAD, so a rename would strand the sealed blob."""
    await _make(conns)
    with pytest.raises(connections.ConnectionStoreError, match="cannot change: name"):
        await connections.update(conns, f"{PREFIX}-db", {"name": "other"})


# --------------------------------------------------------------------------
# test()
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_type_with_no_test_says_so_rather_than_reporting_success(conns):
    """`ok: None`, not `ok: True`. Green because nothing ran is the worst answer."""
    await _make(conns, name=f"{PREFIX}-smtp", type_="email_smtp",
                config={"host": "smtp.example.com"}, secret={"password": "p"})
    assert await connections.test(conns, f"{PREFIX}-smtp") == {
        "ok": None, "error": "no test implemented for type email_smtp"
    }


@pytest.mark.asyncio
async def test_a_file_connection_tests_the_root_and_records_the_outcome(conns):
    await _make(conns, name=f"{PREFIX}-file", type_="file",
                config={"root": "/tmp"}, secret=None)
    assert (await connections.test(conns, f"{PREFIX}-file"))["ok"] is True

    await connections.update(conns, f"{PREFIX}-file",
                             {"config": {"root": "/no/such/place"}})
    result = await connections.test(conns, f"{PREFIX}-file")
    assert result["ok"] is False and "not a directory" in result["error"]

    row = await connections.get(conns, f"{PREFIX}-file")
    assert row["last_test_ok"] is False and row["last_test_at"] is not None


# --------------------------------------------------------------------------
# routes
# --------------------------------------------------------------------------

@pytest_asyncio.fixture
async def client(db, token):
    await db_module.init_pool()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver",
        headers={"authorization": f"Bearer {token}"},
    ) as c:
        yield c
    await db_module.close_pool()


@pytest_asyncio.fixture
async def plain_token(db):
    """Authenticated, not admin."""
    raw = auth.new_token()
    await db.execute("DELETE FROM service_accounts WHERE name = '_pytest_plain'")
    await db.execute(
        "INSERT INTO service_accounts (name, token_hash, max_tier, is_admin) "
        "VALUES ('_pytest_plain', $1, 4, false)",
        auth.hash_token(raw),
    )
    yield raw
    await db.execute("DELETE FROM service_accounts WHERE name = '_pytest_plain'")


BODY = {
    "name": f"{PREFIX}-api",
    "type": "database",
    "config": {"host": "localhost", "database": "x"},
    "secret": {"password": "hunter2"},
    "tier": 2,
    "scope": "repository",
    "scope_targets": ["SCIMAC"],
}


@pytest.mark.asyncio
async def test_the_route_round_trips_without_the_secret(client, conns):
    created = await client.post("/rest/v1/connections", json=BODY)
    assert created.status_code == 201
    assert "hunter2" not in created.text and created.json()["has_secret"] is True

    for url in ("/rest/v1/connections", f"/rest/v1/connections/{PREFIX}-api"):
        r = await client.get(url)
        assert r.status_code == 200 and "hunter2" not in r.text


@pytest.mark.asyncio
async def test_writes_are_admin_only_and_reads_are_not(client, conns, plain_token):
    # Guard: CONN-006, CONN-007.
    await client.post("/rest/v1/connections", json=BODY)
    headers = {"authorization": f"Bearer {plain_token}"}

    assert (await client.get("/rest/v1/connections", headers=headers)).status_code == 200
    for method, url in [
        ("post", "/rest/v1/connections"),
        ("patch", f"/rest/v1/connections/{PREFIX}-api"),
        ("delete", f"/rest/v1/connections/{PREFIX}-api"),
        ("post", f"/rest/v1/connections/{PREFIX}-api/test"),
    ]:
        r = await getattr(client, method)(url, headers=headers, **(
            {"json": BODY} if method in ("post", "patch") else {}))
        assert r.status_code == 403, f"{method} {url} returned {r.status_code}"


@pytest.mark.asyncio
async def test_a_duplicate_name_is_a_409(client, conns):
    await client.post("/rest/v1/connections", json=BODY)
    assert (await client.post("/rest/v1/connections", json=BODY)).status_code == 409


@pytest.mark.asyncio
async def test_a_secret_that_is_not_an_object_is_a_400(client, conns):
    r = await client.post(
        "/rest/v1/connections", json={**BODY, "secret": "just-a-string"}
    )
    assert r.status_code == 400 and "object of credential fields" in r.text


@pytest.mark.asyncio
async def test_storing_a_secret_without_a_key_is_a_503_not_a_500(
    client, conns, monkeypatch
):
    """A configuration gap the operator can act on, said in the response."""
    monkeypatch.delenv(crypto.KEY_ENV, raising=False)
    r = await client.post("/rest/v1/connections", json=BODY)
    assert r.status_code == 503 and crypto.KEY_ENV in r.text
    assert await connections.get(conns, f"{PREFIX}-api") is None


@pytest.mark.asyncio
async def test_a_missing_connection_is_a_404_on_every_route(client, conns):
    for method, url in [
        ("get", "/rest/v1/connections/nope"),
        ("patch", "/rest/v1/connections/nope"),
        ("delete", "/rest/v1/connections/nope"),
        ("post", "/rest/v1/connections/nope/test"),
    ]:
        r = await getattr(client, method)(url, **(
            {"json": {"tier": 2}} if method == "patch" else {}))
        assert r.status_code == 404, f"{method} {url} returned {r.status_code}"
