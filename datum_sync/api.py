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
import time
import uuid
import zipfile
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
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
    agents, audit, auth, automations, config, connections, crypto, db, errors, events,
    execute, jobs, mcp, oauth, schedules, services, tokens, ui, uploads,
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
                request,
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
            request, exc.status, exc.code, exc.message, exc.detail, exc.headers
        )
    return await call_next(request)


# Reads are excluded, per 10 §1. Including them would multiply the table by the
# UI's polling without adding a fact: a GET that changed nothing is answered by
# the access log, and burying the writes under it is how an audit trail stops
# being read.
AUDIT_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Surfaces that write their own, richer audit rows. `011_audit_log.sql` states
# that `audit_log` and `mcp_call_log` agreeing row for row is the check on B2's
# phase one -- so adding a second row per `/mcp` post would not merely duplicate,
# it would retire that check silently while every existing test still passed.
AUDIT_SELF_LOGGING = ("/mcp",)


def _via(path: str) -> str:
    """Which surface the request arrived on, for `audit_log.via`.

    The column exists to stop two surfaces' verbs colliding, so this only has to
    separate them, not describe them. Anything not obviously the UI or the OAuth
    dance is the programmatic API -- including the root service paths
    (`/upload/...`), which are REST endpoints that happen not to sit under the
    version prefix.
    """
    if path.startswith("/ui"):
        return "ui"
    if path.startswith("/oauth") or path.startswith("/.well-known"):
        return "oauth"
    return "rest"


@app.middleware("http")
async def trace_and_audit(request: Request, call_next):
    """Mint the request trace, and write one audit row per write request.

    Registered *after* `authenticate` and therefore outermost: Starlette's
    `add_middleware` inserts at index 0 and builds the stack so index 0 wraps the
    rest, which means the last decorator in this file runs first -- backwards
    from how the file reads, so `test_audit_middleware.py` asserts it.

    Only one thing needs that order, and it is not the principal: the principal
    is read after `call_next` returns, by which point `authenticate` has run
    whichever side of this it sits on. The order is for the trace, which has to
    be minted outside `authenticate` so that a request rejected *by* it still
    has one to quote. Worth stating plainly because the reflex is to assume the
    audit layer must sit inside auth to see who the caller is, and that reasoning
    would be wrong in a way that happens to produce the same behaviour.

    One middleware, not the spec's separate trace and audit layers. Minting on
    the way in and writing on the way out is a single request lifecycle; splitting
    it across two layers would add an ordering constraint between them and buy
    nothing, since neither half is useful without the other.

    Why a middleware at all, when `audit.write` is deliberately called explicitly
    elsewhere: those call sites pass a rich domain verb and are worth keeping,
    but the contract "remember to call this in your new route" has already been
    run as an experiment. It produced two call sites, both on one surface, while
    the REST API and UI wrote nothing for a whole release. Coverage that depends
    on being remembered is coverage that decays. So this row is deliberately
    coarse -- `rest.post`, and the path -- and its whole merit is that a route
    added tomorrow is audited without anybody touching this function.

    Never fails the request. `audit.write` swallows its own failures, but it is
    handed an open connection, so acquiring one is a step it cannot cover --
    hence the `except` below, which counts the loss through `audit.drop()` so a
    gap stays visible on `/health` rather than reporting as zero.
    """
    trace = audit.Trace.mint(request.headers.get("X-Trace-Id") or None)
    request.state.trace = trace
    t0 = time.monotonic()
    try:
        response = await call_next(request)
    except Exception:
        # A crash reaches this function as a raise, not as a 500 response:
        # Starlette's ServerErrorMiddleware -- the thing that turns an unhandled
        # exception into a response -- is installed *outside* every user
        # middleware, so it never runs before this point. Written and re-raised
        # rather than returned, so the traceback still reaches the server log.
        #
        # Measured, not reasoned: before this branch existed, an unhandled
        # exception left no audit row at all, which is the request most worth
        # having a row for.
        await _audit_row(request, trace, 500, t0)
        raise
    await _audit_row(request, trace, response.status_code, t0)
    return response


