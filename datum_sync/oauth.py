"""OAuth 2.1 authorization server, sized for one job: letting an MCP client
such as Claude.ai obtain a token bound to a service account.

Endpoints:

    GET  /.well-known/oauth-protected-resource   RFC 9728
    GET  /.well-known/oauth-authorization-server  RFC 8414
    POST /oauth/register                          RFC 7591 (dynamic registration)
    GET  /oauth/authorize                         login + consent screen
    POST /oauth/authorize                         consent submitted
    POST /oauth/token                             code exchange, refresh rotation
    POST /oauth/revoke                            RFC 7009

Why hand-rolled rather than a library: the parts a library would give us
(code exchange, PKCE) are the small half. Dynamic client registration, the two
discovery documents and RFC 8707 audience binding are the half that MCP
requires and that we would be writing regardless, and the mainstream Python
authorization-server libraries are shaped for Flask/Django session handling
that this app does not have.

**These endpoints do not use the project's error envelope.** OAuth clients
parse `{"error": "invalid_grant", ...}` as defined by RFC 6749 §5.2, and a
client that cannot read our failure will retry forever rather than
re-authorize. One envelope everywhere is the rule; interoperability with a
wire format we do not own is the exception, and it stops at this module.
"""
from __future__ import annotations

import base64
import hashlib
import html
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode, urlparse

import asyncpg
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from datum_sync import auth, config, db

router = APIRouter()

# The only PKCE method we accept. RFC 7636 also permits 'plain', which sends
# the verifier in the clear and defeats the point of the exchange.
CODE_CHALLENGE_METHOD = "S256"

