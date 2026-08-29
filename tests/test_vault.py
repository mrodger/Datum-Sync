import pytest

from datum_sync.errors import ApiError
from datum_sync.vault import (
    VaultPathError,
    VaultScopeError,
    check,
    matches,
    normalise,
    permits,
    validate_scope,
)

SCOPE = {
    "read": ["dev/**", "shared/long_term/**", "notes/*.md"],
    "write": ["dev/**"],
    "quarantine": ["quarantine/research/**"],
    "promote": ["quarantine/**"],
    "deny": ["dev/secrets/**", "**/*.key"],
}


# --- normalisation ---------------------------------------------------------

def test_plain_path_survives():
    assert normalise("dev/config/foo.md") == "dev/config/foo.md"


def test_trailing_slash_stripped():
    assert normalise("dev/config/") == "dev/config"


def test_surrounding_whitespace_stripped():
    assert normalise("  dev/foo.md  ") == "dev/foo.md"


@pytest.mark.parametrize(
    "path",
    [
        "",
        "   ",
        "/etc/passwd",
        "../secrets/key",
        "dev/../../etc/passwd",
        "dev/./foo",
        "dev//foo",
        "dev\\foo",
        "dev/\x00foo",
        "a" * 1025,
    ],
)
def test_malformed_paths_rejected(path):
    with pytest.raises(VaultPathError):
        normalise(path)


def test_traversal_is_rejected_not_resolved():
    # `dev/../dev/x` resolves to a path inside scope. Resolving it would accept
    # a written form that escapes whatever it was checked against.
    with pytest.raises(VaultPathError):
        normalise("dev/../dev/x")


def test_double_dot_inside_a_segment_is_a_filename():
    assert normalise("dev/notes..md") == "dev/notes..md"


# --- glob semantics --------------------------------------------------------

def test_star_does_not_cross_a_separator():
    # fnmatch would say True here. That is the bug this module exists to avoid.
    assert not matches("*", "secrets/key")
    assert matches("*", "secrets")


def test_doublestar_crosses_separators():
    assert matches("dev/**", "dev/a/b/c.md")


def test_doublestar_directory_matches_zero_directories():
    assert matches("dev/**/x.md", "dev/x.md")
    assert matches("dev/**/x.md", "dev/a/b/x.md")


def test_pattern_is_anchored_at_both_ends():
    assert not matches("dev/*.md", "other/dev/foo.md")
    assert not matches("dev/foo", "dev/foobar")


def test_question_mark_matches_one_non_separator():
    assert matches("dev/?.md", "dev/a.md")
    assert not matches("dev/?.md", "dev/ab.md")


# --- scope decisions -------------------------------------------------------

def test_read_allowed_inside_scope():
    assert permits(SCOPE, "read", "dev/config/foo.md")


def test_read_refused_outside_scope():
    assert not permits(SCOPE, "read", "private/diary.md")


def test_null_scope_is_no_access():
    assert not permits(None, "read", "dev/foo.md")


def test_empty_scope_is_no_access():
    assert not permits({}, "read", "dev/foo.md")


def test_deny_beats_an_explicit_allow():
    # dev/** grants read; dev/secrets/** denies it. Deny wins.
    assert matches("dev/**", "dev/secrets/token.md")
    assert not permits(SCOPE, "read", "dev/secrets/token.md")


def test_deny_beats_write_too():
    assert not permits(SCOPE, "write", "dev/secrets/token.md")


def test_deny_pattern_can_span_directories():
    assert not permits(SCOPE, "read", "dev/config/private.key")


def test_write_does_not_imply_read():
    # Literal by design: the coherence of a scope is the SHACL shape's job, and
    # inferring it here would let the shape be deleted with tests still green.
    assert permits({"write": ["dev/**"]}, "write", "dev/foo.md")
    assert not permits({"write": ["dev/**"]}, "read", "dev/foo.md")


def test_actions_are_independent():
    assert permits(SCOPE, "quarantine", "quarantine/research/a.md")
    assert not permits(SCOPE, "write", "quarantine/research/a.md")


def test_promote_scope_separate_from_quarantine():
    assert permits(SCOPE, "promote", "quarantine/other/a.md")
    assert not permits(SCOPE, "quarantine", "quarantine/other/a.md")