async def _audit_row(
    request: Request, trace: audit.Trace, status: int, t0: float
) -> None:
    """Write the one row for a finished request, or account for its loss.

    Called from both of `trace_and_audit`'s exits. One body rather than two so
    that a filter added to the normal path cannot be forgotten on the crash
    path, which is the one nobody exercises by hand.
    """
    path = request.url.path
    if path.startswith(AUDIT_SELF_LOGGING):
        return

    principal = getattr(request.state, "principal", None)

    # A refused credential is the one event someone goes to an audit log to find,
    # and until migration 014 it could not be written at all: `actor_id` was NOT
    # NULL, so a request that never established an identity had no row to be.
    #
    # Checked before the read-method filter, deliberately. Successful reads stay
    # unaudited because of their volume, but a 401 is not a read -- it is a
    # credential being tried -- and auditing those only when they arrive by POST
    # would miss the shape probing actually takes.
    #
    # Narrowed to 401 rather than every principal-less request: an unauthenticated
    # 200 is a public path (`/health`, the OAuth discovery documents) and a 403
    # always has a principal, because authentication succeeded and authorisation
    # is what refused it.
    if principal is None:
        if status == 401:
            try:
                async with db.pool().acquire() as conn:
                    await audit.write_anon(
                        conn,
                        trace=trace,
                        via=_via(path),
                        verb=f"{_via(path)}.{request.method.lower()}",
                        target_kind="path",
                        target=path,
                        outcome="error",
                        error_code=status,
                        detail={"auth": _auth_scheme(request)},
                    )
            except Exception:
                audit.drop()
        return

    if request.method in AUDIT_READ_METHODS:
        return

    ok = status < 400
    try:
        async with db.pool().acquire() as conn:
            await audit.write(
                conn,
                trace=trace,
                principal=principal,
                via=_via(path),
                # Coarse on purpose -- see above. A path-to-domain-verb mapping
                # is the same "remember your route" contract in a new costume.
                verb=f"{_via(path)}.{request.method.lower()}",
                target_kind="path",
                target=path,
                outcome="ok" if ok else "error",
                # Still no 'denied': the CHECK is ('ok','error') and the
                # classification defect that justifies it is unchanged.
                error_code=None if ok else status,
                duration_ms=int((time.monotonic() - t0) * 1000),
            )
    except Exception:
        # The pool itself being unavailable is the case audit.write cannot
        # swallow, because it never gets the connection. A request must not fail
        # because it could not be recorded.
        audit.drop()


def _auth_scheme(request: Request) -> str:
    """The authentication scheme a refused request offered, and only that.

    Returns `bearer`, `basic`, `none`, or `other` -- never the credential. The
    whole reason this is a function rather than an inline expression is that the
    obvious version logs `request.headers["authorization"]`, which writes the
    token being probed into the table built to be read after a breach. Splitting
    on whitespace and keeping element zero is the entire logic; the comment is
    the point.
    """
    header = request.headers.get("authorization")
    if not header:
        return "none"
    scheme = header.split(" ", 1)[0].lower()
    return scheme if scheme in ("bearer", "basic") else "other"


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
    # audit_dropped is here because audit.write() swallows its exceptions, as
    # every writer beside it does: a logging failure must not fail the call it
    # was recording. But a swallowed audit write is a hole in an audit trail,
    # and an audit gap that nobody can observe is the one kind of failure this
    # table cannot tolerate. Non-zero means rows are missing, not that the
    # service is unhealthy -- so it is a field, not a status.
    return {
        "status": "ok",
        "database": "ok",
        "worker": "running" if worker else "down",
        "audit_dropped": audit.dropped(),
    }


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