GRANT_TYPES = ["authorization_code", "refresh_token"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


# -- OAuth-shaped errors ---------------------------------------------------


class OAuthError(Exception):
    def __init__(self, error: str, description: str, status: int = 400) -> None:
        super().__init__(description)
        self.error = error
        self.description = description
        self.status = status

    def response(self) -> JSONResponse:
        body = {"error": self.error, "error_description": self.description}
        headers = {"Cache-Control": "no-store", "Pragma": "no-cache"}
        if self.status == 401:
            headers["WWW-Authenticate"] = "Basic"
        return JSONResponse(status_code=self.status, content=body, headers=headers)


def install(app) -> None:
    @app.exception_handler(OAuthError)
    async def _oauth_error(_: Request, exc: OAuthError) -> JSONResponse:
        return exc.response()

    app.include_router(router)


# -- discovery -------------------------------------------------------------


@router.get("/.well-known/oauth-protected-resource")
async def protected_resource_metadata() -> dict[str, Any]:
    """RFC 9728. Reached from the WWW-Authenticate challenge on a 401.

    This is what turns a bare 401 into something a client can act on: it names
    the resource identifier the client must ask for (`resource`) and which
    authorization server to ask.
    """
    return {
        "resource": auth.MCP_RESOURCE,
        "authorization_servers": [config.PUBLIC_URL],
        "bearer_methods_supported": ["header"],
    }


@router.get("/.well-known/oauth-authorization-server")
async def authorization_server_metadata() -> dict[str, Any]:
    """RFC 8414."""
    return {
        "issuer": config.PUBLIC_URL,
        "authorization_endpoint": f"{config.PUBLIC_URL}/oauth/authorize",
        "token_endpoint": f"{config.PUBLIC_URL}/oauth/token",
        "registration_endpoint": f"{config.PUBLIC_URL}/oauth/register",
        "revocation_endpoint": f"{config.PUBLIC_URL}/oauth/revoke",
        "response_types_supported": ["code"],
        "grant_types_supported": GRANT_TYPES,
        "code_challenge_methods_supported": [CODE_CHALLENGE_METHOD],
        "token_endpoint_auth_methods_supported": ["none", "client_secret_post"],
    }


# -- dynamic client registration -------------------------------------------


def _valid_redirect_uri(raw: str) -> bool:
    """https, or http on loopback.

    Claude.ai's callback is https. Loopback http is permitted because that is
    how a desktop client receives a redirect, and OAuth 2.1 carves it out
    explicitly. Everything else -- custom schemes, http to a real host -- is
    refused: an http redirect hands the code to anyone on the path.
    """
    try:
        u = urlparse(raw)
    except ValueError:
        return False
    if u.fragment:
        # RFC 6749 §3.1.2: a redirect URI must not carry a fragment, because
        # the authorization response appends its own.
        return False
    if u.scheme == "https":
        return bool(u.netloc)
    if u.scheme == "http":
        return u.hostname in ("localhost", "127.0.0.1", "::1")
    return False


@router.post("/oauth/register", status_code=201)
async def register(body: dict[str, Any]) -> JSONResponse:
    """RFC 7591. Open registration.

    Claude.ai has no client id before it first sees this server, so
    pre-registration is not an option and the endpoint is unauthenticated by
    design. What that gets an attacker is a client id -- which grants nothing
    on its own, because every token still requires a human to log in at the
    consent screen with an account that exists here.
    """
    uris = body.get("redirect_uris")
    if not isinstance(uris, list) or not uris:
        raise OAuthError("invalid_redirect_uri", "redirect_uris is required")
    if not all(isinstance(u, str) and _valid_redirect_uri(u) for u in uris):
        raise OAuthError(
            "invalid_redirect_uri",
            "each redirect_uri must be https, or http on loopback, "
            "and must not contain a fragment",
        )

    grants = body.get("grant_types") or ["authorization_code", "refresh_token"]
    unsupported = sorted(set(grants) - set(GRANT_TYPES))
    if unsupported:
        raise OAuthError(
            "invalid_client_metadata", f"unsupported grant_types: {unsupported}"
        )

    client_id = secrets.token_urlsafe(16)
    # A client that asks for 'none' is public and gets no secret; that is what
    # MCP clients use, paired with PKCE.
    public = body.get("token_endpoint_auth_method", "none") == "none"
    secret = None if public else auth.new_token()

    async with db.pool().acquire() as conn:
        await conn.execute(
            """
            INSERT INTO oauth_clients
                (client_id, client_name, secret_hash, redirect_uris, grant_types)
            VALUES ($1, $2, $3, $4, $5)
            """,
            client_id,
            body.get("client_name"),
            auth.hash_token(secret) if secret else None,
            uris,
            grants,
        )

    out: dict[str, Any] = {
        "client_id": client_id,
        "client_name": body.get("client_name"),
        "redirect_uris": uris,
        "grant_types": grants,
        "token_endpoint_auth_method": "none" if public else "client_secret_post",
        # 0 = does not expire (RFC 7591 §3.2.1).
        "client_id_issued_at": int(_now().timestamp()),
    }
    if secret:
        out["client_secret"] = secret
        out["client_secret_expires_at"] = 0
    return JSONResponse(status_code=201, content=out, headers={"Cache-Control": "no-store"})


# -- authorize -------------------------------------------------------------


_CHALLENGE_RE = re.compile(r"^[A-Za-z0-9._~-]{43,128}$")


async def _client(conn: asyncpg.Connection, client_id: str | None) -> asyncpg.Record:
    if not client_id:
        raise OAuthError("invalid_request", "client_id is required")
    row = await conn.fetchrow(
        "SELECT * FROM oauth_clients WHERE client_id = $1", client_id
    )
    if row is None:
        raise OAuthError("invalid_client", "unknown client_id", status=401)
    return row


def _redirect_uri(client: asyncpg.Record, requested: str | None) -> str:
    """Exact match against the registered list, or the sole registered URI.

    Not prefix matching and not a wildcard: 'starts with the registered value'
    accepts `https://claude.ai.evil.test/...` and turns this endpoint into an
    open redirector that hands out authorization codes.
    """
    registered = list(client["redirect_uris"])
    if requested is None:
        if len(registered) == 1:
            return registered[0]
        raise OAuthError(
            "invalid_request",
            "redirect_uri is required when the client registered more than one",
        )
    if requested not in registered:
        raise OAuthError("invalid_request", "redirect_uri is not registered")
    return requested


def _check_resource(resource: str | None) -> str:
    """RFC 8707. The audience the resulting token will be bound to.

    A client that asks for a different resource is asking us to mint a token
    for someone else, and we would have no way to know it was misused.
    """
    if resource is None:
        # MCP clients send it; scripted callers of our own token endpoint may
        # not. Defaulting to this server is the only audience we can honestly
        # issue for.
        return auth.MCP_RESOURCE
    if auth.canonical_resource(resource) != auth.canonical_resource(auth.MCP_RESOURCE):
        raise OAuthError(
            "invalid_target", f"this server does not issue tokens for {resource}"
        )
    return auth.MCP_RESOURCE


def _error_redirect(uri: str, error: str, description: str, state: str | None):
    params = {"error": error, "error_description": description}
    if state:
        params["state"] = state
    joiner = "&" if urlparse(uri).query else "?"
    return RedirectResponse(f"{uri}{joiner}{urlencode(params)}", status_code=302)


def _fail_page(message: str) -> HTMLResponse:
    """Errors that must not be redirected.

    An unknown client, or a redirect_uri that is not registered, means the
    place the request wants us to send the error is not a place we trust. The
    only safe audience for it is the person at the browser.
    """
    return HTMLResponse(_page(f"<h1>Cannot continue</h1><p>{html.escape(message)}</p>"), 400)


_STYLE = """
:root { --navy:#1D3A5C; --amber:#C89632; --dark:#0F1923; --line:#2d4e76; }
* { box-sizing:border-box; }
body { margin:0; min-height:100vh; display:flex; align-items:center;
       justify-content:center; background:var(--dark); color:#e8edf3;
       font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; }
.card { width:100%; max-width:380px; padding:32px; background:var(--navy);
        border:1px solid var(--line); border-radius:10px; }
h1 { margin:0 0 4px; font-size:19px; font-weight:600; }
p { margin:0 0 20px; color:#a9bcd0; font-size:13px; }
strong { color:var(--amber); font-weight:600; }
label { display:block; margin-bottom:6px; font-size:12px; color:#a9bcd0;
        text-transform:uppercase; letter-spacing:.04em; }
input { width:100%; padding:10px 12px; margin-bottom:16px; border-radius:6px;
        border:1px solid var(--line); background:var(--dark); color:#e8edf3;
        font-size:14px; }
input:focus { outline:2px solid var(--amber); outline-offset:-1px; }
button { width:100%; padding:11px; border:0; border-radius:6px; cursor:pointer;
         background:var(--amber); color:var(--dark); font-size:14px;
         font-weight:600; }
.err { padding:10px 12px; margin-bottom:16px; border-radius:6px; font-size:13px;
       background:#4a1d1d; border:1px solid #7a2e2e; color:#f3c4c4; }
.scope { margin:0 0 20px; padding:10px 12px; border-radius:6px; font-size:13px;
         background:rgba(0,0,0,.2); border:1px solid var(--line); color:#a9bcd0; }
"""


def _page(inner: str) -> str:
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width,initial-scale=1">'
        f"<title>Datum-Sync</title><style>{_STYLE}</style></head>"
        f"<body><div class=card>{inner}</div></body></html>"
    )


