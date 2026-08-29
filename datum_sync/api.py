"""REST API.

    uvicorn datum_sync.api:app --host 0.0.0.0 --port 8200

Programmatic endpoints live under `/rest/v1/`. The three service paths
(`/stream`, `/download`, `/upload`) sit at the root because they are the URLs
handed to end users and embedded in other people's applications.

Every route requires a credential except the handful in `PUBLIC_PATHS`, and
every route naming a repository checks the caller is scoped to it. The check is
a middleware rather than a per-route dependency; the reasoning is at
`PUBLIC_PATHS` below. The credential is a bearer token everywhere, plus a
session cookie on the paths the web UI calls -- see `COOKIE_PATHS`.

The synchronous services do not run anything themselves: they submit to the
same queue the async endpoint uses and wait for the result. Executing a
workspace inside the request handler would put every guarantee the worker
provides -- concurrency limit, timeout, cancellation, one child per job --
behind an unbounded number of concurrent HTTP connections.
"""
from __future__ import annotations

import io
import json
import logging
import uuid
import zipfile
from contextlib import asynccontextmanager
from typing import Any

import asyncpg
from fastapi import Body, Depends, FastAPI, Header, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
# Not fastapi.UploadFile: that is a *subclass*, used for signature annotations.
# `request.form()` yields the starlette base class, so an isinstance check
# against the FastAPI one silently matches nothing and every file is treated as
# an ordinary text field.
from starlette.datastructures import UploadFile

from datum_sync import (
    auth, automations, config, connections, crypto, db, errors, events, execute,
    jobs, mcp, oauth, schedules, services, ui, uploads,
)
from datum_sync.auth import Principal
from datum_sync.errors import ApiError

log = logging.getLogger("datum_sync")

# uvicorn configures its own loggers and leaves the root alone, so a record
# logged here propagates to a root with no handler and is discarded. Found by
# launching the server and seeing the startup line simply absent -- no test
# catches this, because nothing under ASGITransport runs the lifespan. A
# diagnostic that silently prints nothing is worse than none at all, so attach a
# handler when, and only when, nothing upstream would have printed it.
if not log.handlers and not logging.getLogger().handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(levelname)s:     %(message)s"))
    log.addHandler(_handler)
    log.setLevel(logging.INFO)

MAX_UPLOAD_BYTES = 200 * 1024 * 1024

# Everything not named here requires a bearer token. An allowlist rather than a
# per-route dependency because the failure modes are not symmetric: forgetting
# to add a route here makes it return 401 and someone complains, while
# forgetting a `Depends(...)` publishes it and nobody notices.
PUBLIC_PATHS = frozenset(
    {
        "/health",
        # OAuth's own endpoints cannot require the credential they exist to
        # issue. Each authenticates by its own rules -- PKCE, the consent
        # screen's password, the client secret.
        "/.well-known/oauth-authorization-server",
        "/.well-known/oauth-protected-resource",
        "/oauth/register",
        "/oauth/authorize",
        "/oauth/token",
        "/oauth/revoke",
        # The interface description, not the data behind it. Every route it
        # lists still refuses an unauthenticated call.
        "/docs",
        "/openapi.json",
        # The web UI shell and its sign-in. The shell is markup and script with
        # no data in it: it asks /rest/v1/whoami who the viewer is and renders a
        # sign-in form if the answer is 401. Sign-in obviously cannot require the
        # session it exists to mint, and sign-out must work on an expired one.
        "/ui",
        "/ui/login",
        "/ui/logout",
        # The v2 shell, public for the same reason and with the same contents.
        "/ui/v2",
    }
)

# Prefix-matched, for the UI's own assets. Separate from PUBLIC_PATHS because a
# prefix is a blunter instrument -- everything beneath it is public -- so the
# two are not worth blurring into one set.
PUBLIC_PREFIXES = ("/ui/static/", "/v2/")

# Where a session cookie is accepted. Everything else requires a bearer token
# even when a valid cookie is attached.
#
# The service paths are excluded deliberately. `GET /stream/{repo}/{ws}` and
# `GET /download/{repo}/{ws}` *execute a workspace*, and SameSite=Lax sends
# cookies on top-level navigation, so accepting the cookie there would let any
# page on the internet run someone's workspace by linking to it. Those URLs are
# for programmatic callers, which have tokens.
# `/serve/` is included, and is the one service-shaped path that is. It reads
# files from a directory a job already produced; it executes nothing, so a
# link to it cannot make anything happen. Excluding it would mean a hosted
# dashboard could not be opened by the signed-in person it was built for, which
# is the only way anyone opens one.
COOKIE_PATHS = ("/rest/v1/", "/ui/", "/serve/")


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Before the pool, so a misconfigured server fails on the setting rather
    # than on whatever the setting later breaks. Logged even when valid: a
    # PUBLIC_URL that is set but wrong fails exactly like one that is right,
    # until a client tries to use the token, so the resolved value needs to be
    # visible at startup rather than inferred from a failure.
    log.info("public URL: %s", config.require_public_url())
    # Refuses outright if this would be reachable from anywhere but this
    # machine. Warned about on every start even when safe, because the whole
    # failure mode is a flag nobody remembers is on.
    config.require_safe_auth()
    if config.AUTH_DISABLED:
        log.warning(
            "AUTHENTICATION IS DISABLED (DATUM_SYNC_AUTH=off) -- every request "
            "is served as an administrator. Development only."
        )
    await db.init_pool()
    try:
        yield
    finally:
        await db.close_pool()


app = FastAPI(
    title="Datum-Sync",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs",
    openapi_url="/openapi.json",
)
errors.install(app)
oauth.install(app)
ui.install(app)
app.include_router(mcp.router)


