"""Authority as data: does one grant narrow another, and what is the meet.

Deliberately pure, like vault.py: nothing here touches the database or a
request. The interesting failures are all in the decision -- a child that is
wider than its parent in one field, a pattern that looks narrower and is not
-- and a decision that needs a database to test is a decision that gets
tested once.

An "authority tuple" is a dict with these keys, each optional:

    max_tier            int 1..5
    repo_scope          list[str]      -- `*`, `NAME`, `NAME/*`
    vault_scope         dict | None    -- see vault.py; None is no access
    proxy_grants        list[str]
    federation_scope    dict | None    -- see spec/agent-auth-plane/05 §2
    limits              dict           -- concurrent_sessions, jobs_per_hour, ...
    rate_limit_per_min  int | None     -- None is unlimited

Two operations, and the relation between them is the invariant the rest of
the system rests on (PRIN-001, PRIN-002):

  * `narrows(child, parent)` -- every field of `child` reaches no further than
    the same field of `parent`. Checked at every write to either row.
  * `intersect(a, b)` -- the meet. Used at resolve time to fold a principal's
    ancestors into its effective authority, so that narrowing or disabling a
    sponsor takes effect on its agents at their next request. Because
    `narrows` held at write time, `intersect` is normally the identity; it is
    conservative where the two overlap without either subsuming the other,
    which is the right direction for an authority computation.

Glob subsumption (`subsumes`) is exact over vault.py's glob language: `*` is
one segment, `**` is any number including zero. It is a segment walk rather
than a regex comparison because regex inclusion is not decidable in general
and this language is small enough that it does not need to be.
"""
from __future__ import annotations

from typing import Any

from datum_sync import vault

FIELDS = (
    "max_tier",
    "repo_scope",
    "vault_scope",
    "proxy_grants",
    "federation_scope",
    "limits",
    "rate_limit_per_min",
)

# Blocks of a federation scope, and the pattern-list fields inside each that
# narrow by subsumption. `commands.allow` and `tools.allow` narrow by string
# equality (a regex is not subsumed by another regex); deny lists widen.
_FED_PATTERN_FIELDS = {
    "code": ("repos",),
    "compute": ("hosts",),
    "documents": ("folders",),
}


# -- globs -------------------------------------------------------------------


def matches(pattern: str, value: str) -> bool:
    """vault.py's glob rules, applied to any `/`-separated target."""
    return vault.matches(pattern, value)


def subsumes(outer: str, inner: str) -> bool:
    """True iff every path `inner` matches, `outer` matches too."""
    return _walk(outer.split("/"), 0, inner.split("/"), 0)


def _walk(o: list[str], i: int, n: list[str], j: int) -> bool:
    if i == len(o) and j == len(n):
        return True
    if i == len(o):
        return False
    if o[i] == "**":
        # `**/` absorbs zero or more inner segments; a trailing `**` absorbs
        # at least one. That is vault.py's compilation exactly (`dev/**` is
        # `dev/.*`, which `dev` does not match), and the property test in
        # tests/test_grants.py is what holds the two in step.
        first = j + 1 if i == len(o) - 1 else j
        return any(_walk(o, i + 1, n, k) for k in range(first, len(n) + 1))
    if j == len(n):
        return False
    if n[j] == "**":
        # The inner side is broader here, unless the outer side is too, and
        # that case was taken above.
        return False
    if o[i] == "*" or o[i] == n[j]:
        return _walk(o, i + 1, n, j + 1)
    if ("*" in o[i] or "?" in o[i]) and not ("*" in n[j] or "?" in n[j]):
        # A wildcard segment against a literal one: compare literally.
        return vault.matches(o[i], n[j]) and _walk(o, i + 1, n, j + 1)
    return False


def _covered(inner_patterns: list[str], outer_patterns: list[str]) -> list[str]:
    """The inner patterns no outer pattern subsumes."""
    return [p for p in inner_patterns if not any(subsumes(q, p) for q in outer_patterns)]


# -- repositories ------------------------------------------------------------


def _repo_name(pattern: str) -> str:
    return pattern[:-2] if pattern.endswith("/*") else pattern


def _repo_narrows(child: list[str], parent: list[str]) -> bool:
    if "*" in parent:
        return True
    if "*" in child:
        return False
    held = {_repo_name(p) for p in parent}
    return all(_repo_name(c) in held for c in child)


