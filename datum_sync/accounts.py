"""Service accounts, from the command line.

    python -m datum_sync.accounts create scimac --scope 'SCIMAC/*'
    python -m datum_sync.accounts token add scimac ci-runner
    python -m datum_sync.accounts token list scimac
    python -m datum_sync.accounts token revoke scimac ci-runner
    python -m datum_sync.accounts passwd marcus
    python -m datum_sync.accounts list

There is no HTTP route that creates an account, and that is deliberate: the
first account has nobody to authenticate it, so a bootstrap endpoint would have
to be either unauthenticated (an open door for however long it stays deployed)
or seeded with a secret that then has to be delivered somehow. A shell on the
box is already the trust boundary.

A raw token is printed exactly once, here. Only sha256(token) is stored, so a
lost token is re-minted rather than recovered.

An account may hold several live tokens at once, each under a label, so a
rotation is `token add` -- deploy it -- `token revoke <old label>`, with a
window in between during which both work. Before `account_tokens` existed
there was one column, and rotation broke every holder at the instant of the
write.
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import sys
from datetime import datetime, timedelta, timezone

import asyncpg

from datum_sync import auth, config, tokens, vault

# The label an account's first token gets. Named rather than left to the
# operator so that `create` cannot produce an unlabelled row, and distinct from
# 'legacy' (migration 012's backfill) so the two are never confused.
INITIAL_LABEL = "initial"


def _when(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M") if value else "-"


def _scopes(values: list[str] | None) -> list[str]:
    """`--scope` repeated, or absent meaning every repository.

    An absent flag still means everything, because that is what this CLI has
    always done and changing it would quietly narrow accounts made by scripts
    that predate migration 013. What changed is that it is now *written down*
    as the wildcard instead of stored as NULL, so the row says what it grants.
    """
    return values or ["*"]


async def _connect() -> asyncpg.Connection:
    return await asyncpg.connect(config.DATABASE_URL)


async def create(
    name: str,
    scopes: list[str] | None,
    max_tier: int,
    admin: bool,
    description: str | None,
    vault_scope: dict | None = None,
) -> int:
    # Validate vault_scope before touching the database.
    if vault_scope is not None:
        try:
            vault.validate_scope(vault_scope)
        except vault.VaultScopeError as exc:
            print(f"invalid vault_scope: {exc}", file=sys.stderr)
            return 1

    conn = await _connect()
    try:
        vault_json = json.dumps(vault_scope) if vault_scope is not None else None
        # One transaction: an account created without its first token is an
        # account nobody can authenticate as, and the operator would have no
        # signal that the second half failed.
        try:
            async with conn.transaction():
                account_id = await conn.fetchval(
                    """
                    INSERT INTO service_accounts
                        (name, description, max_tier, repo_scope, is_admin,
                         vault_scope)
                    VALUES ($1, $2, $3, $4, $5, $6)
                    RETURNING id
                    """,
                    name,
                    description,
                    max_tier,
                    _scopes(scopes),
                    admin,
                    vault_json,
                )
                _, raw = await tokens.create(conn, account_id, INITIAL_LABEL)
        except asyncpg.UniqueViolationError:
            print(f"account {name!r} already exists", file=sys.stderr)
            return 1
    finally:
        await conn.close()

    print(f"created account {name} (id {account_id})")
    print(f"token: {raw}  (label {INITIAL_LABEL})")
    print("This is the only time the token is shown. Store it now.")
    return 0


async def _account_id(conn: asyncpg.Connection, name: str) -> int | None:
    return await conn.fetchval("SELECT id FROM service_accounts WHERE name = $1", name)


async def token_add(name: str, label: str, days: int | None) -> int:
    """Mint an additional token. Existing ones keep working."""
    expires = None
    if days is not None:
        expires = datetime.now(timezone.utc) + timedelta(days=days)

    conn = await _connect()
    try:
        account_id = await _account_id(conn, name)
        if account_id is None:
            print(f"no such account: {name}", file=sys.stderr)
            return 1
        try:
            _, raw = await tokens.create(conn, account_id, label, expires)
        except asyncpg.UniqueViolationError:
            print(
                f"{name} already has a live token labelled {label!r}; "
                f"revoke it first or choose another label",
                file=sys.stderr,
            )
            return 1
    finally:
        await conn.close()

    print(f"token: {raw}")
    print("This is the only time the token is shown. Store it now.")
    print(
        f"Every other live token on {name} still works. "
        f"Revoke the one being replaced with: token revoke {name} <label>"
    )
    return 0


async def token_list(name: str) -> int:
    conn = await _connect()
    try:
        account_id = await _account_id(conn, name)
        if account_id is None:
            print(f"no such account: {name}", file=sys.stderr)
            return 1
        rows = await tokens.list_for_account(conn, account_id, include_revoked=True)
    finally:
        await conn.close()

    if not rows:
        print(f"{name} has no tokens")
        return 0
    print(f"{'LABEL':20} {'CREATED':16} {'EXPIRES':16} {'LAST USED':16} STATE")
    for r in rows:
        state = "revoked" if r["revoked_at"] is not None else "live"
        if state == "live" and r["expires_at"] is not None:
            if r["expires_at"] < datetime.now(timezone.utc):
                state = "expired"
        print(
            f"{r['label']:20} {_when(r['created_at']):16} "
            f"{_when(r['expires_at']):16} {_when(r['last_used_at']):16} {state}"
        )
    return 0


async def token_revoke(name: str, label: str) -> int:
    conn = await _connect()
    try:
        account_id = await _account_id(conn, name)
        if account_id is None:
            print(f"no such account: {name}", file=sys.stderr)
            return 1
        done = await tokens.revoke(conn, account_id, label)
        remaining = await tokens.live_count(conn, account_id) if done else 0
    finally:
        await conn.close()

    if not done:
        print(f"{name} has no live token labelled {label!r}", file=sys.stderr)
        return 1
    print(f"revoked {label} on {name}; it no longer authenticates")
    if remaining == 0:
        # Worth saying out loud: the operator has just locked the account out,
        # and the failure would otherwise show up as an unrelated 401 later.
        print(f"WARNING: {name} now has no live token")
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


async def rate_limit(name: str, per_min: int | None) -> int:
    """Set `rate_limit_per_min`, or clear it with `none`.

    This subcommand is the whole reason the limit is reachable. The column has
    existed since 001_core.sql with no way to set it short of psql, which is
    how it stayed unread for as long as it did -- a control nobody can turn on
    is indistinguishable from one that is not implemented.
    """
    conn = await _connect()
    try:
        updated = await conn.fetchval(
            "UPDATE service_accounts SET rate_limit_per_min = $2 "
            "WHERE name = $1 RETURNING id",
            name,
            per_min,
        )
    finally:
        await conn.close()

    if updated is None:
        print(f"no such account: {name}", file=sys.stderr)
        return 1
    if per_min is None:
        print(f"{name} is no longer rate limited")
    else:
        print(f"{name} is limited to {per_min} requests per minute")
    return 0


async def show() -> int:
    conn = await _connect()
    try:
        rows = await conn.fetch(
            """
            SELECT a.name, a.max_tier, a.repo_scope, a.is_admin, a.disabled,
                   a.rate_limit_per_min,
                   a.password_hash IS NOT NULL AS has_password,
                   -- From account_tokens, never a.token_hash: that column has
                   -- not been the credential since migration 012 and reading
                   -- it here would report on a token nothing accepts.
                   EXISTS (SELECT 1 FROM account_tokens t
                            WHERE t.account_id = a.id AND t.revoked_at IS NULL
                              AND (t.expires_at IS NULL OR t.expires_at > now())
                          ) AS has_token,
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
    print(f"{'NAME':20} {'TIER':>4}  {'SCOPE':24} {'FLAGS':24} {'OAUTH':>5}  LAST USED")
    for r in rows:
        # `*` prints as itself. It used to print as "(all)" because the value
        # was NULL and had to be translated; now the row holds the wildcard the
        # operator would type into --scope, so showing anything else would make
        # the display and the input disagree.
        scope = ",".join(r["repo_scope"]) or "(none)"
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
        # Shown here so the limit is visible where it is set. A control that
        # can be turned on but not read back gets set twice and trusted once.
        if r["rate_limit_per_min"] is not None:
            flags = f"{flags},rate:{r['rate_limit_per_min']}" if flags \
                else f"rate:{r['rate_limit_per_min']}"
        used = r["last_used_at"].strftime("%Y-%m-%d %H:%M") if r["last_used_at"] else "never"
        print(
            f"{r['name']:20} {r['max_tier']:>4}  {scope:24} {flags:24} "
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
    p.add_argument("--max-tier", type=int, default=1, choices=(1, 2, 3, 4, 5))
    p.add_argument("--admin", action="store_true")
    p.add_argument("--description")
    p.add_argument(
        "--vault-scope",
        metavar="JSON",
        help='vault scope as JSON, e.g. \'{"read":["dev/**"],"write":["dev/**"]}\'',
    )

    p = sub.add_parser("token", help="mint, list and revoke account tokens")
    tsub = p.add_subparsers(dest="token_command", required=True)

    t = tsub.add_parser("add", help="mint an additional token; others keep working")
    t.add_argument("name")
    t.add_argument("label", help="operator-facing name, e.g. 'ci-runner'")
    t.add_argument(
        "--expires-days",
        type=int,
        metavar="N",
        help="expire this token after N days. Omit for no expiry.",
    )

    t = tsub.add_parser("list", help="show an account's tokens, revoked included")
    t.add_argument("name")

    t = tsub.add_parser("revoke", help="withdraw one token by label")
    t.add_argument("name")
    t.add_argument("label")

    p = sub.add_parser("passwd", help="set the consent-screen password")
    p.add_argument("name")

    p = sub.add_parser("rate-limit", help="cap this account's requests per minute")
    p.add_argument("name")
    p.add_argument(
        "per_min",
        metavar="N|none",
        help="requests per minute, or 'none' to remove the limit",
    )

    p = sub.add_parser("disable", help="refuse this account's credentials")
    p.add_argument("name")
    p = sub.add_parser("enable", help="undo disable")
    p.add_argument("name")

    sub.add_parser("list", help="show every account")

    args = parser.parse_args()

    if args.command == "create":
        vs = None
        if args.vault_scope:
            try:
                vs = json.loads(args.vault_scope)
            except json.JSONDecodeError as exc:
                print(f"--vault-scope is not valid JSON: {exc}", file=sys.stderr)
                return 1
        return asyncio.run(
            create(args.name, args.scope, args.max_tier, args.admin, args.description, vs)
        )
    if args.command == "token":
        if args.token_command == "add":
            return asyncio.run(token_add(args.name, args.label, args.expires_days))
        if args.token_command == "list":
            return asyncio.run(token_list(args.name))
        return asyncio.run(token_revoke(args.name, args.label))
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
    if args.command == "rate-limit":
        if args.per_min.lower() in ("none", "off"):
            return asyncio.run(rate_limit(args.name, None))
        try:
            n = int(args.per_min)
        except ValueError:
            print(f"not a number: {args.per_min}", file=sys.stderr)
            return 1
        if n < 1:
            # Zero would read as "no limit" to anyone typing it and mean "refuse
            # every request" to the code. Rejected rather than translated: an
            # operator who means unlimited has `none`, and one who means locked
            # out has `disable`.
            print("rate limit must be at least 1; use 'none' to remove it "
                  "or `disable` to lock the account out", file=sys.stderr)
            return 1
        return asyncio.run(rate_limit(args.name, n))
    if args.command in ("disable", "enable"):
        return asyncio.run(disable(args.name, args.command == "disable"))
    return asyncio.run(show())


if __name__ == "__main__":
    sys.exit(main())
