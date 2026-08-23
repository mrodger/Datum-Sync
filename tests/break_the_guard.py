"""Prove the security tests are load-bearing.

Not a test module -- run it directly:

    .venv/bin/python tests/break_the_guard.py

A test asserting 403 passes just as well against a route that is broken for an
unrelated reason, and a test that has never been seen to fail is not a gate.
Each case here removes exactly one guard from the source, runs the test that is
supposed to notice, and requires it to FAIL. A case that still passes with its
guard deleted is reported as UNPROVEN: the assertion is not testing what its
name says.

Every edit is reverted in a `finally`, and the file contents are compared
afterwards, so a crash mid-run cannot leave a disarmed check in the tree.
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

# (label, file, old, new, test that must break)
CASES = [
    (
        "reuse detection: revoke the family",
        "datum_sync/oauth.py",
        "    except _Reuse as reuse:\n"
        "        await _revoke_family(conn, reuse.client_id, reuse.account_id)\n"
        "        raise OAuthError(\"invalid_grant\", reuse.message) from None\n"
        "\n"
        "\nasync def _exchange_code_txn(",
        "    except _Reuse as reuse:\n"
        "        raise OAuthError(\"invalid_grant\", reuse.message) from None\n"
        "\n"
        "\nasync def _exchange_code_txn(",
        "tests/test_auth.py::test_replaying_an_authorization_code_revokes_the_whole_family",
    ),
    (
        "reuse detection: revoking OUTSIDE the rolled-back transaction",
        "datum_sync/oauth.py",
        # The bug as originally written: revoke in place, inside the very
        # transaction the raise is about to roll back.
        "        if row[\"used_at\"] is not None:\n"
        "            raise _Reuse(\n"
        "                row[\"client_id\"],\n"
        "                row[\"account_id\"],\n"
        "                \"authorization code has already been used\",\n"
        "            )",
        "        if row[\"used_at\"] is not None:\n"
        "            await _revoke_family(conn, row[\"client_id\"], row[\"account_id\"])\n"
        "            raise OAuthError(\n"
        "                \"invalid_grant\", \"authorization code has already been used\"\n"
        "            )",
        "tests/test_auth.py::test_replaying_an_authorization_code_revokes_the_whole_family",
    ),
    (
        "RFC 8707 audience binding",
        "datum_sync/auth.py",
        "        if row[\"resource\"] is not None and canonical_resource(\n"
        "            row[\"resource\"]\n"
        "        ) != canonical_resource(MCP_RESOURCE):",
        "        if False:",
        "tests/test_auth.py::test_a_token_for_another_audience_is_refused",
    ),
    (
        "redirect_uri exact match (open redirector)",
        "datum_sync/oauth.py",
        "    if requested not in registered:",
        "    if False:",
        "tests/test_auth.py::test_an_unregistered_redirect_uri_is_not_redirected_to",
    ),
    (
        "PKCE verification",
        "datum_sync/oauth.py",
        "        if not _pkce_ok(code_verifier, row[\"code_challenge\"]):",
        "        if False:",
        "tests/test_auth.py::test_a_wrong_pkce_verifier_is_refused",
    ),
    (
        "refresh rotation replay",
        "datum_sync/oauth.py",
        "        if row[\"rotated_to\"] is not None:",
        "        if False:",
        "tests/test_auth.py::test_a_rotated_refresh_token_cannot_be_replayed",
    ),
    (
        "repo scope on submit",
        "datum_sync/api.py",
        "    auth.require_repo(caller, repo)\n"
        "    params = body.get(\"params\", {})",
        "    params = body.get(\"params\", {})",
        "tests/test_auth.py::test_scope_is_enforced_on_submit_not_only_on_reads",
    ),
    (
        "the WWW-Authenticate challenge survives the middleware",
        "datum_sync/api.py",
        "        return errors.envelope(\n"
        "            exc.status, exc.code, exc.message, exc.detail, exc.headers\n"
        "        )",
        "        return errors.envelope(exc.status, exc.code, exc.message, exc.detail)",
        "tests/test_auth.py::test_an_anonymous_request_is_refused_with_a_discovery_challenge",
    ),
    (
        "the bearer guard itself (allowlist everything)",
        "datum_sync/api.py",
        "    if request.url.path in PUBLIC_PATHS:",
        "    if True:",
        "tests/test_auth.py::test_an_unknown_token_is_refused",
    ),
    (
        "scope filter on the repository listing",
        "datum_sync/api.py",
        "return {\"items\": [dict(r) for r in rows if caller.allows_repo(r[\"name\"])]}",
        "return {\"items\": [dict(r) for r in rows]}",
        "tests/test_auth.py::test_a_listing_hides_repositories_outside_scope",
    ),
    (
        "scope check on a job's own repository",
        "datum_sync/api.py",
        "    row = await execute.job_row(conn, job_id)\n"
        "    auth.require_repo(caller, row[\"repository\"])",
        "    row = await execute.job_row(conn, job_id)",
        "tests/test_auth.py::test_a_job_in_another_repository_is_not_readable",
    ),
    (
        "MCP hides a workspace with a required FILE parameter",
        "datum_sync/mcp.py",
        "    return not any(\n"
        "        p.type is ParameterType.FILE and p.required for p in manifest.parameters\n"
        "    )",
        "    return True",
        "tests/test_auth.py::test_tools_list_hides_a_workspace_that_could_only_fail",
    ),
    (
        "MCP scope filter",
        "datum_sync/mcp.py",
        "        if not principal.allows_repo(repo):\n            continue",
        "        if False:\n            continue",
        "tests/test_auth.py::test_tools_list_respects_repository_scope",
    ),
    (
        "the login lockout",
        "datum_sync/auth.py",
        "    if retry_after:\n        raise TooManyAttempts(retry_after)",
        "    if False:\n        raise TooManyAttempts(retry_after)",
        "tests/test_auth.py::test_repeated_wrong_passwords_lock_the_account_out",
    ),
    (
        "the lockout's window (a lockout that never lifts)",
        "datum_sync/auth.py",
        "    recent = [t for t in _attempts.get(name, []) if now - t < window]",
        "    recent = list(_attempts.get(name, []))",
        "tests/test_auth.py::test_the_lockout_expires",
    ),
    (
        "clearing the counter on a successful login",
        "datum_sync/auth.py",
        "    _attempts.pop(name, None)\n    return row",
        "    return row",
        "tests/test_auth.py::test_a_successful_login_clears_the_counter",
    ),
    (
        "counting failures for names that do not exist (enumeration)",
        "datum_sync/auth.py",
        # The plausible version of this bug: only bother counting attempts
        # against accounts that are real. It reads like an optimisation and it
        # turns the limiter into an account oracle.
        "        _record_failure(name, now)",
        "        if row is not None:\n            _record_failure(name, now)",
        "tests/test_auth.py::test_the_lockout_does_not_reveal_whether_an_account_exists",
    ),
    (
        "PUBLIC_URL must be configured, not guessed",
        "datum_sync/config.py",
        "    if not PUBLIC_URL_CONFIGURED:",
        "    if False:",
        "tests/test_auth.py::test_an_unset_public_url_refuses_to_start",
    ),
]

# Not covered here, and deliberately not faked: the semaphore bounding
# concurrent argon2 work. It changes how *fast* the login form can burn CPU,
# not whether a request is allowed, so a functional test either asserts nothing
# or asserts a timing threshold that will flake on a loaded machine. Left as
# reasoning in `auth.authenticate_password` rather than a test that would only
# look like proof.


def run(test: str) -> bool:
    """True if the test passed."""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", test, "-q", "--no-header", "-p", "no:cacheprovider"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0


def main() -> int:
    unproven: list[str] = []
    skipped: list[str] = []

    for label, relpath, old, new, test in CASES:
        path = ROOT / relpath
        original = path.read_text()
        if original.count(old) != 1:
            # The source moved. Loudly, because a break case that no longer
            # applies is a case that silently stops proving anything.
            skipped.append(f"{label}: pattern matches {original.count(old)} times")
            print(f"  SKIP  {label} (pattern not found exactly once)")
            continue

        path.write_text(original.replace(old, new))
        try:
            passed = run(test)
        finally:
            path.write_text(original)
            assert path.read_text() == original, f"failed to restore {relpath}"

        if passed:
            unproven.append(label)
            print(f"  UNPROVEN  {label}\n            {test} still passes without it")
        else:
            print(f"  ok        {label}")

    print()
    if skipped:
        print(f"{len(skipped)} case(s) skipped:")
        for s in skipped:
            print(f"  - {s}")
    if unproven:
        print(f"{len(unproven)} guard(s) NOT proven by any test.")
        return 1
    if skipped:
        return 1
    print(f"All {len(CASES)} guards proven: removing each one breaks its test.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
