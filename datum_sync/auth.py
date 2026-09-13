"""Authentication: credentials in, a `Principal` out.

Three credentials reach this module. Two arrive as `Authorization: Bearer
<token>`:

  * a **service account token**, created out of band for scripts and stored in
    `account_tokens` -- one row per credential, so an account can hold several
    at once and rotation does not have to break every holder at the instant of
    the write;
  * an **access token** minted by the OAuth flow in `oauth.py`, stored in
    `oauth_tokens`, for an MCP client such as Claude.ai.

The third is a **session**, minted when a person signs in to the web UI and
returned in a cookie, because a browser has no token to present.

All three resolve to the same `Principal`, because an OAuth grant and a session
are both *bound to a service account* rather than carrying permissions of their
own. `max_tier`, `repo_scope` and `vault_scope` therefore have exactly one home
and there is no second permission model to keep in sync.

Only sha256(token) is ever stored. The raw value is returned once, at mint
time, and cannot be recovered from the database. sha256 is the right primitive
here and argon2 is not: these tokens are 32 random bytes, so there is no
dictionary to attack, and the hash is on the lookup path of every request.
Passwords are the opposite case and use argon2 below.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import time
import dataclasses
from dataclasses import dataclass, field
from datetime import datetime, timezone

import asyncpg
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from fastapi import Request

from datum_sync import config, db, grants, tokens, vault
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
    # The repositories this caller holds, always stated. `['*']` is everything
    # and `[]` is nothing; there is no third form meaning "unset", because an
    # unset scope used to mean EVERYTHING and that is the wrong direction for a
    # column nobody filled in (migration 013).
    repo_scope: list[str]
    is_admin: bool
    # 'token' for a service account token, 'oauth' for an OAuth access token.
    source: str
    # Vault paths this caller may reach, by action. See migration 007 for the
    # shape and datum_sync/vault.py for how it is applied. None is no access,
    # the same direction as an empty repo_scope: what was never granted is not
    # held.
    #
    # No default value, deliberately. A default would let a new construction
    # site forget this field and get a principal with no vault access -- which
    # fails closed, raises nothing, and passes every existing test. Requiring it
    # turns that omission into a TypeError at the call site.
    vault_scope: dict | None
    # Requests per minute this caller may make, or None for no limit.
    # `service_accounts.rate_limit_per_min`, which was declared in 001_core.sql
    # and read by nothing until C3.
    #
    # No default value, for the same reason as `vault_scope` above and with the
    # hazard pointing the other way: a construction site that forgets this field
    # would get a principal nothing rate-limits. That failure is silent, passes
    # every test, and is invisible until someone goes looking for the 429 that
    # never came. Requiring it makes the omission a TypeError instead.
    rate_limit_per_min: int | None
    client_id: str | None = None
    scope: str | None = None
    # Agent identity — set when the token belongs to an agent, not the account.
    # proxy_grants is the agent's own list, and is the only per-principal
    # connection grant that anything enforces (proxy.py). `service_accounts`
    # once carried a `connection_grants` column beside it; see the note above
    # `_ACCOUNT_COLS` in tokens.py for why it is no longer read.
    agent_id: int | None = None
    agent_name: str | None = None
    proxy_grants: list[str] | None = None
    # -- spec/agent-auth-plane/03 --------------------------------------------
    # Descriptive fields default; enforced values do not. `kind`, `parent_id`
    # and `state` describe the row and are read for labelling and for the
    # lifecycle checks in `_effective`, which raise before a Principal with a
    # non-active state is ever returned -- so a construction site that leaves
    # them at their defaults gets a principal that behaves as an active human
    # would, which is what every pre-existing construction site meant.
    kind: str = "human"
    parent_id: int | None = None
    # The sponsor's name, filled in by `effective()` from the ancestor walk so
    # the mirror tables (`mcp_call_log`, `proxy_log`) can keep recording the
    # *account* an agent belongs to, as they did before agents were rows.
    parent_name: str | None = None
    state: str = "active"
    # The credential's own ceiling (account_tokens.max_tier), or None. Folded
    # into `effective_tier`, which is the only tier `require_tier` reads.
    token_tier_cap: int | None = None
    limits: dict = field(default_factory=dict)
    federation_scope: dict | None = None

    @property
    def effective_tier(self) -> int:
        """The tier this credential may act at. Never above `max_tier`.

        The minimum of the principal's tier and the credential's cap. A scope
        cap (OAuth, spec 04 §2) folds in here too once it exists. Everything
        that asks "may this caller do X" asks this and not `max_tier`, so a
        baseline token on a tier-4 principal is a tier-2 caller everywhere at
        once rather than at the call sites someone remembered.
        """
        tier = self.max_tier
        if self.token_tier_cap is not None:
            tier = min(tier, self.token_tier_cap)
        cap = scope_tier(self.scope)
        if cap is not None:
            tier = min(tier, cap)
        return tier

    def authority(self) -> dict:
        """The authority tuple, in the shape grants.py works on."""
        return {
            "max_tier": self.max_tier,
            "repo_scope": list(self.repo_scope),
            "vault_scope": self.vault_scope,
            "proxy_grants": list(self.proxy_grants or []),
            "federation_scope": self.federation_scope,
            "limits": dict(self.limits or {}),
            "rate_limit_per_min": self.rate_limit_per_min,
        }

    def all_repos(self) -> bool:
        """Whether this caller's scope is the wildcard.

        Distinct from `allows_repo`, which asks about one name. Three call
        sites need the difference: they skip a query, or refuse an *unfiltered*
        operation, and "holds every repository there is" is not something you
        can conclude by asking about the repositories that happen to exist.
        """
        return "*" in self.repo_scope

    def allows_repo(self, repo: str) -> bool:
        # `*` is handled by _scope_matches, so the wildcard needs no case here.
        return any(_scope_matches(p, repo) for p in self.repo_scope)

    def allows_vault_path(self, action: str, path: str) -> bool:
        """Whether this caller may take `action` on an already-normalised path.

        Thin on purpose: the decision lives in vault.py so it can be tested
        without constructing a principal, and so there is one implementation
        rather than one here and another wherever a job's delegated scope is
        applied.
        """
        return vault.permits(self.vault_scope, action, path)


def principal_json(p: Principal) -> dict:
    """The caller's own identity, as the API reports it.

    Lives here rather than in api.py so that ui.py can return it from sign-in
    without importing the module that imports ui.py.
    """
    return {
        "name": p.name,
        "is_admin": p.is_admin,
        "max_tier": p.max_tier,
        # Always a list. `["*"]` is every repository and `[]` is none; the UI
        # renders the wildcard rather than having to know that a missing value
        # meant the widest possible one.
        "repo_scope": p.repo_scope,
        "vault_scope": p.vault_scope,
        "source": p.source,
        "kind": p.kind,
        "state": p.state,
        "parent_id": p.parent_id,
        "effective_tier": p.effective_tier,
        "token_tier_cap": p.token_tier_cap,
        "scope": p.scope,
        "limits": p.limits,
        "proxy_grants": list(p.proxy_grants or []),
        "federation_scope": p.federation_scope,
        # What a caller below the ceiling could ask for. Computed from the
        # tier table so an agent can tell its operator which scope to request
        # without parsing the grant (spec 03 §8).
        "elevate": elevation_hints(p),
    }


# Scope vocabulary (spec 04 §2). A scope caps the tier; it never raises one.
# `None` for an absent or unknown scope means no cap: an OAuth token minted
# before scopes meant anything must keep working, and refusing unknown scopes
# belongs at the authorize endpoint where the client can be told (WP4).
SCOPE_TIERS = {"mcp": 2, "mcp:operate": 3, "mcp:admin": 4}

# What each tier unlocks, for `elevate` hints and the TIER_REQUIRED detail.
TIER_VERBS = {
    1: "read: whoami, list repositories and workspaces, vault read",
    2: "read jobs, logs and artifacts; job_status and job_result",
    3: "operate: submit jobs, vault write, proxy, schedules, federated writes",
    4: "administer: principals, connections, approvals, publish",
    5: "superuser: tier-5 principals, key rotation, delete",
}


def scope_tier(scope: str | None) -> int | None:
    """The tier ceiling a scope string imposes, or None for no cap."""
    if not scope:
        return None
    tiers = [SCOPE_TIERS[s] for s in scope.split() if s in SCOPE_TIERS]
    return max(tiers) if tiers else None


def elevation_hints(p: Principal) -> dict[str, str]:
    """Scopes that would raise this credential's effective tier, and to what."""
    out = {}
    for scope, tier in SCOPE_TIERS.items():
        if p.effective_tier < tier <= p.max_tier:
            out[scope] = f"unlocks tier {tier}: {TIER_VERBS[tier]}"
    return out


