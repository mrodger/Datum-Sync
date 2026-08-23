"""Migration runner.

Applies migrations/*.sql in filename order, once each, inside a transaction.
Records a sha256 of every applied file so that editing a migration that has
already run is reported rather than silently ignored - otherwise the database
and the repo drift apart with nothing to show for it.

    python -m datum_sync.migrate          apply pending migrations
    python -m datum_sync.migrate --status show applied/pending, apply nothing
"""
import asyncio
import hashlib
import sys
from pathlib import Path

import asyncpg

from datum_sync import config

TRACKING_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename   TEXT PRIMARY KEY,
    checksum   TEXT NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


def _discover() -> list[Path]:
    return sorted(config.MIGRATIONS_PATH.glob("*.sql"))


def _checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def _applied(conn: asyncpg.Connection) -> dict[str, str]:
    rows = await conn.fetch("SELECT filename, checksum FROM schema_migrations")
    return {r["filename"]: r["checksum"] for r in rows}


def _check_drift(files: list[Path], applied: dict[str, str]) -> list[str]:
    drift = []
    for path in files:
        prev = applied.get(path.name)
        if prev is not None and prev != _checksum(path):
            drift.append(path.name)
    return drift


async def status() -> int:
    conn = await asyncpg.connect(config.DATABASE_URL)
    try:
        await conn.execute(TRACKING_TABLE)
        applied = await _applied(conn)
        files = _discover()

        for path in files:
            mark = "applied" if path.name in applied else "pending"
            print(f"  [{mark}] {path.name}")

        drift = _check_drift(files, applied)
        for name in drift:
            print(f"  [DRIFT ] {name} - applied copy differs from the file on disk")
        return 1 if drift else 0
    finally:
        await conn.close()


async def migrate() -> int:
    conn = await asyncpg.connect(config.DATABASE_URL)
    try:
        await conn.execute(TRACKING_TABLE)
        applied = await _applied(conn)
        files = _discover()

        drift = _check_drift(files, applied)
        if drift:
            for name in drift:
                print(f"ERROR: {name} was already applied but has since been edited.")
            print("Write a new migration instead of editing an applied one.")
            return 1

        pending = [p for p in files if p.name not in applied]
        if not pending:
            print("No pending migrations.")
            return 0

        for path in pending:
            sql = path.read_text()
            async with conn.transaction():
                await conn.execute(sql)
                await conn.execute(
                    "INSERT INTO schema_migrations (filename, checksum) VALUES ($1, $2)",
                    path.name,
                    _checksum(path),
                )
            print(f"applied {path.name}")

        print(f"{len(pending)} migration(s) applied.")
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    coro = status() if "--status" in sys.argv else migrate()
    sys.exit(asyncio.run(coro))
