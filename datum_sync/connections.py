"""Connections: the scoped credential store.

A connection is two halves that are deliberately never rejoined in the API.

  `config`  identifies it   -- host, port, database, base_url. Returned freely.
  `secret`  authenticates it -- password, token, key. Sealed on write, and
            returned by nothing.

The caller declares the split; this module does not guess it. Guessing would
mean a field named `apikey` slipping through a list that knows `api_key`, and
that failure is silent -- the connection works, so nobody looks. Instead
`_FORBIDDEN_IN_CONFIG` refuses the obvious secret names in the readable half
with a message saying where to put them, and everything else is the caller's
declaration.

`_COLUMNS` has no `secret` in it, and that is the load-bearing detail of this
file. Every read path -- get, listing, create's RETURNING, update's RETURNING --
goes through that one string, so a sealed blob cannot reach a response by
someone forgetting to strip it. The only code that reads the column is
`_sealed()`, called by `resolve()` and `test()`, neither of which returns to an
HTTP client.

Scope is enforced here; tier is enforced elsewhere, and the split is deliberate.
Scope is an addressing rule -- two repositories can each own a "MainDB" and mean
different databases -- so resolution needs it to be correct at all, on every
call. Tier is a permission, and the person it constrains is whoever *published*
the workspace, not whoever submitted the job: a workspace runs with its own
authority, so the credential decision was made once, at publish time. It is
therefore checked once, in `publish.check_connections`, against the publisher's
`max_tier`. Checking it again at resolution would ask the wrong person.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import asyncpg

from datum_sync import crypto

TYPES = ("database", "http", "email_smtp", "email_imap", "file", "oauth_client")
SCOPES = ("global", "repository", "workspace")
_AUTH_INJECT_TYPES = frozenset({"bearer", "basic", "header", "query_param"})
ACCESS = ("read", "write")

# Config keys that are almost certainly credentials. Refused in the readable
# half so the mistake is a 400 at write time rather than a password sitting in
# every list response and every UI table for the life of the connection.
_FORBIDDEN_IN_CONFIG = frozenset({
    "password", "passwd", "pass", "secret", "token", "access_token",
    "refresh_token", "client_secret", "api_key", "apikey", "private_key",
    "passphrase", "credentials",
})

# Config keys `test()` needs to do anything meaningful. A connection can be
# stored without them only if its type has no test.
_REQUIRED_CONFIG = {
    "database": ("host", "database"),
    "http": ("base_url",),
    "email_smtp": ("host",),
    "email_imap": ("host",),
    "file": ("root",),
    "oauth_client": ("token_url", "client_id"),
}


class ConnectionStoreError(Exception):
    """A connection definition that cannot be stored, or cannot be resolved."""


# Never contains `secret`. See the module docstring.
_COLUMNS = """
    id, name, type, tier, scope, scope_targets, access, description, config,
    created_by, created_at, updated_at,
    last_test_at, last_test_ok, last_test_error,
    (secret IS NOT NULL) AS has_secret
