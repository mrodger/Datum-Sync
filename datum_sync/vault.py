"""Vault paths: normalising them, and deciding whether a scope permits one.

Deliberately pure. Nothing here touches the filesystem or the database, because
the interesting failures are all in the *decision* -- a pattern that matches one
segment too many, a traversal that survives normalisation -- and a decision that
needs a mounted vault to test is a decision that gets tested once.

Two rules the rest of the system depends on:

  * `deny` wins. It is checked before any allow, and a match ends the question.
  * An allow is literal. `write` on a path does not imply `read` on it here,
    even though a scope granting one without the other is incoherent. That
    incoherence is caught at *write* time by validate_scope(), and inferring
    it here as well would mean the validation could be deleted with every test
    still passing.

Glob semantics are the ones an operator writing `dev/**` expects, which are not
the ones `fnmatch` implements: `fnmatch` translates `*` to `.*`, so the pattern
`*` matches `secrets/key`. Here `*` stops at a separator and only `**` crosses
one.
"""
from __future__ import annotations

import re
from functools import lru_cache

from datum_sync.errors import ApiError

# The keys a scope may carry. `deny` is listed but is never an `action` argument
# -- nothing asks "may I deny this path".
ACTIONS = ("read", "write", "quarantine", "promote")
SCOPE_KEYS = ACTIONS + ("deny",)

# A vault path is relative, slash-separated and has no traversal in it. The
# limit is not a security control -- normalise rejects traversal outright -- but
# an unbounded path is a pointless thing to hand to a filesystem call.
MAX_PATH = 1024


class VaultPathError(ValueError):
    """The path is not a well-formed vault path, whatever the scope says."""


@lru_cache(maxsize=512)
def _compiled(pattern: str) -> re.Pattern[str]:
    """A glob pattern as a regex, with `*` confined to one path segment.

    `**/` consumes whole directories including none of them, so `dev/**/x`
    matches `dev/x` as well as `dev/a/b/x`. Writing it as `.*` instead would
    require the separator to be there and quietly fail the zero-directory case.
    """
    out: list[str] = []
    i, n = 0, len(pattern)
    while i < n:
        if pattern.startswith("**/", i):
            out.append("(?:[^/]+/)*")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def matches(pattern: str, path: str) -> bool:
    return _compiled(pattern).match(path) is not None


def normalise(path: str) -> str:
    """A vault-relative path, or raise.

    Traversal is rejected rather than resolved. Collapsing `a/../b` to `b` would
    accept a path whose *written* form escapes the scope it was checked against,
    so the two would have to be checked in the right order forever after. A path
    that needs resolving is refused instead.
    """
    if not isinstance(path, str):
        raise VaultPathError("path must be a string")
    path = path.strip()
    if not path:
        raise VaultPathError("path is empty")
    if len(path) > MAX_PATH:
        raise VaultPathError(f"path is longer than {MAX_PATH} characters")
    if "\x00" in path:
        raise VaultPathError("path contains a null byte")
    if "\\" in path:
        # Not a separator here. Allowing it would mean two spellings of the same
        # path, only one of which the patterns are written against.
        raise VaultPathError("path contains a backslash")
    if path.startswith("/"):
        raise VaultPathError("path must be relative to the vault root")

    path = path.rstrip("/")
    segments = path.split("/")
    for segment in segments:
        # Segment-wise, not substring: `notes..md` is a legal filename and only
        # a whole segment of `..` is traversal.
        if segment == "..":
            raise VaultPathError("path contains a parent-directory segment")
        if segment == ".":
            raise VaultPathError("path contains a current-directory segment")
        if segment == "":
            raise VaultPathError("path contains an empty segment")
    return "/".join(segments)


def permits(scope: dict | None, action: str, path: str) -> bool:
    """Does this scope allow `action` on this already-normalised path?

    A NULL scope is no access. That is the default every account carries after
    migration 007, so the failure mode of forgetting to grant a scope is a
    refusal rather than an open vault.
    """
    if action not in ACTIONS:
        raise ValueError(f"unknown vault action {action!r}")
    if not scope:
        return False
    if any(matches(p, path) for p in scope.get("deny") or ()):
        return False
    return any(matches(p, path) for p in scope.get(action) or ())


