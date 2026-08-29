"""Vault filesystem operations, scope-enforced.

Every call normalises and authorises the path via ``vault.check()`` before
touching disk. The vault root is ``config.VAULT_PATH``; paths inside it are
relative and slash-separated, with no traversal (vault.normalise rejects it).

The enforcement lives here rather than in the caller so there is exactly one
place that builds ``VAULT_PATH / normalised``, and a second caller cannot
forget the check.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from datum_sync import config, vault
from datum_sync.auth import Principal
from datum_sync.errors import ApiError

# Read responses larger than this are truncated with a notice.
MAX_READ_BYTES = 256 * 1024


def _resolve(principal: Principal, action: str, path: str) -> Path:
    """Normalise, authorise, and return the absolute filesystem path.

    Raises ApiError (400 or 403) on failure.
    """
    normalised = vault.check(principal.vault_scope, action, path)
    return config.VAULT_PATH / normalised


async def read(principal: Principal, path: str) -> dict[str, Any]:
    """Read a file from the vault. Returns an MCP content dict."""
    fs_path = _resolve(principal, "read", path)
    if not fs_path.is_file():
        raise ApiError(404, "NOT_FOUND", f"no such file: {path}")
    data = fs_path.read_bytes()
    truncated = len(data) > MAX_READ_BYTES
    text = data[:MAX_READ_BYTES].decode("utf-8", "replace")
    if truncated:
        text += f"\n\n[truncated at {MAX_READ_BYTES} bytes]"
    return {
        "content": [{"type": "text", "text": text}],
        "isError": False,
        "_meta": {"path": path, "size": len(data), "truncated": truncated},
    }


async def write(principal: Principal, path: str, content: str) -> dict[str, Any]:
    """Write a file to the vault. Creates parent directories as needed."""
    fs_path = _resolve(principal, "write", path)
    fs_path.parent.mkdir(parents=True, exist_ok=True)
    fs_path.write_text(content, encoding="utf-8")
    return {
        "content": [{"type": "text", "text": f"wrote {len(content)} bytes to {path}"}],
        "isError": False,
        "_meta": {"path": path, "size": len(content)},
    }


def _scope_covers_dir(scope: dict | None, dir_path: str) -> bool:
    """Could any read pattern in the scope match a file under dir_path?

    This is a prefix check, not a full glob match: ``dev/**`` covers ``dev/``
    and ``dev/sub/``. Without this, listing a directory outside the scope
    would return an empty list rather than 403, leaking that the directory
    exists.
    """
    if not scope:
        return False
    for pattern in scope.get("read") or ():
        # A pattern that starts with the directory path (or is ``**``) could
        # match something inside it.
        if pattern == "**":
            return True
        if pattern.startswith(dir_path + "/") or pattern.startswith(dir_path + "/**"):
            return True
        # A globstar pattern like ``**/foo`` could match inside any dir.
        if pattern.startswith("**/"):
            return True
        # The pattern itself might be a parent: ``dev/**`` covers ``dev/sub/``.
        # Strip the glob suffix and compare the literal prefix.
        literal = pattern.split("*")[0].rstrip("/")
        if dir_path.startswith(literal + "/") or dir_path == literal:
            return True
    return False


async def list_dir(principal: Principal, path: str) -> dict[str, Any]:
    """List a directory in the vault.

    A caller may list a directory if their read scope covers anything inside
    it. Each child entry is individually scope-checked so files outside the
    scope are invisible.
    """
    normalised = vault.normalise(path)
    if not _scope_covers_dir(principal.vault_scope, normalised):
        raise ApiError(
            403, "FORBIDDEN",
            f"this account may not list {normalised}",
            {"path": normalised, "action": "read"},
        )
    fs_path = config.VAULT_PATH / normalised
    if not fs_path.is_dir():
        raise ApiError(404, "NOT_FOUND", f"no such directory: {path}")
    entries: list[dict[str, str]] = []
    for child in sorted(fs_path.iterdir()):
        child_rel = f"{path}/{child.name}"
        if not vault.permits(principal.vault_scope, "read", child_rel):
            continue
        entries.append({
            "name": child.name,
            "type": "directory" if child.is_dir() else "file",
        })
    text = "\n".join(
        f"{'[dir] ' if e['type'] == 'directory' else ''}{e['name']}"
        for e in entries
    ) or "(empty)"
    return {
        "content": [{"type": "text", "text": text}],
        "isError": False,
        "_meta": {"path": path, "count": len(entries)},
    }
