"""File intake for FILE parameters.

A FILE parameter's value is an *upload id*, never a path. If it were a path,
any caller could submit `/etc/passwd` to a workspace that returns its input as
an artifact and read it straight back out. The id is a UUID naming a directory
this module created, so there is nothing a caller can put in it that resolves
anywhere else.

    id = await save(filename, stream)      # POST /upload
    path = resolve(id)                     # worker, just before the run
"""
from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Any

from datum_sync import config
from datum_sync.manifest import Manifest, ParameterType

# Anything outside this is replaced, so the stored name cannot contain a
# separator, traverse, or start a flag when a workspace shells out.
_SAFE = re.compile(r"[^A-Za-z0-9._-]")
FALLBACK_NAME = "upload"


class UploadError(Exception):
    """Upload id does not name a stored file."""


def _root() -> Path:
    return config.DATA_PATH / "uploads"


def safe_name(filename: str | None) -> str:
    name = _SAFE.sub("_", Path(filename or "").name).lstrip(".-")
    return name or FALLBACK_NAME


def save(filename: str | None, data: bytes) -> str:
    """Store bytes under a fresh upload id. Returns the id."""
    upload_id = str(uuid.uuid4())
    target = _root() / upload_id
    target.mkdir(parents=True)
    (target / safe_name(filename)).write_bytes(data)
    return upload_id


def resolve(upload_id: str) -> Path:
    """Absolute path of an uploaded file. Raises if the id is not one of ours."""
    try:
        # Rejects traversal, absolute paths and anything else that is not a
        # bare id -- a directory name that is a valid UUID cannot escape _root.
        canonical = str(uuid.UUID(str(upload_id)))
    except (ValueError, AttributeError, TypeError):
        raise UploadError(f"not an upload id: {upload_id!r}") from None

    directory = _root() / canonical
    files = sorted(p for p in directory.glob("*") if p.is_file()) if directory.is_dir() else []
    if not files:
        raise UploadError(f"no such upload: {canonical}")
    return files[0]


def resolve_params(manifest: Manifest, params: dict[str, Any]) -> dict[str, Any]:
    """Swap every FILE parameter's upload id for its absolute path.

    Called by the worker, not at submit time: the job row keeps the id, so a
    resubmit months later still names something meaningful and the stored
    params never leak the server's filesystem layout to a caller reading them
    back off the jobs table.
    """
    file_params = {
        p.name for p in manifest.parameters if p.type is ParameterType.FILE
    }
    return {
        k: (str(resolve(v)) if k in file_params else v) for k, v in params.items()
    }
