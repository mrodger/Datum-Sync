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

from datum_sync import auth, config, db
from datum_sync.errors import ApiError

STATIC_DIR = config.REPO_ROOT / "datum_sync" / "static"

router = APIRouter(tags=["ui"])


def install(app: FastAPI) -> None:
    app.include_router(router)
    app.mount("/ui/static", StaticFiles(directory=STATIC_DIR), name="ui-static")


@router.get("/ui", include_in_schema=False)
async def shell() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


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

    async with db.pool().acquire() as conn:
        try:
            account = await auth.authenticate_password(conn, name, password)
        except auth.TooManyAttempts as exc:
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
            raise ApiError(401, "INVALID_CREDENTIALS", "incorrect name or password")

        raw = await auth.create_session(conn, account["id"])

    principal = auth.Principal(
        account_id=account["id"],
        name=account["name"],
        max_tier=account["max_tier"],
        repo_scope=account["repo_scope"],
        connection_grants=account["connection_grants"],
        is_admin=account["is_admin"],
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