def _repo_intersect(a: list[str], b: list[str]) -> list[str]:
    if "*" in a:
        return list(b)
    if "*" in b:
        return list(a)
    names = {_repo_name(p) for p in b}
    return [p for p in a if _repo_name(p) in names]


# -- vault scope -------------------------------------------------------------


def _vault_narrows(child: dict | None, parent: dict | None) -> list[str]:
    """Field names (as `vault_scope.read` etc.) where the child is wider."""
    if not child:
        return []
    if not parent:
        # Anything held by the child is more than the parent's nothing.
        return [f"vault_scope.{k}" for k in vault.ACTIONS if child.get(k)]
    wider = []
    for action in vault.ACTIONS:
        if _covered(list(child.get(action) or []), list(parent.get(action) or [])):
            wider.append(f"vault_scope.{action}")
    # Deny goes the other way: every parent deny must be matched or exceeded
    # by a child deny. A child may forbid itself more, never less.
    if _covered(list(parent.get("deny") or []), list(child.get("deny") or [])):
        wider.append("vault_scope.deny")
    return wider


def _vault_intersect(a: dict | None, b: dict | None) -> dict | None:
    if not a or not b:
        return None
    out: dict[str, list[str]] = {}
    for action in vault.ACTIONS:
        pa, pb = list(a.get(action) or []), list(b.get(action) or [])
        kept = [p for p in pa if any(subsumes(q, p) for q in pb)]
        kept += [q for q in pb if any(subsumes(p, q) for p in pa) and q not in kept]
        if kept:
            out[action] = kept
    deny = list(dict.fromkeys(list(a.get("deny") or []) + list(b.get("deny") or [])))
    if deny:
        out["deny"] = deny
    return out


# -- federation scope --------------------------------------------------------


def _fed_narrows(child: dict | None, parent: dict | None) -> list[str]:
    if not child:
        return []
    if not parent:
        return [f"federation_scope.{k}" for k in child if child.get(k)]
    wider: list[str] = []
    for kind, block in child.items():
        if not block:
            continue
        pblock = parent.get(kind)
        if not pblock:
            wider.append(f"federation_scope.{kind}")
            continue
        prefix = f"federation_scope.{kind}"
        if kind == "mcp":
            # {"connections": {name: {tools: {allow, deny}}}}
            pc, cc = pblock.get("connections") or {}, block.get("connections") or {}
            for name, spec in cc.items():
                if name not in pc:
                    wider.append(f"{prefix}.connections.{name}")
                    continue
                if _tools_wider((spec or {}).get("tools"), (pc[name] or {}).get("tools")):
                    wider.append(f"{prefix}.connections.{name}.tools")
            continue
        if not set(block.get("connections") or []) <= set(pblock.get("connections") or []):
            wider.append(f"{prefix}.connections")
        for field in _FED_PATTERN_FIELDS.get(kind, ()):
            if _covered(list(block.get(field) or []), list(pblock.get(field) or [])):
                wider.append(f"{prefix}.{field}")
        for flag in ("write", "share"):
            if block.get(flag) and not pblock.get(flag):
                wider.append(f"{prefix}.{flag}")
        if _tools_wider(block.get("tools"), pblock.get("tools")):
            wider.append(f"{prefix}.tools")
        if kind == "compute":
            cc, pc = block.get("commands") or {}, pblock.get("commands") or {}
            if not set(cc.get("allow") or []) <= set(pc.get("allow") or []):
                wider.append(f"{prefix}.commands.allow")
            if not set(pc.get("deny") or []) <= set(cc.get("deny") or []):
                wider.append(f"{prefix}.commands.deny")
            cp, pp = block.get("paths") or {}, pblock.get("paths") or {}
            for access in ("read", "write"):
                if _covered(list(cp.get(access) or []), list(pp.get(access) or [])):
                    wider.append(f"{prefix}.paths.{access}")
            if not set(pblock.get("approval_required") or []) <= set(
                block.get("approval_required") or []
            ):
                wider.append(f"{prefix}.approval_required")
    return wider


def _tools_wider(child: dict | None, parent: dict | None) -> bool:
    """`tools: {allow, deny}` -- allow by string equality, deny widens."""
    c, p = child or {}, parent or {}
    c_allow = list(c.get("allow") if c.get("allow") is not None else ["*"])
    p_allow = list(p.get("allow") if p.get("allow") is not None else ["*"])
    if "*" not in p_allow and not set(c_allow) <= set(p_allow):
        return True
    return not set(p.get("deny") or []) <= set(c.get("deny") or [])


