"""Authentication: credentials in, a `Principal` out.

Two credentials reach this module and both arrive as `Authorization: Bearer
<token>`:

  * a **service account token**, created out of band for scripts and stored on
    `service_accounts.token_hash`;
  * an **access token** minted by the OAuth flow in `oauth.py`, stored in
    `oauth_tokens`, for an MCP client such as Claude.ai.

Both resolve to the same `Principal`, because an OAuth grant is *bound to a
service account* rather than carrying permissions of its own. `max_tier`,
`repo_scope` and `connection_grants` therefore have exactly one home and there
is no second permission model to keep in sync.

Only sha256(token) is ever stored. The raw value is returned once, at mint
time, and cannot be recovered from the database. sha256 is the right primitive
here and argon2 is not: these tokens are 32 random bytes, so there is no
dictionary to attack, and the hash is on the lookup path of every request.
Passwords are the opposite case and use argon2 below.
"""
from __future__ import annotations

import asyncio
import hashlib
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import asyncpg
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from fastapi import Request

from datum_sync import config, db
from datum_sync.errors import ApiError

_hasher = PasswordHasher()

# The MCP server's own URL, and the audience an access token is minted for.
# RFC 8707 calls this the resource. Derived from the configured PUBLIC_URL and
# never from the request's Host header -- see config.PUBLIC_URL.
MCP_RESOURCE = f"{config.PUBLIC_URL}/mcp"

# RFC 9728. A 401 must point the client here so it can discover which
# authorization server to use.
RESOURCE_METADATA_URL = f"{config.PUBLIC_URL}/.well-known/oauth-protected-resource"

WWW_AUTHENTICATE = f'Bearer resource_metadata="{RESOURCE_METADATA_URL}"'


# -- primitives ------------------------------------------------------------


def new_token() -> str:
    """A fresh opaque credential. 32 bytes of entropy, URL-safe."""
    return secrets.token_urlsafe(32)


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def hash_password(raw: str) -> str:
    return _hasher.hash(raw)


def verify_password(stored: str | None, raw: str) -> bool:
    """Constant-ish time password check that never raises.

    A missing hash returns False rather than short-circuiting, so an account
    with no password (the machine accounts) is indistinguishable from a wrong
    password. Otherwise the login form enumerates which names are human.
    """
    if not stored:
        # Still pay the cost of a hash so the timing does not leak the answer.
        _hasher.hash(raw)
        return False
    try:
        return _hasher.verify(stored, raw)
    except VerificationError:
        return False


def canonical_resource(value: str) -> str:
    """Normalise a resource URI for comparison.

    Only the trailing slash and case of the scheme/host are normalised. The
    path is compared exactly: `/mcp` and `/mcp/admin` are different resources
    and treating them as one is how an audience restriction stops restricting.
    """
    value = value.strip().rstrip("/")
    if "://" not in value:
        return value
    scheme, rest = value.split("://", 1)
    host, _, path = rest.partition("/")
    return f"{scheme.lower()}://{host.lower()}" + (f"/{path}" if path else "")


# -- principal -------------------------------------------------------------


@dataclass(frozen=True)
class Principal:
    """Who is calling, and what they are allowed to reach."""

    account_id: int
    name: str
    max_tier: int
    repo_scope: list[str] | None
    connection_grants: list[str] | None
    is_admin: bool
    # 'token' for a service account token, 'oauth' for an OAuth access token.
    source: str
    client_id: str | None = None
    scope: str | None = None

    def allows_repo(self, repo: str) -> bool:
        # NULL scope means every repository (001_core.sql).
        if self.repo_scope is None:
            return True
        return any(_scope_matches(p, repo) for p in self.repo_scope)


def _scope_matches(pattern: str, repo: str) -> bool:
    """`SCIMAC`, `SCIMAC/*` and `*` -- deliberately not fnmatch.

    The spec writes scopes as `SCIMAC/*`, meaning one repository and all of its
    workspaces. Handing that string to fnmatch would also accept `S*` and
    `*C*`, so a typo in an admin form silently widens access instead of
    failing. Two literal forms, nothing else.
    """
    if pattern == "*":
        return True
    if pattern.endswith("/*"):
        pattern = pattern[:-2]
    return pattern == repo


