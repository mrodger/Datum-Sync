"""Credential proxy tests: access checks, SSRF, auth injection, forwarding."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from datum_sync import audit, connections as conn_mod, crypto, db as db_module
from datum_sync.auth import Principal
from datum_sync.errors import ApiError
from datum_sync.proxy import (
    ALLOWED_METHODS,
    check_proxy_access,
    inject_auth,
    validate_upstream_url,
)

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def pool(db):
    """Ensure the db pool is available for tests calling proxy_request."""
    await db_module.init_pool()
    yield
    await db_module.close_pool()


# -- helpers ---------------------------------------------------------------


def _principal(
    agent_id=1, agent_name="test-agent", proxy_grants=None,
    max_tier=4, name="test-account",
):
    return Principal(
        account_id=1,
        name=name,
        max_tier=max_tier,
        repo_scope=["*"],
        is_admin=False,
        vault_scope=None,
        rate_limit_per_min=None, source="agent",
        agent_id=agent_id,
        agent_name=agent_name,
        proxy_grants=proxy_grants or [],
    )


def _conn_row(tier=1, type_="http"):
    """Minimal dict acting as a connection row for access checks."""
    return {"tier": tier, "type": type_}


# -- SSRF guard ------------------------------------------------------------


def test_ssrf_rejects_loopback():
    # Guard: PROXY-004.
    with pytest.raises(ApiError) as exc:
        validate_upstream_url("http://127.0.0.1/evil")
    assert exc.value.code == "BLOCKED_ADDRESS"


def test_ssrf_rejects_link_local():
    with pytest.raises(ApiError) as exc:
        validate_upstream_url("http://169.254.169.254/metadata")
    assert exc.value.code == "BLOCKED_ADDRESS"


def test_ssrf_rejects_private_10():
    with pytest.raises(ApiError) as exc:
        validate_upstream_url("http://10.0.0.1/internal")
    assert exc.value.code == "BLOCKED_ADDRESS"


def test_ssrf_rejects_ipv6_loopback():
    with pytest.raises(ApiError) as exc:
        validate_upstream_url("http://[::1]/evil")
    assert exc.value.code == "BLOCKED_ADDRESS"


def test_ssrf_rejects_bad_scheme():
    with pytest.raises(ApiError) as exc:
        validate_upstream_url("ftp://files.example.com/data")
    assert exc.value.code == "INVALID_URL"


def test_ssrf_rejects_no_host():
    with pytest.raises(ApiError) as exc:
        validate_upstream_url("http:///path")
    assert exc.value.code == "INVALID_URL"


# -- access checks ---------------------------------------------------------


def test_bare_account_token_denied():
    """Account tokens (no agent_id) cannot proxy.

    Guard: PROXY-001.
    """
    p = Principal(
        account_id=1, name="acct", max_tier=4, repo_scope=["*"],
        is_admin=True, vault_scope=None,
        rate_limit_per_min=None, source="token",
    )
    with pytest.raises(ApiError) as exc:
        check_proxy_access(p, _conn_row(), "openrouter")
    assert exc.value.code == "AGENT_REQUIRED"


def test_agent_without_grant_denied():
    # Guard: PROXY-002.
    p = _principal(proxy_grants=["tavily"])
    with pytest.raises(ApiError) as exc:
        check_proxy_access(p, _conn_row(), "openrouter")
    assert exc.value.code == "CONNECTION_DENIED"


def test_agent_with_grant_passes():
    p = _principal(proxy_grants=["openrouter", "tavily"])
    # Should not raise
    check_proxy_access(p, _conn_row(), "openrouter")


def test_empty_grants_denied():
    """An agent with empty proxy_grants cannot proxy anything."""
    p = _principal(proxy_grants=[])
    with pytest.raises(ApiError) as exc:
        check_proxy_access(p, _conn_row(), "openrouter")
    assert exc.value.code == "CONNECTION_DENIED"


def test_tier_denied():
    # Guard: PROXY-003.
    p = _principal(proxy_grants=["secret-conn"], max_tier=2)
    with pytest.raises(ApiError) as exc:
        check_proxy_access(p, _conn_row(tier=3), "secret-conn")
    assert exc.value.code == "TIER_DENIED"


def test_tier_at_boundary_passes():
    p = _principal(proxy_grants=["conn"], max_tier=3)
    check_proxy_access(p, _conn_row(tier=3), "conn")


# -- auth injection --------------------------------------------------------


def test_inject_bearer():
    headers, params = {}, {}
    inject_auth(
        {"auth_inject": {"type": "bearer", "secret_field": "api_key"}},
        {"api_key": "sk-test"},
        headers, params,
    )
    assert headers["Authorization"] == "Bearer sk-test"
    assert not params


def test_inject_basic():
    headers, params = {}, {}
    inject_auth(
        {"auth_inject": {"type": "basic", "user_field": "user", "pass_field": "pass"}},
        {"user": "admin", "pass": "hunter2"},
        headers, params,
    )
    assert headers["Authorization"].startswith("Basic ")
    import base64
    decoded = base64.b64decode(headers["Authorization"].split(" ", 1)[1]).decode()
    assert decoded == "admin:hunter2"


def test_inject_header():
    headers, params = {}, {}
    inject_auth(
        {"auth_inject": {"type": "header", "secret_field": "api_key", "header": "X-Sub-Token"}},
        {"api_key": "tok-123"},
        headers, params,
    )
    assert headers["X-Sub-Token"] == "tok-123"


def test_inject_query_param():
    headers, params = {}, {}
    inject_auth(
        {"auth_inject": {"type": "query_param", "secret_field": "api_key", "param": "key"}},
        {"api_key": "my-key"},
        headers, params,
    )
    assert params["key"] == "my-key"
    assert not headers


def test_inject_missing_field_raises():
    with pytest.raises(ApiError) as exc:
        inject_auth(
            {"auth_inject": {"type": "bearer", "secret_field": "api_key"}},
            {},  # no api_key
            {}, {},
        )
    assert exc.value.code == "MISSING_SECRET_FIELD"


def test_inject_no_rule_does_nothing():
    headers, params = {"Existing": "val"}, {"q": "1"}
    inject_auth({}, {}, headers, params)
    assert headers == {"Existing": "val"}
    assert params == {"q": "1"}


def test_inject_unknown_type_raises():
    with pytest.raises(ApiError) as exc:
        inject_auth(
            {"auth_inject": {"type": "oauth2_magic"}},
            {},
            {}, {},
        )
    assert exc.value.code == "UNKNOWN_AUTH_INJECT"


# -- auth_inject validation on connections ---------------------------------


def test_auth_inject_validation_rejects_bad_type():
    with pytest.raises(conn_mod.ConnectionStoreError, match="auth_inject.type"):
        conn_mod.validate(
            "http", "global", [], "read", 1,
            {"base_url": "https://api.example.com", "auth_inject": {"type": "magic"}},
        )


def test_auth_inject_validation_rejects_non_http():
    with pytest.raises(conn_mod.ConnectionStoreError, match="only valid for http"):
        conn_mod.validate(
            "database", "global", [], "read", 1,
            {"host": "db.example.com", "database": "main",
             "auth_inject": {"type": "bearer", "secret_field": "x"}},
        )


def test_auth_inject_validation_accepts_valid():
    # Should not raise
    conn_mod.validate(
        "http", "global", [], "read", 1,
        {"base_url": "https://api.example.com",
         "auth_inject": {"type": "bearer", "secret_field": "api_key"}},
    )


# -- wrong connection type -------------------------------------------------


async def test_proxy_rejects_non_http_connection(db, pool):
    """proxy_request refuses database connections.

    Guard: PROXY-005.
    """
    from datum_sync import proxy

    # Create a database connection
    if crypto.available():
        await conn_mod.create(
            db, name="_proxy_db_test", type_="database",
            config={"host": "localhost", "database": "test"},
        )
    else:
        await db.execute(
            """
            INSERT INTO connections (name, type, config)
            VALUES ('_proxy_db_test', 'database',
                    '{"host":"localhost","database":"test"}')
            """
        )

    try:
        p = _principal(proxy_grants=["_proxy_db_test"])
        with pytest.raises(ApiError) as exc:
            await proxy.proxy_request(
                p, "_proxy_db_test", "GET", "/test", audit.Trace.mint(None)
            )
        assert exc.value.code == "WRONG_CONNECTION_TYPE"
    finally:
        await db.execute("DELETE FROM connections WHERE name = '_proxy_db_test'")


# -- method validation -----------------------------------------------------


async def test_proxy_rejects_invalid_method():
    from datum_sync import proxy
    p = _principal(proxy_grants=["conn"])
    with pytest.raises(ApiError) as exc:
        await proxy.proxy_request(p, "conn", "TRACE", "/path", audit.Trace.mint(None))
    assert exc.value.code == "INVALID_METHOD"


# -- audit log -------------------------------------------------------------


async def test_audit_log_written(db):
    """_audit_log writes a row to proxy_log AND one to audit_log.

    Both are asserted. Checking only proxy_log would keep passing against a
    version that had stopped writing the trace-carrying row altogether, which
    is the row the rest of B2 depends on.

    Guard: PROXY-006.
    """
    from datum_sync.proxy import _audit_log

    p = _principal(agent_name="audit-agent")
    trace = audit.Trace.mint("client-supplied")
    await _audit_log(db, p, "test-conn", "GET", "/api/test", 200, trace)

    row = await db.fetchrow(
        "SELECT * FROM proxy_log WHERE agent_name = 'audit-agent' ORDER BY id DESC LIMIT 1"
    )
    assert row is not None
    assert row["connection_name"] == "test-conn"
    assert row["method"] == "GET"
    assert row["upstream_status"] == 200

    audited = await db.fetchrow(
        "SELECT * FROM audit_log WHERE trace_id = $1", trace.id
    )
    assert audited is not None
    assert audited["verb"] == "proxy.request"
    assert audited["actor_kind"] == "agent"
    assert audited["actor_name"] == "audit-agent"
    assert audited["outcome"] == "ok"
    # The client string is kept, and kept apart from the id we minted.
    assert audited["client_trace_id"] == "client-supplied"
    assert str(audited["trace_id"]) != audited["client_trace_id"]
    assert row["account_name"] == "test-account"

    await db.execute("DELETE FROM proxy_log WHERE agent_name = 'audit-agent'")
