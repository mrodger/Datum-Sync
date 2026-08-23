"""Service accounts, from the command line.

    python -m datum_sync.accounts create scimac --scope 'SCIMAC/*'
    python -m datum_sync.accounts token scimac
    python -m datum_sync.accounts passwd marcus
    python -m datum_sync.accounts list

There is no HTTP route that creates an account, and that is deliberate: the
first account has nobody to authenticate it, so a bootstrap endpoint would have
to be either unauthenticated (an open door for however long it stays deployed)
or seeded with a secret that then has to be delivered somehow. A shell on the
box is already the trust boundary.

A raw token is printed exactly once, here. Only sha256(token) is stored, so a
lost token is re-minted rather than recovered -- `token` on an account that
already has one replaces it, invalidating the old value.
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import sys

import asyncpg

from datum_sync import auth, config


def _scopes(values: list[str] | None) -> list[str] | None:
    """`--scope` repeated, or absent meaning every repository.

    NULL and the empty array mean opposite things in the database -- all repos
    and none -- so an absent flag must not collapse to `[]`.
    """
    return values or None


async def _connect() -> asyncpg.Connection:
    return await asyncpg.connect(config.DATABASE_URL)


async def create(
    name: str, scopes: list[str] | None, max_tier: int, admin: bool, description: str | None
) -> int:
    conn = await _connect()
    try:
        raw = auth.new_token()
        try:
            account_id = await conn.fetchval(
                """
                INSERT INTO service_accounts
                    (name, description, token_hash, max_tier, repo_scope, is_admin)
                VALUES ($1, $2, $3, $4, $5, $6)
                RETURNING id
                """,
                name,
                description,
                auth.hash_token(raw),
                max_tier,
                _scopes(scopes),
                admin,
            )
        except asyncpg.UniqueViolationError:
            print(f"account {name!r} already exists", file=sys.stderr)
            return 1
    finally:
        await conn.close()

    print(f"created account {name} (id {account_id})")
    print(f"token: {raw}")
    print("This is the only time the token is shown. Store it now.")
    return 0


async def mint(name: str) -> int:
    """Replace an account's token. The previous one stops working."""
    conn = await _connect()
    try:
        raw = auth.new_token()
        updated = await conn.fetchval(
            """
            UPDATE service_accounts SET token_hash = $2, token_expires = NULL
             WHERE name = $1
            RETURNING id
            """,
            name,
            auth.hash_token(raw),
        )
    finally:
        await conn.close()

    if updated is None:
        print(f"no such account: {name}", file=sys.stderr)
        return 1
    print(f"token: {raw}")
    print("The previous token for this account no longer works.")
    return 0


async def passwd(name: str, password: str) -> int:
    """Set the password used by the OAuth consent screen.

    Accounts without one cannot sign in there, which is the right default for
    the machine accounts: a token is their credential and a password would be a
    second way in that nobody is watching.
    """
    conn = await _connect()
    try:
        updated = await conn.fetchval(
            "UPDATE service_accounts SET password_hash = $2 WHERE name = $1 RETURNING id",
            name,
            auth.hash_password(password),
        )
    finally:
        await conn.close()

    if updated is None:
        print(f"no such account: {name}", file=sys.stderr)
        return 1
    print(f"password set for {name}")
    return 0


async def disable(name: str, value: bool) -> int:
    conn = await _connect()
    try:
        updated = await conn.fetchval(
            "UPDATE service_accounts SET disabled = $2 WHERE name = $1 RETURNING id",
            name,
            value,
        )
    finally:
        await conn.close()

    if updated is None:
        print(f"no such account: {name}", file=sys.stderr)
        return 1
    print(f"{name} is now {'disabled' if value else 'enabled'}")
    return 0


async def show() -> int:
    conn = await _connect()
    try:
        rows = await conn.fetch(
            """
            SELECT a.name, a.max_tier, a.repo_scope, a.is_admin, a.disabled,
                   a.password_hash IS NOT NULL AS has_password,
                   a.token_hash IS NOT NULL AS has_token,
                   a.last_used_at,
                   (SELECT count(*) FROM oauth_tokens t
                     WHERE t.account_id = a.id AND t.kind = 'access'
                       AND t.revoked_at IS NULL AND t.expires_at > now()) AS live_tokens
              FROM service_accounts a
             ORDER BY a.name
            """
        )
    finally:
        await conn.close()

    if not rows:
        print("no accounts")
        return 0
    print(f"{'NAME':20} {'TIER':>4}  {'SCOPE':24} {'FLAGS':16} {'OAUTH':>5}  LAST USED")
    for r in rows:
        scope = ",".join(r["repo_scope"]) if r["repo_scope"] is not None else "(all)"
        flags = ",".join(
            f
            for f, on in (
                ("admin", r["is_admin"]),
                ("disabled", r["disabled"]),
                ("pw", r["has_password"]),
                ("token", r["has_token"]),
            )
            if on
        )
        used = r["last_used_at"].strftime("%Y-%m-%d %H:%M") if r["last_used_at"] else "never"
        print(
            f"{r['name']:20} {r['max_tier']:>4}  {scope:24} {flags:16} "
            f"{r['live_tokens']:>5}  {used}"
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="datum_sync.accounts")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("create", help="create an account and print its token")
    p.add_argument("name")
    p.add_argument(
        "--scope",
        action="append",
        metavar="REPO",
        help="repository this account may reach, e.g. 'SCIMAC' or 'SCIMAC/*'; "
        "repeatable. Omit for every repository.",
    )
    p.add_argument("--max-tier", type=int, default=1, choices=(1, 2, 3, 4))
    p.add_argument("--admin", action="store_true")
    p.add_argument("--description")

    p = sub.add_parser("token", help="replace an account's token")
    p.add_argument("name")

    p = sub.add_parser("passwd", help="set the consent-screen password")
    p.add_argument("name")

    p = sub.add_parser("disable", help="refuse this account's credentials")
    p.add_argument("name")
    p = sub.add_parser("enable", help="undo disable")
    p.add_argument("name")

    sub.add_parser("list", help="show every account")

    args = parser.parse_args()

    if args.command == "create":
        return asyncio.run(
            create(args.name, args.scope, args.max_tier, args.admin, args.description)
        )
    if args.command == "token":
        return asyncio.run(mint(args.name))
    if args.command == "passwd":
        # Prompted, never an argument: a password on the command line is in the
        # shell history and in every `ps` listing on the box.
        password = getpass.getpass("password: ")
        if password != getpass.getpass("again: "):
            print("passwords do not match", file=sys.stderr)
            return 1
        if not password:
            print("refusing to set an empty password", file=sys.stderr)
            return 1
        return asyncio.run(passwd(args.name, password))
    if args.command in ("disable", "enable"):
        return asyncio.run(disable(args.name, args.command == "disable"))
    return asyncio.run(show())


if __name__ == "__main__":
    sys.exit(main())
