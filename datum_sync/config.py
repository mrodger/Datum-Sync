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

# Lifetimes. Access tokens are deliberately short-lived; the refresh token is
# the durable credential and it rotates on every use.
ACCESS_TOKEN_TTL_SECONDS = int(os.getenv("ACCESS_TOKEN_TTL_SECONDS", "3600"))
REFRESH_TOKEN_TTL_SECONDS = int(
    os.getenv("REFRESH_TOKEN_TTL_SECONDS", str(30 * 86400))
)
AUTH_CODE_TTL_SECONDS = int(os.getenv("AUTH_CODE_TTL_SECONDS", "600"))

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

MIGRATIONS_PATH = REPO_ROOT / "migrations"