# The caller every request gets when `DATUM_SYNC_AUTH=off`. Synthetic rather
# than a seeded row: `submitted_by` and `created_by` are plain text on every
# table that records who did something, and the only foreign keys onto
# `service_accounts` are sessions and the oauth_* tables -- none of which a
# request can reach with authentication disabled. So this needs no account to
# exist, and cannot leave one behind when the flag goes away.
#
# `source` is not "token" or "oauth" because it is neither, and callers that
# branch on it should see something they do not recognise. It is what /whoami
# reports and what the UI renders its banner from, which is the point: with the
# flag on, the screen says so.
DEV_PRINCIPAL = Principal(
    account_id=0,
    name="auth-disabled",
    max_tier=4,
    repo_scope=["*"],
    is_admin=True,
    # The whole vault, matching the rest of this principal. With the flag on
    # there is no account to scope against and every other permission here is
    # already wide open, so a narrow vault_scope would not be a safety measure
    # -- it would just make the vault tools fail in a way that looks like a bug.
    vault_scope={"read": ["**"], "write": ["**"], "quarantine": ["**"], "promote": ["**"]},
    # Unlimited, matching every other permission here. A limit with the auth
    # flag off would rate-limit the developer who turned authentication off.
    rate_limit_per_min=None,
    source="auth-disabled",
)