# -- scope validation (account write time) ----------------------------------


class VaultScopeError(ValueError):
    """The scope document itself is malformed, independent of any path."""


def validate_scope(scope: dict) -> None:
    """Reject a vault_scope document that violates structural constraints.

    Called at account create/update — before the scope reaches the database.
    Four rules:

      1. **write-implies-read**: every path in `write` must also appear in
         `read`. A write-only grant makes no operational sense (you can't
         verify what you wrote) and would silently fail at the first read
         check.
      2. **deny-disjoint-from-allow**: a deny pattern that is identical to an
         allow pattern is contradictory — deny always wins, so the allow is
         dead. This is almost certainly a copy-paste error.
      3. **no traversal**: patterns must not contain `..` segments.
      4. **star-confinement**: `*` is valid as a whole segment or as `**`.
         Patterns like `*foo` or `f*o` are refused because the glob engine
         treats them correctly but operators never expect `[^/]*foo` semantics.

    Raises VaultScopeError on the first violation found.
    """
    if not isinstance(scope, dict):
        raise VaultScopeError("vault_scope must be an object")

    unknown = set(scope) - set(SCOPE_KEYS)
    if unknown:
        raise VaultScopeError(f"unknown keys in vault_scope: {', '.join(sorted(unknown))}")

    # Collect all patterns for cross-key checks.
    all_patterns: dict[str, list[str]] = {}
    for key in SCOPE_KEYS:
        patterns = scope.get(key)
        if patterns is None:
            continue
        if not isinstance(patterns, list):
            raise VaultScopeError(f"vault_scope.{key} must be a list of patterns")
        for p in patterns:
            if not isinstance(p, str) or not p:
                raise VaultScopeError(f"vault_scope.{key} contains a non-string or empty pattern")
            _validate_pattern(key, p)
        all_patterns[key] = patterns

    # Rule 1: write-implies-read.
    write_set = set(all_patterns.get("write", []))
    read_set = set(all_patterns.get("read", []))
    missing = write_set - read_set
    if missing:
        raise VaultScopeError(
            f"write patterns not in read (write-implies-read): "
            f"{', '.join(sorted(missing))}"
        )

    # Rule 2: deny-disjoint-from-allow.
    deny_set = set(all_patterns.get("deny", []))
    for action in ACTIONS:
        overlap = deny_set & set(all_patterns.get(action, []))
        if overlap:
            raise VaultScopeError(
                f"deny patterns also in {action} (contradictory): "
                f"{', '.join(sorted(overlap))}"
            )


def _validate_pattern(key: str, pattern: str) -> None:
    """Per-pattern structural checks (rules 3 and 4)."""
    segments = pattern.split("/")
    for seg in segments:
        # Rule 3: no traversal.
        if seg == "..":
            raise VaultScopeError(
                f"vault_scope.{key}: pattern {pattern!r} contains a traversal segment"
            )
        if seg == ".":
            raise VaultScopeError(
                f"vault_scope.{key}: pattern {pattern!r} contains a current-directory segment"
            )
        # Rule 4: star-confinement.
        # Allowed: `*` (whole segment), `**` (globstar).
        # Refused: `*foo`, `foo*`, `f*o`, `***`, etc.
        if "*" in seg and seg not in ("*", "**"):
            raise VaultScopeError(
                f"vault_scope.{key}: pattern {pattern!r} has a partial wildcard "
                f"in segment {seg!r} — use * (one segment) or ** (any depth)"
            )


def check(scope: dict | None, action: str, path: str) -> str:
    """Normalise and authorise in one call, or raise the API's error shape.

    Returns 403 and never 404. Answering "no such file" for a path outside the
    scope would let a caller map the vault by asking about paths it cannot
    reach, and the caller has no business knowing either way.
    """
    try:
        normalised = normalise(path)
    except VaultPathError as exc:
        raise ApiError(400, "INVALID_PARAMETER", str(exc), {"path": path}) from exc
    if not permits(scope, action, normalised):
        raise ApiError(
            403,
            "FORBIDDEN",
            f"this account may not {action} {normalised}",
            {"path": normalised, "action": action},
        )
    return normalised
