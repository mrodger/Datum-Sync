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
PUBLIC_URL = os.getenv("PUBLIC_URL", f"http://localhost:{PORT}").rstrip("/")

# Lifetimes. Access tokens are deliberately short-lived; the refresh token is
# the durable credential and it rotates on every use.
ACCESS_TOKEN_TTL_SECONDS = int(os.getenv("ACCESS_TOKEN_TTL_SECONDS", "3600"))
REFRESH_TOKEN_TTL_SECONDS = int(
    os.getenv("REFRESH_TOKEN_TTL_SECONDS", str(30 * 86400))
)
AUTH_CODE_TTL_SECONDS = int(os.getenv("AUTH_CODE_TTL_SECONDS", "600"))

# OAuth 2.1 requires HTTPS for the authorization endpoints and for any redirect
# URI that is not loopback. Relaxed only for local development.
REQUIRE_HTTPS = os.getenv("REQUIRE_HTTPS", "false").lower() in ("1", "true", "yes")

REPOSITORIES_PATH = _path("REPOSITORIES_PATH", "./repositories")
RESOURCES_PATH = _path("RESOURCES_PATH", "./resources")
DATA_PATH = _path("DATA_PATH", "./data")

MIGRATIONS_PATH = REPO_ROOT / "migrations"