def vault_scope_of(row: asyncpg.Record) -> dict | None:
    """The `vault_scope` column as a dict.

    asyncpg has no JSON codec registered on this pool (db.py), so a JSONB column
    arrives as the *string* `{"read": [...]}`. Handing that to vault.permits
    would mean `scope.get` on a str, and the failure would surface far from the
    query that caused it. Decoded in one place so no query site can forget.
    """
    return json_of(row, "vault_scope")


def json_of(row: asyncpg.Record, column: str):
    """A JSONB column as Python, or None. See `vault_scope_of`."""
    try:
        raw = row[column]
    except KeyError:
        return None
    if raw is None:
        return None
    return json.loads(raw) if isinstance(raw, str) else raw


# The columns a Principal is built from, aliased for a `service_accounts sa`
# join. One list so that a permission column added here is added to every
# credential path at once. Defined in tokens.py (imported here) and
# re-exported, because tokens.py cannot import from this module at load time.
PRINCIPAL_COLS = tokens._ACCOUNT_COLS


def principal_from_row(row: asyncpg.Record, source: str, **extra) -> Principal:
    """Build a Principal from a row carrying PRINCIPAL_COLS.

    Not yet effective: the caller passes it through `effective()` so that the
    lifecycle state and the ancestors are applied. Split in two so the tests
    for `grants` can build principals without a database, and so there is one
    place the row-to-field mapping lives.
    """
    kind = row["kind"]
    return Principal(
        account_id=row["account_id"],
        name=row["name"],
        max_tier=row["max_tier"],
        repo_scope=list(row["repo_scope"]),
        is_admin=row["is_admin"],
        vault_scope=json_of(row, "vault_scope"),
        rate_limit_per_min=row["rate_limit_per_min"],
        source=source,
        # An agent is its own principal now; these two fields keep the shape
        # the proxy and the audit writer already read.
        agent_id=row["account_id"] if kind == "agent" else None,
        agent_name=row["name"] if kind == "agent" else None,
        proxy_grants=list(row["proxy_grants"] or []),
        kind=kind,
        parent_id=row["parent_id"],
        state=row["state"],
        limits=json_of(row, "limits") or {},
        federation_scope=json_of(row, "federation_scope"),
        **extra,
    )