def _consent_page(client_name: str, hidden: dict[str, str], error: str | None) -> str:
    fields = "".join(
        f'<input type=hidden name="{html.escape(k)}" value="{html.escape(v)}">'
        for k, v in hidden.items()
        if v is not None
    )
    banner = f'<div class=err>{html.escape(error)}</div>' if error else ""
    name = html.escape(client_name or "An application")
    return _page(
        f"<h1>Authorize {name}</h1>"
        "<p>Sign in with your Datum-Sync account to continue.</p>"
        f"{banner}"
        f"<div class=scope><strong>{name}</strong> will be able to run and read "
        "the workspaces your account can already reach. It gains no permissions "
        "of its own.</div>"
        '<form method=post action="/oauth/authorize" autocomplete=off>'
        f"{fields}"
        "<label for=u>Account</label>"
        "<input id=u name=username autocomplete=username autofocus required>"
        "<label for=p>Password</label>"
        "<input id=p name=password type=password autocomplete=current-password required>"
        "<button type=submit>Authorize</button>"
        "</form>"
    )


@router.get("/oauth/authorize")
async def authorize_form(request: Request):
    q = request.query_params
    async with db.pool().acquire() as conn:
        try:
            client = await _client(conn, q.get("client_id"))
            redirect_uri = _redirect_uri(client, q.get("redirect_uri"))
        except OAuthError as exc:
            return _fail_page(exc.description)

    state = q.get("state")
    try:
        if q.get("response_type") != "code":
            raise OAuthError("unsupported_response_type", "response_type must be code")
        if q.get("code_challenge_method", CODE_CHALLENGE_METHOD) != CODE_CHALLENGE_METHOD:
            raise OAuthError(
                "invalid_request", f"code_challenge_method must be {CODE_CHALLENGE_METHOD}"
            )
        challenge = q.get("code_challenge") or ""
        if not _CHALLENGE_RE.match(challenge):
            raise OAuthError("invalid_request", "a valid PKCE code_challenge is required")
        resource = _check_resource(q.get("resource"))
    except OAuthError as exc:
        return _error_redirect(redirect_uri, exc.error, exc.description, state)

    hidden = {
        "client_id": client["client_id"],
        "redirect_uri": redirect_uri,
        "code_challenge": challenge,
        "resource": resource,
        "state": state or "",
        "scope": q.get("scope") or "",
    }
    return HTMLResponse(_consent_page(client["client_name"], hidden, None))