@app.get("/rest/v1/workspaces")
async def list_all_workspaces(caller: Principal = Caller) -> dict[str, Any]:
    """Every published workspace, across every repository the caller can see.

    The per-repository listing above answers "what is in this repository". This
    answers "what can I run" -- the question the catalogue screen asks, and the
    one an agent's tools/list is derived from. Same scope rule as the repository
    listing: filtered, not refused, because a flat catalogue that 403s on a
    single out-of-scope entry is useless for discovery.
    """
    async with db.pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT r.name AS repository, w.name, w.description, w.version,
                   w.published_at, w.published_by,
                   count(j.id) AS jobs, max(j.submitted_at) AS last_run
              FROM workspaces w
              JOIN repositories r ON r.id = w.repository_id
              LEFT JOIN jobs j
                ON j.repository = r.name AND j.workspace = w.name
             GROUP BY r.name, w.id
             ORDER BY r.name, w.name
            """
        )
    return {
        "items": [
            {
                "repository": r["repository"],
                "name": r["name"],
                "description": r["description"],
                "version": r["version"],
                "published_at": r["published_at"].isoformat(),
                "published_by": r["published_by"],
                "jobs": r["jobs"],
                "last_run": r["last_run"].isoformat() if r["last_run"] else None,
            }
            for r in rows
            if caller.allows_repo(r["repository"])
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
        if not caller.all_repos():
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
        if not caller.all_repos():
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
                   -- From account_tokens, not a.token_hash: since migration
                   -- 012 that column is a stale copy nothing authenticates
                   -- against, so reading it here would be wrong in both
                   -- directions -- an account holding three live tokens
                   -- reported as having none, and an account whose only token
                   -- was revoked reported as still holding a credential.
                   EXISTS (SELECT 1 FROM account_tokens at
                            WHERE at.account_id = a.id AND at.revoked_at IS NULL
                              AND (at.expires_at IS NULL OR at.expires_at > now())
                          ) AS has_token,
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


@app.get("/rest/v1/accounts/{name}")
async def get_account(name: str, caller: Principal = Caller) -> dict[str, Any]:
    auth.require_admin(caller)
    async with db.pool().acquire() as conn:
        r = await conn.fetchrow(
            """
            SELECT id, name, max_tier, repo_scope, is_admin, disabled,
                   vault_scope, created_at, last_used_at,
                   -- See list_accounts: account_tokens is the credential now.
                   EXISTS (SELECT 1 FROM account_tokens at
                            WHERE at.account_id = service_accounts.id
                              AND at.revoked_at IS NULL
                              AND (at.expires_at IS NULL OR at.expires_at > now())
                          ) AS has_token,
                   password_hash IS NOT NULL AS has_password
              FROM service_accounts
             WHERE name = $1
            """,
            name,
        )
    if r is None:
        raise ApiError(404, "NOT_FOUND", f"no such account: {name}")
    vs = r["vault_scope"]
    return {
        "name": r["name"],
        "max_tier": r["max_tier"],
        "repo_scope": r["repo_scope"],
        "is_admin": r["is_admin"],
        "disabled": r["disabled"],
        "vault_scope": json.loads(vs) if vs else None,
        "has_token": r["has_token"],
        "has_password": r["has_password"],
        "created_at": r["created_at"].isoformat(),
        "last_used_at": (r["last_used_at"].isoformat() if r["last_used_at"] else None),
    }


@app.patch("/rest/v1/accounts/{name}")
async def update_account(name: str, body: dict = Body(), caller: Principal = Caller) -> dict[str, Any]:
    """Update account tier or disabled state."""
    auth.require_admin(caller)
    disabled = body.get("disabled")
    max_tier = body.get("max_tier")

    if disabled is None and max_tier is None:
        raise ApiError(400, "INVALID_PARAMETER", "disabled or max_tier is required")
    if disabled is not None and not isinstance(disabled, bool):
        raise ApiError(400, "INVALID_PARAMETER", "disabled must be a boolean")
    if max_tier is not None and (not isinstance(max_tier, int) or max_tier < 1 or max_tier > 5):
        raise ApiError(400, "INVALID_PARAMETER", "max_tier must be an integer 1–5")

    sets, args = [], [name]
    if disabled is not None:
        args.append(disabled);  sets.append(f"disabled = ${len(args)}")
    if max_tier is not None:
        args.append(max_tier);  sets.append(f"max_tier = ${len(args)}")

    async with db.pool().acquire() as conn:
        row = await conn.fetchrow(
            f"UPDATE service_accounts SET {', '.join(sets)} WHERE name = $1 RETURNING name, disabled, max_tier",
            *args,
        )
    if row is None:
        raise ApiError(404, "NOT_FOUND", f"no such account: {name}")
    return {"account": row["name"], "disabled": row["disabled"], "max_tier": row["max_tier"]}


@app.delete("/rest/v1/accounts/{name}")
async def delete_account(name: str, caller: Principal = Caller) -> dict[str, Any]:
    """Permanently delete an account. Blocked if the account has active sessions or grants."""
    auth.require_admin(caller)
    if name == caller.name:
        raise ApiError(400, "INVALID_PARAMETER", "cannot delete your own account")
    async with db.pool().acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id,
                   (SELECT count(*) FROM oauth_tokens WHERE account_id = sa.id AND revoked_at IS NULL) AS sessions,
                   (SELECT count(*) FROM oauth_grants  WHERE account_id = sa.id) AS grants
              FROM service_accounts sa WHERE name = $1
            """,
            name,
        )
        if row is None:
            raise ApiError(404, "NOT_FOUND", f"no such account: {name}")
        if row["sessions"] or row["grants"]:
            raise ApiError(409, "HAS_ACTIVE_ACCESS",
                           "revoke sessions and grants before deleting")
        await conn.execute("DELETE FROM service_accounts WHERE id = $1", row["id"])
    return {"account": name, "deleted": True}


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