_ANCESTORS_SQL = """
    WITH RECURSIVE chain AS (
        SELECT sa.id, sa.parent_id, sa.name, sa.state, sa.max_tier, sa.repo_scope,
               sa.vault_scope, sa.proxy_grants, sa.federation_scope, sa.limits,
               sa.rate_limit_per_min, 0 AS depth
          FROM service_accounts sa WHERE sa.id = $1
        UNION ALL
        SELECT p.id, p.parent_id, p.name, p.state, p.max_tier, p.repo_scope,
               p.vault_scope, p.proxy_grants, p.federation_scope, p.limits,
               p.rate_limit_per_min, chain.depth + 1
          FROM service_accounts p JOIN chain ON p.id = chain.parent_id
         WHERE chain.depth < 16
    )
    SELECT * FROM chain WHERE depth > 0 ORDER BY depth
"""


def _row_authority(row: asyncpg.Record) -> dict:
    return {
        "max_tier": row["max_tier"],
        "repo_scope": list(row["repo_scope"] or []),
        "vault_scope": json_of(row, "vault_scope"),
        "proxy_grants": list(row["proxy_grants"] or []),
        "federation_scope": json_of(row, "federation_scope"),
        "limits": json_of(row, "limits") or {},
        "rate_limit_per_min": row["rate_limit_per_min"],
    }


async def effective(conn: asyncpg.Connection, p: Principal) -> Principal:
    """Apply the lifecycle state and fold in every ancestor (spec 03 §2, §4).

    Raises 401 for a state that does not authenticate, on the principal or on
    any ancestor -- disabling a sponsor disables its agents at their next
    request without a tree walk at write time. Returns a Principal whose
    authority is the meet of its own and its ancestors'. Because `narrows`
    held at every write, the meet is normally the identity.
    """
    state = p.state
    if state in ("pending", "retired", "rejected"):
        raise _unauthenticated(f"principal is {state}", f"PRINCIPAL_{state.upper()}")
    # On every request, for every credential kind (AUTH-022): disabling an
    # account must end sessions and tokens already in flight.
    if state == "disabled":
        raise _unauthenticated("account is disabled", "ACCOUNT_DISABLED")

    authority = p.authority()
    if state == "restricted":
        authority = grants.restricted(authority)

    parent_name = None
    if p.parent_id is not None:
        for anc in await conn.fetch(_ANCESTORS_SQL, p.account_id):
            if parent_name is None:
                parent_name = anc["name"]
            if anc["state"] != "active":
                raise _unauthenticated(
                    f"sponsor {anc['name']!r} is {anc['state']}",
                    f"ANCESTOR_{anc['state'].upper()}",
                )
            authority = grants.intersect(authority, _row_authority(anc))

    return dataclasses.replace(
        p,
        parent_name=parent_name,
        max_tier=authority["max_tier"],
        is_admin=authority["max_tier"] >= 4,
        repo_scope=authority["repo_scope"],
        vault_scope=authority["vault_scope"],
        proxy_grants=authority["proxy_grants"],
        federation_scope=authority["federation_scope"],
        limits=authority["limits"],
        rate_limit_per_min=authority["rate_limit_per_min"],
    )


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