@router.post("/oauth/authorize")
async def authorize_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    client_id: str = Form(...),
    redirect_uri: str = Form(...),
    code_challenge: str = Form(...),
    resource: str = Form(""),
    state: str = Form(""),
    scope: str = Form(""),
):
    """The consent screen posts back.

    Every hidden field is revalidated from scratch. They travelled through the
    user's browser, so they are caller-supplied input on this request no matter
    that we rendered them ourselves; trusting `redirect_uri` here because the
    GET checked it would let anyone post their own.

    There is no CSRF token because there is no session to ride: the credential
    is submitted in this same request, so a forged post can only succeed by
    already knowing the password.
    """
    async with db.pool().acquire() as conn:
        try:
            client = await _client(conn, client_id)
            redirect_uri = _redirect_uri(client, redirect_uri)
        except OAuthError as exc:
            return _fail_page(exc.description)

        try:
            if not _CHALLENGE_RE.match(code_challenge):
                raise OAuthError("invalid_request", "invalid PKCE code_challenge")
            audience = _check_resource(resource or None)
        except OAuthError as exc:
            return _error_redirect(redirect_uri, exc.error, exc.description, state)

        account = await auth.authenticate_password(conn, username, password)
        if account is None:
            hidden = {
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "code_challenge": code_challenge,
                "resource": audience,
                "state": state,
                "scope": scope,
            }
            return HTMLResponse(
                _consent_page(
                    client["client_name"], hidden, "Incorrect account or password."
                ),
                status_code=401,
            )

        code = secrets.token_urlsafe(32)
        await conn.execute(
            """
            INSERT INTO oauth_codes
                (code, client_id, account_id, redirect_uri, code_challenge,
                 resource, scope, expires_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            """,
            code,
            client_id,
            account["id"],
            redirect_uri,
            code_challenge,
            audience,
            scope or None,
            _now() + timedelta(seconds=config.AUTH_CODE_TTL_SECONDS),
        )

    params = {"code": code}
    if state:
        params["state"] = state
    joiner = "&" if urlparse(redirect_uri).query else "?"
    return RedirectResponse(f"{redirect_uri}{joiner}{urlencode(params)}", status_code=302)


# -- token -----------------------------------------------------------------


def _pkce_ok(verifier: str, challenge: str) -> bool:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return secrets.compare_digest(expected, challenge)


class _Reuse(Exception):
    """A credential was presented twice.

    Raised out of the transaction that detected it rather than handled inside,
    because the response is a *write* -- revoking the family -- and the same
    transaction is about to roll back. Revoking in place would be undone the
    moment the OAuth error was raised, leaving the attacker's tokens live and
    the log claiming they were revoked.
    """

    def __init__(self, client_id: str, account_id: int, message: str) -> None:
        super().__init__(message)
        self.client_id = client_id
        self.account_id = account_id
        self.message = message


async def _revoke_family(
    conn: asyncpg.Connection, client_id: str, account_id: int
) -> None:
    """Credential reuse means one of the two copies is not ours.

    Presenting an authorization code twice, or a refresh token that has already
    been rotated, is the signature of a stolen credential -- the legitimate
    client has no reason to do either. We cannot tell which presenter is the
    attacker, so every token this client holds for this account is revoked and
    the human re-authorizes. OAuth 2.1 §4.14.2.
    """
    await conn.execute(
        """
        UPDATE oauth_tokens SET revoked_at = now()
         WHERE client_id = $1 AND account_id = $2 AND revoked_at IS NULL
        """,
        client_id,
        account_id,
    )