# -- agents ----------------------------------------------------------------
# Sub-identities of a service account, each with its own bearer token and
# proxy grants. Admin-only for creation and grant changes; the agent itself
# authenticates via its own token and can only proxy.


async def _account_id_or_404(conn, name: str) -> int:
    account_id = await conn.fetchval(
        "SELECT id FROM service_accounts WHERE name = $1", name
    )
    if account_id is None:
        raise ApiError(404, "NOT_FOUND", f"no such account: {name}")
    return account_id


@app.get("/rest/v1/accounts/{account_name}/agents")
async def list_agents(account_name: str, caller: Principal = Caller) -> dict:
    auth.require_admin(caller)
    async with db.pool().acquire() as conn:
        account_id = await _account_id_or_404(conn, account_name)
        rows = await agents.list_for_account(conn, account_id)
    return {"items": [agents.public(r) for r in rows]}


@app.post("/rest/v1/accounts/{account_name}/agents", status_code=201)
async def create_agent(
    account_name: str, body: dict = Body(), caller: Principal = Caller,
) -> dict:
    auth.require_admin(caller)
    agent_name = body.get("name")
    if not isinstance(agent_name, str) or not agent_name.strip():
        raise ApiError(400, "INVALID_PARAMETER", "agent name is required")
    proxy_grants = body.get("proxy_grants", [])
    if not isinstance(proxy_grants, list):
        raise ApiError(400, "INVALID_PARAMETER", "proxy_grants must be a list")

    async with db.pool().acquire() as conn:
        account_id = await _account_id_or_404(conn, account_name)
        try:
            row, raw_token = await agents.create(
                conn, account_id, agent_name.strip(), proxy_grants,
            )
        except asyncpg.UniqueViolationError:
            raise ApiError(
                409, "ALREADY_EXISTS",
                f"agent {agent_name!r} already exists",
            )
    return agents.public(row, token=raw_token)


@app.get("/rest/v1/accounts/{account_name}/agents/{agent_name}")
async def get_agent(
    account_name: str, agent_name: str, caller: Principal = Caller,
) -> dict:
    auth.require_admin(caller)
    async with db.pool().acquire() as conn:
        await _account_id_or_404(conn, account_name)
        row = await agents.get(conn, agent_name)
    if row is None:
        raise ApiError(404, "NOT_FOUND", f"no agent named {agent_name!r}")
    return agents.public(row)


