"""Environment configuration. Loaded once at import."""
import os
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent

load_dotenv(REPO_ROOT / ".env")


def _path(env_name: str, default: str) -> Path:
    raw = os.getenv(env_name, default)
    p = Path(raw)
    return p if p.is_absolute() else (REPO_ROOT / p).resolve()


DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://datumsync:datumsync_local@localhost:5435/datumsync",
)

HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8200"))

# The externally reachable origin. Used to build the OAuth discovery documents
# and the token audience, so it must be configured rather than derived from the
# request's Host header: an attacker able to set Host would otherwise steer
# discovery -- and with it the audience a token is minted for -- at a server of
# their choosing. No trailing slash.
#
# The fallback keeps the type simple -- nothing downstream has to handle None in
# an f-string -- but it is not a usable default for a deployed server, so
# `require_public_url()` refuses to start without an explicit value. A wrong
# PUBLIC_URL is the worst kind of misconfiguration here: discovery still serves,
# tokens are still minted, and the failure surfaces at the client as an opaque
# authorization error with nothing in the log pointing back at this setting.
_PUBLIC_URL_ENV = os.getenv("PUBLIC_URL")
PUBLIC_URL_CONFIGURED = _PUBLIC_URL_ENV is not None
PUBLIC_URL = (_PUBLIC_URL_ENV or f"http://localhost:{PORT}").rstrip("/")


def require_public_url() -> str:
    """Return PUBLIC_URL, or raise if it was never configured.

    Called from the app lifespan rather than at import, so that importing the
    app -- which the tests do, via a transport that runs no lifespan -- does not
    demand deployment configuration. The check belongs to serving, not to the
    app object.

    Note that `.env` cannot fix an already-exported PUBLIC_URL: python-dotenv
    does not override the environment. Measured, not assumed -- with PORT=8200
    in .env and PORT=8190 exported, this module reads 8190.
    """
    if not PUBLIC_URL_CONFIGURED:
        raise RuntimeError(
            "PUBLIC_URL is not set. It is the OAuth issuer, the discovery "
            "document's advertised origin and the access-token audience, so a "
            "guessed default would mint tokens no client can use. Set it to "
            "the origin clients actually reach, e.g. "
            "PUBLIC_URL=https://sync.example.com (or "
            f"PUBLIC_URL=http://localhost:{PORT} for local work)."
        )
    return PUBLIC_URL

# Development only: serve every route as a fixed administrator, with no
# credential of any kind. Off unless the environment says the exact string
# "off", so a typo, an empty value or an unset variable all leave auth on --
# the one direction a mistake is allowed to go.
#
# The danger is not that this exists, it is that it survives a copied .env into
# somewhere that matters, so `require_safe_auth()` refuses to serve unless the
# socket is also bound to loopback. HOST defaults to 0.0.0.0, which means the
# defaults alone cannot produce a reachable unauthenticated server: turning this
# on costs you a second, deliberate setting.
def _auth_disabled(raw: str | None) -> bool:
    """True only for the exact word "off".

    A named function rather than an inline comparison so the rule can be tested
    without reimporting the module. It matters that it is this strict: read as
    a general truthiness test, "false", "0" and "no" would all *disable*
    authentication, which is the reverse of what anyone setting them intends.
    """
    return (raw or "").strip().lower() == "off"


AUTH_DISABLED = _auth_disabled(os.getenv("DATUM_SYNC_AUTH"))

# Addresses that reach no further than this machine. `localhost` is included on
# the assumption it resolves to one of the other two; that is true here and on
# every platform we target, and getting it wrong fails closed (refusing to
# start) rather than open.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def is_loopback_client(client) -> bool:
    """Did this request arrive from the machine the server runs on?

    `client` is Starlette's `request.client` -- the peer address of the socket,
    not a header, so it cannot be forged by the caller the way X-Forwarded-For
    can. None means no peer address is available (an in-process test transport),
    which is treated as local: it is not a network connection at all.

    This is the enforcing half of the auth-off guard, and `require_safe_auth()`
    is only the early warning. The startup check reads HOST, but `uvicorn
    --host 0.0.0.0` binds the socket without consulting it, so a check made once
    at startup can be walked around and one made per connection cannot.
    """
    if client is None:
        return True
    host = client.host
    # IPv4-mapped IPv6, which is what a dual-stack listener reports for an IPv4
    # loopback connection.
    if host.startswith("::ffff:"):
        host = host[len("::ffff:"):]
    # The whole 127.0.0.0/8 block is loopback, not just 127.0.0.1.
    return host in _LOOPBACK_HOSTS or host.startswith("127.")