async def _mint(
    conn: asyncpg.Connection,
    client_id: str,
    account_id: int,
    resource: str | None,
    scope: str | None,
) -> dict[str, Any]:
    access, refresh = auth.new_token(), auth.new_token()
    await conn.execute(
        """
        INSERT INTO oauth_tokens
            (token_hash, kind, client_id, account_id, resource, scope, expires_at)
        VALUES ($1, 'access', $2, $3, $4, $5, $6), ($7, 'refresh', $2, $3, $4, $5, $8)
        """,
        auth.hash_token(access),
        client_id,
        account_id,
        resource,
        scope,
        _now() + timedelta(seconds=config.ACCESS_TOKEN_TTL_SECONDS),
        auth.hash_token(refresh),
        _now() + timedelta(seconds=config.REFRESH_TOKEN_TTL_SECONDS),
    )
    out = {
        "access_token": access,
        "token_type": "Bearer",
        "expires_in": config.ACCESS_TOKEN_TTL_SECONDS,
        "refresh_token": refresh,
    }
    if scope:
        out["scope"] = scope
    return out


async def _authenticate_client(
    conn: asyncpg.Connection, client_id: str | None, client_secret: str | None
) -> asyncpg.Record:
    client = await _client(conn, client_id)
    if client["secret_hash"] is not None:
        if not client_secret or not secrets.compare_digest(
            auth.hash_token(client_secret), client["secret_hash"]
        ):
            raise OAuthError("invalid_client", "client authentication failed", status=401)
    return client


@router.post("/oauth/token")
async def token(
    grant_type: str = Form(...),
    code: str | None = Form(None),
    redirect_uri: str | None = Form(None),
    code_verifier: str | None = Form(None),
    refresh_token: str | None = Form(None),
    client_id: str | None = Form(None),
    client_secret: str | None = Form(None),
    resource: str | None = Form(None),
) -> JSONResponse:
    if grant_type not in GRANT_TYPES:
        raise OAuthError("unsupported_grant_type", f"unsupported grant_type {grant_type!r}")

    async with db.pool().acquire() as conn:
        client = await _authenticate_client(conn, client_id, client_secret)

        if grant_type == "authorization_code":
            body = await _exchange_code(
                conn, client, code, redirect_uri, code_verifier, resource
            )
        else:
            body = await _rotate_refresh(conn, client, refresh_token, resource)

    return JSONResponse(
        content=body, headers={"Cache-Control": "no-store", "Pragma": "no-cache"}
    )


async def _exchange_code(
    conn: asyncpg.Connection,
    client: asyncpg.Record,
    code: str | None,
    redirect_uri: str | None,
    code_verifier: str | None,
    resource: str | None,
) -> dict[str, Any]:
    if not code or not code_verifier:
        raise OAuthError("invalid_request", "code and code_verifier are required")

    try:
        return await _exchange_code_txn(
            conn, client, code, redirect_uri, code_verifier, resource
        )
    except _Reuse as reuse:
        await _revoke_family(conn, reuse.client_id, reuse.account_id)
        raise OAuthError("invalid_grant", reuse.message) from None


async def _exchange_code_txn(
    conn: asyncpg.Connection,
    client: asyncpg.Record,
    code: str,
    redirect_uri: str | None,
    code_verifier: str,
    resource: str | None,
) -> dict[str, Any]:
    async with conn.transaction():
        # FOR UPDATE, so two simultaneous presentations of the same code
        # serialise and the second sees used_at set rather than both passing.
        row = await conn.fetchrow(
            "SELECT * FROM oauth_codes WHERE code = $1 FOR UPDATE", code
        )
        if row is None:
            raise OAuthError("invalid_grant", "unknown authorization code")
        if row["client_id"] != client["client_id"]:
            raise OAuthError("invalid_grant", "code was issued to a different client")
        if row["used_at"] is not None:
            raise _Reuse(
                row["client_id"],
                row["account_id"],
                "authorization code has already been used",
            )
        if row["expires_at"] < _now():
            raise OAuthError("invalid_grant", "authorization code has expired")
        if redirect_uri is not None and redirect_uri != row["redirect_uri"]:
            raise OAuthError("invalid_grant", "redirect_uri does not match the request")
        if not _pkce_ok(code_verifier, row["code_challenge"]):
            raise OAuthError("invalid_grant", "PKCE verification failed")
        if resource is not None:
            # The token request must not widen the audience the human consented
            # to at the authorize step.
            if auth.canonical_resource(resource) != auth.canonical_resource(
                row["resource"] or auth.MCP_RESOURCE
            ):
                raise OAuthError("invalid_target", "resource does not match the grant")

        await conn.execute(
            "UPDATE oauth_codes SET used_at = now() WHERE code = $1", code
        )
        account = await conn.fetchrow(
            "SELECT id, disabled FROM service_accounts WHERE id = $1", row["account_id"]
        )
        if account is None or account["disabled"]:
            raise OAuthError("invalid_grant", "account is no longer active")

        return await _mint(
            conn, client["client_id"], row["account_id"], row["resource"], row["scope"]
        )