@app.patch("/rest/v1/accounts/{account_name}/agents/{agent_name}")
async def update_agent(
    account_name: str, agent_name: str,
    body: dict = Body(), caller: Principal = Caller,
) -> dict:
    auth.require_admin(caller)
    proxy_grants = body.get("proxy_grants")
    disabled = body.get("disabled")

    if proxy_grants is None and disabled is None:
        raise ApiError(400, "INVALID_PARAMETER", "proxy_grants or disabled is required")
    if proxy_grants is not None and not isinstance(proxy_grants, list):
        raise ApiError(400, "INVALID_PARAMETER", "proxy_grants must be a list")
    if disabled is not None and not isinstance(disabled, bool):
        raise ApiError(400, "INVALID_PARAMETER", "disabled must be a boolean")

    async with db.pool().acquire() as conn:
        await _account_id_or_404(conn, account_name)
        if proxy_grants is not None:
            row = await agents.update_grants(conn, agent_name, proxy_grants)
        else:
            row = await agents.get(conn, agent_name)
        if row is None:
            raise ApiError(404, "NOT_FOUND", f"no agent named {agent_name!r}")
        if disabled is not None:
            await agents.disable(conn, agent_name, disabled)
            row = await agents.get(conn, agent_name)
    return agents.public(row)


@app.post("/rest/v1/accounts/{account_name}/agents/{agent_name}/token")
async def mint_agent_token(
    account_name: str, agent_name: str, caller: Principal = Caller,
) -> dict:
    """Replace an agent's bearer token. The previous one stops working."""
    auth.require_admin(caller)
    async with db.pool().acquire() as conn:
        await _account_id_or_404(conn, account_name)
        raw_token, found = await agents.mint_token(conn, agent_name)
    if not found:
        raise ApiError(404, "NOT_FOUND", f"no agent named {agent_name!r}")
    return {"agent": agent_name, "token": raw_token}


