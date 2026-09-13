"""grants.py: subsumption, narrowing and the meet, without a database.

The property that matters is the one `narrows` rests on: if `subsumes(a, b)`
says every path `b` matches is matched by `a`, then for any path that is
true. Checked by generating patterns and paths at random rather than by
listing cases, because the segment walk has more branches than anyone will
enumerate by hand and a wrong branch is an agent wider than its sponsor.
"""
from __future__ import annotations

import random

import pytest

from datum_sync import grants, vault

SEGMENTS = ["dev", "shared", "a", "b", "x", "*", "**", "d?v", "sh*"]
LITERALS = ["dev", "shared", "a", "b", "x", "div", "sha", "long", "dev2"]


def _pattern(rng: random.Random) -> str:
    return "/".join(rng.choice(SEGMENTS) for _ in range(rng.randint(1, 4)))


def _path(rng: random.Random) -> str:
    return "/".join(rng.choice(LITERALS) for _ in range(rng.randint(1, 5)))


def test_subsumes_implies_match_implication():
    """subsumes(a, b) => for every path p: match(b, p) => match(a, p).

    2,000 random (a, b) pairs, each checked against 40 random paths. A
    counterexample is printed in full so it can be turned into a fixed case.

    Guard: GRANT-001.
    """
    rng = random.Random(20260913)
    checked = 0
    for _ in range(2000):
        a, b = _pattern(rng), _pattern(rng)
        if not grants.subsumes(a, b):
            continue
        for _ in range(40):
            p = _path(rng)
            if vault.matches(b, p):
                assert vault.matches(a, p), f"subsumes({a!r}, {b!r}) but {p!r} matches b only"
                checked += 1
    assert checked > 200, "the generator produced too few informative cases"


@pytest.mark.parametrize(
    "outer, inner, expected",
    [
        ("dev/**", "dev/a/b", True),
        ("dev/**", "dev", False),
        ("dev/**/x", "dev/x", True),
        ("dev/**", "dev/**", True),
        ("**", "**", True),
        ("**", "anything/at/all", True),
        ("dev/*", "dev/a", True),
        ("dev/*", "dev/a/b", False),
        ("dev/a", "dev/**", False),
        ("dev/*/x", "dev/a/x", True),
        ("dev/*/x", "dev/**/x", False),
        ("d?v/**", "dev/notes", True),
        ("dev/**", "shared/**", False),
    ],
)
def test_subsumes_fixed_cases(outer, inner, expected):
    assert grants.subsumes(outer, inner) is expected


def test_narrows_names_every_wider_field():
    parent = {
        "max_tier": 3,
        "repo_scope": ["SCIMAC"],
        "vault_scope": {"read": ["dev/**"], "write": ["dev/x/**"], "deny": ["secrets/**"]},
        "proxy_grants": ["openai"],
        "limits": {"jobs_per_hour": 10},
        "rate_limit_per_min": 60,
    }
    child = {
        "max_tier": 4,
        "repo_scope": ["SCIMAC", "Other"],
        "vault_scope": {"read": ["shared/**"], "write": ["dev/x/**"], "deny": []},
        "proxy_grants": ["openai", "tavily"],
        "limits": {"jobs_per_hour": 20},
        "rate_limit_per_min": None,
    }
    assert grants.narrows(child, parent) == [
        "max_tier", "repo_scope", "vault_scope.read", "vault_scope.deny",
        "proxy_grants", "limits.jobs_per_hour", "rate_limit_per_min",
    ]
    assert grants.narrows(parent, parent) == []
    # The empty grant narrows anything -- once it states a rate limit, since
    # an absent limit means unlimited and unlimited is wider than 60.
    assert grants.narrows({"max_tier": 1, "repo_scope": [], "rate_limit_per_min": 1}, parent) == []
    assert grants.narrows({"max_tier": 1, "repo_scope": []}, parent) == ["rate_limit_per_min"]


def test_wildcard_repo_scope_is_only_narrowed_by_itself():
    assert grants.narrows({"repo_scope": ["*"]}, {"repo_scope": ["*"]}) == []
    assert grants.narrows({"repo_scope": ["*"]}, {"repo_scope": ["A"]}) == ["repo_scope"]
    assert grants.narrows({"repo_scope": ["A/*"]}, {"repo_scope": ["A"]}) == []


def test_null_vault_scope_narrows_anything_and_is_narrowed_by_nothing():
    assert grants.narrows({"vault_scope": None}, {"vault_scope": {"read": ["**"]}}) == []
    assert grants.narrows({"vault_scope": {"read": ["a"]}}, {"vault_scope": None}) == ["vault_scope.read"]