def test_unknown_action_is_a_programming_error():
    with pytest.raises(ValueError):
        permits(SCOPE, "delete", "dev/foo.md")


def test_deny_alone_refuses_everything():
    assert not permits({"deny": ["**"]}, "read", "anything.md")


# --- the check() wrapper ---------------------------------------------------

def test_check_returns_the_normalised_path():
    assert check(SCOPE, "read", "dev/config/") == "dev/config"


def test_check_refuses_with_403_not_404():
    with pytest.raises(ApiError) as exc:
        check(SCOPE, "read", "private/diary.md")
    assert exc.value.status == 403
    assert exc.value.code == "FORBIDDEN"


def test_check_reports_a_malformed_path_as_400():
    with pytest.raises(ApiError) as exc:
        check(SCOPE, "read", "../etc/passwd")
    assert exc.value.status == 400
    assert exc.value.code == "INVALID_PARAMETER"


def test_check_normalises_before_authorising():
    # The trailing slash must not be the reason a permitted path is refused.
    assert check(SCOPE, "write", "dev/notes/") == "dev/notes"


# --- validate_scope --------------------------------------------------------

def test_valid_scope_passes():
    validate_scope({
        "read": ["dev/**", "shared/**"],
        "write": ["dev/**"],
        "deny": ["dev/secrets/**"],
    })


def test_write_implies_read():
    with pytest.raises(VaultScopeError, match="write-implies-read"):
        validate_scope({
            "read": ["dev/**"],
            "write": ["dev/**", "shared/**"],  # shared not in read
        })


def test_write_implies_read_passes_when_subset():
    # write is a subset of read — valid
    validate_scope({
        "read": ["dev/**", "shared/**"],
        "write": ["dev/**"],
    })


def test_deny_contradicts_allow():
    with pytest.raises(VaultScopeError, match="contradictory"):
        validate_scope({
            "read": ["dev/**"],
            "deny": ["dev/**"],  # same pattern in both
        })


def test_deny_contradicts_write():
    with pytest.raises(VaultScopeError, match="contradictory"):
        validate_scope({
            "read": ["dev/**"],
            "write": ["dev/**"],
            "deny": ["dev/**"],
        })


def test_deny_different_pattern_is_fine():
    validate_scope({
        "read": ["dev/**"],
        "write": ["dev/**"],
        "deny": ["dev/secrets/**"],  # different pattern, no contradiction
    })


def test_traversal_in_pattern_rejected():
    with pytest.raises(VaultScopeError, match="traversal"):
        validate_scope({"read": ["dev/../etc/passwd"]})


def test_dot_segment_in_pattern_rejected():
    with pytest.raises(VaultScopeError, match="current-directory"):
        validate_scope({"read": ["dev/./foo"]})


def test_partial_wildcard_rejected():
    with pytest.raises(VaultScopeError, match="partial wildcard"):
        validate_scope({"read": ["dev/*.md"]})


def test_partial_wildcard_prefix_rejected():
    with pytest.raises(VaultScopeError, match="partial wildcard"):
        validate_scope({"read": ["dev/*foo"]})


def test_star_and_doublestar_allowed():
    validate_scope({"read": ["*", "dev/**"]})


def test_unknown_key_rejected():
    with pytest.raises(VaultScopeError, match="unknown keys"):
        validate_scope({"read": ["dev/**"], "execute": ["dev/**"]})


def test_non_list_value_rejected():
    with pytest.raises(VaultScopeError, match="must be a list"):
        validate_scope({"read": "dev/**"})


def test_non_string_pattern_rejected():
    with pytest.raises(VaultScopeError, match="non-string or empty"):
        validate_scope({"read": [123]})


def test_empty_pattern_rejected():
    with pytest.raises(VaultScopeError, match="non-string or empty"):
        validate_scope({"read": [""]})


def test_non_dict_scope_rejected():
    with pytest.raises(VaultScopeError, match="must be an object"):
        validate_scope(["dev/**"])


def test_empty_scope_is_valid():
    # An empty dict means no access — same as NULL. Not an error.
    validate_scope({})