@app.delete("/rest/v1/accounts/{account_name}/agents/{agent_name}")
async def delete_agent(
    account_name: str, agent_name: str, caller: Principal = Caller,
) -> dict:
    auth.require_admin(caller)
    async with db.pool().acquire() as conn:
        await _account_id_or_404(conn, account_name)
        if not await agents.delete(conn, agent_name):
            raise ApiError(404, "NOT_FOUND", f"no agent named {agent_name!r}")
    return {"agent": agent_name, "deleted": True}


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
    if not caller.all_repos() and not caller.is_admin:
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

    The wildcard has to be caught here rather than passed through. `*` is a
    pattern, and everything below this line treats the list as literal names --
    `= ANY(ARRAY['*'])` asks for a repository called `*` and finds none, so an
    unrestricted caller would see an empty list instead of all of it.
    """
    if caller.all_repos():
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
            f"{crypto.KEY_ENV_PREFIX}* is not configured, so connection secrets "
            f"cannot be stored. Generate a key with "
            f"`python -m datum_sync.crypto`.",
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


# -- account tokens -------------------------------------------------------


@app.get("/rest/v1/accounts/{name}/tokens")
async def list_account_tokens(name: str, caller: Principal = Caller) -> dict[str, Any]:
    """List live tokens for an account. Admin only."""
    auth.require_admin(caller)
    async with db.pool().acquire() as conn:
        account_id = await conn.fetchval(
            "SELECT id FROM service_accounts WHERE name = $1", name
        )
        if account_id is None:
            raise ApiError(404, "NOT_FOUND", f"no such account: {name}")
        rows = await tokens.list_for_account(conn, account_id)
    return {"items": [tokens.public(r) for r in rows]}


@app.post("/rest/v1/accounts/{name}/tokens", status_code=201)
async def mint_account_token(
    name: str, body: dict = Body(), caller: Principal = Caller
) -> dict[str, Any]:
    """Mint a new labelled token for an account. Returns the raw token once. Admin only."""
    auth.require_admin(caller)
    label = str(body.get("label") or "").strip()
    if not label:
        raise ApiError(400, "INVALID_PARAMETER", "label is required")
    expires_days = body.get("expires_days")
    expires_at = None
    if expires_days is not None:
        # `int()` on caller-supplied JSON: unguarded this raised ValueError
        # inside the handler and surfaced as a 500, reporting a bad parameter as
        # a server fault. `True` is excluded because bool is an int subclass and
        # `{"expires_days": true}` would otherwise quietly mean one day.
        if isinstance(expires_days, bool) or not isinstance(expires_days, (int, str)):
            raise ApiError(400, "INVALID_PARAMETER", "expires_days must be a number")
        try:
            days = int(expires_days)
        except ValueError:
            raise ApiError(400, "INVALID_PARAMETER", "expires_days must be a number")
        if days < 1:
            raise ApiError(400, "INVALID_PARAMETER", "expires_days must be positive")
        expires_at = datetime.now(timezone.utc) + timedelta(days=days)
    async with db.pool().acquire() as conn:
        account_id = await conn.fetchval(
            "SELECT id FROM service_accounts WHERE name = $1", name
        )
        if account_id is None:
            raise ApiError(404, "NOT_FOUND", f"no such account: {name}")
        try:
            row, raw = await tokens.create(conn, account_id, label, expires_at)
        except asyncpg.UniqueViolationError:
            # Narrow on purpose. Catching bare Exception here reported every
            # failure -- a dropped connection, a bug in tokens.create -- as
            # "that label is taken", which is a wrong answer that looks like a
            # correct one and sends the caller off renaming their token.
            raise ApiError(
                409, "LABEL_TAKEN",
                f"account {name!r} already has a live token labelled {label!r}"
            )
    return tokens.public(row, raw)


@app.delete("/rest/v1/accounts/{name}/tokens/{label}")
async def revoke_account_token(
    name: str, label: str, caller: Principal = Caller
) -> dict[str, Any]:
    """Revoke one token by label. Admin only."""
    auth.require_admin(caller)
    async with db.pool().acquire() as conn:
        account_id = await conn.fetchval(
            "SELECT id FROM service_accounts WHERE name = $1", name
        )
        if account_id is None:
            raise ApiError(404, "NOT_FOUND", f"no such account: {name}")
        revoked = await tokens.revoke(conn, account_id, label)
    if not revoked:
        raise ApiError(
            404, "NOT_FOUND",
            f"no live token labelled {label!r} on account {name!r}"
        )
    return {}


# -- analytics -------------------------------------------------------


@app.get("/rest/v1/analytics/summary")
async def analytics_summary(caller: Principal = Caller) -> dict[str, Any]:
    """Platform-wide usage summary: jobs, audit trail, and MCP calls.

    Admin only. The `recent` block is the last 20 `audit_log` rows across every
    principal -- the governance record of who did what, which must not be
    readable by the parties it governs. Narrowing rather than refusing is not
    available here: an audit `target` is free text with no owning repository, so
    there is nothing to filter on without inventing a per-row ownership model.
    """
    auth.require_admin(caller)
    async with db.pool().acquire() as conn:
        job_rows = await conn.fetch(
            "SELECT status, count(*) AS n FROM jobs GROUP BY status ORDER BY n DESC"
        )
        audit_verbs = await conn.fetch(
            """SELECT verb, outcome, count(*) AS n
               FROM audit_log GROUP BY verb, outcome ORDER BY n DESC"""
        )
        mcp_tools = await conn.fetch(
            """SELECT coalesce(nullif(tool_name, ''), '(other)') AS tool,
                      count(*) AS n,
                      count(*) FILTER (WHERE outcome = 'error') AS errors
               FROM mcp_call_log
               GROUP BY tool ORDER BY n DESC LIMIT 10"""
        )
        mcp_total = await conn.fetchval("SELECT count(*) FROM mcp_call_log")
        recent = await conn.fetch(
            """SELECT actor_name, verb, target, outcome, duration_ms, created_at
               FROM audit_log ORDER BY created_at DESC LIMIT 20"""
        )
    return {
        "jobs": {
            "total": sum(r["n"] for r in job_rows),
            "by_status": {r["status"]: r["n"] for r in job_rows},
        },
        "audit": {
            "total": sum(r["n"] for r in audit_verbs),
            "by_verb": [
                {"verb": r["verb"], "outcome": r["outcome"], "count": r["n"]}
                for r in audit_verbs
            ],
            "recent": [
                {
                    "actor": r["actor_name"],
                    "verb": r["verb"],
                    "target": r["target"],
                    "outcome": r["outcome"],
                    "duration_ms": r["duration_ms"],
                    "created_at": r["created_at"].isoformat(),
                }
                for r in recent
            ],
        },
        "mcp": {
            "total": mcp_total,
            "top_tools": [
                {"tool": r["tool"], "count": r["n"], "errors": r["errors"]}
                for r in mcp_tools
            ],
        },
    }


# -- queue control -------------------------------------------------------


@app.get("/rest/v1/queue")
async def queue_status(caller: Principal = Caller) -> dict[str, Any]:
    """Active jobs: queued and running. Admin only."""
    auth.require_admin(caller)
    async with db.pool().acquire() as conn:
        rows = await conn.fetch(
            """SELECT id, repository, workspace, status, submitted_by,
                      submitted_at, started_at
               FROM jobs
               WHERE status IN ('queued', 'running')
               ORDER BY submitted_at ASC"""
        )
    return {
        "items": [
            {
                "id": str(r["id"]),
                "repository": r["repository"],
                "workspace": r["workspace"],
                "status": r["status"],
                "submitted_by": r["submitted_by"],
                "submitted_at": r["submitted_at"].isoformat(),
                "started_at": r["started_at"].isoformat() if r["started_at"] else None,
            }
            for r in rows
        ]
    }


# -- system configuration -------------------------------------------------------


@app.get("/rest/v1/system/config")
async def system_config_view(caller: Principal = Caller) -> dict[str, Any]:
    """Runtime configuration and applied migrations. Admin only."""
    auth.require_admin(caller)
    async with db.pool().acquire() as conn:
        migrations = await conn.fetch(
            "SELECT filename, applied_at FROM schema_migrations ORDER BY applied_at"
        )
    return {
        "server": {
            "host": config.HOST,
            "port": config.PORT,
            "public_url": config.PUBLIC_URL,
            "auth_disabled": config.AUTH_DISABLED,
            "require_https": config.REQUIRE_HTTPS,
        },
        "limits": {
            "access_token_ttl_seconds": config.ACCESS_TOKEN_TTL_SECONDS,
            "refresh_token_ttl_seconds": config.REFRESH_TOKEN_TTL_SECONDS,
            "session_ttl_seconds": config.SESSION_TTL_SECONDS,
            "password_max_attempts": config.PASSWORD_MAX_ATTEMPTS,
        },
        "paths": {
            "repositories": str(config.REPOSITORIES_PATH),
            "data": str(config.DATA_PATH),
            "vault": str(config.VAULT_PATH),
        },
        "migrations": [
            {"filename": r["filename"], "applied_at": r["applied_at"].isoformat()}
            for r in migrations
        ],
    }


# -- notifications -------------------------------------------------------


@app.get("/rest/v1/notifications")
async def list_notifications(caller: Principal = Caller) -> dict[str, Any]:
    """Recent errors and failures derived from the audit log and jobs table.

    Unlike the other dashboard reads this stays open to a non-admin, because a
    caller seeing its own repositories fail is the point of the route. Each half
    is narrowed separately:

    `audit_errors` is admin-only -- same reasoning as `analytics/summary`, there
    is no owning repository on an audit row. A non-admin gets an empty list
    rather than a 403 so the other half is still reachable.

    `failed_jobs` is filtered by `allows_repo`, matching what the jobs routes
    already do. It matters more here than it looks: `jobs.error` is whatever the
    workspace printed on its way down, which is where a connection string or a
    filesystem path ends up.

    Filtering happens in Python, after the query, for the same reason the jobs
    routes do it that way -- `repo_scope` is a glob list and `_scope_matches` is
    the one implementation of what a glob means. The LIMIT is applied before the
    filter, so a caller can see fewer than 20 of its own jobs when other
    repositories are failing; that is a display quirk, not a leak.
    """
    async with db.pool().acquire() as conn:
        audit_errors = (
            await conn.fetch(
                """SELECT actor_name, verb, target, error_code, trace_id, created_at
                   FROM audit_log
                   WHERE outcome = 'error'
                   ORDER BY created_at DESC LIMIT 50"""
            )
            if caller.is_admin
            else []
        )
        failed_jobs = await conn.fetch(
            """SELECT id, repository, workspace, submitted_by, completed_at, error
               FROM jobs
               WHERE status = 'failed'
               ORDER BY completed_at DESC NULLS LAST LIMIT 20"""
        )
    failed_jobs = [r for r in failed_jobs if caller.allows_repo(r["repository"])]
    return {
        "audit_errors": [
            {
                "actor": r["actor_name"],
                "verb": r["verb"],
                "target": r["target"],
                "error_code": r["error_code"],
                "trace_id": str(r["trace_id"]),
                "created_at": r["created_at"].isoformat(),
            }
            for r in audit_errors
        ],
        "failed_jobs": [
            {
                "id": str(r["id"]),
                "repository": r["repository"],
                "workspace": r["workspace"],
                "submitted_by": r["submitted_by"],
                "error": r["error"],
                "completed_at": r["completed_at"].isoformat() if r["completed_at"] else None,
            }
            for r in failed_jobs
        ],
    }


# -- MCP servers -------------------------------------------------------


@app.get("/rest/v1/mcp-servers")
async def list_mcp_servers(caller: Principal = Caller) -> dict[str, Any]:
    """MCP servers seen in the call log, with usage summary.

    Admin only. `mcp_call_log.target` is a proxy destination URL, so this is a
    list of the internal hosts the platform can reach -- reconnaissance for any
    account that can read it. As with the analytics summary there is no owning
    repository on the row to filter by.
    """
    auth.require_admin(caller)
    async with db.pool().acquire() as conn:
        rows = await conn.fetch(
            """SELECT coalesce(target, '(unknown)') AS target,
                      count(*) AS calls,
                      count(*) FILTER (WHERE outcome = 'error') AS errors,
                      max(created_at) AS last_seen
               FROM mcp_call_log
               GROUP BY target
               ORDER BY calls DESC"""
        )
    return {
        "items": [
            {
                "target": r["target"],
                "calls": r["calls"],
                "errors": r["errors"],
                "last_seen": r["last_seen"].isoformat(),
            }
            for r in rows
        ]
    }


# -- authentication services -------------------------------------------------------


@app.get("/rest/v1/auth/clients")
async def list_auth_clients(caller: Principal = Caller) -> dict[str, Any]:
    """Registered OAuth clients, newest first. Admin only.

    Bounded, and `total` says by how much. `oauth_clients` grows on its own --
    dynamic client registration means anything that speaks the protocol can add
    a row without an operator involved, and this instance was holding 3056 when
    the screen was first opened. Unbounded, the route joined all of them against
    `oauth_tokens`, aggregated per client and rendered 390 KB into one table.
    Every sibling read added here has a LIMIT; this one did not, and the reason
    is only that the table was empty while it was being written.

    `ORDER BY created_at` was also ascending, so the 100 a truncated list would
    have shown were the oldest -- exactly the ones an operator does not need.
    """
    auth.require_admin(caller)
    async with db.pool().acquire() as conn:
        rows = await conn.fetch(
            """SELECT c.client_id, c.client_name, c.redirect_uris,
                      c.grant_types, c.created_at,
                      count(t.id) FILTER (
                          WHERE t.kind = 'refresh' AND t.revoked_at IS NULL
                            AND t.rotated_to IS NULL
                      ) AS active_grants
               FROM oauth_clients c
               LEFT JOIN oauth_tokens t ON t.client_id = c.client_id
               GROUP BY c.client_id
               ORDER BY c.created_at DESC LIMIT 100"""
        )
        total = await conn.fetchval("SELECT count(*) FROM oauth_clients")
    return {
        "total": total,
        "items": [
            {
                "client_id": r["client_id"],
                "name": r["client_name"],
                "redirect_uris": list(r["redirect_uris"]),
                "grant_types": list(r["grant_types"]),
                "active_grants": r["active_grants"],
                "created_at": r["created_at"].isoformat(),
            }
            for r in rows
        ]
    }


def main() -> int:
    import uvicorn

    uvicorn.run(app, host=config.HOST, port=config.PORT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