def test_federation_blocks_narrow_by_connection_pattern_flag_and_tools():
    parent = {"federation_scope": {"code": {
        "connections": ["github"], "repos": ["datum/*"], "write": True,
        "tools": {"allow": ["*"], "deny": ["delete_*"]},
    }}}
    ok = {"federation_scope": {"code": {
        "connections": ["github"], "repos": ["datum/gateway"], "write": False,
        "tools": {"allow": ["*"], "deny": ["delete_*", "force_*"]},
    }}}
    assert grants.narrows(ok, parent) == []
    wider = {"federation_scope": {"code": {
        "connections": ["github", "gitea"], "repos": ["other/*"], "write": True,
        "tools": {"allow": ["*"], "deny": []},
    }}}
    assert grants.narrows(wider, parent) == [
        "federation_scope.code.connections", "federation_scope.code.repos",
        "federation_scope.code.tools",
    ]
    assert grants.narrows({"federation_scope": {"compute": {"connections": ["vm"]}}}, parent) == [
        "federation_scope.compute"
    ]


def test_compute_commands_narrow_by_string_equality():
    parent = {"federation_scope": {"compute": {
        "connections": ["vm"], "hosts": ["vm102"],
        "commands": {"allow": ["^ls\\b", "^cat\\b"], "deny": ["\\bsudo\\b"]},
        "paths": {"read": ["/home/**"], "write": []}, "approval_required": ["restart"],
    }}}
    child = {"federation_scope": {"compute": {
        "connections": ["vm"], "hosts": ["vm102"],
        # A regex that would match a subset of the parent's is still not the
        # parent's: regexes are not subsumed, only reused.
        "commands": {"allow": ["^ls -l\\b"], "deny": ["\\bsudo\\b"]},
        "paths": {"read": ["/home/ubuntu/**"], "write": []}, "approval_required": ["restart"],
    }}}
    assert grants.narrows(child, parent) == ["federation_scope.compute.commands.allow"]


def test_intersect_is_the_meet():
    a = {"max_tier": 3, "repo_scope": ["*"], "vault_scope": {"read": ["dev/**"], "deny": ["a/**"]},
         "proxy_grants": ["x", "y"], "limits": {"jobs_per_hour": 5}, "rate_limit_per_min": 100}
    b = {"max_tier": 2, "repo_scope": ["A/*"], "vault_scope": {"read": ["dev/x/**", "shared/**"]},
         "proxy_grants": ["y"], "limits": {"jobs_per_hour": 9, "concurrent_jobs": 1},
         "rate_limit_per_min": None}
    out = grants.intersect(a, b)
    assert out["max_tier"] == 2
    assert out["repo_scope"] == ["A/*"]
    assert out["vault_scope"] == {"read": ["dev/x/**"], "deny": ["a/**"]}
    assert out["proxy_grants"] == ["y"]
    assert out["limits"] == {"jobs_per_hour": 5, "concurrent_jobs": 1}
    assert out["rate_limit_per_min"] == 100
    # Idempotent once narrowed.
    assert grants.intersect(out, out) == out


def test_intersect_with_nothing_is_nothing():
    a = {"max_tier": 5, "repo_scope": ["*"], "vault_scope": {"read": ["**"]}, "proxy_grants": ["x"]}
    out = grants.intersect(a, {"max_tier": 1, "repo_scope": [], "vault_scope": None, "proxy_grants": []})
    assert out["repo_scope"] == [] and out["vault_scope"] is None and out["proxy_grants"] == []
    assert out["max_tier"] == 1


def test_restricted_keeps_only_reads():
    t = {"max_tier": 4, "repo_scope": ["A"], "vault_scope": {"read": ["dev/**"], "write": ["dev/**"], "deny": ["s/**"]},
         "proxy_grants": ["x"], "federation_scope": {"code": {"connections": ["gh"]}},
         "limits": {"concurrent_sessions": 4}, "rate_limit_per_min": 10}
    r = grants.restricted(t)
    assert r["max_tier"] == 1
    assert r["vault_scope"] == {"read": ["dev/**"], "deny": ["s/**"]}
    assert r["proxy_grants"] == [] and r["federation_scope"] is None
    assert r["limits"]["concurrent_sessions"] == 1
    assert r["repo_scope"] == ["A"] and r["rate_limit_per_min"] == 10