# -- limits ------------------------------------------------------------------


def _limits_narrows(child: dict, parent: dict) -> list[str]:
    wider = []
    for key, value in (child or {}).items():
        pv = (parent or {}).get(key)
        if pv is not None and value is not None and value > pv:
            wider.append(f"limits.{key}")
    return wider


def _limits_intersect(a: dict, b: dict) -> dict:
    out = dict(a or {})
    for key, value in (b or {}).items():
        if value is None:
            continue
        out[key] = value if out.get(key) is None else min(out[key], value)
    return out


# -- the two operations ------------------------------------------------------


def narrows(child: dict[str, Any], parent: dict[str, Any]) -> list[str]:
    """Field names where `child` reaches further than `parent`. Empty = ok.

    A list rather than a bool so the refusal can name the field: an operator
    told "grant not narrower" edits the wrong thing; one told
    "vault_scope.write" edits the right one.
    """
    wider: list[str] = []
    if (child.get("max_tier") or 1) > (parent.get("max_tier") or 1):
        wider.append("max_tier")
    if not _repo_narrows(list(child.get("repo_scope") or []), list(parent.get("repo_scope") or [])):
        wider.append("repo_scope")
    wider += _vault_narrows(child.get("vault_scope"), parent.get("vault_scope"))
    if not set(child.get("proxy_grants") or []) <= set(parent.get("proxy_grants") or []):
        wider.append("proxy_grants")
    wider += _fed_narrows(child.get("federation_scope"), parent.get("federation_scope"))
    wider += _limits_narrows(child.get("limits") or {}, parent.get("limits") or {})
    prl, crl = parent.get("rate_limit_per_min"), child.get("rate_limit_per_min")
    if prl is not None and (crl is None or crl > prl):
        wider.append("rate_limit_per_min")
    return wider


def intersect(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """The meet of two authority tuples."""
    out: dict[str, Any] = {}
    out["max_tier"] = min(a.get("max_tier") or 1, b.get("max_tier") or 1)
    out["repo_scope"] = _repo_intersect(
        list(a.get("repo_scope") or []), list(b.get("repo_scope") or [])
    )
    out["vault_scope"] = _vault_intersect(a.get("vault_scope"), b.get("vault_scope"))
    out["proxy_grants"] = [
        g for g in (a.get("proxy_grants") or []) if g in set(b.get("proxy_grants") or [])
    ]
    out["federation_scope"] = _fed_intersect(a.get("federation_scope"), b.get("federation_scope"))
    out["limits"] = _limits_intersect(a.get("limits") or {}, b.get("limits") or {})
    ra, rb = a.get("rate_limit_per_min"), b.get("rate_limit_per_min")
    out["rate_limit_per_min"] = ra if rb is None else (rb if ra is None else min(ra, rb))
    return out


def _fed_intersect(a: dict | None, b: dict | None) -> dict | None:
    """Conservative: a block survives only where the child already narrows.

    `narrows` is the exact check and it ran at write time. Here a block that
    is not narrowed by the other side is dropped whole rather than merged
    field by field, because a partially merged federation block could reach
    a tool neither side granted on its own.
    """
    if not a or not b:
        return None
    out = {}
    for kind, block in a.items():
        if block and kind in b and not _fed_narrows({kind: block}, {kind: b[kind]}):
            out[kind] = block
    return out or None


def restricted(t: dict[str, Any]) -> dict[str, Any]:
    """What a `restricted` principal is forced to (spec 03 §4)."""
    vs = t.get("vault_scope") or {}
    read_only = {"read": list(vs.get("read") or [])} if vs.get("read") else None
    if read_only and vs.get("deny"):
        read_only["deny"] = list(vs["deny"])
    limits = dict(t.get("limits") or {})
    limits["concurrent_sessions"] = 1
    return {
        "max_tier": 1,
        "repo_scope": list(t.get("repo_scope") or []),
        "vault_scope": read_only,
        "proxy_grants": [],
        "federation_scope": None,
        "limits": limits,
        "rate_limit_per_min": t.get("rate_limit_per_min"),
    }