def require_repo(principal: Principal, repo: str) -> None:
    if not principal.allows_repo(repo):
        raise ApiError(
            403,
            "FORBIDDEN",
            f"{principal.name} is not scoped to repository {repo}",
            {"repository": repo},
        )


def require_admin(principal: Principal) -> None:
    if not principal.is_admin:
        raise ApiError(403, "FORBIDDEN", "administrator access required")


# -- resolution ------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _unauthenticated(message: str, code: str = "UNAUTHENTICATED") -> ApiError:
    # RFC 9728: the challenge tells an MCP client where to find the resource
    # metadata, and through it the authorization server. Without this header a
    # fresh Claude.ai connection has nothing to go on but a 401.
    return ApiError(401, code, message, headers={"WWW-Authenticate": WWW_AUTHENTICATE})


def bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization")
    if not header:
        return None
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return None
    return value.strip()


async def resolve(conn: asyncpg.Connection, raw_token: str) -> Principal:
    """Turn a raw bearer credential into a Principal, or raise 401.

    OAuth tokens are checked first: they are the shorter-lived credential and
    the one presented on nearly every MCP request.
    """
    token_hash = hash_token(raw_token)

    row = await conn.fetchrow(
        """
        SELECT t.id, t.client_id, t.scope, t.resource, t.expires_at,
               t.revoked_at, t.rotated_to,
               a.id AS account_id, a.name, a.max_tier, a.repo_scope,
               a.connection_grants, a.is_admin, a.disabled
          FROM oauth_tokens t
          JOIN service_accounts a ON a.id = t.account_id
         WHERE t.token_hash = $1 AND t.kind = 'access'
        """,
        token_hash,
    )
    if row is not None:
        if row["revoked_at"] is not None:
            raise _unauthenticated("token has been revoked", "TOKEN_REVOKED")
        if row["expires_at"] is not None and row["expires_at"] < _now():
            raise _unauthenticated("token has expired", "TOKEN_EXPIRED")
        if row["disabled"]:
            raise _unauthenticated("account is disabled", "ACCOUNT_DISABLED")
        # RFC 8707 audience binding. A token minted for somewhere else must not
        # work here even if it is otherwise valid, or a compromised downstream
        # server can replay its tokens against this one.
        if row["resource"] is not None and canonical_resource(
            row["resource"]
        ) != canonical_resource(MCP_RESOURCE):
            raise _unauthenticated(
                "token was not issued for this resource", "WRONG_AUDIENCE"
            )
        await conn.execute(
            "UPDATE oauth_tokens SET last_used_at = now() WHERE id = $1", row["id"]
        )
        return Principal(
            account_id=row["account_id"],
            name=row["name"],
            max_tier=row["max_tier"],
            repo_scope=row["repo_scope"],
            connection_grants=row["connection_grants"],
            is_admin=row["is_admin"],
            source="oauth",
            client_id=row["client_id"],
            scope=row["scope"],
        )

    row = await conn.fetchrow(
        """
        SELECT id, name, max_tier, repo_scope, connection_grants, is_admin,
               disabled, token_expires
          FROM service_accounts
         WHERE token_hash = $1
        """,
        token_hash,
    )
    if row is None:
        raise _unauthenticated("unknown or invalid token")
    if row["disabled"]:
        raise _unauthenticated("account is disabled", "ACCOUNT_DISABLED")
    if row["token_expires"] is not None and row["token_expires"] < _now():
        raise _unauthenticated("token has expired", "TOKEN_EXPIRED")

    await conn.execute(
        "UPDATE service_accounts SET last_used_at = now() WHERE id = $1", row["id"]
    )
    return Principal(
        account_id=row["id"],
        name=row["name"],
        max_tier=row["max_tier"],
        repo_scope=row["repo_scope"],
        connection_grants=row["connection_grants"],
        is_admin=row["is_admin"],
        source="token",
    )


