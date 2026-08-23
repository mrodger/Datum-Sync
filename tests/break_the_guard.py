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
        "    if path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES):",
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
    (
        "the session cookie is not accepted on the service paths (CSRF)",
        "datum_sync/api.py",
        # The plausible mistake: the UI wants to link to a download, so someone
        # adds the service paths to the list. `GET /stream/...` runs a
        # workspace, and Lax sends the cookie on top-level navigation.
        'COOKIE_PATHS = ("/rest/v1/", "/ui/")',
        'COOKIE_PATHS = ("/rest/v1/", "/ui/", "/stream/", "/download/")',
        "tests/test_auth.py::test_the_session_cookie_is_refused_on_the_service_paths",
    ),
    (
        "a session is not a bearer token (the two kinds must not cross)",
        "datum_sync/auth.py",
        "         WHERE t.token_hash = $1 AND t.kind = 'access'",
        "         WHERE t.token_hash = $1 AND t.kind IN ('access', 'session')",
        "tests/test_auth.py::test_a_session_is_not_usable_as_a_bearer_token",
    ),
    (
        "sign-out revokes server-side, not only in the browser",
        "datum_sync/ui.py",
        "    raw = request.cookies.get(auth.SESSION_COOKIE)\n"
        "    if raw:\n"
        "        async with db.pool().acquire() as conn:\n"
        "            await auth.revoke_session(conn, raw)\n"
        "    response = JSONResponse({\"status\": \"signed out\"})",
        "    response = JSONResponse({\"status\": \"signed out\"})",
        "tests/test_auth.py::test_signing_out_revokes_the_session_and_not_only_the_cookie",
    ),
    (
        "a disabled account is rejected on every request, not just at sign-in",
        "datum_sync/auth.py",
        "    # Checked on every request, not only at sign-in: disabling an account has to\n"
        "    # take effect against sessions already in flight, or the control does\n"
        "    # nothing for up to SESSION_TTL_SECONDS.\n"
        "    if row[\"disabled\"]:\n"
        "        raise _unauthenticated(\"account is disabled\", \"ACCOUNT_DISABLED\")\n",
        "",
        "tests/test_auth.py::test_disabling_an_account_kills_a_session_already_in_flight",
    ),
    (
        "session expiry",
        "datum_sync/auth.py",
        "    if row[\"expires_at\"] is not None and row[\"expires_at\"] < _now():\n"
        "        raise _unauthenticated(\"session has expired\", \"TOKEN_EXPIRED\")\n",
        "",
        "tests/test_auth.py::test_an_expired_session_is_refused",
    ),
    (
        "scope on the job listing",
        "datum_sync/api.py",
        "               AND ($2::text[] IS NULL OR repository = ANY($2))",
        "               AND ($2::text[] IS NULL OR true)",
        "tests/test_auth.py::test_the_job_listing_hides_jobs_outside_scope",
    ),
    (
        "scope applied before LIMIT, not after",
        "datum_sync/api.py",
        # Filter the result instead of the query. This still hides the rows --
        # the case above stays green -- so the only test that can tell the
        # difference is the one that checks a scoped caller is not paged out of
        # their own jobs.
        "               AND ($2::text[] IS NULL OR repository = ANY($2))\n"
        "               AND ($3::text IS NULL OR repository = $3)\n"
        "               AND ($4::text IS NULL OR workspace = $4)\n"
        "             ORDER BY submitted_at DESC\n"
        "             LIMIT $5\n"
        "            \"\"\",\n"
        "            status,\n"
        "            allowed,\n"
        "            repository,\n"
        "            workspace,\n"
        "            limit,\n"
        "        )",
        "               AND ($2::text IS NULL OR repository = $2)\n"
        "               AND ($3::text IS NULL OR workspace = $3)\n"
        "             ORDER BY submitted_at DESC\n"
        "             LIMIT $4\n"
        "            \"\"\",\n"
        "            status,\n"
        "            repository,\n"
        "            workspace,\n"
        "            limit,\n"
        "        )\n"
        "        rows = [r for r in rows if allowed is None or r[\"repository\"] in allowed]",
        "tests/test_auth.py::test_the_job_listing_applies_scope_before_the_limit",
    ),
    (
        "admin gate on the accounts listing",
        "datum_sync/api.py",
        "    auth.require_admin(caller)\n"
        "    async with db.pool().acquire() as conn:\n"
        "        rows = await conn.fetch(\n"
        "            \"\"\"\n"
        "            SELECT a.id, a.name, a.max_tier, a.repo_scope, a.is_admin,",
        "    async with db.pool().acquire() as conn:\n"
        "        rows = await conn.fetch(\n"
        "            \"\"\"\n"
        "            SELECT a.id, a.name, a.max_tier, a.repo_scope, a.is_admin,",
        "tests/test_auth.py::test_the_accounts_listing_requires_admin",
    ),
    (
        # The plausible mistake, and the reason the ban is absolute: this
        # particular use is harmless. It is also how most people spell it, and
        # once one line in the file assigns markup the next one is no longer
        # conspicuous. The test greps, so the guard is the absence itself.
        "the UI never assigns markup (innerHTML ban)",
        "datum_sync/static/app.js",
        "    node.replaceChildren();",
        "    node.innerHTML = '';",
        "tests/test_ui.py::test_the_ui_never_assigns_markup",
    ),
    (
        # Not the whole of safe_name: `.name` alone. _SAFE would still replace
        # the separators, so the stored name has no slash either way -- what
        # comes back is `_.._etc_passwd`, and the assertion that notices is the
        # one about `..`. Removing both guards at once would prove only that
        # one of them works.
        "an upload's stored name is taken from the basename",
        "datum_sync/uploads.py",
        '    name = _SAFE.sub("_", Path(filename or "").name).lstrip(".-")',
        '    name = _SAFE.sub("_", filename or "").lstrip(".-")',
        "tests/test_ui.py::test_an_upload_id_is_not_a_path",
    ),
    (
        # Zero files is still refused; two are silently narrowed to the first.
        # That is the version worth breaking: the caller believes they sent a
        # file under a parameter name the server never looked at, and gets a
        # 201 saying so.
        "an upload carries exactly one file, not at least one",
        "datum_sync/api.py",
        "        if len(files) != 1:",
        "        if not files:",
        "tests/test_ui.py::test_an_upload_needs_exactly_one_file",
    ),
    (
        # The same mistake as the scope filter, one row down: filter the result
        # instead of the query and LIMIT is applied first. Milder consequence
        # -- a workspace's recent runs vanish whenever the queue is busy rather
        # than a caller being paged out of their own jobs -- and no less wrong.
        "the workspace filter is applied in SQL, before LIMIT",
        "datum_sync/api.py",
        "               AND ($3::text IS NULL OR repository = $3)\n"
        "               AND ($4::text IS NULL OR workspace = $4)\n"
        "             ORDER BY submitted_at DESC\n"
        "             LIMIT $5\n"
        "            \"\"\",\n"
        "            status,\n"
        "            allowed,\n"
        "            repository,\n"
        "            workspace,\n"
        "            limit,\n"
        "        )",
        "             ORDER BY submitted_at DESC\n"
        "             LIMIT $3\n"
        "            \"\"\",\n"
        "            status,\n"
        "            allowed,\n"
        "            limit,\n"
        "        )\n"
        "        rows = [\n"
        "            r for r in rows\n"
        "            if (repository is None or r[\"repository\"] == repository)\n"
        "            and (workspace is None or r[\"workspace\"] == workspace)\n"
        "        ]",
        "tests/test_ui.py::test_the_job_listing_filters_by_workspace",
    ),
    (
        # Not a security guard -- included because the harness is the only
        # place that keeps a claim honest, and this one was silently false for
        # five build steps: a subscriber watched a job sit at QUEUED for its
        # whole run.
        "the queued -> running transition is announced",
        "datum_sync/jobs.py",
        '        await notify(conn, claimed["id"], event="status", status="running")\n',
        "",
        "tests/test_api.py::test_every_status_frame_carries_the_same_fields",
    ),
    (
        "a status frame is built from the row, not the notification",
        "datum_sync/events.py",
        "                current = await jobs.get(conn, job_id)\n"
        "                if current is None:\n"
        "                    return\n"
        "                yield _status_event(current)\n"
        "                if current[\"status\"] in jobs.TERMINAL:\n"
        "                    return",
        "                if body.get(\"status\") in jobs.TERMINAL:\n"
        "                    final = await jobs.get(conn, job_id)\n"
        "                    if final is not None:\n"
        "                        yield _status_event(final)\n"
        "                    return\n"
        "                yield _sse(\"status\", {\"job_id\": wanted,\n"
        "                                      \"status\": body.get(\"status\")})",
        "tests/test_api.py::test_every_status_frame_carries_the_same_fields",
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