def require_safe_auth() -> None:
    """Refuse at startup to serve an unauthenticated server others can reach.

    Called from the app lifespan for the same reason as `require_public_url()`:
    importing the app must not demand deployment configuration, and this is a
    property of *serving*, not of the app object. The consequence is that the
    test suite cannot reach it -- `ASGITransport` runs no lifespan -- so the
    check is also exercised by really launching.

    This refuses early and loudly on the ordinary start path. It is not the
    thing standing between an auth-off server and the network: that is
    `is_loopback_client()`, which runs per request. This one can be bypassed by
    passing --host to uvicorn directly; that one cannot.
    """
    if not AUTH_DISABLED:
        return
    if HOST not in _LOOPBACK_HOSTS:
        raise RuntimeError(
            f"DATUM_SYNC_AUTH=off with HOST={HOST!r} would publish every route, "
            "including the connection store and the admin API, to anything that "
            "can reach this machine. Bind it to the machine it is being tested "
            "on (HOST=127.0.0.1) or turn authentication back on."
        )


# Lifetimes. Access tokens are deliberately short-lived; the refresh token is
# the durable credential and it rotates on every use.
ACCESS_TOKEN_TTL_SECONDS = int(os.getenv("ACCESS_TOKEN_TTL_SECONDS", "3600"))
REFRESH_TOKEN_TTL_SECONDS = int(
    os.getenv("REFRESH_TOKEN_TTL_SECONDS", str(30 * 86400))
)
AUTH_CODE_TTL_SECONDS = int(os.getenv("AUTH_CODE_TTL_SECONDS", "600"))

# A web UI session. Longer than an access token because there is no refresh
# mechanism behind it -- expiry means signing in again -- and shorter than the
# refresh token because a browser is a shared and easily-walked-away-from
# device. One working day.
SESSION_TTL_SECONDS = int(os.getenv("SESSION_TTL_SECONDS", str(12 * 3600)))

# Consent-screen password attempts. Keyed by the submitted account name, not by
# client address: behind a tunnel or reverse proxy every request arrives from
# one address, so an address-keyed limit either locks out all clients at once or
# does nothing, and trusting X-Forwarded-For instead would let the caller pick
# its own key and opt out of the limit entirely.
PASSWORD_MAX_ATTEMPTS = int(os.getenv("PASSWORD_MAX_ATTEMPTS", "5"))
PASSWORD_WINDOW_SECONDS = int(os.getenv("PASSWORD_WINDOW_SECONDS", "900"))

# Argon2 is deliberately expensive, which makes the login form a cheap way to
# burn CPU -- more so because an unknown account still pays for a full hash, to
# avoid leaking which names exist. A concurrency bound rather than a rate limit:
# the aim is that the rest of the server stays responsive, not that a legitimate
# login is refused.
PASSWORD_MAX_CONCURRENT = int(os.getenv("PASSWORD_MAX_CONCURRENT", "4"))

# OAuth 2.1 requires HTTPS for the authorization endpoints and for any redirect
# URI that is not loopback. Relaxed only for local development.
REQUIRE_HTTPS = os.getenv("REQUIRE_HTTPS", "false").lower() in ("1", "true", "yes")

REPOSITORIES_PATH = _path("REPOSITORIES_PATH", "./repositories")
RESOURCES_PATH = _path("RESOURCES_PATH", "./resources")
DATA_PATH = _path("DATA_PATH", "./data")
VAULT_PATH = _path("VAULT_PATH", "./vault")

MIGRATIONS_PATH = REPO_ROOT / "migrations"


def job_dir(job_id) -> Path:
    """Where a job's artifacts live.

    Here rather than in the modules that use it, because three of them do -- the
    worker writes it, execute.py resolves files out of it, services.py records a
    directory inside it as a served root -- and until this existed each spelled
    the layout out again. A layout written in three places is one that gets
    changed in two.

    It also removes an import cycle with no other reason to exist: services.py
    wanted one path helper from execute.py, which imports the worker, which
    imports services.
    """
    return DATA_PATH / "jobs" / str(job_id)
