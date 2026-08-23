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

REPOSITORIES_PATH = _path("REPOSITORIES_PATH", "./repositories")
RESOURCES_PATH = _path("RESOURCES_PATH", "./resources")
DATA_PATH = _path("DATA_PATH", "./data")

MIGRATIONS_PATH = REPO_ROOT / "migrations"