def require_tier(principal: Principal, tier: int, verb: str) -> None:
    """Refuse a verb the caller's effective tier does not reach.

    The one function every verb ceiling goes through (spec 03 §6). The
    detail names the tier required, the tier held, and the scope that would
    close the gap -- which is what an agent's operator needs to read.
    """
    held = principal.effective_tier
    if held >= tier:
        return
    elevate = {s: t for s, t in SCOPE_TIERS.items() if t >= tier and t <= principal.max_tier}
    raise ApiError(
        403,
        "TIER_REQUIRED",
        f"{verb} requires tier {tier}; {principal.name} is acting at tier {held}",
        {
            "required": tier,
            "effective": held,
            "max_tier": principal.max_tier,
            "elevate": min(elevate, key=elevate.get) if elevate else None,
        },
    )


def require_admin(principal: Principal) -> None:
    """Tier 4, by the credential's effective tier rather than the row's flag.

    `is_admin` is derived from `max_tier` (migration 016), so the flag and the
    tier agree on the row; what the flag cannot see is the credential's cap.
    A baseline token on an administrator's principal is not an administrator.
    """
    if principal.effective_tier < 4:
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
        f"""
        SELECT t.id, t.client_id, t.scope, t.resource, t.expires_at,
               t.revoked_at, t.rotated_to,
               {PRINCIPAL_COLS}
          FROM oauth_tokens t
          JOIN service_accounts sa ON sa.id = t.account_id
         WHERE t.token_hash = $1 AND t.kind = 'access'
        """,
        token_hash,
    )
    if row is not None:
        if row["revoked_at"] is not None:
            raise _unauthenticated("token has been revoked", "TOKEN_REVOKED")
        if row["expires_at"] is not None and row["expires_at"] < _now():
            raise _unauthenticated("token has expired", "TOKEN_EXPIRED")
        # The account's state (disabled, pending, retired...) is judged once,
        # in `effective()`, for every credential kind. Not here as well: two
        # copies of the check let one be deleted with the test still green.
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
        return await effective(
            conn,
            principal_from_row(
                row, "oauth", client_id=row["client_id"], scope=row["scope"]
            ),
        )

    # Service account tokens live in `account_tokens`, one row per credential.
    # Agent tokens live there too since migration 017 (label 'agent'): an agent
    # is a principal of kind 'agent' with a parent, and `effective` below is
    # what narrows it to its sponsor -- the agent branch this function used to
    # have, which handed an agent its parent's whole authority, is gone.
    row = await tokens.resolve(conn, token_hash)
    if row is None:
        raise _unauthenticated("unknown or invalid token")
    if row["revoked_at"] is not None:
        raise _unauthenticated("token has been revoked", "TOKEN_REVOKED")
    if row["expires_at"] is not None and row["expires_at"] < _now():
        raise _unauthenticated("token has expired", "TOKEN_EXPIRED")

    await tokens.mark_used(conn, row["token_id"])
    await conn.execute(
        "UPDATE service_accounts SET last_used_at = now() WHERE id = $1",
        row["account_id"],
    )
    return await effective(
        conn,
        principal_from_row(row, "token", token_tier_cap=row["token_tier_cap"]),
    )


class UnknownAccount(Exception):
    """No such account, or it is disabled."""


async def principal_by_name(conn: asyncpg.Connection, name: str) -> Principal:
    """Look an account up by name, with no credential involved.

    Only for callers that have already established their authority some other
    way -- today that is the publish CLI, where the authority is having a shell
    on the server. It is emphatically not an authentication path: it takes a
    name and returns full permissions, so exposing it over HTTP would be a
    login form with the password field removed.

    It lives here rather than in the CLI because it builds a Principal, and the
    column list that populates one should exist once. Written out at the call
    site instead, a new permission column gets added to `resolve` and silently
    missed here, and the CLI publishes with a permission set that quietly
    differs from the same account's over the API.
    """
    row = await conn.fetchrow(
        f"SELECT {PRINCIPAL_COLS} FROM service_accounts sa WHERE sa.name = $1", name
    )
    if row is None:
        raise UnknownAccount(f"no account named {name!r}")
    if row["disabled"]:
        raise UnknownAccount(f"account {name!r} is disabled")
    try:
        return await effective(conn, principal_from_row(row, "local"))
    except ApiError as exc:
        raise UnknownAccount(f"account {name!r}: {exc.message}") from None