async def _rotate_refresh(
    conn: asyncpg.Connection,
    client: asyncpg.Record,
    refresh_token: str | None,
    resource: str | None,
) -> dict[str, Any]:
    if not refresh_token:
        raise OAuthError("invalid_request", "refresh_token is required")

    try:
        return await _rotate_refresh_txn(conn, client, refresh_token, resource)
    except _Reuse as reuse:
        await _revoke_family(conn, reuse.client_id, reuse.account_id)
        raise OAuthError("invalid_grant", reuse.message) from None


async def _rotate_refresh_txn(
    conn: asyncpg.Connection,
    client: asyncpg.Record,
    refresh_token: str,
    resource: str | None,
) -> dict[str, Any]:
    async with conn.transaction():
        row = await conn.fetchrow(
            """
            SELECT * FROM oauth_tokens
             WHERE token_hash = $1 AND kind = 'refresh'
             FOR UPDATE
            """,
            auth.hash_token(refresh_token),
        )
        if row is None:
            raise OAuthError("invalid_grant", "unknown refresh token")
        if row["client_id"] != client["client_id"]:
            raise OAuthError("invalid_grant", "token was issued to a different client")
        if row["rotated_to"] is not None:
            # Recognised as replay rather than as an unknown token, which is
            # the whole reason the replacement is recorded instead of the row
            # being deleted.
            raise _Reuse(
                row["client_id"],
                row["account_id"],
                "refresh token has already been used",
            )
        if row["revoked_at"] is not None:
            raise OAuthError("invalid_grant", "refresh token has been revoked")
        if row["expires_at"] is not None and row["expires_at"] < _now():
            raise OAuthError("invalid_grant", "refresh token has expired")
        if resource is not None and auth.canonical_resource(
            resource
        ) != auth.canonical_resource(row["resource"] or auth.MCP_RESOURCE):
            raise OAuthError("invalid_target", "resource does not match the grant")

        account = await conn.fetchrow(
            "SELECT id, disabled FROM service_accounts WHERE id = $1", row["account_id"]
        )
        if account is None or account["disabled"]:
            raise OAuthError("invalid_grant", "account is no longer active")

        body = await _mint(
            conn, client["client_id"], row["account_id"], row["resource"], row["scope"]
        )
        replacement = await conn.fetchval(
            "SELECT id FROM oauth_tokens WHERE token_hash = $1",
            auth.hash_token(body["refresh_token"]),
        )
        await conn.execute(
            """
            UPDATE oauth_tokens
               SET revoked_at = now(), rotated_to = $2, last_used_at = now()
             WHERE id = $1
            """,
            row["id"],
            replacement,
        )
        return body


# -- revoke ----------------------------------------------------------------


@router.post("/oauth/revoke")
async def revoke(
    token: str = Form(...),
    token_type_hint: str | None = Form(None),
    client_id: str | None = Form(None),
    client_secret: str | None = Form(None),
):
    """RFC 7009.

    Always 200, even for a token that does not exist. Distinguishing 'revoked'
    from 'never existed' would turn this into an oracle for guessing tokens,
    and the client's outcome is identical either way.
    """
    async with db.pool().acquire() as conn:
        client = await _authenticate_client(conn, client_id, client_secret)
        # Scoped to the presenting client: otherwise any registered client can
        # revoke any other client's tokens by guessing.
        await conn.execute(
            """
            UPDATE oauth_tokens SET revoked_at = now()
             WHERE token_hash = $1 AND client_id = $2 AND revoked_at IS NULL
            """,
            auth.hash_token(token),
            client["client_id"],
        )
    return JSONResponse(content={}, headers={"Cache-Control": "no-store"})
