"""The database must be the one the migrations describe.

This exists because it was not true. `agents`, `proxy_log` and `mcp_call_log`
were created by hand and never recorded in `schema_migrations`, and the tier
ceiling was widened from 4 to 5 directly against the running database. The
repository said one thing and the server said another for roughly two weeks,
and every existing check passed throughout: the application only ever talks to
the live database, so it never had occasion to notice.

`migrate.py` cannot catch this on its own. Its drift check compares the
checksum of a file against the checksum recorded when that file was applied,
which detects an *edited* migration and nothing else. A change made without a
migration leaves no file to checksum, and an unrecorded migration is
indistinguishable from one that has simply not run yet -- `--status` printed
`[pending]` for two files whose tables already existed.

So the check has to come from the other direction: build a database from the
migrations alone and compare it to the real one, object by object.
"""
from __future__ import annotations

import asyncio
import uuid

import asyncpg
import pytest

from datum_sync import config, migrate

# --- the three queries that define "the same schema" -------------------------
# Compared as sets of tuples rather than as pg_dump text: a textual diff of two
# dumps reports ordering and ownership noise, and the point of failing is to say
# which object differs.

COLUMNS = """
SELECT table_name, column_name, data_type, is_nullable,
       coalesce(column_default, '')
FROM information_schema.columns
WHERE table_schema = 'public'
"""

CONSTRAINTS = """
SELECT c.conrelid::regclass::text, c.contype::text, pg_get_constraintdef(c.oid)
FROM pg_constraint c
JOIN pg_class t ON t.oid = c.conrelid
JOIN pg_namespace n ON n.oid = t.relnamespace
WHERE n.nspname = 'public'
"""

INDEXES = """
SELECT tablename, indexdef
FROM pg_indexes
WHERE schemaname = 'public'
"""


async def _snapshot(conn: asyncpg.Connection) -> dict[str, set[tuple]]:
    return {
        "columns": {tuple(r) for r in await conn.fetch(COLUMNS)},
        "constraints": {tuple(r) for r in await conn.fetch(CONSTRAINTS)},
        "indexes": {tuple(r) for r in await conn.fetch(INDEXES)},
    }


async def _build_from_migrations(scratch_url: str) -> None:
    """Build the scratch database the way a real deployment would.

    This drives `migrate.migrate()` rather than replaying the .sql files
    directly, so the comparison covers the runner as well as the files -- the
    tracking table it creates, and the order it chooses. Replaying the files by
    hand would compare against something no deployment ever produces.
    """
    original = config.DATABASE_URL
    config.DATABASE_URL = scratch_url
    try:
        rc = await migrate.migrate()
        assert rc == 0, "migrate.py failed against a clean database"
    finally:
        config.DATABASE_URL = original


def _describe(kind: str, only_live: set, only_built: set) -> list[str]:
    out = []
    for row in sorted(only_live):
        out.append(f"  {kind}: in the database, NOT in the migrations: {row}")
    for row in sorted(only_built):
        out.append(f"  {kind}: in the migrations, NOT in the database: {row}")
    return out


@pytest.mark.asyncio
async def test_live_schema_matches_migrations():
    """A database built from migrations/ must equal the running database.

    Guard: SCHEMA-001.
    """
    try:
        live = await asyncpg.connect(config.DATABASE_URL, timeout=3)
    except (OSError, asyncpg.PostgresError):
        pytest.skip("database unavailable")

    scratch = f"datumsync_drift_{uuid.uuid4().hex[:8]}"
    admin = await asyncpg.connect(config.DATABASE_URL)
    try:
        try:
            await admin.execute(f'CREATE DATABASE "{scratch}"')
        except asyncpg.InsufficientPrivilegeError:
            pytest.skip("cannot CREATE DATABASE as this role")

        scratch_url = config.DATABASE_URL.rsplit("/", 1)[0] + "/" + scratch
        try:
            await _build_from_migrations(scratch_url)
            built_conn = await asyncpg.connect(scratch_url)
            try:
                built = await _snapshot(built_conn)
                live_snap = await _snapshot(live)
            finally:
                await built_conn.close()
        finally:
            # Terminate stragglers or DROP DATABASE blocks.
            await admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = $1 AND pid <> pg_backend_pid()", scratch)
            await admin.execute(f'DROP DATABASE IF EXISTS "{scratch}"')
    finally:
        await admin.close()
        await live.close()

    problems: list[str] = []
    for kind in ("columns", "constraints", "indexes"):
        only_live = live_snap[kind] - built[kind]
        only_built = built[kind] - live_snap[kind]
        problems.extend(_describe(kind, only_live, only_built))

    assert not problems, (
        "The running database is not the one migrations/ describes.\n"
        "Something was changed without writing a migration, or a migration was\n"
        "applied without being recorded.\n\n" + "\n".join(problems)
    )
