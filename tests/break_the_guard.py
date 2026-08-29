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

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from datum_sync import db  # noqa: E402  -- after the path insert, necessarily

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
        'COOKIE_PATHS = ("/rest/v1/", "/ui/", "/serve/")',
        'COOKIE_PATHS = ("/rest/v1/", "/ui/", "/serve/", "/stream/", "/download/")',
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

    # -- step 7: schedules -------------------------------------------------
    (
        # The CHECK constraint cannot parse a cron expression, so without this
        # the row is accepted and sits enabled and never fires. A schedule with
        # no symptom is the worst thing this feature can produce.
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
        "params are decoded before being merged and re-encoded",
        "datum_sync/schedules.py",
        "    merged[\"params\"] = json.loads(current[\"params\"])",
        "    merged[\"params\"] = current[\"params\"]",
        "tests/test_schedules.py::test_pausing_a_schedule_does_not_eat_its_parameters",
    ),
    (
        # A schedule that can be repointed is a scope check that happened once,
        # on a row that no longer says what it said when it happened.
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
        "the self-trigger check is inside parse, not beside it",
        "datum_sync/automations.py",
        "    _reject_self_trigger(config)\n    return config",
        "    return config",
        "tests/test_automations.py::test_an_automation_that_triggers_on_what_it_runs_is_refused",
    ),
    (
        # A template naming something outside the namespace would otherwise
        # render as the empty string, forever, silently.
        "a placeholder is checked when the automation is written",
        "datum_sync/automations.py",
        "        if name.startswith(\"job.\") and name[4:] in _JOB_FIELDS:\n"
        "            continue",
        "        if name.startswith(\"job.\"):\n"
        "            continue",
        "tests/test_automations.py::test_a_placeholder_naming_nothing_is_refused_where_it_was_typed",
    ),
    (
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
        "an automation does not fire on jobs older than itself",
        "datum_sync/automations.py",
        "        \"WHERE enabled AND created_at <= $1\",\n        job[\"completed_at\"],",
        "        \"WHERE enabled AND $1 IS NOT NULL\",\n        job[\"completed_at\"],",
        "tests/test_automations.py::test_a_job_that_finished_before_the_automation_existed_is_not_delivered",
    ),
    (
        # The runtime half of the loop guard. Without it an automation whose
        # trigger names no workspace runs forever.
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
        "consider claims the job itself",
        "datum_sync/automations.py",
        "        \"WHERE id = $1 AND automations_at IS NULL RETURNING id\",",
        "        \"WHERE id = $1 RETURNING id\",",
        "tests/test_automations.py::test_considering_is_recorded_so_the_next_poll_does_not_redeliver",
    ),
    (
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
        "an unfiltered trigger requires unfiltered scope",
        "datum_sync/api.py",
        "    if caller.repo_scope is not None and not caller.is_admin:",
        "    if False:",
        "tests/test_automations.py::test_a_scoped_caller_cannot_watch_every_repository",
    ),
    (
        # Check only the stored document and the check is bypassed by writing
        # something harmless and then editing it into something privileged.
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
        "repo scope on the dashboard's job counts",
        "datum_sync/api.py",
        "        if caller.repo_scope is not None:\n"
        "            names = await conn.fetch(\"SELECT DISTINCT repository FROM jobs\")\n"
        "            allowed = [r[\"repository\"] for r in names"
        " if caller.allows_repo(r[\"repository\"])]\n"
        "            if not allowed:\n"
        "                return {\"counts\": dict.fromkeys(JOB_STATUSES, 0), \"total\": 0}\n",
        "",
        "tests/test_api.py::test_a_scoped_caller_is_not_told_how_busy_the_others_are",
    ),

    # -- connections -------------------------------------------------------
    (
        # The whole reason `secret` is a separate column rather than a key in
        # `config`: no read path can emit it, because no read path selects it.
        # Put it back in _COLUMNS and every response carries the ciphertext --
        # not the plaintext, but the thing an offline attack is run against.
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
        "an auth-off server answers nobody but its own machine",
        "datum_sync/api.py",
        "        if not config.is_loopback_client(request.client):",
        "        if False:",
        "tests/test_auth.py::test_a_remote_caller_is_refused_even_with_auth_off",
    ),
    (
        # Strip the IPv4-mapped prefix without re-checking and ::ffff:8.8.8.8
        # reads as loopback.
        "a mapped public address is not loopback",
        "datum_sync/config.py",
        "    return host in _LOOPBACK_HOSTS or host.startswith(\"127.\")",
        "    return True",
        "tests/test_auth.py::test_a_remote_peer_is_not_local",
    ),
    (
        # Read as a general truthiness test, DATUM_SYNC_AUTH=false disables
        # authentication. The strictness is the guard.
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
        "the publisher's max_tier bounds what it can publish",
        "datum_sync/publish.py",
        "        if row[\"tier\"] > publisher.max_tier:",
        "        if False:",
        "tests/test_publish.py::test_the_publisher_tier_decides",
    ),
    (
        "a workspace cannot publish against a connection scoped elsewhere",
        "datum_sync/publish.py",
        "        if not connections.matches_scope(row, repository, manifest.name):",
        "        if False:",
        "tests/test_publish.py::"
        "test_a_connection_out_of_scope_is_refused_at_publish_not_at_run",
    ),
    (
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
        "the smoke test runs only when the manifest opts in",
        "datum_sync/publish.py",
        "    if smoke and manifest.smoke_test:",
        "    if smoke:",
        "tests/test_publish.py::test_the_smoke_test_only_runs_when_the_manifest_asks",
    ),
    (
        # Reordered, a workspace with no MANIFEST.md still costs a process
        # launch -- and runs arbitrary code before anything has vouched for it.
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
        "served root containment uses is_relative_to, not startswith",
        "datum_sync/services.py",
        "    return target == root or target.is_relative_to(root)",
        "    return str(target).startswith(str(root))",
        "tests/test_services.py::test_resolve_refuses_a_sibling_that_shares_a_prefix",
    ),
    (
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
        "resolve() refuses a service type this server does not run",
        "datum_sync/services.py",
        "    if row[\"type\"] in SUPERVISED:",
        "    if False:",
        "tests/test_services.py::test_resolve_refuses_a_supervised_service",
    ),
    (
        # A containment bug at write time is a containment bug on every read.
        "registration refuses a served root outside the job's artifacts",
        "datum_sync/services.py",
        "    if root not in path.parents:\n"
        "        raise ServiceError(f\"{filename!r} resolves outside the job's artifacts\")\n",
        "",
        "tests/test_services.py::test_register_refuses_a_name_that_escapes_the_job",
    ),
    (
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
        "a refused service registration is raised, not passed over quietly",
        "datum_sync/services.py",
        "        if claimed is None:",
        "        if False:",
        "tests/test_services.py::test_another_workspace_cannot_take_the_url",
    ),
    (
        "a service/* output must be a directory",
        "datum_sync/runner.py",
        "            if is_service and not is_dir:",
        "            if False:",
        "tests/test_services.py::test_a_service_output_returning_a_file_fails",
    ),
    (
        # Allowed through, this is a 500 on the download of a job that
        # reported success.
        "a directory under an ordinary output is refused at the run",
        "datum_sync/runner.py",
        "            if is_dir and not is_service:",
        "            if False:",
        "tests/test_services.py::test_a_directory_under_a_non_service_output_fails",
    ),
    (
        "the gate refuses a service type this server cannot run",
        "datum_sync/publish.py",
        "        if out.type in services.SUPERVISED:",
        "        if False:",
        "tests/test_services.py::test_gate_refuses_a_supervised_output",
    ),
    (
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
        "the service listing is filtered by repository scope",
        "datum_sync/api.py",
        "            for r in rows\n"
        "            if caller.allows_repo(r[\"repository\"])",
        "            for r in rows",
        "tests/test_services.py::test_listing_is_filtered_by_scope",
    ),
    (
        # A built site is built from a repository's data. Public because static
        # files feel harmless publishes whatever the last job wrote.
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
        "the v2 assets are public",
        "datum_sync/api.py",
        "PUBLIC_PREFIXES = (\"/ui/static/\", \"/v2/\")",
        "PUBLIC_PREFIXES = (\"/ui/static/\",)",
        "tests/test_ui.py::test_every_asset_the_v2_shell_names_is_served",
    ),
    (
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
        "the v2 shell loads app.js as a module",
        "datum_sync/static-v2/index.html",
        "<script type=\"module\" src=\"/v2/app.js\"></script>",
        "<script src=\"/v2/app.js\"></script>",
        "tests/test_ui.py::test_the_v2_shell_loads_app_js_as_a_module",
    ),
    (
        # A 404 on a module import aborts the whole module. The shell's script
        # tags do not name icons.js, so a scan of the markup cannot see it.
        "every asset the v2 shell reaches for is served, imports included",
        "datum_sync/static-v2/app.js",
        "from '/v2/icons.js'",
        "from '/v2/icon.js'",
        "tests/test_ui.py::test_every_asset_the_v2_shell_names_is_served",
    ),
    (
        # app.js lifts the `d` out of each icon. A second element would be
        # dropped and the icon would still render, slightly wrong, forever.
        "every v2 icon is a single path",
        "datum_sync/static-v2/icons.js",
        "\"columns\": \"<path d=",
        "\"columns\": \"<circle cx=\\\\\"1\\\\\"/><path d=",
        "tests/test_ui.py::test_every_v2_icon_is_a_single_path",
    ),
    (
        # icons.js exports an icon() that assigns svg.innerHTML. Reaching for
        # it is a one-line change that reads as the obvious thing to do.
        "the v2 UI never assigns markup",
        "datum_sync/static-v2/app.js",
        "        svg.append(path);",
        "        svg.innerHTML = ICONS[name];",
        "tests/test_ui.py::test_the_v2_ui_never_assigns_markup",
    ),
    (
        # nav-collapse.js already binds #nav-toggle. A second handler makes the
        # button flip the state and flip it back: a nav that does not move.
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
        "a conditional child handed to the DOM's append rather than ours",
        "datum_sync/static-v2/app.js",
        "    append(view, [\n"
        "        crumbs(['Automations', '#/automations'], [a.name]),\n",
        "    view.append(\n"
        "        crumbs(['Automations', '#/automations'], [a.name]),\n",
        "tests/test_ui.py::test_v2_never_appends_a_child_that_can_be_nothing",
    ),
    (
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
        "the noopener on the link out to a hosted service",
        "datum_sync/static-v2/app.js",
        "href: s.url, target: '_blank', rel: 'noopener',",
        "href: s.url, target: '_blank',",
        "tests/test_ui.py::test_every_v2_new_window_link_disowns_its_opener",
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
        drop_bytecode(path)
        try:
            passed = run(test)
        finally:
            path.write_text(original)
            assert path.read_text() == original, f"failed to restore {relpath}"
            drop_bytecode(path)
            sweep()

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