# -- browser sessions ------------------------------------------------------
# A third credential, and the only one that is not a bearer token: a person at
# the web UI has nothing to present, so signing in mints an opaque session and
# returns it in a cookie. Stored in oauth_tokens with kind='session' (see
# 003_sessions.sql), which is deliberately a *different* kind from the one
# `resolve` accepts -- the bearer path queries kind='access' and the cookie
# path queries kind='session', so a credential minted for one channel cannot be
# replayed through the other.

SESSION_COOKIE = "datum_session"


def session_cookie_secure() -> bool:
    """Whether to set the Secure flag, from the origin the *browser* sees.

    Derived from PUBLIC_URL rather than from the request scheme: behind the
    tunnel the browser is on HTTPS while this process is handed plain HTTP, so
    asking the request would drop the flag on exactly the deployment that needs
    it.
    """
    return config.PUBLIC_URL.startswith("https://")


async def create_session(conn: asyncpg.Connection, account_id: int) -> str:
    """Mint a session for an account. Returns the raw value, once."""
    raw = new_token()
    await conn.execute(
        """
        INSERT INTO oauth_tokens (token_hash, kind, account_id, expires_at)
        VALUES ($1, 'session', $2, now() + make_interval(secs => $3))
        """,
        hash_token(raw),
        account_id,
        config.SESSION_TTL_SECONDS,
    )
    return raw


async def revoke_session(conn: asyncpg.Connection, raw_token: str) -> None:
    """Sign out. Idempotent, and silent about whether the session existed."""
    await conn.execute(
        """
        UPDATE oauth_tokens SET revoked_at = now()
         WHERE token_hash = $1 AND kind = 'session' AND revoked_at IS NULL
        """,
        hash_token(raw_token),
    )


async def resolve_session(conn: asyncpg.Connection, raw_token: str) -> Principal:
    """Turn a session cookie into a Principal, or raise 401."""
    row = await conn.fetchrow(
        f"""
        SELECT t.id, t.expires_at, t.revoked_at,
               {PRINCIPAL_COLS}
          FROM oauth_tokens t
          JOIN service_accounts sa ON sa.id = t.account_id
         WHERE t.token_hash = $1 AND t.kind = 'session'
        """,
        hash_token(raw_token),
    )
    if row is None:
        raise _unauthenticated("unknown or invalid session")
    if row["revoked_at"] is not None:
        raise _unauthenticated("session has been signed out", "TOKEN_REVOKED")
    if row["expires_at"] is not None and row["expires_at"] < _now():
        raise _unauthenticated("session has expired", "TOKEN_EXPIRED")
    # The account's state is checked on every request, not only at sign-in --
    # in `effective()` below, which every credential kind goes through --
    # because disabling an account has to take effect against sessions already
    # in flight, or the control does nothing for up to SESSION_TTL_SECONDS.
    await conn.execute(
        "UPDATE oauth_tokens SET last_used_at = now() WHERE id = $1", row["id"]
    )
    return await effective(conn, principal_from_row(row, "session"))


async def require_auth(request: Request, allow_cookie: bool = False) -> Principal:
    """FastAPI dependency. 401 with a WWW-Authenticate challenge, or a Principal.

    `allow_cookie` defaults to False so that a caller which has not thought
    about it gets the stricter behaviour. Only the paths the web UI actually
    calls pass True -- see COOKIE_PATHS in api.py for why the service paths
    must not.

    A bearer token wins over a cookie when both are present: the explicit
    credential is the one the caller chose to send for this request, whereas the
    cookie is attached by the browser whether or not it was meant.
    """
    token = bearer_token(request)
    if token is not None:
        async with db.pool().acquire() as conn:
            return await resolve(conn, token)

    if allow_cookie:
        cookie = request.cookies.get(SESSION_COOKIE)
        if cookie:
            async with db.pool().acquire() as conn:
                return await resolve_session(conn, cookie)

    raise _unauthenticated("missing bearer token")


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