@app.middleware("http")
async def authenticate(request: Request, call_next):
    """Fail closed: no credential, no route.

    The envelope is built here rather than raised, because middleware sits
    *outside* Starlette's exception middleware -- an ApiError raised at this
    point would surface as a bare 500 with the WWW-Authenticate challenge
    stripped, which is precisely the header an MCP client needs.
    """
    path = request.url.path
    if path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES):
        return await call_next(request)
    # Development only, and refused at startup unless bound to loopback.
    #
    # Placed after the allowlist rather than before it so the flag can only ever
    # widen what was already reachable, and never attach a principal to an
    # endpoint that authenticates by its own rules. That is defensive, not
    # load-bearing: no OAuth handler reads request.state.principal today, so
    # both orders behave identically from outside and no test distinguishes
    # them. It is written this way so that it stays true if one ever does.
    if config.AUTH_DISABLED:
        # The startup check refuses to serve unless config.HOST is loopback, but
        # it can be walked around: `uvicorn --host 0.0.0.0` binds the socket
        # itself and never consults config.HOST. So the real guard is here, on
        # the peer address of the actual connection, which no start-up flag can
        # change and no header can forge -- request.client is the socket, not
        # X-Forwarded-For.
        if not config.is_loopback_client(request.client):
            return errors.envelope(
                403,
                "FORBIDDEN",
                "this server is running with authentication disabled and will "
                "only answer the machine it runs on",
            )
        request.state.principal = auth.DEV_PRINCIPAL
        return await call_next(request)
    try:
        request.state.principal = await auth.require_auth(
            request, allow_cookie=path.startswith(COOKIE_PATHS)
        )
    except ApiError as exc:
        return errors.envelope(
            exc.status, exc.code, exc.message, exc.detail, exc.headers
        )
    return await call_next(request)


# -- helpers ---------------------------------------------------------------


def _caller(request: Request) -> Principal:
    return request.state.principal


Caller = Depends(_caller)


def _job_id(raw: str) -> uuid.UUID:
    try:
        return uuid.UUID(raw)
    except (ValueError, AttributeError):
        raise ApiError(400, "INVALID_PARAMETER", f"not a job id: {raw!r}") from None


async def _job_for(
    conn: asyncpg.Connection, caller: Principal, job_id: uuid.UUID
) -> asyncpg.Record:
    """Fetch a job, refusing one in a repository the caller is not scoped to.

    The job's repository is the denormalised text on the row, not a lookup:
    scope has to be checked against what the job actually ran, which survives
    the workspace being unpublished.
    """
    row = await execute.job_row(conn, job_id)
    auth.require_repo(caller, row["repository"])
    return row


def _job_json(row: asyncpg.Record) -> dict[str, Any]:
    def when(key: str) -> str | None:
        value = row[key]
        return value.isoformat() if value is not None else None

    return {
        "id": str(row["id"]),
        "repository": row["repository"],
        "workspace": row["workspace"],
        "status": row["status"],
        "params": json.loads(row["params"]),
        "artifacts": json.loads(row["artifacts"]),
        "error": row["error"],
        "submitted_by": row["submitted_by"],
        # What caused this run, as opposed to who owns it. A scheduled job's
        # submitted_by is also `schedule:<name>`, but that is a convention the
        # scheduler happens to follow; this is the column, and it is what the
        # loop guard and the UI's "why did this run?" both read.
        "triggered_by": row["triggered_by"],
        "parent_job": str(row["parent_job"]) if row["parent_job"] else None,
        "submitted_at": when("submitted_at"),
        "started_at": when("started_at"),
        "completed_at": when("completed_at"),
    }



# -- system ----------------------------------------------------------------


@app.get("/health")
async def health() -> dict[str, Any]:
    try:
        async with db.pool().acquire() as conn:
            await conn.fetchval("SELECT 1")
            worker = await execute.worker_is_running(conn)
    except Exception as e:
        raise ApiError(
            503, "SERVICE_UNAVAILABLE", f"database unreachable: {e}"
        ) from None
    return {"status": "ok", "database": "ok", "worker": "running" if worker else "down"}


@app.get("/rest/v1/whoami")
async def whoami(caller: Principal = Caller) -> dict[str, Any]:
    """Who the presented credential belongs to.

    The UI's first call: a 401 here is how it knows to draw a sign-in form, and
    `is_admin` and `repo_scope` are what it needs to decide which nav items are
    worth showing. Hiding a control is presentation, not enforcement -- every
    route behind it still checks for itself.
    """
    return auth.principal_json(caller)


@app.get("/rest/v1/engines")
async def engines() -> dict[str, Any]:
    async with db.pool().acquire() as conn:
        running = await execute.worker_is_running(conn)
        queued = await conn.fetchval(
            "SELECT count(*) FROM jobs WHERE status = 'queued'"
        )
        active = await conn.fetchval(
            "SELECT count(*) FROM jobs WHERE status = 'running'"
        )
    return {
        "workers": 1 if running else 0,
        "queued": queued,
        "running": active,
    }


# -- repositories and workspaces -------------------------------------------


@app.get("/rest/v1/repositories")
async def list_repositories(caller: Principal = Caller) -> dict[str, Any]:
    async with db.pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT r.name, r.path, count(w.id) AS workspaces
            FROM repositories r LEFT JOIN workspaces w ON w.repository_id = r.id
            GROUP BY r.id ORDER BY r.name
            """
        )
    # Filtered, not refused: a listing is the one place where out-of-scope
    # repositories should simply not appear.
    return {"items": [dict(r) for r in rows if caller.allows_repo(r["name"])]}


@app.get("/rest/v1/repositories/{repo}")
async def get_repository(repo: str, caller: Principal = Caller) -> dict[str, Any]:
    auth.require_repo(caller, repo)
    async with db.pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT name, path FROM repositories WHERE name = $1", repo
        )
        if row is None:
            raise ApiError(404, "NOT_FOUND", f"no such repository {repo}")
        names = await conn.fetch(
            """
            SELECT w.name FROM workspaces w JOIN repositories r
              ON r.id = w.repository_id
            WHERE r.name = $1 ORDER BY w.name
            """,
            repo,
        )
    return {**dict(row), "workspaces": [r["name"] for r in names]}


@app.get("/rest/v1/repositories/{repo}/workspaces")
async def list_workspaces(repo: str, caller: Principal = Caller) -> dict[str, Any]:
    auth.require_repo(caller, repo)
    async with db.pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT w.name, w.description, w.version, w.published_at
            FROM workspaces w JOIN repositories r ON r.id = w.repository_id
            WHERE r.name = $1 ORDER BY w.name
            """,
            repo,
        )
    return {
        "items": [
            {**dict(r), "published_at": r["published_at"].isoformat()} for r in rows
        ]
    }


