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

import asyncio
import importlib.util
import pathlib
import subprocess
import sys
import tempfile
from xml.etree import ElementTree

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from datum_sync import db  # noqa: E402  -- after the path insert, necessarily

# (id, label, file, old, new, test that must break)
#
# The id is the durable name of the guard. It is written here and repeated in a
# `Guard: <ID>` line in the named test, and tests/test_guard_registry.py checks
# both directions -- so a test cannot quietly stop being the thing that proves a
# guard, and a case cannot outlive the test it names. Ids are frozen once
# assigned: a new case appends within its domain, it does not renumber the rest.
CASES = [
    (
        "AUTH-001",
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
        "AUTH-002",
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
        "AUTH-003",
        "RFC 8707 audience binding",
        "datum_sync/auth.py",
        "        if row[\"resource\"] is not None and canonical_resource(\n"
        "            row[\"resource\"]\n"
        "        ) != canonical_resource(MCP_RESOURCE):",
        "        if False:",
        "tests/test_auth.py::test_a_token_for_another_audience_is_refused",
    ),
    (
        "AUTH-004",
        "redirect_uri exact match (open redirector)",
        "datum_sync/oauth.py",
        "    if requested not in registered:",
        "    if False:",
        "tests/test_auth.py::test_an_unregistered_redirect_uri_is_not_redirected_to",
    ),
    (
        "AUTH-005",
        "PKCE verification",
        "datum_sync/oauth.py",
        "        if not _pkce_ok(code_verifier, row[\"code_challenge\"]):",
        "        if False:",
        "tests/test_auth.py::test_a_wrong_pkce_verifier_is_refused",
    ),
    (
        "AUTH-006",
        "refresh rotation replay",
        "datum_sync/oauth.py",
        "        if row[\"rotated_to\"] is not None:",
        "        if False:",
        "tests/test_auth.py::test_a_rotated_refresh_token_cannot_be_replayed",
    ),
    (
        "AUTH-007",
        "repo scope on submit",
        "datum_sync/api.py",
        "    auth.require_repo(caller, repo)\n"
        "    params = body.get(\"params\", {})",
        "    params = body.get(\"params\", {})",
        "tests/test_auth.py::test_scope_is_enforced_on_submit_not_only_on_reads",
    ),
    (
        "AUTH-008",
        "the WWW-Authenticate challenge survives the middleware",
        "datum_sync/api.py",
        "        return errors.envelope(\n"
        "            exc.status, exc.code, exc.message, exc.detail, exc.headers\n"
        "        )",
        "        return errors.envelope(exc.status, exc.code, exc.message, exc.detail)",
        "tests/test_auth.py::test_an_anonymous_request_is_refused_with_a_discovery_challenge",
    ),
    (
        "AUTH-009",
        "the bearer guard itself (allowlist everything)",
        "datum_sync/api.py",
        "    if path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES):",
        "    if True:",
        "tests/test_auth.py::test_an_unknown_token_is_refused",
    ),
    (
        "AUTH-010",
        "scope filter on the repository listing",
        "datum_sync/api.py",
        "return {\"items\": [dict(r) for r in rows if caller.allows_repo(r[\"name\"])]}",
        "return {\"items\": [dict(r) for r in rows]}",
        "tests/test_auth.py::test_a_listing_hides_repositories_outside_scope",
    ),
    (
        "AUTH-011",
        "scope check on a job's own repository",
        "datum_sync/api.py",
        "    row = await execute.job_row(conn, job_id)\n"
        "    auth.require_repo(caller, row[\"repository\"])",
        "    row = await execute.job_row(conn, job_id)",
        "tests/test_auth.py::test_a_job_in_another_repository_is_not_readable",
    ),
    (
        "AUTH-012",
        "MCP hides a workspace with a required FILE parameter",
        "datum_sync/mcp.py",
        "    return not any(\n"
        "        p.type is ParameterType.FILE and p.required for p in manifest.parameters\n"
        "    )",
        "    return True",
        "tests/test_auth.py::test_tools_list_hides_a_workspace_that_could_only_fail",
    ),
    (
        "AUTH-013",
        "MCP scope filter",
        "datum_sync/mcp.py",
        "        if not principal.allows_repo(repo):\n            continue",
        "        if False:\n            continue",
        "tests/test_auth.py::test_tools_list_respects_repository_scope",
    ),
    (
        "AUTH-014",
        "the login lockout",
        "datum_sync/auth.py",
        "    if retry_after:\n        raise TooManyAttempts(retry_after)",
        "    if False:\n        raise TooManyAttempts(retry_after)",
        "tests/test_auth.py::test_repeated_wrong_passwords_lock_the_account_out",
    ),
    (
        "AUTH-015",
        "the lockout's window (a lockout that never lifts)",
        "datum_sync/auth.py",
        "    recent = [t for t in _attempts.get(name, []) if now - t < window]",
        "    recent = list(_attempts.get(name, []))",
        "tests/test_auth.py::test_the_lockout_expires",
    ),
    (
        "AUTH-016",
        "clearing the counter on a successful login",
        "datum_sync/auth.py",
        "    _attempts.pop(name, None)\n    return row",
        "    return row",
        "tests/test_auth.py::test_a_successful_login_clears_the_counter",
    ),
    (
        "AUTH-017",
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
        "AUTH-018",
        "PUBLIC_URL must be configured, not guessed",
        "datum_sync/config.py",
        "    if not PUBLIC_URL_CONFIGURED:",
        "    if False:",
        "tests/test_auth.py::test_an_unset_public_url_refuses_to_start",
    ),
    (
        "AUTH-019",
        "the session cookie is not accepted on the service paths (CSRF)",
        "datum_sync/api.py",
        # The plausible mistake: the UI wants to link to a download, so someone
        # adds the service paths to the list. `GET /stream/...` runs a
        # workspace, and Lax sends the cookie on top-level navigation.
        'COOKIE_PATHS = ("/rest/v1/", "/ui/", "/serve/")',
        'COOKIE_PATHS = ("/rest/v1/", "/ui/", "/serve/", "/stream/", "/download/")',
        "tests/test_auth.py::test_the_session_cookie_is_refused_on_the_service_paths",
    ),
    (
        "AUTH-020",
        "a session is not a bearer token (the two kinds must not cross)",
        "datum_sync/auth.py",
        "         WHERE t.token_hash = $1 AND t.kind = 'access'",
        "         WHERE t.token_hash = $1 AND t.kind IN ('access', 'session')",
        "tests/test_auth.py::test_a_session_is_not_usable_as_a_bearer_token",
    ),
    (
        "AUTH-021",
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
        "AUTH-022",
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
        "AUTH-023",
        "session expiry",
        "datum_sync/auth.py",
        "    if row[\"expires_at\"] is not None and row[\"expires_at\"] < _now():\n"
        "        raise _unauthenticated(\"session has expired\", \"TOKEN_EXPIRED\")\n",
        "",
        "tests/test_auth.py::test_an_expired_session_is_refused",
    ),
    (
        "AUTH-024",
        "scope on the job listing",
        "datum_sync/api.py",
        "               AND ($2::text[] IS NULL OR repository = ANY($2))",
        "               AND ($2::text[] IS NULL OR true)",
        "tests/test_auth.py::test_the_job_listing_hides_jobs_outside_scope",
    ),
    (
        "AUTH-025",
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
        "AUTH-026",
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
        "UI-001",
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
        "UI-002",
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
        "UI-003",
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
        "UI-004",
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
        "API-001",
        "the queued -> running transition is announced",
        "datum_sync/jobs.py",
        '        await notify(conn, claimed["id"], event="status", status="running")\n',
        "",
        "tests/test_api.py::test_every_status_frame_carries_the_same_fields",
    ),
    (
        "API-002",
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

    # -- step 7: schedules -------------------------------------------------
    (
        # The CHECK constraint cannot parse a cron expression, so without this
        # the row is accepted and sits enabled and never fires. A schedule with
        # no symptom is the worst thing this feature can produce.
        "SCHED-001",
        "a cron expression is parsed before the schedule is stored",
        "datum_sync/schedules.py",
        "    if cron is not None and not croniter.is_valid(cron):",
        "    if False:",
        "tests/test_schedules.py::test_a_definition_the_worker_could_not_run_is_refused",
    ),
    (
        # Evaluate the cron in UTC and the UTC hour is held while the local one
        # moves, so "every weekday at 07:00" becomes 08:00 for half the year.
        # Nothing looks wrong: the schedule fires, daily, at a time.
        "SCHED-002",
        "cron is evaluated in the schedule's own timezone",
        "datum_sync/schedules.py",
        "    zone = _zone(timezone)\n"
        "    local = moment.astimezone(zone)\n"
        "    following = croniter(cron, local).get_next(dt.datetime)\n"
        "    return following.astimezone(dt.timezone.utc)",
        "    _zone(timezone)\n"
        "    following = croniter(cron, moment).get_next(dt.datetime)\n"
        "    return following.astimezone(dt.timezone.utc)",
        "tests/test_schedules.py::test_a_daily_cron_holds_its_local_hour_across_a_dst_change",
    ),
    (
        # Return the missed time instead of walking past it. It still fires --
        # immediately -- and then again on the next poll, once for every period
        # it owed.
        "SCHED-003",
        "a missed schedule is walked forward, not replayed",
        "datum_sync/schedules.py",
        "        if upcoming > moment:\n            return upcoming",
        "        return upcoming",
        "tests/test_schedules.py::test_five_missed_days_are_one_run_and_not_five",
    ),
    (
        # Advance from now rather than from the missed time. Identical for a
        # cron schedule, which is why this needs an interval to notice: the
        # hourly job quietly moves to :15 and stays there.
        "SCHED-004",
        "a missed interval keeps its phase",
        "datum_sync/schedules.py",
        "    upcoming = row[\"next_run\"]\n"
        "    for _ in range(_MAX_CATCHUP_STEPS):",
        "    upcoming = moment\n"
        "    for _ in range(_MAX_CATCHUP_STEPS):",
        "tests/test_schedules.py::test_a_missed_interval_keeps_its_phase",
    ),
    (
        # A worker spinning through 86,400 steps of catch-up stops claiming
        # jobs, so one badly-configured schedule takes the whole queue down.
        "SCHED-005",
        "the catch-up walk is bounded",
        "datum_sync/schedules.py",
        "_MAX_CATCHUP_STEPS = 1000",
        "_MAX_CATCHUP_STEPS = 100_000_000",
        "tests/test_schedules.py::test_a_pathologically_overdue_interval_gives_up_walking",
    ),
    (
        # Advance only on success and a schedule pointing at an unpublished
        # workspace stays due forever: the worker retries it every poll, which
        # is a hot loop that also floods the log.
        "SCHED-006",
        "a schedule that failed to submit still advances",
        "datum_sync/schedules.py",
        "        upcoming = _advance(row, moment)\n"
        "\n"
        "        job_id = None",
        "        upcoming = row[\"next_run\"]\n"
        "\n"
        "        job_id = None",
        "tests/test_schedules.py::test_a_schedule_pointing_at_nothing_records_it_and_still_advances",
    ),
    (
        # Re-enabling a schedule paused for a week must not mean "fire now,
        # then catch up". The next_run is a week in the past.
        "SCHED-007",
        "re-enabling re-times instead of replaying",
        "datum_sync/schedules.py",
        "    enabling = changes.get(\"enabled\") and not current[\"enabled\"]",
        "    enabling = False",
        "tests/test_schedules.py::test_re_enabling_does_not_replay_the_runs_it_missed",
    ),
    (
        # asyncpg hands back jsonb as JSON *text*. Carrying that text through
        # the merge unchanged means json.dumps re-encodes it, so a patch that
        # only flips `enabled` stores a JSON string where an object was -- and
        # the next patch wraps it again. Silent, and invisible to every test
        # that patches params, because those overwrite the corrupted value.
        "SCHED-008",
        "params are decoded before being merged and re-encoded",
        "datum_sync/schedules.py",
        "    merged[\"params\"] = json.loads(current[\"params\"])",
        "    merged[\"params\"] = current[\"params\"]",
        "tests/test_schedules.py::test_pausing_a_schedule_does_not_eat_its_parameters",
    ),
    (
        # A schedule that can be repointed is a scope check that happened once,
        # on a row that no longer says what it said when it happened.
        "SCHED-009",
        "repository and workspace are not patchable",
        "datum_sync/schedules.py",
        "_PATCHABLE = (\"params\", \"cron\", \"interval_s\", \"timezone\", \"enabled\")",
        "_PATCHABLE = (\"params\", \"cron\", \"interval_s\", \"timezone\", \"enabled\",\n"
        "              \"repository\", \"workspace\")",
        "tests/test_schedules.py::test_a_schedule_cannot_be_repointed_at_another_workspace",
    ),
    (
        # The schedule submits jobs later with nobody present to check scope,
        # and jobs.submit performs no check of its own.
        "SCHED-010",
        "repo scope on creating a schedule",
        "datum_sync/api.py",
        "    auth.require_repo(caller, body[\"repository\"])\n"
        "\n"
        "    async with db.pool().acquire() as conn:\n"
        "        try:\n"
        "            row = await schedules.create(",
        "    async with db.pool().acquire() as conn:\n"
        "        try:\n"
        "            row = await schedules.create(",
        "tests/test_schedules.py::test_a_scoped_caller_cannot_schedule_another_repository",
    ),
    (
        "SCHED-011",
        "scope filter on the schedules listing",
        "datum_sync/api.py",
        "    where, args = _scope_sql(caller, \"repository\", 1)",
        "    where, args = \"\", []",
        "tests/test_schedules.py::test_a_scoped_caller_does_not_see_other_repositories_schedules",
    ),

    # -- step 7: automations -----------------------------------------------
    (
        # The highest-consequence line in the module. `load` constructs
        # arbitrary Python from tags, in a field a user types YAML into.
        # The payload run here is `true`, deliberately: the break has to
        # actually execute for the case to prove anything.
        "AUTO-001",
        "YAML is parsed with safe_load, never load",
        "datum_sync/automations.py",
        "        doc = yaml.safe_load(text)",
        "        doc = yaml.load(text, Loader=yaml.UnsafeLoader)",
        "tests/test_automations.py::test_a_python_object_tag_is_not_constructed",
    ),
    (
        # Not deleting the function -- moving it back out of `parse` to where
        # it started, as a step each writer had to remember. `create` no longer
        # calls it, so an automation that submits jobs forever is stored.
        "AUTO-002",
        "the self-trigger check is inside parse, not beside it",
        "datum_sync/automations.py",
        "    _reject_self_trigger(config)\n    return config",
        "    return config",
        "tests/test_automations.py::test_an_automation_that_triggers_on_what_it_runs_is_refused",
    ),
    (
        # A template naming something outside the namespace would otherwise
        # render as the empty string, forever, silently.
        "AUTO-003",
        "a placeholder is checked when the automation is written",
        "datum_sync/automations.py",
        "        if name.startswith(\"job.\") and name[4:] in _JOB_FIELDS:\n"
        "            continue",
        "        if name.startswith(\"job.\"):\n"
        "            continue",
        "tests/test_automations.py::test_a_placeholder_naming_nothing_is_refused_where_it_was_typed",
    ),
    (
        "AUTO-004",
        "the server refuses to fetch its own network",
        "datum_sync/automations.py",
        "        if not address.is_global or address.is_multicast:",
        "        if False:",
        "tests/test_automations.py::test_the_server_refuses_to_fetch_its_own_network",
    ),
    (
        # The plausible version, and the one everybody writes: let httpx follow
        # the redirects. Only the first URL was ever validated, so a webhook
        # that 302s to 169.254.169.254 walks straight through.
        "AUTO-005",
        "every redirect hop is checked, not just the first",
        "datum_sync/automations.py",
        "    async with httpx.AsyncClient(follow_redirects=False,",
        "    async with httpx.AsyncClient(follow_redirects=True,",
        "tests/test_automations.py::test_every_redirect_hop_is_checked_not_just_the_first",
    ),
    (
        # Deploying an automation would deliver every run in the job history:
        # webhooks posted and jobs submitted for work that finished weeks ago.
        # This is not hypothetical -- it is what the first end-to-end run did.
        "AUTO-006",
        "an automation does not fire on jobs older than itself",
        "datum_sync/automations.py",
        "        \"WHERE enabled AND created_at <= $1\",\n        job[\"completed_at\"],",
        "        \"WHERE enabled AND $1 IS NOT NULL\",\n        job[\"completed_at\"],",
        "tests/test_automations.py::test_a_job_that_finished_before_the_automation_existed_is_not_delivered",
    ),
    (
        # The runtime half of the loop guard. Without it an automation whose
        # trigger names no workspace runs forever.
        "AUTO-007",
        "a job an automation caused does not re-fire it",
        "datum_sync/automations.py",
        "    return job[\"triggered_by\"] == f\"automation:{name}\"",
        "    return False",
        "tests/test_automations.py::test_a_job_this_automation_caused_does_not_re_fire_it",
    ),
    (
        # The claim moved out of `consider` and back into the caller's WHERE
        # clause -- where it started. `run_pending` still filters, so the
        # worker still behaves; only a direct second caller double-delivers.
        "AUTO-008",
        "consider claims the job itself",
        "datum_sync/automations.py",
        "        \"WHERE id = $1 AND automations_at IS NULL RETURNING id\",",
        "        \"WHERE id = $1 RETURNING id\",",
        "tests/test_automations.py::test_considering_is_recorded_so_the_next_poll_does_not_redeliver",
    ),
    (
        "AUTO-009",
        "repo scope on writing an automation",
        "datum_sync/api.py",
        "            _require_automation_scope(caller, automations.parse(text))\n"
        "            row = await automations.create(conn, text, created_by=caller.name)",
        "            row = await automations.create(conn, text, created_by=caller.name)",
        "tests/test_automations.py::test_a_scoped_caller_cannot_automate_another_repository",
    ),
    (
        # Naming no repository in a trigger is not "no repository", it is all
        # of them -- every job in the system, including its parameters.
        "AUTO-010",
        "an unfiltered trigger requires unfiltered scope",
        "datum_sync/api.py",
        "    if not caller.all_repos() and not caller.is_admin:",
        "    if False:",
        "tests/test_automations.py::test_a_scoped_caller_cannot_watch_every_repository",
    ),
    (
        # Check only the stored document and the check is bypassed by writing
        # something harmless and then editing it into something privileged.
        "AUTO-011",
        "an edit is checked against the new document too",
        "datum_sync/api.py",
        "            _require_automation_scope(caller, automations.parse(text))\n"
        "            row = await automations.replace(conn, automation_id, text)",
        "            row = await automations.replace(conn, automation_id, text)",
        "tests/test_automations.py::test_scope_is_checked_against_the_new_document_not_only_the_old",
    ),

    (
        # The dashboard counts jobs, and the count of jobs you may not see is
        # still a fact about them. Anchored on the summary's own early return,
        # so it cannot match the identical-looking block in the job list.
        "API-003",
        "repo scope on the dashboard's job counts",
        "datum_sync/api.py",
        "        if not caller.all_repos():\n"
        "            names = await conn.fetch(\"SELECT DISTINCT repository FROM jobs\")\n"
        "            allowed = [r[\"repository\"] for r in names"
        " if caller.allows_repo(r[\"repository\"])]\n"
        "            if not allowed:\n"
        "                return {\"counts\": dict.fromkeys(JOB_STATUSES, 0), \"total\": 0}\n",
        "",
        "tests/test_api.py::test_a_scoped_caller_is_not_told_how_busy_the_others_are",
    ),
    (
        # Removing this does not raise and does not deny anything. It makes an
        # unrestricted caller's scope compile to `= ANY(ARRAY['*'])`, which
        # matches a repository literally named `*` -- so the schedule and
        # automation lists come back empty and correct-looking.
        "AUTHZ-003",
        "the repo-scope wildcard never reaches SQL as a literal name",
        "datum_sync/api.py",
        "    if caller.all_repos():\n        return \"\", []",
        "    if False:\n        return \"\", []",
        "tests/test_auth.py::test_the_wildcard_is_not_passed_to_sql_as_a_repository_name",
    ),

    # -- connections -------------------------------------------------------
    (
        # The whole reason `secret` is a separate column rather than a key in
        # `config`: no read path can emit it, because no read path selects it.
        # Put it back in _COLUMNS and every response carries the ciphertext --
        # not the plaintext, but the thing an offline attack is run against.
        "CONN-001",
        "the secret column is outside every read path",
        "datum_sync/connections.py",
        "    (secret IS NOT NULL) AS has_secret",
        "    secret, (secret IS NOT NULL) AS has_secret",
        "tests/test_connections.py::test_no_read_path_returns_the_secret",
    ),
    (
        # Without the name as associated data, a sealed value is portable: one
        # UPDATE moves a tier-4 production password onto a connection anyone
        # can resolve, and every read still succeeds. GCM authenticates the
        # ciphertext, not where it was stored.
        "CONN-002",
        "a sealed secret is bound to its connection name",
        "datum_sync/crypto.py",
        "    return name.encode()",
        "    return b\"datum-sync\"",
        "tests/test_connections.py::test_a_secret_sealed_for_one_connection_will_not_open_for_another",
    ),
    (
        # `config` is returned by the API and rendered in the UI. A password
        # accepted there is a password on screen, and no amount of care in the
        # secret path undoes it.
        "CONN-003",
        "a credential in the readable half is refused",
        "datum_sync/connections.py",
        "    leaked = sorted(set(config) & _FORBIDDEN_IN_CONFIG)",
        "    leaked = []",
        "tests/test_connections.py::test_a_credential_in_config_is_refused",
    ),
    (
        # Scope is the only thing standing between a workspace and every
        # credential in the system. Resolution runs in the worker, which is
        # already past authentication -- there is no second check behind this.
        "CONN-004",
        "scope is enforced at resolution, not only at display",
        "datum_sync/connections.py",
        "        if not matches_scope(row, repository, workspace):",
        "        if False:",
        "tests/test_connections.py::test_scope_decides_who_can_resolve",
    ),
    (
        # asyncpg hands back jsonb as JSON *text*. Carry it through json.dumps
        # and a patch that only touches `description` stores a JSON string
        # where the config object was -- silently, until something reads it.
        # This is the step-7 schedules bug, which 251 tests could not see.
        "CONN-005",
        "jsonb is decoded before it is merged and rewritten",
        "datum_sync/connections.py",
        "    merged[\"config\"] = json.loads(current[\"config\"])",
        "    merged[\"config\"] = current[\"config\"]",
        "tests/test_connections.py::test_a_patch_that_does_not_mention_config_leaves_it_an_object",
    ),
    (
        # Reads are open on purpose -- `config` is what a workspace author needs
        # to declare a connection. Writes are not: creating one is handing the
        # server a credential to hold and deciding who may reach it.
        "CONN-006",
        "creating a connection is admin-only",
        "datum_sync/api.py",
        "async def create_connection(\n"
        "    body: dict[str, Any] = Body(...), caller: Principal = Caller\n"
        ") -> dict[str, Any]:\n"
        "    auth.require_admin(caller)",
        "async def create_connection(\n"
        "    body: dict[str, Any] = Body(...), caller: Principal = Caller\n"
        ") -> dict[str, Any]:",
        "tests/test_connections.py::test_writes_are_admin_only_and_reads_are_not",
    ),
    (
        # Test is read-shaped but makes the server dial out to whatever host the
        # config names, with the stored credential, on request. That is a probe
        # anyone authenticated could aim, so it sits with the writes.
        "CONN-007",
        "testing a connection is admin-only",
        "datum_sync/api.py",
        "    auth.require_admin(caller)\n"
        "    async with db.pool().acquire() as conn:\n"
        "        try:\n"
        "            return await connections.test(conn, name)",
        "    async with db.pool().acquire() as conn:\n"
        "        try:\n"
        "            return await connections.test(conn, name)",
        "tests/test_connections.py::test_writes_are_admin_only_and_reads_are_not",
    ),

    # -- development mode --------------------------------------------------
    (
        # Without the loopback requirement, DATUM_SYNC_AUTH=off in a copied .env
        # publishes the admin API and the connection store to the whole network,
        # and nothing anywhere says so except a line in the log.
        "AUTH-027",
        "an unauthenticated server must be unreachable from other machines",
        "datum_sync/config.py",
        "    if HOST not in _LOOPBACK_HOSTS:",
        "    if False:",
        "tests/test_auth.py::test_a_reachable_server_refuses_to_start_without_authentication",
    ),
    (
        # The load-bearing half. The startup check reads config.HOST, which
        # `uvicorn --host 0.0.0.0` never consults; this runs on the socket's own
        # peer address, so remove it and an auth-off server answers the network
        # even though it refused to start on one.
        "AUTH-028",
        "an auth-off server answers nobody but its own machine",
        "datum_sync/api.py",
        "        if not config.is_loopback_client(request.client):",
        "        if False:",
        "tests/test_auth.py::test_a_remote_caller_is_refused_even_with_auth_off",
    ),
    (
        # Strip the IPv4-mapped prefix without re-checking and ::ffff:8.8.8.8
        # reads as loopback.
        "AUTH-029",
        "a mapped public address is not loopback",
        "datum_sync/config.py",
        "    return host in _LOOPBACK_HOSTS or host.startswith(\"127.\")",
        "    return True",
        "tests/test_auth.py::test_a_remote_peer_is_not_local",
    ),
    (
        # Read as a general truthiness test, DATUM_SYNC_AUTH=false disables
        # authentication. The strictness is the guard.
        "AUTH-030",
        "only the word off disables authentication",
        "datum_sync/config.py",
        "    return (raw or \"\").strip().lower() == \"off\"",
        "    return not (raw or \"on\").strip().lower() in (\"on\", \"1\", \"true\")",
        "tests/test_auth.py::test_anything_but_the_word_off_leaves_authentication_on",
    ),

    # -- step 9: the publish gate --------------------------------------------
    (
        # The step-8 deferral. Without it any account that can publish can hand
        # a tier-4 credential to everyone who can submit a job.
        "PUBLISH-001",
        "the publisher's max_tier bounds what it can publish",
        "datum_sync/publish.py",
        "        if row[\"tier\"] > publisher.max_tier:",
        "        if False:",
        "tests/test_publish.py::test_the_publisher_tier_decides",
    ),
    (
        "PUBLISH-002",
        "a workspace cannot publish against a connection scoped elsewhere",
        "datum_sync/publish.py",
        "        if not connections.matches_scope(row, repository, manifest.name):",
        "        if False:",
        "tests/test_publish.py::"
        "test_a_connection_out_of_scope_is_refused_at_publish_not_at_run",
    ),
    (
        "PUBLISH-003",
        "declared write access must match what is stored",
        "datum_sync/publish.py",
        "        if ref.access == \"write\" and row[\"access\"] != \"write\":",
        "        if False:",
        "tests/test_publish.py::"
        "test_declaring_write_on_a_read_only_connection_is_refused",
    ),
    (
        # Presence-only: the file exists, the headings are there, nothing is
        # under them. Exactly what a required-file rule produces.
        "PUBLISH-004",
        "a section with no content is not a documented section",
        "datum_sync/publish.py",
        "    blank = [s for s in REQUIRED_SECTIONS if _is_empty(lowered[s.lower()])]",
        "    blank = []",
        "tests/test_publish.py::"
        "test_the_five_headings_with_nothing_under_them_do_not_pass",
    ),
    (
        # Always-on, and "the smoke test passed" starts meaning "the workspace
        # ran for real" for every workspace that has no --smoke handler.
        "PUBLISH-005",
        "the smoke test runs only when the manifest opts in",
        "datum_sync/publish.py",
        "    if smoke and manifest.smoke_test:",
        "    if smoke:",
        "tests/test_publish.py::test_the_smoke_test_only_runs_when_the_manifest_asks",
    ),
    (
        # Reordered, a workspace with no MANIFEST.md still costs a process
        # launch -- and runs arbitrary code before anything has vouched for it.
        "PUBLISH-006",
        "the free checks run before the one that spawns a process",
        "datum_sync/publish.py",
        "    check_docs(ws_path)\n"
        "    await check_services(conn, manifest, repository)\n"
        "    await check_connections(conn, manifest, repository, publisher)\n"
        "    if smoke and manifest.smoke_test:\n"
        "        await run_smoke(ws_path)",
        "    if smoke and manifest.smoke_test:\n"
        "        await run_smoke(ws_path)\n"
        "    check_docs(ws_path)\n"
        "    await check_services(conn, manifest, repository)\n"
        "    await check_connections(conn, manifest, repository, publisher)",
        "tests/test_publish.py::test_the_cheap_checks_run_before_the_expensive_one",
    ),
    (
        # Report the failure but keep the workspace in `loaded`, and _upsert
        # writes it anyway: the gate becomes a warning.
        "PUBLISH-007",
        "failing the gate drops the workspace from what gets written",
        "datum_sync/repository.py",
        "    report.loaded = passed",
        "    report.loaded = report.loaded",
        "tests/test_publish.py::"
        "test_failing_the_gate_leaves_the_published_version_alone",
    ),
    (
        # Stale computed from what loaded rather than what is on disk. A
        # manifest typo plus --prune then deregisters a working workspace.
        "PUBLISH-008",
        "stale means removed from disk, not failed to load",
        "datum_sync/repository.py",
        "    on_disk = _on_disk(root)",
        "    on_disk = set()",
        "tests/test_publish.py::test_a_workspace_that_fails_the_gate_is_not_stale",
    ),

    # -- hosted services ----------------------------------------------------

    (
        # The classic. /data/app and /data/app-secrets share a prefix as
        # strings and share no directory as paths.
        "SERVICE-001",
        "served root containment uses is_relative_to, not startswith",
        "datum_sync/services.py",
        "    return target == root or target.is_relative_to(root)",
        "    return str(target).startswith(str(root))",
        "tests/test_services.py::test_resolve_refuses_a_sibling_that_shares_a_prefix",
    ),
    (
        "SERVICE-002",
        "/serve/ refuses a path that escapes the service root",
        "datum_sync/services.py",
        "    if not _within(root, target):\n"
        "        raise ServiceError(\"path escapes the service root\")\n",
        "",
        "tests/test_services.py::test_serve_refuses_traversal",
    ),
    (
        # A registered supervised row can only exist if the publish gate was
        # bypassed -- which is exactly when this has to hold.
        "SERVICE-003",
        "resolve() refuses a service type this server does not run",
        "datum_sync/services.py",
        "    if row[\"type\"] in SUPERVISED:",
        "    if False:",
        "tests/test_services.py::test_resolve_refuses_a_supervised_service",
    ),
    (
        # A containment bug at write time is a containment bug on every read.
        "SERVICE-004",
        "registration refuses a served root outside the job's artifacts",
        "datum_sync/services.py",
        "    if root not in path.parents:\n"
        "        raise ServiceError(f\"{filename!r} resolves outside the job's artifacts\")\n",
        "",
        "tests/test_services.py::test_register_refuses_a_name_that_escapes_the_job",
    ),
    (
        "SERVICE-005",
        "registration refuses a served root that is not a directory",
        "datum_sync/services.py",
        "    if not path.is_dir():\n"
        "        raise ServiceError(f\"{filename!r} is not a directory\")\n",
        "",
        "tests/test_services.py::test_register_refuses_a_file",
    ),
    (
        # Without the WHERE, the later job simply takes the URL: the first
        # workspace's site is replaced by the second's and nothing says so.
        "SERVICE-006",
        "one workspace cannot upsert over another's service name",
        "datum_sync/services.py",
        "            WHERE hosted_services.repository = EXCLUDED.repository\n"
        "              AND hosted_services.workspace = EXCLUDED.workspace\n",
        "",
        "tests/test_services.py::test_another_workspace_cannot_take_the_url",
    ),
    (
        # The WHERE still holds, so the URL is not stolen -- but the job
        # reports success while its service went nowhere, and nothing says so.
        "SERVICE-007",
        "a refused service registration is raised, not passed over quietly",
        "datum_sync/services.py",
        "        if claimed is None:",
        "        if False:",
        "tests/test_services.py::test_another_workspace_cannot_take_the_url",
    ),
    (
        "SERVICE-008",
        "a service/* output must be a directory",
        "datum_sync/runner.py",
        "            if is_service and not is_dir:",
        "            if False:",
        "tests/test_services.py::test_a_service_output_returning_a_file_fails",
    ),
    (
        # Allowed through, this is a 500 on the download of a job that
        # reported success.
        "SERVICE-009",
        "a directory under an ordinary output is refused at the run",
        "datum_sync/runner.py",
        "            if is_dir and not is_service:",
        "            if False:",
        "tests/test_services.py::test_a_directory_under_a_non_service_output_fails",
    ),
    (
        "SERVICE-010",
        "the gate refuses a service type this server cannot run",
        "datum_sync/publish.py",
        "        if out.type in services.SUPERVISED:",
        "        if False:",
        "tests/test_services.py::test_gate_refuses_a_supervised_output",
    ),
    (
        "SERVICE-011",
        "the gate refuses a service name another workspace already serves",
        "datum_sync/publish.py",
        "        owner = await conn.fetchrow(\n"
        "            \"SELECT repository, workspace FROM hosted_services WHERE name = $1\",\n"
        "            out.name,\n"
        "        )",
        "        owner = None",
        "tests/test_services.py::test_gate_refuses_a_name_another_workspace_serves",
    ),
    (
        # The URL carries no repository, so without this a scoped caller reads
        # another repository's site through a name that gives no hint of it.
        "SERVICE-012",
        "/serve/ checks the scope of the repository that owns the service",
        "datum_sync/api.py",
        # Anchored on the line above it: `require_repo(caller, row["repository"])`
        # appears three times in this file, and a pattern matching all three
        # would break three guards at once and prove none of them.
        "        raise ApiError(404, \"NOT_FOUND\", f\"no hosted service named {name!r}\")\n"
        "    auth.require_repo(caller, row[\"repository\"])\n",
        "        raise ApiError(404, \"NOT_FOUND\", f\"no hosted service named {name!r}\")\n",
        "tests/test_services.py::test_serve_checks_the_owning_repositorys_scope",
    ),
    (
        "SERVICE-013",
        "the service listing is filtered by repository scope",
        "datum_sync/api.py",
        # Anchored on the two lines above the comprehension, not on the filter
        # alone. The published-workspace listing applies a character-identical
        # filter, so the short anchor came to match twice once that listing was
        # added -- and a case matching twice is refused, which had this harness
        # exiting 1 on a case that was never wrong about anything.
        "                \"source_job\": str(r[\"source_job\"]) if r[\"source_job\"] else None,\n"
        "                \"updated_at\": r[\"updated_at\"].isoformat(),\n"
        "            }\n"
        "            for r in rows\n"
        "            if caller.allows_repo(r[\"repository\"])",
        "                \"source_job\": str(r[\"source_job\"]) if r[\"source_job\"] else None,\n"
        "                \"updated_at\": r[\"updated_at\"].isoformat(),\n"
        "            }\n"
        "            for r in rows",
        "tests/test_services.py::test_listing_is_filtered_by_scope",
    ),
    (
        # A built site is built from a repository's data. Public because static
        # files feel harmless publishes whatever the last job wrote.
        "SERVICE-014",
        "/serve/ is not public",
        "datum_sync/api.py",
        "PUBLIC_PREFIXES = (\"/ui/static/\", \"/v2/\")",
        "PUBLIC_PREFIXES = (\"/ui/static/\", \"/v2/\", \"/serve/\")",
        "tests/test_services.py::test_serve_needs_a_credential",
    ),
    (
        # FileResponse on a directory is a 500 at the transport layer, and
        # artifact_path's 404 is true but useless to someone looking at a job
        # that plainly produced the thing.
        "SERVICE-015",
        "a service artifact is refused by the file-download route",
        "datum_sync/api.py",
        "    if services.is_service(artifact[\"type\"]):",
        "    if False:",
        "tests/test_services.py::test_a_service_artifact_is_not_downloadable",
    ),
    (
        # Not a leak -- the opposite. The v2 assets are fetched by a page
        # nobody has signed in to yet, so gating them means an unstyled,
        # inert sign-in form, and the only symptom is in the console.
        "UI-005",
        "the v2 assets are public",
        "datum_sync/api.py",
        "PUBLIC_PREFIXES = (\"/ui/static/\", \"/v2/\")",
        "PUBLIC_PREFIXES = (\"/ui/static/\",)",
        "tests/test_ui.py::test_every_asset_the_v2_shell_names_is_served",
    ),
    (
        "UI-006",
        "the v2 shell is public",
        "datum_sync/api.py",
        "        \"/ui/v2\",\n",
        "",
        "tests/test_ui.py::test_the_v2_shell_is_served_without_a_credential",
    ),
    # The five below are not backend guards -- they are properties of static
    # files. They are here rather than trusted because every one of them fails
    # SILENTLY in a browser: a blank page, a slightly wrong glyph, or a button
    # that does nothing. None raises, so none reaches a log, and a test that
    # has never been seen to fail is the wrong instrument to rely on for a
    # class of bug whose entire signature is the absence of a symptom.
    (
        # Without type="module" the browser parses app.js as a classic script,
        # throws on the import before executing a line, and leaves both panes
        # hidden -- the same blank page as having no app.js at all.
        "UI-007",
        "the v2 shell loads app.js as a module",
        "datum_sync/static-v2/index.html",
        "<script type=\"module\" src=\"/v2/app.js\"></script>",
        "<script src=\"/v2/app.js\"></script>",
        "tests/test_ui.py::test_the_v2_shell_loads_app_js_as_a_module",
    ),
    (
        # A 404 on a module import aborts the whole module. The shell's script
        # tags do not name icons.js, so a scan of the markup cannot see it.
        "UI-008",
        "every asset the v2 shell reaches for is served, imports included",
        "datum_sync/static-v2/app.js",
        "from '/v2/icons.js'",
        "from '/v2/icon.js'",
        "tests/test_ui.py::test_every_asset_the_v2_shell_names_is_served",
    ),
    (
        # app.js lifts the `d` out of each icon. A second element would be
        # dropped and the icon would still render, slightly wrong, forever.
        "UI-009",
        "every v2 icon is a single path",
        "datum_sync/static-v2/icons.js",
        "\"columns\": \"<path d=",
        "\"columns\": \"<circle cx=\\\\\"1\\\\\"/><path d=",
        "tests/test_ui.py::test_every_v2_icon_is_a_single_path",
    ),
    (
        # icons.js exports an icon() that assigns svg.innerHTML. Reaching for
        # it is a one-line change that reads as the obvious thing to do.
        "UI-010",
        "the v2 UI never assigns markup",
        "datum_sync/static-v2/app.js",
        "        svg.append(path);",
        "        svg.innerHTML = ICONS[name];",
        "tests/test_ui.py::test_the_v2_ui_never_assigns_markup",
    ),
    (
        # nav-collapse.js already binds #nav-toggle. A second handler makes the
        # button flip the state and flip it back: a nav that does not move.
        "UI-011",
        "the v2 UI leaves the nav toggle to nav-collapse.js",
        "datum_sync/static-v2/app.js",
        "// No handler for #nav-toggle here.",
        "$('nav-toggle').addEventListener('click', () => 0);",
        "tests/test_ui.py::test_the_v2_ui_leaves_the_nav_toggle_to_nav_collapse_js",
    ),
    # Chunk 2 added two more of the same kind. icon() appends a <path> only if
    # it found one, and route() renders "Not found" rather than throwing, so
    # both of these leave a working page with something quietly absent from it.
    (
        # A glyph name no icon answers to draws a correctly sized, empty <svg>.
        "UI-012",
        "every v2 icon app.js asks for exists",
        "datum_sync/static-v2/app.js",
        "    complete: 'ok',",
        "    complete: 'okay',",
        "tests/test_ui.py::test_every_v2_icon_app_js_asks_for_exists",
    ),
    (
        # The dashboard mockup's first create tile points at #/run, which is
        # not a section. Copied faithfully it is a dead link on the first
        # screen anybody sees, and it looks live until it is pressed.
        "UI-013",
        "every v2 hash link names a screen that exists",
        "datum_sync/static-v2/app.js",
        "['Run Workspace', 'repositories', '#/repositories'],",
        "['Run Workspace', 'repositories', '#/run'],",
        "tests/test_ui.py::test_every_v2_hash_link_names_a_screen_that_exists",
    ),
    (
        # Same empty <svg> as above, reached the other way: this glyph is an
        # argument to cellName(), so it names no icon() call site and the four
        # sources the guard read before chunk 3 all miss it.
        "UI-014",
        "a list row's glyph is checked too",
        "datum_sync/static-v2/app.js",
        "cellName('repositories',",
        "cellName('repository',",
        "tests/test_ui.py::test_every_v2_icon_app_js_asks_for_exists",
    ),
    (
        # table.js assigns disabled from the selection count over the whole
        # action bar, so data-needs on a button with no handler is a control
        # that greys correctly, wakes on the first tick, and does nothing.
        "UI-015",
        "no v2 toolbar button is woken up with nothing behind it",
        "datum_sync/static-v2/app.js",
        "action('Edit', null, { off: true,",
        "action('Edit', null, { needs: 'one',",
        "tests/test_ui.py::test_no_v2_toolbar_button_is_woken_up_with_nothing_behind_it",
    ),
    (
        # The other direction: action() drops the handler on the `off` path,
        # so wiring one up without clearing the flag is a button that looks
        # deliberate and is silently inert.
        "UI-016",
        "an off button that was handed a handler",
        "datum_sync/static-v2/app.js",
        "action('Upload', null, { off: true,",
        "action('Upload', doUpload, { off: true,",
        "tests/test_ui.py::test_no_v2_toolbar_button_is_woken_up_with_nothing_behind_it",
    ),
    (
        # Without it the browser's own submit runs as well: a GET on the
        # current URL, which in an SPA is a full reload that tears down the
        # fetch the handler just started. Whether the POST lands is a race, so
        # the Run button either runs the workspace or does nothing, and both
        # outcomes look like a page that merely refreshed.
        #
        # Only chunk 4's handler is removed -- the sign-in one at the top of
        # the file is indented differently and stays -- so the test's `>= 2`
        # sanity check still holds and it fails on the missing call rather than
        # on a parser that found nothing to check.
        #
        # The next line is part of the anchor, and has to be. The bare
        # `event.preventDefault();` at this indent was unique when chunk 4
        # wrote it and stopped being unique the moment chunk 6 added two more
        # forms: the harness then skipped this case for matching three times,
        # so ordinary feature work disarmed a guard nobody had touched. The
        # following line names the submit button, which differs per form.
        "UI-017",
        "a v2 submit handler that lets the browser navigate",
        "datum_sync/static-v2/app.js",
        "        event.preventDefault();\n        run.disabled = true;\n",
        "        run.disabled = true;\n",
        "tests/test_ui.py::test_every_v2_submit_handler_stops_the_browser_submitting",
    ),
    (
        # v1's jobs list repolled straight into route() and lost nothing by it,
        # because v1's list had no selection. Chunk 5 gave it tick boxes and a
        # bulk Cancel, and the same timer now rebuilds the table -- and with it
        # a fresh, empty `selected` -- every four seconds. The break restores
        # exactly v1's line, which is why it is worth testing: it is not a typo
        # anyone would write, it is the code that was correct until the screen
        # around it changed.
        "UI-018",
        "a live list that repolls over a selection",
        "datum_sync/static-v2/app.js",
        "            if (handle && handle.selection().length) {\n"
        "                timer = setTimeout(poll, 4000);\n"
        "                return;\n"
        "            }\n"
        "            route();\n",
        "            route();\n",
        "tests/test_ui.py::test_no_v2_list_repolls_itself_out_from_under_a_selection",
    ),
    (
        # The break is not invented: it is what chunk 6 first wrote, restored
        # verbatim. `.form-actions` sounds like a class this design system
        # would own, renders without error, and leaves the footer unstyled --
        # which reads as a deliberately plain div rather than a mistake.
        "UI-019",
        "a form footer styled by a class that does not exist",
        "datum_sync/static-v2/app.js",
        "        el('div', { class: 'action-bar' },\n"
        "            el('div', { class: 'actions' },\n"
        "                save,\n"
        "                action('Cancel', () => go('#/schedules')))),\n"
        "        status);\n",
        "        el('div', { class: 'form-actions' }, save),\n"
        "        status);\n",
        "tests/test_ui.py::test_v2_invents_no_css_classes",
    ),
    (
        # Also not invented: this is what chunk 7 shipped to the browser, and
        # the screenshot of it printed a blue "null" under the automation's
        # title. The suite, and the browser script's twelve assertions about
        # that same screen, all passed.
        "UI-020",
        "a conditional child handed to the DOM's append rather than ours",
        "datum_sync/static-v2/app.js",
        "    append(view, [\n"
        "        crumbs(['Automations', '#/automations'], [a.name]),\n",
        "    view.append(\n"
        "        crumbs(['Automations', '#/automations'], [a.name]),\n",
        "tests/test_ui.py::test_v2_never_appends_a_child_that_can_be_nothing",
    ),
    (
        "UI-021",
        "the label for one of the server's action types",
        "datum_sync/static-v2/app.js",
        "    http_request: 'webhook',\n",
        "",
        "tests/test_ui.py::test_v2_labels_every_automation_action",
    ),
    (
        # Exactly what v1's connection form is: nine labels with no `for`. The
        # page renders identically -- same text, same position, same styling --
        # and the only difference is that clicking the word does nothing and a
        # screen reader reads the control out unnamed.
        #
        # Anchored on `hint instanceof Node`, which is chunk 8's line and
        # nobody else's. The label line alone is not unique: chunk 6 has two
        # fieldOf helpers of its own, and an anchor matching three call sites
        # would weaken all three and prove none.
        "UI-022",
        "the `for` on a v2 form label",
        "datum_sync/static-v2/app.js",
        "        el('label', { for: id }, label), input,\n"
        "        hint instanceof Node",
        "        el('label', {}, label), input,\n"
        "        hint instanceof Node",
        "tests/test_ui.py::test_every_v2_form_label_names_its_control",
    ),
    (
        # The other half, and the more likely one: the control is renamed and
        # the call site is not. The `for` is still there, still looks right in
        # the source, and points at nothing -- so the field behaves exactly as
        # if the attribute had been deleted.
        "UI-023",
        "the agreement between a control's id and the label pointing at it",
        "datum_sync/static-v2/app.js",
        "class: 'yaml', id: 'conn-secret', spellcheck: 'false', rows: 5,",
        "class: 'yaml', id: 'conn-password', spellcheck: 'false', rows: 5,",
        "tests/test_ui.py::test_every_v2_form_label_names_its_control",
    ),
    (
        # v1 writes this exact link with no rel at all, so the break is the
        # port done faithfully rather than a mistake somebody would have to
        # make. It opens correctly either way, in every browser in use today --
        # which is the whole reason it needs a test and not a review.
        "UI-024",
        "the noopener on the link out to a hosted service",
        "datum_sync/static-v2/app.js",
        "href: s.url, target: '_blank', rel: 'noopener',",
        "href: s.url, target: '_blank',",
        "tests/test_ui.py::test_every_v2_new_window_link_disowns_its_opener",
    ),
    (
        # The state this was in for nine chunks, with a comment in buildNav
        # asserting the opposite.
        "UI-025",
        "the router's refusal of the admin sections",
        "datum_sync/static-v2/app.js",
        "const denied = !me.is_admin && SECTIONS.some((s) => s.id === section && s.adminOnly);",
        "const denied = false;",
        "tests/test_ui.py::"
        "test_the_v2_admin_group_is_refused_by_the_router_not_just_hidden",
    ),
    (
        # Refusing the right section for the wrong reason: this stops the one
        # screen anybody thinks of and leaves the four stubs beside it open,
        # which is the version that would survive a review.
        "UI-026",
        "reading the admin sections off SECTIONS rather than naming one",
        "datum_sync/static-v2/app.js",
        "const denied = !me.is_admin && SECTIONS.some((s) => s.id === section && s.adminOnly);",
        "const denied = !me.is_admin && section === 'admin';",
        "tests/test_ui.py::"
        "test_the_v2_admin_group_is_refused_by_the_router_not_just_hidden",
    ),
    (
        # Not the original bug -- that was the block order, and this is the way
        # back into it: one more .danger rule at the foot of the file, which is
        # where a variant gets added when the block it belongs in is 400 lines
        # up. Same cascade, same invisible result.
        "UI-027",
        "the disabled rule outranking the button variants",
        "datum_sync/static-v2/style.css",
        "    filter: none;\n}\n",
        "    filter: none;\n}\n"
        "button.danger    { color: var(--failed); }\n",
        "tests/test_ui.py::"
        "test_the_v2_disabled_button_rule_outranks_every_button_variant",
    ),

    # -- credential proxy guards ------------------------------------------------

    (
        "PROXY-001",
        "the proxy requires an agent token, not an account token",
        "datum_sync/proxy.py",
        "    if principal.agent_id is None:\n"
        "        raise ApiError(\n"
        "            403, \"AGENT_REQUIRED\",\n"
        "            \"proxy_request requires an agent token, not an account token\",\n"
        "        )",
        "    if False:\n"
        "        raise ApiError(\n"
        "            403, \"AGENT_REQUIRED\",\n"
        "            \"proxy_request requires an agent token, not an account token\",\n"
        "        )",
        "tests/test_proxy.py::test_bare_account_token_denied",
    ),
    (
        "PROXY-002",
        "the proxy checks the agent's grant list",
        "datum_sync/proxy.py",
        "    if conn_name not in (principal.proxy_grants or []):",
        "    if False:",
        "tests/test_proxy.py::test_agent_without_grant_denied",
    ),
    (
        "PROXY-003",
        "the proxy enforces the account's tier ceiling",
        "datum_sync/proxy.py",
        "    if principal.max_tier < conn_row[\"tier\"]:",
        "    if False:",
        "tests/test_proxy.py::test_tier_denied",
    ),
    (
        "PROXY-004",
        "the SSRF guard blocks private and loopback addresses",
        "datum_sync/proxy.py",
        "        addr.is_private\n"
        "        or addr.is_loopback",
        "        False\n"
        "        or False",
        "tests/test_proxy.py::test_ssrf_rejects_loopback",
    ),
    (
        "AGENT-001",
        "agents are never admin regardless of account",
        "datum_sync/auth.py",
        "            is_admin=False,  # agents are never admin",
        "            is_admin=agent_row[\"is_admin\"],  # agents are never admin",
        "tests/test_agents.py::test_agent_token_resolves_to_principal",
    ),
    (
        "PROXY-005",
        "the proxy refuses non-http connection types",
        "datum_sync/proxy.py",
        "        if row[\"type\"] != \"http\":",
        "        if False:",
        "tests/test_proxy.py::test_proxy_rejects_non_http_connection",
    ),
    (
        "PROXY-006",
        "the proxy still writes proxy_log alongside the new audit_log",
        "datum_sync/proxy.py",
        "            INSERT INTO proxy_log\n",
        "            INSERT INTO proxy_log_retired\n",
        "tests/test_proxy.py::test_audit_log_written",
    ),

    # -- audit spine guards -----------------------------------------------------
    #
    # PROXY-006's break points the insert at a table that does not exist. It is
    # wrapped in `try/except: pass`, so nothing is raised and nothing is written
    # -- which is precisely why its test asserts on the proxy_log row as well as
    # the audit_log one. Adding the second writer is exactly the kind of change
    # that quietly costs you the first.

    (
        "AUDIT-001",
        "every dispatchable method maps to a dotted audit verb",
        "datum_sync/mcp.py",
        "    return \"mcp.\" + method.replace(\"/\", \".\")",
        "    return \"mcp.\" + method",
        "tests/test_audit_trace.py::test_verb_covers_every_dispatchable_method",
    ),
    (
        "AUDIT-002",
        "each mint is a fresh id, not a function of the client's header",
        "datum_sync/audit.py",
        "        return cls(id=str(uuid.uuid4()), client_id=client_id)",
        "        return cls(id=client_id or str(uuid.uuid4()), client_id=client_id)",
        "tests/test_audit_trace.py::test_minting_gives_a_different_id_every_time",
    ),
    (
        "AUDIT-003",
        "the proxy row carries the trace of the request that caused it",
        "datum_sync/proxy.py",
        "    await audit.write(\n        conn,\n        trace=trace,",
        "    await audit.write(\n        conn,\n        trace=audit.Trace.mint(None),",
        "tests/test_audit_trace.py::"
        "test_one_trace_covers_the_mcp_row_and_the_proxy_row",
    ),
    (
        "AUDIT-004",
        "a repeated X-Trace-Id does not merge two requests into one trace",
        # Followed the mint when it moved out of the /mcp endpoint into the
        # `trace_and_audit` middleware. The line is identical; only the file it
        # lives in changed, which is exactly the drift a fixed anchor catches.
        "datum_sync/api.py",
        "    trace = audit.Trace.mint(request.headers.get(\"X-Trace-Id\") or None)",
        # Derives the trace from the client's header while still producing a
        # real UUID, so the insert succeeds and the test fails on the grouping.
        # A break that made the id un-insertable would fail the same test for
        # the wrong reason, proving nothing about whether the header is
        # authoritative -- the failure has to be the merge itself.
        "    _ct = request.headers.get(\"X-Trace-Id\") or None\n"
        "    _uu = __import__(\"uuid\")\n"
        "    trace = audit.Trace(\n"
        "        id=str(_uu.uuid5(_uu.NAMESPACE_OID, _ct)) if _ct else str(_uu.uuid4()),\n"
        "        client_id=_ct,\n"
        "    )",
        "tests/test_audit_trace.py::"
        "test_a_repeated_client_trace_does_not_merge_two_requests",
    ),
    (
        "AUDIT-005",
        "a forged X-Trace-Id cannot splice a caller into another's trace",
        # See AUDIT-004: the mint moved to the middleware, so this follows it.
        "datum_sync/api.py",
        "    trace = audit.Trace.mint(request.headers.get(\"X-Trace-Id\") or None)",
        # The direct forgery: honour the header verbatim as the trace id. The
        # value that test sends is a real server-minted trace id, so this
        # inserts cleanly and the attacker's rows land in the victim's trace.
        "    _ct = request.headers.get(\"X-Trace-Id\") or None\n"
        "    trace = audit.Trace(\n"
        "        id=_ct or str(__import__(\"uuid\").uuid4()), client_id=_ct,\n"
        "    )",
        "tests/test_audit_trace.py::"
        "test_a_client_cannot_splice_itself_into_another_trace",
    ),
    (
        "AUDIT-006",
        "the audit row mirrors the mcp_call_log row it is written beside",
        "datum_sync/mcp.py",
        "                duration_ms=duration_ms,\n"
        "                governance=_is_governance(target),",
        "                duration_ms=None,\n"
        "                governance=_is_governance(target),",
        "tests/test_audit_trace.py::test_the_audit_row_agrees_with_the_mcp_call_log_row",
    ),
    (
        "AUDIT-008",
        "/health reports the audit drop count from the real counter",
        "datum_sync/api.py",
        "\"audit_dropped\": audit.dropped()",
        "\"audit_dropped\": 0",
        "tests/test_audit_trace.py::test_health_reports_the_audit_drop_count",
    ),
    (
        "AUDIT-009",
        "a failed audit write is counted, not silently swallowed",
        "datum_sync/audit.py",
        "    except Exception:\n        _dropped += 1",
        "    except Exception:\n        pass",
        "tests/test_audit_trace.py::test_a_failed_audit_write_is_counted_not_hidden",
    ),

    # -- vault_scope validation guards ------------------------------------------

    (
        "VAULT-001",
        "write-implies-read on vault_scope",
        "datum_sync/vault.py",
        "    missing = write_set - read_set\n"
        "    if missing:",
        "    missing = write_set - read_set\n"
        "    if False:",
        "tests/test_vault.py::test_write_implies_read",
    ),
    (
        "VAULT-002",
        "deny-disjoint-from-allow on vault_scope",
        "datum_sync/vault.py",
        "        overlap = deny_set & set(all_patterns.get(action, []))\n"
        "        if overlap:",
        "        overlap = deny_set & set(all_patterns.get(action, []))\n"
        "        if False:",
        "tests/test_vault.py::test_deny_contradicts_allow",
    ),
    (
        "VAULT-003",
        "traversal in vault_scope patterns",
        "datum_sync/vault.py",
        "        if seg == \"..\":\n"
        "            raise VaultScopeError(\n"
        "                f\"vault_scope.{key}: pattern {pattern!r} contains a traversal segment\"\n"
        "            )",
        "        if False:\n"
        "            raise VaultScopeError(\n"
        "                f\"vault_scope.{key}: pattern {pattern!r} contains a traversal segment\"\n"
        "            )",
        "tests/test_vault.py::test_traversal_in_pattern_rejected",
    ),
    (
        "VAULT-004",
        "star-confinement in vault_scope patterns",
        "datum_sync/vault.py",
        "        if \"*\" in seg and seg not in (\"*\", \"**\"):",
        "        if False:",
        "tests/test_vault.py::test_partial_wildcard_rejected",
    ),

    # -- vault filesystem guards -----------------------------------------------

    (
        "VAULTFS-001",
        "vault_read enforces scope via vault.check",
        "datum_sync/vault.py",
        "    if not permits(scope, action, normalised):\n"
        "        raise ApiError(\n"
        "            403,\n"
        "            \"FORBIDDEN\",\n"
        "            f\"this account may not {action} {normalised}\",\n"
        "            {\"path\": normalised, \"action\": action},\n"
        "        )",
        "    pass  # guard removed",
        "tests/test_vault_fs.py::test_read_outside_scope_is_403",
    ),
    (
        "VAULTFS-002",
        "vault_write enforces scope via vault.check",
        "datum_sync/vault.py",
        "    if not permits(scope, action, normalised):\n"
        "        raise ApiError(\n"
        "            403,\n"
        "            \"FORBIDDEN\",\n"
        "            f\"this account may not {action} {normalised}\",\n"
        "            {\"path\": normalised, \"action\": action},\n"
        "        )",
        "    pass  # guard removed",
        "tests/test_vault_fs.py::test_write_outside_scope_is_403",
    ),
    (
        "VAULTFS-003",
        "vault_list checks scope covers directory",
        "datum_sync/vault_fs.py",
        "    if not _scope_covers_dir(principal.vault_scope, normalised):\n"
        "        raise ApiError(\n"
        "            403, \"FORBIDDEN\",\n"
        "            f\"this account may not list {normalised}\",\n"
        "            {\"path\": normalised, \"action\": \"read\"},\n"
        "        )",
        "    pass  # guard removed",
        "tests/test_vault_fs.py::test_list_outside_scope_is_403",
    ),
    (
        "VAULTFS-004",
        "vault tools hidden from accounts without vault_scope",
        "datum_sync/mcp.py",
        "    if principal.vault_scope:\n"
        "        tools.extend(VAULT_TOOLS)",
        "    tools.extend(VAULT_TOOLS)",
        "tests/test_vault_fs.py::test_mcp_vault_tools_hidden_without_scope",
    ),

    # -- mcp_call_log guards ---------------------------------------------------

    (
        "MCPLOG-001",
        "governance flag set for skills/** writes",
        "datum_sync/mcp.py",
        '    return target in _GOVERNANCE_PATHS or any(\n'
        '        target.startswith(p) for p in _GOVERNANCE_PREFIXES\n'
        '    )',
        '    return False',
        "tests/test_mcp_call_log.py::test_log_governance_skill_write",
    ),
    (
        "MCPLOG-002",
        "vault_read target logged as vault path not None",
        "datum_sync/mcp.py",
        '    if tool_name in _VAULT_TOOL_NAMES:\n'
        '        return args.get("path")',
        '    if tool_name in _VAULT_TOOL_NAMES:\n'
        '        return None',
        "tests/test_mcp_call_log.py::test_log_vault_read",
    ),
    (
        "MCPLOG-003",
        "X-Trace-Id stored as client_trace_id",
        "datum_sync/mcp.py",
        # Re-anchored when the trace refactor replaced the `client_trace_id`
        # local with `Trace.client_id`. The anchor is the column write rather
        # than the header read: that is what the guard is actually about, and
        # the header read now also anchors AUDIT-004 and AUDIT-005.
        "                trace.client_id,",
        "                None,",
        "tests/test_mcp_call_log.py::test_log_trace_id",
    ),
    (
        "MCPLOG-004",
        "error outcome logged on RpcError",
        "datum_sync/mcp.py",
        '        await _log_call(\n'
        '            principal, method, tool_name_val, target,\n'
        '            "error", exc.code, duration_ms, trace,\n'
        '        )',
        '        pass  # log removed',
        "tests/test_mcp_call_log.py::test_log_error_outcome",
    ),
    (
        "TOKEN-001",
        "a revoked token is refused",
        "datum_sync/auth.py",
        '    if row["revoked_at"] is not None:\n'
        '        raise _unauthenticated("token has been revoked", "TOKEN_REVOKED")\n'
        '    if row["disabled"]:',
        '    if row["disabled"]:',
        "tests/test_account_tokens.py::"
        "test_revoking_one_token_leaves_the_others_working",
    ),
    (
        "TOKEN-002",
        "expiry is read from the token row, not the account",
        "datum_sync/auth.py",
        '    if row["expires_at"] is not None and row["expires_at"] < _now():\n'
        '        raise _unauthenticated("token has expired", "TOKEN_EXPIRED")\n'
        "\n"
        '    await tokens.mark_used(conn, row["token_id"])',
        '    await tokens.mark_used(conn, row["token_id"])',
        "tests/test_account_tokens.py::test_expiry_is_per_token_not_per_account",
    ),
    (
        "TOKEN-003",
        "revoke by label is scoped to one account",
        "datum_sync/tokens.py",
        "         WHERE account_id = $1 AND label = $2 AND revoked_at IS NULL",
        # $1 stays referenced so asyncpg still accepts two arguments -- the
        # break has to be the missing scope, not an arity error.
        "         WHERE label = $2 AND revoked_at IS NULL AND $1 IS NOT NULL",
        "tests/test_account_tokens.py::test_revoke_does_not_reach_across_accounts",
    ),
    (
        "TOKEN-004",
        "revoking an already-revoked token does not re-stamp the time",
        "datum_sync/tokens.py",
        "         WHERE account_id = $1 AND label = $2 AND revoked_at IS NULL",
        "         WHERE account_id = $1 AND label = $2",
        "tests/test_account_tokens.py::"
        "test_revoking_twice_does_not_move_the_revocation_time",
    ),
    (
        "TOKEN-005",
        "the pre-012 token_hash column is not a fallback read path",
        "datum_sync/auth.py",
        "    row = await tokens.resolve(conn, token_hash)\n"
        "    if row is None:\n"
        '        raise _unauthenticated("unknown or invalid token")',
        # The tempting migration-safety measure: if the new table does not know
        # this token, try the old column. Both hold the same hash after 012's
        # backfill, so this makes revoking a backfilled row a no-op that
        # reports success. Only test_account_tokens.py notices.
        "    row = await tokens.resolve(conn, token_hash)\n"
        "    if row is None:\n"
        "        row = await conn.fetchrow(\n"
        '            "SELECT sa.id AS account_id, sa.name, sa.max_tier, "\n'
        '            "sa.repo_scope, sa.is_admin, "\n'
        '            "sa.disabled, sa.vault_scope, NULL::int AS token_id, "\n'
        '            "NULL::timestamptz AS revoked_at, "\n'
        '            "sa.token_expires AS expires_at "\n'
        '            "FROM service_accounts sa WHERE sa.token_hash = $1",\n'
        "            token_hash,\n"
        "        )\n"
        "    if row is None:\n"
        '        raise _unauthenticated("unknown or invalid token")',
        "tests/test_account_tokens.py::test_the_legacy_column_is_not_a_credential",
    ),
    # -- B4: multi-key secrets (key-id byte) ----------------------------------
    (
        "SECRET-005",
        "seal prepends the current key id, not a hardcoded value",
        "datum_sync/crypto.py",
        "    return bytes([kid]) + nonce + AESGCM(key).encrypt(nonce, plaintext, _aad(name))",
        "    return bytes([0]) + nonce + AESGCM(key).encrypt(nonce, plaintext, _aad(name))",
        "tests/test_crypto_multikey.py::test_seal_prepends_key_id_byte",
    ),
    (
        "SECRET-006",
        "open_ selects the key from the blob's prefix byte, not the current key",
        "datum_sync/crypto.py",
        "    kid = blob[0]\n"
        "    keys = _load_keys()\n"
        "    if kid not in keys:",
        "    keys = _load_keys()\n"
        "    kid = _current_key_id()\n"
        "    if kid not in keys:",
        "tests/test_crypto_multikey.py::test_rotation_both_keys_loaded",
    ),
    (
        "SECRET-007",
        "open_ fails closed when the key id is unknown (removed after rotation)",
        "datum_sync/crypto.py",
        "    if kid not in keys:\n"
        "        raise CryptoError(\n"
        '            f"sealed value for {name!r} uses key id {kid}, but "\n'
        '            f"{KEY_ENV_PREFIX}{kid:02d} is not in the environment. "\n'
        '            f"The key may have been removed after rotation."\n'
        "        )",
        "    pass  # key-id check removed",
        "tests/test_crypto_multikey.py::test_open_with_wrong_key_id_fails_closed",
    ),
    # -- C1: the REST surface writes audit rows --------------------------------
    (
        "AUDIT-010",
        "every write request writes a row, including routes added later",
        "datum_sync/api.py",
        "    ok = response.status_code < 400\n    try:",
        # Returns before the write. The response is unchanged, so only the
        # absence of the row can fail the test -- which is the property.
        "    ok = response.status_code < 400\n    return response\n    try:",
        "tests/test_audit_middleware.py::"
        "test_a_route_the_middleware_does_not_know_about_is_audited",
    ),
    (
        "AUDIT-011",
        "reads are not audited, so writes are not buried under UI polling",
        "datum_sync/api.py",
        "    if request.method in AUDIT_READ_METHODS:\n        return response",
        "    if request.method in ():\n        return response",
        "tests/test_audit_middleware.py::test_a_read_is_not_a_row",
    ),
    (
        "AUDIT-012",
        "a failed request is recorded as an error, with its status",
        "datum_sync/api.py",
        "    ok = response.status_code < 400",
        # Not `outcome="ok"` directly: `error_code` is derived from the same
        # flag, so flipping the flag proves both columns at once, and a break
        # that only touched `outcome` would leave a row saying ok with a 400 in
        # error_code -- self-contradictory, and passing.
        "    ok = True",
        "tests/test_audit_middleware.py::test_a_failed_request_is_recorded_as_error",
    ),
    (
        "AUDIT-013",
        "/mcp is not double-counted, so audit_log still mirrors mcp_call_log",
        "datum_sync/api.py",
        "    if path.startswith(AUDIT_SELF_LOGGING):\n        return response",
        "    if path.startswith(()):\n        return response",
        "tests/test_audit_middleware.py::"
        "test_mcp_writes_its_own_rows_and_gains_no_duplicate",
    ),
    (
        "AUDIT-014",
        "one request mints one trace: /mcp reads it rather than minting a second",
        "datum_sync/mcp.py",
        "    trace: audit.Trace = request.state.trace",
        # The previous implementation, restored. It inserts cleanly and every
        # mcp test still passes -- the only visible difference is that the
        # request now holds two traces that cannot be joined to each other.
        "    trace = audit.Trace.mint(request.headers.get(\"X-Trace-Id\") or None)",
        "tests/test_audit_middleware.py::"
        "test_the_mcp_row_uses_the_trace_the_middleware_minted",
    ),
    (
        "AUDIT-015",
        "an audit failure is counted and does not fail the request",
        "datum_sync/api.py",
        "    except Exception:",
        # Catches nothing, so the failure propagates: the request 500s and the
        # counter stays put. Both halves of the test then fail, which is right
        # -- answering the caller and recording the gap are one property.
        "    except _NeverRaised:",
        "tests/test_audit_middleware.py::"
        "test_a_request_survives_an_unwritable_audit_row",
    ),
]

# Not covered here, and deliberately not faked: the semaphore bounding
# concurrent argon2 work. It changes how *fast* the login form can burn CPU,
# not whether a request is allowed, so a functional test either asserts nothing
# or asserts a timing threshold that will flake on a loaded machine. Left as
# reasoning in `auth.authenticate_password` rather than a test that would only
# look like proof.

# Guards that are real, are named by a test, and cannot be proven by editing the
# source -- mapped to the reason, which is the point of the dict. A guard with no
# break case is never proven, and the failure mode this whole file exists to
# prevent is exactly that going unsaid. These are printed in their own section on
# every run, so "no case" stays a visible claim someone has to keep making rather
# than an absence nobody sees.
UNPROVABLE = {
    "SCHEMA-001": (
        "The guard is that the live database equals the one migrations/ build. "
        "Breaking it means altering a database, not a source file, so there is no "
        "`old` string to remove. Its own test also skips when no database is "
        "reachable -- see the SKIPPED handling in main()."
    ),
    "AUDIT-007": (
        "The guard is that no audit row carries the proxy's injected secret. It "
        "holds because nothing in a row is derived from a header or a body, so "
        "there is no line to delete -- the change that would make it false is an "
        "addition (putting a response into `detail`). Deleting something and "
        "watching the test still pass would report it as not load-bearing, which "
        "is true of every property that holds by construction and says nothing "
        "about whether the property is worth asserting."
    ),
    "AUTHZ-001": (
        "The guard is that no field sits on a Principal claiming an authority "
        "nothing enforces -- `service_accounts.connection_grants` was exactly "
        "that for five migrations. Making it false means ADDING the field back "
        "to the dataclass and to the queries, in two files at once, and the "
        "harness proves a guard by removing one string from one file. Deleting "
        "the assertion and watching the test pass measures the test, not the "
        "property."
    ),
    "AUTHZ-002": (
        "The guard is a column default in migration 013, and the harness "
        "breaks guards by editing Python. Editing the migration would prove "
        "nothing either: it has already been applied, and the schema the test "
        "runs against comes from the database, not from re-reading the file. "
        "The property is checked directly instead -- an INSERT that never "
        "mentions repo_scope must produce a principal that reaches no "
        "repository -- which is the same shape of check, run against the real "
        "schema rather than a patched copy of the source."
    ),
}

# pytest's exit code is not an outcome. It is 0 for a pass AND for a skip, and
# non-zero for a failure AND for a test id that does not exist (4) AND for a
# collection error (2). The old `returncode == 0` test therefore read a deleted
# or renamed test as "the guard broke its test" and printed `ok` -- a guard
# reported as proven by a run in which nothing executed. Every case's test id
# collects today, so this was latent rather than actively lying, but it is the
# one silent-pass path in the harness. The junit report gives the per-test
# outcome directly instead of inferring it.
FAILED, PASSED, SKIPPED, ERRORED, NOTFOUND = (
    "FAILED", "PASSED", "SKIPPED", "ERRORED", "NOTFOUND")


def run(test: str) -> str:
    """The outcome of `test`: one of the five constants above.

    FAILED is the only one that proves anything. PASSED means the guard was not
    load-bearing. The other three mean the case did not get to ask the question.
    """
    with tempfile.TemporaryDirectory() as tmp:
        report = pathlib.Path(tmp) / "report.xml"
        subprocess.run(
            [sys.executable, "-m", "pytest", test, "-q", "--no-header",
             "-p", "no:cacheprovider", f"--junit-xml={report}"],
            cwd=ROOT, capture_output=True, text=True,
        )
        if not report.exists():
            # pytest exits 4 without writing a report when the id is unresolvable.
            return NOTFOUND
        cases = ElementTree.parse(report).getroot().iter("testcase")
        outcomes = set()
        for case in cases:
            kinds = {child.tag for child in case}
            if "failure" in kinds:
                outcomes.add(FAILED)
            elif "error" in kinds:
                outcomes.add(ERRORED)
            elif "skipped" in kinds:
                outcomes.add(SKIPPED)
            else:
                outcomes.add(PASSED)
    if not outcomes:
        return NOTFOUND
    # A single id selects every parametrisation of the test, and they need not
    # agree. The question this harness asks is "does removing the guard turn the
    # suite red", so ONE failing parametrisation is a proof, and the ones that
    # still pass alongside it are not evidence against it.
    #
    # Ranking PASSED above FAILED here reported AUTH-030 -- 5 of whose 8
    # parameters fail without the guard -- as UNPROVEN, along with three others.
    # The mistake looked exactly like a discovery: four guards apparently not
    # load-bearing, surfaced by a run whose whole purpose is to find such things.
    # It was caught only by re-running those four against HEAD, where they were
    # proven, which is the check worth keeping in mind here -- a plausible
    # finding from a just-changed measurement is a claim about the measurement.
    #
    # NOTFOUND and ERRORED still outrank FAILED: both mean the run itself was
    # unsound, and a failure recorded during an unsound run is not a proof.
    # SKIPPED and PASSED both mean "not proven" and rank below.
    for kind in (NOTFOUND, ERRORED, FAILED, SKIPPED, PASSED):
        if kind in outcomes:
            return kind


async def _sweep() -> None:
    await db.init_pool()
    try:
        async with db.pool().acquire() as conn:
            for table in ("automations", "schedules", "connections"):
                await conn.execute(f"DELETE FROM {table} WHERE name LIKE '_pytest%'")
    finally:
        await db.close_pool()


def sweep() -> None:
    """Delete the rows a broken run wrote, before the next case starts.

    Reverting the source is not reverting the run. Half these cases delete a
    permission check, and a test that then asserts 403 fails *after* the write
    it was meant to prevent -- so its own cleanup, which lives past the failing
    assert, never happens. The row survives the revert.

    That is not theoretical tidiness. An enabled `_pytest-watch` automation left
    behind this way watches every workspace, and it fired alongside six later
    tests in the ordinary suite, each failing with a message about something
    other than the cause. Cleaning up here rather than in each test is the
    narrower fix: the harness is what runs code it knows to be broken, so the
    mess is the harness's to own, and a case added later inherits this for free.

    Only the two tables that act on their own. Leftover jobs are inert -- an
    automation is asked about one job by id, never about the table.
    """
    asyncio.run(_sweep())


def drop_bytecode(path: pathlib.Path) -> None:
    """Delete the cached .pyc for `path`, both before and after a break.

    Reverting the source is not reverting the *bytecode*. CPython decides a
    .pyc is current by comparing the source's mtime-in-whole-seconds and its
    byte length against the header it wrote. This harness defeats both at once:
    the break and the restore land in the same second, and a replacement is
    routinely the same length as what it replaced.

    That is not a hypothetical pairing. `    return name.encode()` and
    `    return b"datum-sync"` are both 24 characters, so after proving the AAD
    case the .pyc compiled from the *broken* source stayed valid indefinitely.
    Every later run in that tree imported a `_aad()` returning a constant --
    the connection-name binding simply absent from the running program -- while
    `git diff` was clean, the file on disk read correctly, and even
    `inspect.getsource` printed the good version, because it reads the .py and
    the interpreter runs the .pyc. It surfaced as one unrelated-looking failure
    in `test_connections.py` some minutes later.

    So the harness owns this the same way it owns `sweep()`: it is the thing
    that deliberately runs known-broken code, and clearing up after that
    includes the artefacts CPython leaves behind.
    """
    if path.suffix != ".py":
        return
    cached = importlib.util.cache_from_source(str(path))
    pathlib.Path(cached).unlink(missing_ok=True)


def main() -> int:
    unproven: list[str] = []
    errors: list[str] = []

    for gid, label, relpath, old, new, test in CASES:
        path = ROOT / relpath
        original = path.read_text()
        if original.count(old) != 1:
            # The source moved. An error, not a skip: a break case that no longer
            # applies has silently stopped proving anything.
            errors.append(
                f"{gid} {label}: anchor matches {original.count(old)} times in "
                f"{relpath}, must match exactly once")
            print(f"  ERROR     {gid}  anchor not found exactly once")
            continue

        path.write_text(original.replace(old, new))
        drop_bytecode(path)
        try:
            outcome = run(test)
        finally:
            path.write_text(original)
            assert path.read_text() == original, f"failed to restore {relpath}"
            drop_bytecode(path)
            sweep()

        if outcome == FAILED:
            print(f"  ok        {gid}  {label}")
        elif outcome == PASSED:
            unproven.append(f"{gid} {label}: {test} still passes without it")
            print(f"  UNPROVEN  {gid}  {label}\n            {test} still passes without it")
        elif outcome == SKIPPED:
            # Not a pass. The test declined to run, so it said nothing about the
            # guard, and counting that as proof is the failure this file exists
            # to prevent.
            unproven.append(f"{gid} {label}: {test} SKIPPED, so it proved nothing")
            print(f"  UNPROVEN  {gid}  {label}\n            {test} skipped -- it never ran")
        else:
            errors.append(
                f"{gid} {label}: {test} {outcome} -- the test did not run, so the "
                f"non-zero exit says nothing about the guard")
            print(f"  ERROR     {gid}  {test} {outcome}")

    print()
    if UNPROVABLE:
        print(f"{len(UNPROVABLE)} guard(s) registered with no break case:")
        for gid, why in sorted(UNPROVABLE.items()):
            print(f"  - {gid}: {why}")
        print()
    if errors:
        print(f"{len(errors)} case(s) could not be evaluated:")
        for e in errors:
            print(f"  - {e}")
    if unproven:
        print(f"{len(unproven)} guard(s) NOT proven by any test:")
        for u in unproven:
            print(f"  - {u}")
    if errors or unproven:
        return 1
    print(f"All {len(CASES)} guards proven: removing each one breaks its test.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