async def require_auth(request: Request) -> Principal:
    """FastAPI dependency. 401 with a WWW-Authenticate challenge, or a Principal."""
    token = bearer_token(request)
    if token is None:
        raise _unauthenticated("missing bearer token")
    async with db.pool().acquire() as conn:
        return await resolve(conn, token)


class TooManyAttempts(Exception):
    """Raised by `authenticate_password` when a name is locked out.

    Distinct from returning None so the caller can answer 429 instead of
    re-rendering the form as though the password were merely wrong: a client
    that cannot tell those apart keeps retrying and stays locked out.
    """

    def __init__(self, retry_after: int):
        self.retry_after = retry_after
        super().__init__(f"too many failed attempts; retry in {retry_after}s")


# Failure timestamps per submitted account name. In-memory on purpose: there is
# one process, and a restart clearing the counters is not something an attacker
# can reach. Move it to the database when a second process appears and not
# before -- a write on every login attempt buys nothing today.
_attempts: dict[str, list[float]] = {}

# Keys come from the request body, so the caller chooses them. The semaphore
# below bounds how fast failures can be recorded, which bounds this dict in
# normal operation; the cap covers the case where that reasoning is wrong.
_ATTEMPTS_MAX_KEYS = 10_000

_verify_slots = asyncio.Semaphore(config.PASSWORD_MAX_CONCURRENT)


def _locked_for(name: str, now: float) -> int:
    """Seconds until `name` may try again, or 0 if it may try now."""
    window = config.PASSWORD_WINDOW_SECONDS
    recent = [t for t in _attempts.get(name, []) if now - t < window]
    if recent:
        _attempts[name] = recent
    else:
        _attempts.pop(name, None)
    if len(recent) < config.PASSWORD_MAX_ATTEMPTS:
        return 0
    return max(1, int(window - (now - recent[0])))


def _record_failure(name: str, now: float) -> None:
    if name not in _attempts and len(_attempts) >= _ATTEMPTS_MAX_KEYS:
        window = config.PASSWORD_WINDOW_SECONDS
        for key, times in list(_attempts.items()):
            if all(now - t >= window for t in times):
                del _attempts[key]
    _attempts.setdefault(name, []).append(now)


def reset_attempts() -> None:
    """Drop every recorded failure. For tests; nothing in the app calls it."""
    _attempts.clear()


async def authenticate_password(
    conn: asyncpg.Connection, name: str, password: str
) -> asyncpg.Record | None:
    """The consent screen's login. Returns the account row, or None.

    Raises `TooManyAttempts` once a name has failed
    `config.PASSWORD_MAX_ATTEMPTS` times within the window. The lockout is
    checked *before* the database is consulted, so a locked-out unknown name
    behaves exactly like a locked-out real one and the limit does not become an
    account oracle.

    The limit lives here rather than in the route so it cannot be left off a
    second caller. This is the only place a guessable secret is accepted --
    bearer tokens are 32 random bytes, so rate-limiting those would add a
    lockout to abuse without removing an attack that exists.
    """
    now = time.monotonic()
    retry_after = _locked_for(name, now)
    if retry_after:
        raise TooManyAttempts(retry_after)

    # Bounds concurrent argon2 work rather than the attempt rate: the hash runs
    # even for an unknown account, so without this the form is a CPU sink that
    # costs the caller nothing to drive.
    async with _verify_slots:
        row = await conn.fetchrow(
            "SELECT * FROM service_accounts WHERE name = $1", name
        )
        stored = row["password_hash"] if row is not None else None
        # verify_password is called even when the name is unknown, so a wrong
        # name and a wrong password cost the same and the form does not
        # enumerate accounts.
        ok = verify_password(stored, password)

    if not ok or row["disabled"]:
        # A disabled account counts as a failure: it is still a name whose
        # password someone is guessing.
        _record_failure(name, now)
        return None
    _attempts.pop(name, None)
    return row