@app.get("/rest/v1/repositories/{repo}/workspaces/{ws}")
async def get_workspace(
    repo: str, ws: str, caller: Principal = Caller
) -> dict[str, Any]:
    auth.require_repo(caller, repo)
    async with db.pool().acquire() as conn:
        manifest = await jobs.load_manifest_for(conn, repo, ws)
    return {"repository": repo, **manifest.model_dump(mode="json")}


# -- job submission --------------------------------------------------------


@app.post("/rest/v1/transformations/submit/{repo}/{ws}", status_code=202)
async def submit(
    repo: str,
    ws: str,
    response: Response,
    body: dict[str, Any] = Body(default_factory=dict),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    caller: Principal = Caller,
) -> dict[str, Any]:
    auth.require_repo(caller, repo)
    params = body.get("params", {})
    if not isinstance(params, dict):
        raise ApiError(400, "INVALID_PARAMETER", "'params' must be an object")

    async with db.pool().acquire() as conn:
        await execute.manifest_for(conn, repo, ws, "job_submitter")
        job_id = await jobs.submit_idempotent(
            conn,
            repo,
            ws,
            params,
            submitted_by=caller.name,
            idempotency_key=idempotency_key,
        )
    response.headers["Location"] = f"/rest/v1/transformations/jobs/id/{job_id}"
    return {"id": str(job_id), "status": "queued"}


JOB_STATUSES = ("queued", "running", "complete", "failed", "cancelled")


@app.get("/rest/v1/transformations/jobs")
async def list_jobs(
    status: str | None = Query(default=None),
    repository: str | None = Query(default=None),
    workspace: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    caller: Principal = Caller,
) -> dict[str, Any]:
    """The job queue, newest first. `status` is one of JOB_STATUSES, or absent
    for all of them; `repository` and `workspace` narrow it to one workspace's
    run history.

    Every filter is applied in SQL rather than to the result, because filtering
    afterwards applies LIMIT first. For scope that is a security bug -- a caller
    restricted to one quiet repository would page through empty results while
    the queue was busy elsewhere, and could count the rows they were not allowed
    to see. For `repository`/`workspace` it is the same mistake with a milder
    consequence: a workspace's recent runs disappearing whenever the queue is
    busy. There is no reason to write it correctly once and carelessly twice.
    """
    if status is not None and status not in JOB_STATUSES:
        raise ApiError(
            400,
            "INVALID_PARAMETER",
            f"unknown status {status!r}",
            {"valid": list(JOB_STATUSES)},
        )

    async with db.pool().acquire() as conn:
        allowed: list[str] | None = None
        if caller.repo_scope is not None:
            # From the jobs themselves, not the repositories table: `repository`
            # on a job is denormalised text that outlives the repository being
            # removed, and a job in a deleted repository still must not leak.
            names = await conn.fetch("SELECT DISTINCT repository FROM jobs")
            allowed = [r["repository"] for r in names if caller.allows_repo(r["repository"])]
            if not allowed:
                return {"items": [], "count": 0}

        rows = await conn.fetch(
            """
            SELECT id, repository, workspace, status, submitted_by, error,
                   submitted_at, started_at, completed_at,
                   jsonb_array_length(artifacts) AS artifact_count
              FROM jobs
             WHERE ($1::text IS NULL OR status = $1)
               AND ($2::text[] IS NULL OR repository = ANY($2))
               AND ($3::text IS NULL OR repository = $3)
               AND ($4::text IS NULL OR workspace = $4)
             ORDER BY submitted_at DESC
             LIMIT $5
            """,
            status,
            allowed,
            repository,
            workspace,
            limit,
        )

    def when(row, key):
        value = row[key]
        return value.isoformat() if value is not None else None

    items = [
        {
            "id": str(r["id"]),
            "repository": r["repository"],
            "workspace": r["workspace"],
            "status": r["status"],
            "submitted_by": r["submitted_by"],
            "error": r["error"],
            "artifact_count": r["artifact_count"],
            "submitted_at": when(r, "submitted_at"),
            "started_at": when(r, "started_at"),
            "completed_at": when(r, "completed_at"),
        }
        for r in rows
    ]
    return {"items": items, "count": len(items)}


@app.get("/rest/v1/transformations/jobs/summary")
async def job_summary(caller: Principal = Caller) -> dict[str, Any]:
    """How many jobs are in each status. Counted in SQL, for two reasons.

    The dashboard cannot get this from the job list. That endpoint's `count` is
    the length of the page it returned, so any status with more jobs than the
    limit would report the limit -- a wrong number, arrived at silently, on the
    first screen anybody sees.

    And the scope filter is the same one the list applies, for the same reason
    it applies it: a caller restricted to one repository must not learn how busy
    the others are. A count is a smaller leak than a row, not a different one.
    """
    async with db.pool().acquire() as conn:
        allowed: list[str] | None = None
        if caller.repo_scope is not None:
            names = await conn.fetch("SELECT DISTINCT repository FROM jobs")
            allowed = [r["repository"] for r in names if caller.allows_repo(r["repository"])]
            if not allowed:
                return {"counts": dict.fromkeys(JOB_STATUSES, 0), "total": 0}

        rows = await conn.fetch(
            """
            SELECT status, count(*) AS n
              FROM jobs
             WHERE ($1::text[] IS NULL OR repository = ANY($1))
             GROUP BY status
            """,
            allowed,
        )

    # Every status is present even at zero, so the dashboard draws a stable set
    # of tiles rather than a row that changes width as the queue empties.
    counts = dict.fromkeys(JOB_STATUSES, 0)
    for row in rows:
        counts[row["status"]] = row["n"]
    # Summed from the rows, not from `counts`, so a status this build does not
    # know about is still counted in the total rather than quietly dropped.
    return {"counts": counts, "total": sum(r["n"] for r in rows)}


@app.get("/rest/v1/transformations/jobs/id/{raw_id}")
async def get_job(raw_id: str, caller: Principal = Caller) -> dict[str, Any]:
    async with db.pool().acquire() as conn:
        return _job_json(await _job_for(conn, caller, _job_id(raw_id)))


@app.delete("/rest/v1/transformations/jobs/id/{raw_id}")
async def cancel_job(raw_id: str, caller: Principal = Caller) -> dict[str, Any]:
    job_id = _job_id(raw_id)
    async with db.pool().acquire() as conn:
        await _job_for(conn, caller, job_id)
        result = await jobs.cancel(conn, job_id)
    # 'signalled' means a worker was asked to stop a running child: the job is
    # not cancelled yet, and saying so would be a lie the caller acts on.
    return {"id": raw_id, "result": result}