"""


def validate(
    type_: str,
    scope: str,
    scope_targets: list[str],
    access: str,
    tier: int,
    config: dict[str, Any],
) -> None:
    """Reject what the database would take but `resolve` or `test` could not use."""
    if type_ not in TYPES:
        raise ConnectionStoreError(f"unknown type {type_!r}; expected one of {', '.join(TYPES)}")
    if scope not in SCOPES:
        raise ConnectionStoreError(f"unknown scope {scope!r}; expected one of {', '.join(SCOPES)}")
    if access not in ACCESS:
        raise ConnectionStoreError(f"unknown access {access!r}; expected one of {', '.join(ACCESS)}")
    if not 1 <= tier <= 4:
        raise ConnectionStoreError(f"tier must be 1-4, got {tier}")

    # Mirrors connections_scope_targets, so the caller gets a message rather
    # than a constraint violation.
    if scope == "global" and scope_targets:
        raise ConnectionStoreError("a global connection takes no scope_targets")
    if scope != "global" and not scope_targets:
        raise ConnectionStoreError(f"a {scope}-scoped connection needs at least one scope_target")
    if scope == "workspace":
        bad = [t for t in scope_targets if t.count("/") != 1]
        if bad:
            raise ConnectionStoreError(
                f"workspace scope_targets are 'Repository/Workspace': {', '.join(bad)}"
            )

    leaked = sorted(set(config) & _FORBIDDEN_IN_CONFIG)
    if leaked:
        raise ConnectionStoreError(
            f"{', '.join(leaked)} belongs in `secret`, not `config` -- `config` "
            f"is returned by the API and rendered in the UI"
        )

    missing = [k for k in _REQUIRED_CONFIG.get(type_, ()) if not config.get(k)]
    if missing:
        raise ConnectionStoreError(
            f"a {type_} connection needs config: {', '.join(missing)}"
        )

    auth_inject = config.get("auth_inject")
    if auth_inject is not None:
        if type_ != "http":
            raise ConnectionStoreError(
                "auth_inject is only valid for http connections"
            )
        if not isinstance(auth_inject, dict):
            raise ConnectionStoreError("auth_inject must be an object")
        inject_type = auth_inject.get("type")
        if inject_type not in _AUTH_INJECT_TYPES:
            raise ConnectionStoreError(
                f"auth_inject.type must be one of "
                f"{', '.join(sorted(_AUTH_INJECT_TYPES))}"
            )


def matches_scope(row: asyncpg.Record, repository: str, workspace: str) -> bool:
    """Whether `repository/workspace` may resolve this connection."""
    if row["scope"] == "global":
        return True
    if row["scope"] == "repository":
        return repository in row["scope_targets"]
    return f"{repository}/{workspace}" in row["scope_targets"]


async def create(
    conn: asyncpg.Connection,
    name: str,
    type_: str,
    config: dict[str, Any] | None = None,
    secret: dict[str, Any] | None = None,
    tier: int = 1,
    scope: str = "global",
    scope_targets: list[str] | None = None,
    access: str = "read",
    description: str | None = None,
    created_by: str | None = None,
) -> asyncpg.Record:
    config = config or {}
    scope_targets = scope_targets or []
    validate(type_, scope, scope_targets, access, tier, config)

    # Sealed before the INSERT, so a missing key fails the request instead of
    # leaving a connection row with no credentials that looks fine in the list.
    sealed = crypto.seal(name, secret) if secret else None

    return await conn.fetchrow(
        f"""
        INSERT INTO connections (name, type, tier, scope, scope_targets, access,
                                 description, config, secret, created_by)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
        RETURNING {_COLUMNS}
        """,
        name, type_, tier, scope, scope_targets, access, description,
        json.dumps(config), sealed, created_by,
    )


async def get(conn: asyncpg.Connection, name: str) -> asyncpg.Record | None:
    return await conn.fetchrow(f"SELECT {_COLUMNS} FROM connections WHERE name = $1", name)


async def listing(conn: asyncpg.Connection) -> list[asyncpg.Record]:
    return await conn.fetch(f"SELECT {_COLUMNS} FROM connections ORDER BY name")


_PATCHABLE = ("type", "tier", "scope", "scope_targets", "access", "description",
              "config", "secret")


async def update(
    conn: asyncpg.Connection, name: str, changes: dict[str, Any]
) -> asyncpg.Record | None:
    """Apply a partial update.

    `secret` is write-only: passing it replaces the sealed blob, passing
    `None` for it clears the blob, and omitting it leaves it untouched. There
    is no way to read the current value back, so it can never be part of a
    read-modify-write.
    """
    unknown = set(changes) - set(_PATCHABLE)
    if unknown:
        raise ConnectionStoreError(f"cannot change: {', '.join(sorted(unknown))}")

    current = await get(conn, name)
    if current is None:
        return None

    merged = {k: current[k] for k in _PATCHABLE if k != "secret"}
    # Decoded before merging: asyncpg hands back jsonb as JSON *text*, and this
    # function writes it out with json.dumps. Carrying the text through
    # unchanged re-encodes it, so a patch that only changes `description`
    # quietly stores a JSON string where the config object was. Same trap that
    # bit schedules.update -- see the note there.
    merged["config"] = json.loads(current["config"])
    merged["scope_targets"] = list(current["scope_targets"])
    merged.update({k: v for k, v in changes.items() if k != "secret"})

    validate(merged["type"], merged["scope"], merged["scope_targets"],
             merged["access"], merged["tier"], merged["config"])

    args = [
        name, merged["type"], merged["tier"], merged["scope"],
        merged["scope_targets"], merged["access"], merged["description"],
        json.dumps(merged["config"]),
    ]
    # The placeholder only exists when there is something to bind to it.
    # asyncpg counts arguments against placeholders, so an unused $9 is an
    # error, not a spare.
    if "secret" in changes:
        sealed_sql = "secret = $9"
        args.append(crypto.seal(name, changes["secret"]) if changes["secret"] else None)
    else:
        sealed_sql = "secret = secret"

    return await conn.fetchrow(
        f"""
        UPDATE connections
           SET type = $2, tier = $3, scope = $4, scope_targets = $5,
               access = $6, description = $7, config = $8, {sealed_sql},
               updated_at = now()
         WHERE name = $1
        RETURNING {_COLUMNS}
        """,
        *args,
    )


async def delete(conn: asyncpg.Connection, name: str) -> bool:
    result = await conn.execute("DELETE FROM connections WHERE name = $1", name)
    return result.endswith(" 1")


async def _sealed(conn: asyncpg.Connection, name: str) -> bytes | None:
    """The only read of the `secret` column. Never called from a response path."""
    return await conn.fetchval("SELECT secret FROM connections WHERE name = $1", name)


async def resolve(
    conn: asyncpg.Connection,
    repository: str,
    workspace: str,
    names: list[str],
) -> dict[str, dict[str, Any]]:
    """Build the `connections` dict a workspace's `run()` receives.

    Raises rather than omitting. A workspace that declared a connection and got
    a dict without it fails later, inside its own code, with a KeyError that
    says nothing about scope -- so the failure has to happen here, where the
    reason is known.
    """
    resolved: dict[str, dict[str, Any]] = {}
    for name in names:
        row = await get(conn, name)
        if row is None:
            raise ConnectionStoreError(f"no connection named {name!r}")
        if not matches_scope(row, repository, workspace):
            raise ConnectionStoreError(
                f"connection {name!r} is {row['scope']}-scoped and does not "
                f"cover {repository}/{workspace}"
            )
        obj = {
            "name": row["name"],
            "type": row["type"],
            "access": row["access"],
            **json.loads(row["config"]),
        }
        blob = await _sealed(conn, name)
        if blob is not None:
            obj.update(crypto.open_(name, blob))
        resolved[name] = obj
    return resolved


# --- test -----------------------------------------------------------------
# Only the types that can actually be reached from here have a test. The others
# report that they have none. A test that returns green because it did nothing
# is worse than no test: it is the same screen as a working connection.

_TESTABLE = ("database", "http", "file")
TEST_TIMEOUT_SECONDS = 10


async def _test_database(cfg: dict[str, Any]) -> None:
    target = await asyncpg.connect(
        host=cfg["host"], port=int(cfg.get("port", 5432)),
        database=cfg["database"], user=cfg.get("user"),
        password=cfg.get("password"), timeout=TEST_TIMEOUT_SECONDS,
    )
    try:
        await target.execute("SELECT 1")
    finally:
        await target.close()


async def _test_http(cfg: dict[str, Any]) -> None:
    import httpx

    headers = dict(cfg.get("headers") or {})
    if cfg.get("token"):
        headers.setdefault("Authorization", f"Bearer {cfg['token']}")
    auth = None
    if cfg.get("username"):
        auth = (cfg["username"], cfg.get("password") or "")

    async with httpx.AsyncClient(timeout=TEST_TIMEOUT_SECONDS) as client:
        r = await client.get(cfg["base_url"], headers=headers, auth=auth)
    # Any response at all proves reachability and credentials; a 404 on a base
    # URL is a routing detail, not a broken connection. 401/403 is not.
    if r.status_code in (401, 403):
        raise ConnectionStoreError(f"authentication rejected: HTTP {r.status_code}")


async def _test_file(cfg: dict[str, Any]) -> None:
    root = Path(cfg["root"])
    if not root.is_dir():
        raise ConnectionStoreError(f"{root} is not a directory")


async def test(conn: asyncpg.Connection, name: str) -> dict[str, Any]:
    """Try the connection for real and record the outcome on the row."""
    row = await get(conn, name)
    if row is None:
        raise ConnectionStoreError(f"no connection named {name!r}")

    if row["type"] not in _TESTABLE:
        return {"ok": None, "error": f"no test implemented for type {row['type']}"}

    cfg = json.loads(row["config"])
    blob = await _sealed(conn, name)
    if blob is not None:
        cfg.update(crypto.open_(name, blob))

    runner = {"database": _test_database, "http": _test_http, "file": _test_file}
    ok, error = True, None
    try:
        await asyncio.wait_for(runner[row["type"]](cfg), TEST_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        ok, error = False, f"timed out after {TEST_TIMEOUT_SECONDS}s"
    except Exception as e:
        # The message can carry a host and a username; it cannot carry the
        # password, because the password is never in an exception's args here.
        ok, error = False, f"{type(e).__name__}: {e}"[:500]

    await conn.execute(
        "UPDATE connections SET last_test_at = now(), last_test_ok = $2, "
        "last_test_error = $3 WHERE name = $1",
        name, ok, error,
    )
    return {"ok": ok, "error": error}


def public(row: asyncpg.Record) -> dict[str, Any]:
    """A connection as the API returns it."""
    return {
        "name": row["name"],
        "type": row["type"],
        "tier": row["tier"],
        "scope": row["scope"],
        "scope_targets": list(row["scope_targets"]),
        "access": row["access"],
        "description": row["description"],
        "config": json.loads(row["config"]),
        "has_secret": row["has_secret"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "last_test_at": row["last_test_at"],
        "last_test_ok": row["last_test_ok"],
        "last_test_error": row["last_test_error"],
    }
