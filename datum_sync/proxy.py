"""Credential proxy: authenticated HTTP forwarding through the connection store.

An agent calls `proxy_request` via MCP with a connection name, method, path,
optional headers and body. This module:

  1. Requires the caller to be an agent (agent_id set on the Principal).
  2. Checks the agent's proxy_grants include the named connection.
  3. Checks the account's max_tier >= the connection's tier.
  4. Fetches and unseals the connection's config + secret.
  5. Applies the auth_inject rule to build the outgoing request.
  6. Validates the upstream URL against SSRF rules.
  7. Makes the HTTP call and returns the response.

The key never reaches the agent. Request and response bodies are never logged
(they may contain PII); only the account, agent, connection, method, path,
upstream status, and timestamp are recorded.
"""
from __future__ import annotations

import base64
import ipaddress
import json
import socket
from typing import Any
from urllib.parse import urlparse

import httpx

from datum_sync import connections, crypto, db
from datum_sync.auth import Principal
from datum_sync.errors import ApiError

MAX_RESPONSE_BYTES = 1 * 1024 * 1024  # 1 MB
PROXY_TIMEOUT_SECONDS = 30
MAX_REDIRECTS = 5
ALLOWED_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"})


# -- SSRF guard ---------------------------------------------------------------


def _ip_is_blocked(ip_str: str) -> bool:
    """Reject private, loopback, link-local, reserved, multicast IPs."""
    addr = ipaddress.ip_address(ip_str)
    return (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
    )