@app.post("/rest/v1/transformations/jobs/id/{raw_id}/resubmit", status_code=202)
async def resubmit_job(
    raw_id: str, response: Response, caller: Principal = Caller
) -> dict[str, Any]:
    job_id = _job_id(raw_id)
    async with db.pool().acquire() as conn:
        row = await _job_for(conn, caller, job_id)
        new_id = await jobs.submit(
            conn,
            row["repository"],
            row["workspace"],
            json.loads(row["params"]),
            # Whoever re-ran it, not whoever ran it first: the audit trail
            # should name the account that caused this execution.
            submitted_by=caller.name,
            parent_job=job_id,
        )
    response.headers["Location"] = f"/rest/v1/transformations/jobs/id/{new_id}"
    return {"id": str(new_id), "status": "queued", "parent_job": raw_id}


@app.get("/rest/v1/transformations/jobs/id/{raw_id}/log")
async def get_job_log(
    raw_id: str,
    after_id: int = Query(default=0, ge=0),
    caller: Principal = Caller,
) -> dict[str, Any]:
    job_id = _job_id(raw_id)
    async with db.pool().acquire() as conn:
        await _job_for(conn, caller, job_id)
        entries = await jobs.get_log(conn, job_id, after_id=after_id)
    return {
        "items": [
            {"id": e["id"], "ts": e["ts"].isoformat(), "level": e["level"],
             "message": e["message"]}
            for e in entries
        ]
    }


