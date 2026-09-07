"""The web UI: its shell, its assets, and the sign-in that mints a session.

Everything with data in it lives under `/rest/v1/`; this module serves markup
and handles the one thing the REST API cannot, which is turning a name and
password into a credential a browser can carry.

The shell is public. It contains no data -- it asks `/rest/v1/whoami` who the
viewer is and draws a sign-in form if the answer is 401 -- so gating it would
only mean serving a 401 page instead of a sign-in page.
"""
from __future__ import annotations

from fastapi import APIRouter, Body, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from datum_sync import audit, auth, config, db
from datum_sync.errors import ApiError

STATIC_DIR = config.REPO_ROOT / "datum_sync" / "static"
STATIC_V2_DIR = config.REPO_ROOT / "datum_sync" / "static-v2"

router = APIRouter(tags=["ui"])


class _RevalidatedStatics(StaticFiles):
    """Static assets that must be asked about, not assumed.

    StaticFiles sends an ETag and a Last-Modified but no Cache-Control, and a
    response with neither a Cache-Control nor an Expires is one the browser is
    free to *heuristically* cache -- roughly a tenth of the file's age, without
    asking. That is the bad case: not a stale file served after a check, but a
    stale file served with no check at all, so nothing the server does can
    dislodge it. It shipped a CSS change to a demo audience who saw the old
    stylesheet and no error.

    `no-cache` does not mean "do not store"; it means "revalidate before use".
    The ETag still does the work -- an unchanged asset is a 304 with no body --
    so this costs one conditional request per asset per load and removes the
    guess. There is no build step here to hash filenames with, which is the
    other way to solve this and is not worth adding for six files.
    """

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


def install(app: FastAPI) -> None:
    app.include_router(router)
    app.mount("/ui/static", _RevalidatedStatics(directory=STATIC_DIR), name="ui-static")
    # The v2 reskin, served alongside v1 rather than over it, so the two can be
    # opened side by side while the port runs. `/v2` is the prefix static-v2's
    # markup already names in its <link> and <script> tags.
    app.mount("/v2", _RevalidatedStatics(directory=STATIC_V2_DIR), name="ui-static-v2")


@router.get("/ui", include_in_schema=False)
async def shell() -> FileResponse:
    # The shell names the assets, so caching it hides a change to any of them.
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


@router.get("/ui/v2", include_in_schema=False)
async def shell_v2() -> FileResponse:
    return FileResponse(STATIC_V2_DIR / "index.html", headers={"Cache-Control": "no-cache"})


@router.post("/ui/login")
async def login(request: Request, body: dict = Body(...)) -> JSONResponse:
    """Sign in. Sets the session cookie and returns the caller's identity.

    Returning the identity rather than an empty 204 saves the client a round
    trip to /whoami, which it would otherwise have to make before it could
    draw anything.
    """
    name = str(body.get("name") or "")
    password = str(body.get("password") or "")
    if not name or not password:
        raise ApiError(400, "INVALID_PARAMETER", "name and password are required")

    trace = getattr(request.state, "trace", None) or audit.Trace.mint(None)

    async with db.pool().acquire() as conn:
        try:
            account = await auth.authenticate_password(conn, name, password)
        except auth.TooManyAttempts as exc:
            await audit.write_anon(conn, trace=trace, via="ui",
                                   verb="auth.login", target_kind=None,
                                   target=None, outcome="error",
                                   error_code=429, actor_name=name,
                                   detail={"reason": "too_many_attempts"})
            raise ApiError(
                429,
                "TOO_MANY_ATTEMPTS",
                f"too many failed attempts; try again in {exc.retry_after} seconds",
                headers={"Retry-After": str(exc.retry_after)},
            ) from None
        if account is None:
            # One message for a wrong name and a wrong password. The two are
            # already indistinguishable in timing (see verify_password); saying
            # "no such account" here would give back what that bought.
            await audit.write_anon(conn, trace=trace, via="ui",
                                   verb="auth.login", target_kind=None,
                                   target=None, outcome="error",
                                   error_code=401, actor_name=name,
                                   detail={"reason": "invalid_credentials"})
            raise ApiError(401, "INVALID_CREDENTIALS", "incorrect name or password")

        raw = await auth.create_session(conn, account["id"])

    principal = auth.Principal(
        account_id=account["id"],
        name=account["name"],
        max_tier=account["max_tier"],
        repo_scope=account["repo_scope"],
        is_admin=account["is_admin"],
        vault_scope=auth.vault_scope_of(account),
        source="session",
    )
    response = JSONResponse(auth.principal_json(principal))
    response.set_cookie(
        auth.SESSION_COOKIE,
        raw,
        max_age=config.SESSION_TTL_SECONDS,
        httponly=True,
        # Lax, not Strict: Strict withholds the cookie on the first request of a
        # top-level navigation, so arriving from a bookmark or a link shows a
        # signed-out UI that becomes signed-in on reload. Lax's carve-out is
        # top-level GET, which is why the service paths that execute a workspace
        # over GET do not accept this cookie at all (COOKIE_PATHS in api.py).
        samesite="lax",
        secure=auth.session_cookie_secure(),
        path="/",
    )
    return response


@router.post("/ui/logout")
async def logout(request: Request) -> JSONResponse:
    """Sign out. Revokes the session server-side, then clears the cookie.

    Both halves matter: clearing the cookie alone leaves a credential that still
    works if it was captured, and revoking alone leaves the browser presenting a
    dead cookie on every request.

    Public, and returns 204-ish success regardless. A sign-out that fails
    because the session has already expired would leave the user looking at a
    UI they cannot leave.
    """
    raw = request.cookies.get(auth.SESSION_COOKIE)
    if raw:
        async with db.pool().acquire() as conn:
            await auth.revoke_session(conn, raw)
    response = JSONResponse({"status": "signed out"})
    response.delete_cookie(
        auth.SESSION_COOKIE,
        path="/",
        httponly=True,
        samesite="lax",
        secure=auth.session_cookie_secure(),
    )
    return response