def validate_upstream_url(url: str) -> str:
    """Validate and return the URL. Raises ApiError on SSRF or bad scheme."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ApiError(400, "INVALID_URL", "upstream URL must use http or https")
    host = parsed.hostname
    if not host:
        raise ApiError(400, "INVALID_URL", "upstream URL has no host")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise ApiError(502, "DNS_FAILED", f"cannot resolve {host}")
    for info in infos:
        if _ip_is_blocked(info[4][0]):
            raise ApiError(
                403,
                "BLOCKED_ADDRESS",
                "upstream resolves to a private or reserved address",
            )
    return url


# -- auth injection -----------------------------------------------------------


def inject_auth(
    config: dict[str, Any],
    secret: dict[str, Any],
    headers: dict[str, str],
    params: dict[str, str],
) -> None:
    """Mutate `headers` and `params` in place to inject credentials.

    The auth_inject rule in config describes how to apply secret fields to the
    outgoing request. Does nothing if no rule is configured.
    """
    rule = config.get("auth_inject")
    if rule is None:
        return

    inject_type = rule.get("type")

    if inject_type == "bearer":
        value = secret.get(rule["secret_field"])
        if not value:
            raise ApiError(
                500, "MISSING_SECRET_FIELD",
                f"secret has no {rule['secret_field']!r} for bearer injection",
            )
        headers["Authorization"] = f"Bearer {value}"

    elif inject_type == "basic":
        user = secret.get(rule.get("user_field", "username"), "")
        pwd = secret.get(rule.get("pass_field", "password"), "")
        encoded = base64.b64encode(f"{user}:{pwd}".encode()).decode()
        headers["Authorization"] = f"Basic {encoded}"

    elif inject_type == "header":
        header_name = rule["header"]
        value = secret.get(rule["secret_field"])
        if not value:
            raise ApiError(
                500, "MISSING_SECRET_FIELD",
                f"secret has no {rule['secret_field']!r} for header injection",
            )
        headers[header_name] = str(value)

    elif inject_type == "query_param":
        param_name = rule["param"]
        value = secret.get(rule["secret_field"])
        if not value:
            raise ApiError(
                500, "MISSING_SECRET_FIELD",
                f"secret has no {rule['secret_field']!r} for query_param injection",
            )
        params[param_name] = str(value)

    else:
        raise ApiError(
            500, "UNKNOWN_AUTH_INJECT",
            f"auth_inject type {inject_type!r} is not supported",
        )


# -- access checks ------------------------------------------------------------


def check_proxy_access(
    principal: Principal, conn_row, conn_name: str
) -> None:
    """Raise 403 if the caller may not proxy through this connection."""
    # Must be an agent — bare account tokens cannot proxy.
    if principal.agent_id is None:
        raise ApiError(
            403, "AGENT_REQUIRED",
            "proxy_request requires an agent token, not an account token",
        )

    # Agent must have the connection in its proxy_grants.
    if conn_name not in (principal.proxy_grants or []):
        raise ApiError(
            403, "CONNECTION_DENIED",
            f"agent {principal.agent_name!r} has no proxy grant "
            f"for connection {conn_name!r}",
        )

    # Account tier ceiling.
    if principal.max_tier < conn_row["tier"]:
        raise ApiError(
            403, "TIER_DENIED",
            f"connection {conn_name!r} is tier {conn_row['tier']}; "
            f"account {principal.name!r} is limited to tier {principal.max_tier}",
        )


# -- the proxy call -----------------------------------------------------------


async def proxy_request(
    principal: Principal,
    connection_name: str,
    method: str,
    path: str,
    headers: dict[str, str] | None = None,
    body: Any = None,
    query_params: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Execute a proxied HTTP request. Returns an MCP result dict."""
    method = method.upper()
    if method not in ALLOWED_METHODS:
        raise ApiError(
            400, "INVALID_METHOD",
            f"method must be one of {', '.join(sorted(ALLOWED_METHODS))}",
        )

    async with db.pool().acquire() as conn:
        # 1. Fetch connection metadata (no secret in this query).
        row = await connections.get(conn, connection_name)
        if row is None:
            raise ApiError(
                404, "CONNECTION_NOT_FOUND",
                f"no connection named {connection_name!r}",
            )
        if row["type"] != "http":
            raise ApiError(
                400, "WRONG_CONNECTION_TYPE",
                f"proxy_request requires an http connection, got {row['type']!r}",
            )

        # 2. Access checks — agent identity, grants, tier.
        check_proxy_access(principal, row, connection_name)

        # 3. Unseal.
        config_data = json.loads(row["config"])
        blob = await connections._sealed(conn, connection_name)
        secret_data = crypto.open_(connection_name, blob) if blob else {}

        # 4. Build upstream URL.
        base_url = config_data.get("base_url", "").rstrip("/")
        if not base_url:
            raise ApiError(
                500, "NO_BASE_URL",
                f"connection {connection_name!r} has no base_url in config",
            )
        if path and not path.startswith("/"):
            path = "/" + path
        upstream_url = base_url + (path or "")

        # 5. SSRF check.
        validate_upstream_url(upstream_url)

        # 6. Build outgoing request.
        out_headers = dict(headers or {})
        out_params = dict(query_params or {})
        inject_auth(config_data, secret_data, out_headers, out_params)

        # 7. Execute with manual redirect handling (SSRF re-validation).
        content_body = (
            json.dumps(body).encode() if body is not None else None
        )
        current_url = upstream_url
        current_method = method
        response = None

        async with httpx.AsyncClient(
            timeout=PROXY_TIMEOUT_SECONDS,
            follow_redirects=False,
            max_redirects=0,
        ) as client:
            for hop in range(MAX_REDIRECTS + 1):
                response = await client.request(
                    current_method,
                    current_url,
                    headers=out_headers if hop == 0 else _strip_auth(out_headers),
                    params=out_params if hop == 0 else None,
                    content=content_body if hop == 0 else None,
                )
                if not response.is_redirect:
                    break
                location = response.headers.get("location")
                if not location:
                    raise ApiError(
                        502, "REDIRECT_NO_LOCATION",
                        "upstream redirect without Location header",
                    )
                current_url = validate_upstream_url(
                    str(response.url.join(location))
                )
                current_method = "GET"
            else:
                raise ApiError(502, "TOO_MANY_REDIRECTS", "upstream redirect loop")

        # 8. Audit log.
        await _audit_log(
            conn, principal, connection_name, method, path,
            response.status_code,
        )

    # 9. Build MCP result.
    content_type = response.headers.get("content-type", "")
    is_text = any(
        t in content_type
        for t in ("text/", "application/json", "application/xml", "application/javascript")
    )

    raw = response.content
    truncated = len(raw) > MAX_RESPONSE_BYTES
    if truncated:
        raw = raw[:MAX_RESPONSE_BYTES]

    if is_text:
        text = raw.decode("utf-8", "replace")
    else:
        text = f"[binary response: {content_type}, {len(response.content)} bytes]"

    return {
        "content": [{"type": "text", "text": text}],
        "isError": response.status_code >= 400,
        "_meta": {
            "upstream_status": response.status_code,
            "upstream_content_type": content_type,
            "truncated": truncated,
        },
    }


def _strip_auth(headers: dict[str, str]) -> dict[str, str]:
    """Headers without auth, for redirect hops."""
    return {k: v for k, v in headers.items() if k.lower() != "authorization"}


async def _audit_log(
    conn,
    principal: Principal,
    connection_name: str,
    method: str,
    path: str,
    upstream_status: int,
) -> None:
    """Write one row to proxy_log. Never raises."""
    try:
        await conn.execute(
            """
            INSERT INTO proxy_log
                (agent_id, agent_name, account_name, connection_name,
                 method, path, upstream_status)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
            principal.agent_id,
            principal.agent_name or "",
            principal.name,
            connection_name,
            method,
            path[:2000],
            upstream_status,
        )
    except Exception:
        pass