@app.get("/rest/v1/transformations/jobs/id/{raw_id}/events")
async def stream_job_events(
    raw_id: str,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    caller: Principal = Caller,
) -> StreamingResponse:
    """SSE feed for one job: status, log and progress until it finishes."""
    job_id = _job_id(raw_id)
    async with db.pool().acquire() as conn:
        await _job_for(conn, caller, job_id)
    try:
        after_id = int(last_event_id) if last_event_id else 0
    except ValueError:
        after_id = 0

    async def body():
        # Its own connection: LISTEN is per-session and this one is held for
        # as long as the client stays subscribed.
        conn = await asyncpg.connect(config.DATABASE_URL)
        try:
            async for frame in events.job_events(conn, job_id, after_id=after_id):
                yield frame
        finally:
            await conn.close()

    return StreamingResponse(
        body(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/rest/v1/transformations/jobs/id/{raw_id}/artifacts/{name}")
async def get_artifact(
    raw_id: str, name: str, caller: Principal = Caller
) -> FileResponse:
    job_id = _job_id(raw_id)
    async with db.pool().acquire() as conn:
        row = await _job_for(conn, caller, job_id)

    artifact = next(
        (a for a in json.loads(row["artifacts"]) if a["name"] == name), None
    )
    if artifact is None:
        raise ApiError(404, "NOT_FOUND", f"job {job_id} has no artifact {name!r}")
    if services.is_service(artifact["type"]):
        # A directory, so FileResponse would 500 -- and `artifact_path` would
        # 404 with "artifact file missing", which is both true and useless to
        # someone looking at a job that plainly produced it.
        raise ApiError(
            409, "IS_SERVICE",
            f"{name!r} is a hosted service, not a file; browse it at "
            f"/serve/{name}/",
        )
    return FileResponse(
        execute.artifact_path(job_id, artifact["file"]),
        media_type=artifact["type"],
        filename=artifact["file"],
    )


# -- uploads ---------------------------------------------------------------


@app.post("/rest/v1/uploads", status_code=201)
async def create_upload(request: Request, caller: Principal = Caller) -> dict[str, Any]:
    """Store one file and return the upload id a FILE parameter carries.

    Distinct from `POST /upload/{repo}/{ws}`, which takes files and submits a
    job in the same request. A parameters form needs the id *before* it can
    submit anything -- it has other fields to send with it -- and the browser
    cannot reach the service path regardless: a session cookie is not accepted
    there (COOKIE_PATHS in this module).

    No repository scope check, deliberately. An upload id is not access to
    anything: the only thing it can be spent on is a submit, and that checks
    scope against the workspace it names. Requiring a repository here would
    mean the caller had to decide where a file was going before choosing the
    workspace, and would still not be a stronger check.
    """
    form = await request.form()
    try:
        files = [v for _, v in form.multi_items() if isinstance(v, UploadFile)]
        if len(files) != 1:
            raise ApiError(
                400,
                "INVALID_PARAMETER",
                f"expected exactly one file part, got {len(files)}",
            )
        data = await files[0].read()
        if len(data) > MAX_UPLOAD_BYTES:
            raise ApiError(
                413,
                "PAYLOAD_TOO_LARGE",
                f"{len(data)} bytes exceeds the {MAX_UPLOAD_BYTES} byte limit",
            )
        upload_id = uploads.save(files[0].filename, data)
        stored = uploads.safe_name(files[0].filename)
    finally:
        await form.close()

    log.info("upload %s stored for %s as %s", upload_id, caller.name, stored)
    return {"id": upload_id, "filename": stored, "bytes": len(data)}


# -- administration --------------------------------------------------------
# Read-only, plus revocation. Creating accounts and minting tokens stays in the
# `accounts` CLI: those are the operations that hand out credentials, and an
# account that can create accounts through the API is one XSS away from being
# every account. Revocation is the opposite -- it only ever removes access, so
# the worst an attacker gains is the ability to log people out.


@app.get("/rest/v1/accounts")
async def list_accounts(caller: Principal = Caller) -> dict[str, Any]:
    auth.require_admin(caller)
    async with db.pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT a.id, a.name, a.max_tier, a.repo_scope, a.is_admin,
                   a.disabled, a.created_at, a.last_used_at,
                   a.token_hash IS NOT NULL AS has_token,
                   a.password_hash IS NOT NULL AS has_password,
                   count(t.id) FILTER (
                       WHERE t.kind = 'session' AND t.revoked_at IS NULL
                         AND t.expires_at > now()
                   ) AS sessions,
                   count(t.id) FILTER (
                       WHERE t.kind = 'refresh' AND t.revoked_at IS NULL
                         AND t.rotated_to IS NULL
                   ) AS grants
              FROM service_accounts a
              LEFT JOIN oauth_tokens t ON t.account_id = a.id
             GROUP BY a.id
             ORDER BY a.name
            """
        )
    return {
        "items": [
            {
                "name": r["name"],
                "max_tier": r["max_tier"],
                "repo_scope": r["repo_scope"],
                "is_admin": r["is_admin"],
                "disabled": r["disabled"],
                # Never the hashes, nor a prefix of them: the point of storing
                # sha256(token) is that the database cannot give the token back.
                "has_token": r["has_token"],
                "has_password": r["has_password"],
                "sessions": r["sessions"],
                "grants": r["grants"],
                "created_at": r["created_at"].isoformat(),
                "last_used_at": (
                    r["last_used_at"].isoformat() if r["last_used_at"] else None
                ),
            }
            for r in rows
        ]
    }


@app.delete("/rest/v1/accounts/{name}/grants")
async def revoke_grants(name: str, caller: Principal = Caller) -> dict[str, Any]:
    """Revoke every OAuth token and session for an account. Sign out everywhere.

    Includes the caller's own if they name themselves, deliberately: an admin
    who suspects their session is compromised needs to be able to end it, and
    an exemption would be a hole exactly where it matters.
    """
    auth.require_admin(caller)
    async with db.pool().acquire() as conn:
        account_id = await conn.fetchval(
            "SELECT id FROM service_accounts WHERE name = $1", name
        )
        if account_id is None:
            raise ApiError(404, "NOT_FOUND", f"no such account: {name}")
        revoked = await conn.fetchval(
            """
            WITH hit AS (
                UPDATE oauth_tokens SET revoked_at = now()
                 WHERE account_id = $1 AND revoked_at IS NULL
             RETURNING 1
            )
            SELECT count(*) FROM hit
            """,
            account_id,
        )
    return {"account": name, "revoked": revoked}


# -- schedules and automations ---------------------------------------------
#
# Both of these create jobs later, without the caller present, which is the
# whole reason they need their own scope checks rather than relying on the ones
# on /submit.
#
# A job an automation submits runs as `automation:<name>`, not as whoever wrote
# the automation, and `jobs.submit` performs no scope check of its own -- that
# has always been the API's job. So without a check at write time, an account
# scoped to one repository could write an automation that runs a workspace in
# another and have the server run it for them. The rule is the obvious one
# stated once: you may only automate what you could have run yourself, and you
# may only watch what you could have seen.


def _require_watch_scope(caller: Principal, repository: str | None) -> None:
    """A trigger with no repository named watches every repository.

    That is a legitimate thing to want and a leak to hand to a scoped account:
    the trigger fires on other repositories' jobs, and an `http_request` action
    can put that job's parameters in the body. So the unfiltered form requires
    unfiltered scope, and a scoped caller must name a repository they hold.
    """
    if repository is not None:
        auth.require_repo(caller, repository)
        return
    if caller.repo_scope is not None and not caller.is_admin:
        raise ApiError(
            403,
            "FORBIDDEN",
            f"{caller.name} is scoped to particular repositories, so a trigger "
            f"must name one; an unfiltered trigger watches all of them",
            {"repo_scope": caller.repo_scope},
        )


def _require_automation_scope(caller: Principal, config: dict[str, Any]) -> None:
    _require_watch_scope(caller, config["trigger"]["repository"])
    for action in config["actions"]:
        if action["type"] == "run_workspace":
            auth.require_repo(caller, action["repository"])


def _scope_sql(caller: Principal, column: str, index: int) -> tuple[str, list]:
    """A WHERE fragment restricting `column` to the caller's repositories.

    In SQL rather than applied to the result for the reason `list_jobs`
    documents at length: filtering after the query means LIMIT is applied to
    rows the caller cannot see, so a scoped account pages through gaps.

    Returns the ARGUMENT LIST, not the repositories -- they are one parameter,
    a text[], and callers splat this with *args. Returning the bare list turned
    a two-repository scope into two placeholders where the query has one, and a
    one-repository scope into a str where asyncpg wants a sequence.
    """
    if caller.repo_scope is None:
        return "", []
    return f" AND {column} = ANY(${index}::text[])", [
        [s.split("/")[0] for s in caller.repo_scope]
    ]


def _schedule_json(row: asyncpg.Record) -> dict[str, Any]:
    def when(key: str) -> str | None:
        value = row[key]
        return value.isoformat() if value is not None else None

    return {
        "id": row["id"],
        "name": row["name"],
        "repository": row["repository"],
        "workspace": row["workspace"],
        "params": json.loads(row["params"]),
        "cron": row["cron"],
        "interval_s": row["interval_s"],
        "timezone": row["timezone"],
        "enabled": row["enabled"],
        "created_by": row["created_by"],
        "created_at": when("created_at"),
        "last_run": when("last_run"),
        "last_job": str(row["last_job"]) if row["last_job"] else None,
        # The one field worth having: it is the same column the worker claims
        # on, so what the screen says is when it will actually run.
        "next_run": when("next_run"),
    }


def _automation_json(row: asyncpg.Record) -> dict[str, Any]:
    def when(key: str) -> str | None:
        value = row[key]
        return value.isoformat() if value is not None else None

    return {
        "id": row["id"],
        "name": row["name"],
        # Both: the YAML is what the editor must show back, verbatim; the
        # parsed config is what the table renders without re-parsing per row.
        "yaml": row["yaml"],
        "config": json.loads(row["config"]),
        "enabled": row["enabled"],
        "created_by": row["created_by"],
        "created_at": when("created_at"),
        "updated_at": when("updated_at"),
        "last_fired": when("last_fired"),
        "last_error": row["last_error"],
    }


@app.get("/rest/v1/schedules")
async def list_schedules(caller: Principal = Caller) -> dict[str, Any]:
    where, args = _scope_sql(caller, "repository", 1)
    async with db.pool().acquire() as conn:
        rows = await conn.fetch(
            f"SELECT * FROM schedules WHERE true{where} "
            f"ORDER BY enabled DESC, next_run, name",
            *args,
        )
    return {"items": [_schedule_json(r) for r in rows]}


@app.post("/rest/v1/schedules", status_code=201)
async def create_schedule(
    body: dict[str, Any] = Body(...), caller: Principal = Caller
) -> dict[str, Any]:
    for key in ("name", "repository", "workspace"):
        if not isinstance(body.get(key), str) or not body[key]:
            raise ApiError(400, "INVALID_PARAMETER", f"'{key}' is required")
    auth.require_repo(caller, body["repository"])

    async with db.pool().acquire() as conn:
        try:
            row = await schedules.create(
                conn,
                name=body["name"],
                repository=body["repository"],
                workspace=body["workspace"],
                params=body.get("params") or {},
                cron=body.get("cron"),
                interval_s=body.get("interval_s"),
                timezone=body.get("timezone", "Pacific/Auckland"),
                enabled=bool(body.get("enabled", True)),
                created_by=caller.name,
            )
        except schedules.ScheduleError as e:
            raise ApiError(400, "INVALID_PARAMETER", str(e)) from None
        except jobs.WorkspaceNotFound as e:
            raise ApiError(404, "NOT_FOUND", str(e)) from None
        except asyncpg.UniqueViolationError:
            raise ApiError(
                409, "ALREADY_EXISTS", f"a schedule named {body['name']!r} exists"
            ) from None
    return _schedule_json(row)


async def _schedule_for(
    conn: asyncpg.Connection, caller: Principal, schedule_id: int
) -> asyncpg.Record:
    row = await schedules.get(conn, schedule_id)
    if row is None:
        raise ApiError(404, "NOT_FOUND", f"no schedule {schedule_id}")
    auth.require_repo(caller, row["repository"])
    return row


@app.get("/rest/v1/schedules/{schedule_id}")
async def get_schedule(
    schedule_id: int, caller: Principal = Caller
) -> dict[str, Any]:
    async with db.pool().acquire() as conn:
        return _schedule_json(await _schedule_for(conn, caller, schedule_id))


@app.patch("/rest/v1/schedules/{schedule_id}")
async def update_schedule(
    schedule_id: int,
    body: dict[str, Any] = Body(...),
    caller: Principal = Caller,
) -> dict[str, Any]:
    """Partial update. Changing the trigger or enabling recomputes `next_run`.

    Repository and workspace are not patchable: a schedule that could be
    repointed is a scope check that happened once, on a row that no longer says
    what it said. Delete it and make another.
    """
    async with db.pool().acquire() as conn:
        await _schedule_for(conn, caller, schedule_id)
        try:
            row = await schedules.update(conn, schedule_id, body)
        except schedules.ScheduleError as e:
            raise ApiError(400, "INVALID_PARAMETER", str(e)) from None
    if row is None:
        raise ApiError(404, "NOT_FOUND", f"no schedule {schedule_id}")
    return _schedule_json(row)


@app.delete("/rest/v1/schedules/{schedule_id}")
async def delete_schedule(
    schedule_id: int, caller: Principal = Caller
) -> dict[str, Any]:
    async with db.pool().acquire() as conn:
        await _schedule_for(conn, caller, schedule_id)
        await schedules.delete(conn, schedule_id)
    return {"id": schedule_id, "deleted": True}


@app.get("/rest/v1/automations")
async def list_automations(caller: Principal = Caller) -> dict[str, Any]:
    where, args = _scope_sql(caller, "config -> 'trigger' ->> 'repository'", 1)
    async with db.pool().acquire() as conn:
        rows = await conn.fetch(
            f"SELECT * FROM automations WHERE true{where} "
            f"ORDER BY enabled DESC, name",
            *args,
        )
    return {"items": [_automation_json(r) for r in rows]}


@app.post("/rest/v1/automations", status_code=201)
async def create_automation(
    body: dict[str, Any] = Body(...), caller: Principal = Caller
) -> dict[str, Any]:
    text = body.get("yaml")
    if not isinstance(text, str) or not text.strip():
        raise ApiError(400, "INVALID_PARAMETER", "'yaml' is required")
    async with db.pool().acquire() as conn:
        try:
            _require_automation_scope(caller, automations.parse(text))
            row = await automations.create(conn, text, created_by=caller.name)
        except automations.AutomationError as e:
            raise ApiError(400, "INVALID_PARAMETER", str(e)) from None
        except asyncpg.UniqueViolationError:
            raise ApiError(
                409, "ALREADY_EXISTS", "an automation with that name exists"
            ) from None
    return _automation_json(row)


async def _automation_for(
    conn: asyncpg.Connection, caller: Principal, automation_id: int
) -> asyncpg.Record:
    row = await automations.get(conn, automation_id)
    if row is None:
        raise ApiError(404, "NOT_FOUND", f"no automation {automation_id}")
    _require_automation_scope(caller, json.loads(row["config"]))
    return row


@app.get("/rest/v1/automations/{automation_id}")
async def get_automation(
    automation_id: int, caller: Principal = Caller
) -> dict[str, Any]:
    async with db.pool().acquire() as conn:
        return _automation_json(await _automation_for(conn, caller, automation_id))


@app.put("/rest/v1/automations/{automation_id}")
async def replace_automation(
    automation_id: int,
    body: dict[str, Any] = Body(...),
    caller: Principal = Caller,
) -> dict[str, Any]:
    """Replace the document. Scope is checked against both versions.

    The old one because editing an automation you cannot see is reading it; the
    new one because otherwise the check is trivially bypassed by writing a
    harmless automation and then editing it into a privileged one.
    """
    text = body.get("yaml")
    if not isinstance(text, str) or not text.strip():
        raise ApiError(400, "INVALID_PARAMETER", "'yaml' is required")
    async with db.pool().acquire() as conn:
        await _automation_for(conn, caller, automation_id)
        try:
            _require_automation_scope(caller, automations.parse(text))
            row = await automations.replace(conn, automation_id, text)
        except automations.AutomationError as e:
            raise ApiError(400, "INVALID_PARAMETER", str(e)) from None
    if row is None:
        raise ApiError(404, "NOT_FOUND", f"no automation {automation_id}")
    return _automation_json(row)


@app.patch("/rest/v1/automations/{automation_id}")
async def toggle_automation(
    automation_id: int,
    body: dict[str, Any] = Body(...),
    caller: Principal = Caller,
) -> dict[str, Any]:
    """Enable or disable without touching the document."""
    if not isinstance(body.get("enabled"), bool):
        raise ApiError(400, "INVALID_PARAMETER", "'enabled' must be true or false")
    async with db.pool().acquire() as conn:
        await _automation_for(conn, caller, automation_id)
        row = await automations.set_enabled(conn, automation_id, body["enabled"])
    return _automation_json(row)


@app.delete("/rest/v1/automations/{automation_id}")
async def delete_automation(
    automation_id: int, caller: Principal = Caller
) -> dict[str, Any]:
    async with db.pool().acquire() as conn:
        await _automation_for(conn, caller, automation_id)
        await automations.delete(conn, automation_id)
    return {"id": automation_id, "deleted": True}


@app.get("/rest/v1/automations/{automation_id}/runs")
async def automation_runs(
    automation_id: int,
    limit: int = Query(default=20, ge=1, le=100),
    caller: Principal = Caller,
) -> dict[str, Any]:
    """What this automation has actually done, newest first."""
    async with db.pool().acquire() as conn:
        await _automation_for(conn, caller, automation_id)
        rows = await automations.runs(conn, automation_id, limit)
    return {
        "items": [
            {
                "id": r["id"],
                "fired_at": r["fired_at"].isoformat(),
                "trigger_job": str(r["trigger_job"]) if r["trigger_job"] else None,
                "results": json.loads(r["results"]),
                "ok": r["ok"],
            }
            for r in rows
        ]
    }


# -- connections -----------------------------------------------------------
#
# Writes are admin-only, reads are not. A connection's `config` is deliberately
# public to any authenticated caller -- it is what a workspace author needs in
# order to declare the connection in their manifest, and withholding it means
# they guess at names. The credential is in `secret`, which no route returns.
#
# There is no route that reads a secret back, deliberately. "Show me what I
# stored" is the request that turns a credential store into a credential
# viewer; the answer is to overwrite it, or to press Test.


def _connection_json(row: asyncpg.Record) -> dict[str, Any]:
    payload = connections.public(row)
    for key in ("created_at", "updated_at", "last_test_at"):
        value = payload[key]
        payload[key] = value.isoformat() if value is not None else None
    return payload


def _secret_body(body: dict[str, Any]) -> dict[str, Any] | None:
    """Pull `secret` out of a request body, rejecting a non-object."""
    secret = body.get("secret")
    if secret is None or isinstance(secret, dict):
        return secret
    raise ApiError(
        400, "INVALID_PARAMETER",
        "'secret' is an object of credential fields, e.g. {\"password\": \"...\"}",
    )


def _require_key() -> None:
    """Refuse a write that would store a credential with no key to seal it.

    Checked before touching the database so the failure is one 503 rather than
    a connection row that exists, has no secret, and looks merely incomplete.
    """
    if not crypto.available():
        raise ApiError(
            503, "SERVICE_UNAVAILABLE",
            f"{crypto.KEY_ENV} is not configured, so connection secrets cannot "
            f"be stored. Generate a key with `python -m datum_sync.crypto`.",
        )


@app.get("/rest/v1/connections")
async def list_connections(caller: Principal = Caller) -> dict[str, Any]:
    async with db.pool().acquire() as conn:
        rows = await connections.listing(conn)
    return {
        "items": [_connection_json(r) for r in rows],
        "key_configured": crypto.available(),
    }


@app.post("/rest/v1/connections", status_code=201)
async def create_connection(
    body: dict[str, Any] = Body(...), caller: Principal = Caller
) -> dict[str, Any]:
    auth.require_admin(caller)
    for key in ("name", "type"):
        if not isinstance(body.get(key), str) or not body[key]:
            raise ApiError(400, "INVALID_PARAMETER", f"'{key}' is required")
    secret = _secret_body(body)
    if secret:
        _require_key()

    async with db.pool().acquire() as conn:
        try:
            row = await connections.create(
                conn,
                name=body["name"],
                type_=body["type"],
                config=body.get("config") or {},
                secret=secret,
                tier=int(body.get("tier", 1)),
                scope=body.get("scope", "global"),
                scope_targets=body.get("scope_targets") or [],
                access=body.get("access", "read"),
                description=body.get("description"),
                created_by=caller.name,
            )
        except connections.ConnectionStoreError as e:
            raise ApiError(400, "INVALID_PARAMETER", str(e)) from None
        except asyncpg.UniqueViolationError:
            raise ApiError(
                409, "ALREADY_EXISTS", f"a connection named {body['name']!r} exists"
            ) from None
    return _connection_json(row)


@app.get("/rest/v1/connections/{name}")
async def get_connection(name: str, caller: Principal = Caller) -> dict[str, Any]:
    async with db.pool().acquire() as conn:
        row = await connections.get(conn, name)
    if row is None:
        raise ApiError(404, "NOT_FOUND", f"no connection named {name!r}")
    return _connection_json(row)


@app.patch("/rest/v1/connections/{name}")
async def update_connection(
    name: str, body: dict[str, Any] = Body(...), caller: Principal = Caller
) -> dict[str, Any]:
    """Partial update. `name` is not patchable.

    The name is the associated data the secret was sealed with, so renaming a
    connection would leave a blob that no longer opens. Renaming is delete and
    recreate, which also forces the credential to be supplied again -- correct,
    since nothing can read the old one out to carry it across.
    """
    auth.require_admin(caller)
    if "secret" in body:
        _secret_body(body)
        _require_key()

    async with db.pool().acquire() as conn:
        try:
            row = await connections.update(conn, name, body)
        except connections.ConnectionStoreError as e:
            raise ApiError(400, "INVALID_PARAMETER", str(e)) from None
    if row is None:
        raise ApiError(404, "NOT_FOUND", f"no connection named {name!r}")
    return _connection_json(row)


@app.delete("/rest/v1/connections/{name}")
async def delete_connection(name: str, caller: Principal = Caller) -> dict[str, Any]:
    auth.require_admin(caller)
    async with db.pool().acquire() as conn:
        if not await connections.delete(conn, name):
            raise ApiError(404, "NOT_FOUND", f"no connection named {name!r}")
    return {"name": name, "deleted": True}


@app.post("/rest/v1/connections/{name}/test")
async def test_connection(name: str, caller: Principal = Caller) -> dict[str, Any]:
    """Open the connection for real and record the outcome.

    Admin-only despite being read-shaped: it makes the server open an outbound
    connection to an address of the caller's choosing, using credentials the
    caller cannot see. That is a probe, and the response distinguishes
    "refused" from "timed out", so an unprivileged caller could map a network
    with it.
    """
    auth.require_admin(caller)
    async with db.pool().acquire() as conn:
        try:
            return await connections.test(conn, name)
        except connections.ConnectionStoreError as e:
            raise ApiError(404, "NOT_FOUND", str(e)) from None


# -- service paths ---------------------------------------------------------


@app.get("/stream/{repo}/{ws}")
async def data_streaming(
    repo: str, ws: str, request: Request, caller: Principal = Caller
) -> Response:
    """Run and return the primary output inline. Parameters come from the query."""
    auth.require_repo(caller, repo)
    row, manifest = await execute.run_sync(
        repo, ws, dict(request.query_params), "data_streaming", caller.name
    )
    primary = manifest.primary_output
    artifacts = json.loads(row["artifacts"])
    chosen = next(
        (a for a in artifacts if primary and a["name"] == primary.name), None
    )
    if chosen is None:
        raise ApiError(
            502,
            "NO_PRIMARY_OUTPUT",
            "the run produced no primary output to stream",
            {"job_id": str(row["id"]),
             "produced": [a["name"] for a in artifacts]},
        )
    path = execute.artifact_path(row["id"], chosen["file"])
    return Response(content=path.read_bytes(), media_type=chosen["type"])


@app.get("/download/{repo}/{ws}")
async def data_download(
    repo: str, ws: str, request: Request, caller: Principal = Caller
) -> Response:
    """Run and return the outputs as an attachment: the file if there is one,
    a zip if there are several."""
    auth.require_repo(caller, repo)
    row, _ = await execute.run_sync(
        repo, ws, dict(request.query_params), "data_download", caller.name
    )
    artifacts = json.loads(row["artifacts"])
    if not artifacts:
        raise ApiError(
            502, "NO_OUTPUT", "the run produced no outputs to download",
            {"job_id": str(row["id"])},
        )

    if len(artifacts) == 1:
        only = artifacts[0]
        return FileResponse(
            execute.artifact_path(row["id"], only["file"]),
            media_type=only["type"],
            filename=only["file"],
        )

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for a in artifacts:
            archive.write(execute.artifact_path(row["id"], a["file"]), arcname=a["file"])
    return Response(
        content=buffer.getvalue(),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{ws}-{row["id"]}.zip"'
        },
    )


@app.post("/upload/{repo}/{ws}", status_code=202)
async def data_upload(
    repo: str,
    ws: str,
    request: Request,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    caller: Principal = Caller,
) -> dict[str, Any]:
    """Take multipart files and submit a job.

    Each file field becomes the parameter of the same name, carrying the
    upload id. Non-file fields are passed through as ordinary parameters.
    """
    auth.require_repo(caller, repo)
    async with db.pool().acquire() as conn:
        # Before the body is read, not after: writing the files first would let
        # an unknown workspace name orphan 200MB on disk for every request that
        # then 404s.
        await execute.manifest_for(conn, repo, ws, "data_upload")

    form = await request.form()
    params: dict[str, Any] = {}
    received: dict[str, str] = {}

    try:
        for field, value in form.multi_items():
            if isinstance(value, UploadFile):
                data = await value.read()
                if len(data) > MAX_UPLOAD_BYTES:
                    raise ApiError(
                        413, "PAYLOAD_TOO_LARGE",
                        f"{field}: {len(data)} bytes exceeds the "
                        f"{MAX_UPLOAD_BYTES} byte limit",
                    )
                upload_id = uploads.save(value.filename, data)
                params[field] = upload_id
                received[field] = uploads.safe_name(value.filename)
            else:
                params[field] = value
    finally:
        await form.close()

    async with db.pool().acquire() as conn:
        job_id = await jobs.submit_idempotent(
            conn,
            repo,
            ws,
            params,
            submitted_by=caller.name,
            idempotency_key=idempotency_key,
        )
    response.headers["Location"] = f"/rest/v1/transformations/jobs/id/{job_id}"
    return {"id": str(job_id), "status": "queued", "files": received}


# -- hosted services -------------------------------------------------------


@app.get("/rest/v1/services")
async def list_services(caller: Principal = Caller) -> dict[str, Any]:
    """Every hosted service the caller's repository scope covers."""
    async with db.pool().acquire() as conn:
        rows = await services.listing(conn)
    return {
        "items": [
            {
                "name": r["name"],
                "type": r["type"],
                "repository": r["repository"],
                "workspace": r["workspace"],
                "status": r["status"],
                "url": f"/serve/{r['name']}/",
                "source_job": str(r["source_job"]) if r["source_job"] else None,
                "updated_at": r["updated_at"].isoformat(),
            }
            for r in rows
            if caller.allows_repo(r["repository"])
        ]
    }


@app.get("/serve/{name}")
@app.get("/serve/{name}/{subpath:path}")
async def serve(
    name: str, subpath: str = "", caller: Principal = Caller
) -> FileResponse:
    """Serve one file from a hosted service's built directory.

    Authenticated like everything else, and accepting the UI's session cookie
    (COOKIE_PATHS) so a signed-in browser can open a dashboard. Not public: a
    hosted service is built from a repository's data, and making the whole
    namespace world-readable because static files feel harmless would publish
    whatever the last job wrote.

    Repository scope is checked against the *owning* workspace, so a caller
    confined to one repository cannot read another's site through a URL that
    happens not to mention it.
    """
    async with db.pool().acquire() as conn:
        row = await services.get(conn, name)

    if row is None:
        raise ApiError(404, "NOT_FOUND", f"no hosted service named {name!r}")
    auth.require_repo(caller, row["repository"])

    try:
        path = services.resolve(row, subpath)
    except services.ServiceError as e:
        # 404 for a missing file, 501 for a type this server does not run. The
        # distinction is the difference between "you asked for the wrong thing"
        # and "we cannot do this at all", and only the second is our problem.
        status = 501 if row["type"] in services.SUPERVISED else 404
        raise ApiError(status, "SERVICE_UNAVAILABLE", str(e)) from None

    return FileResponse(path)


def main() -> int:
    import uvicorn

    uvicorn.run(app, host=config.HOST, port=config.PORT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
