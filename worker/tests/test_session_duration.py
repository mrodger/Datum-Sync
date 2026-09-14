import asyncio
import time

import pytest

import app as worker


@pytest.mark.asyncio
async def test_access_rotates_short_token_within_absolute_session(monkeypatch):
    calls = []

    async def datum_request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return {
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "expires_in": 600,
            "authorization_expires_at": "2099-01-01T00:00:00Z",
        }

    monkeypatch.setattr(worker, "_datum_request", datum_request)
    item = {
        "access_token": "old-access",
        "refresh_token": "old-refresh",
        "client_id": "worker-client",
        "access_expires_epoch": 0,
        "authorization_expires_epoch": time.time() + 7200,
        "authorization_expires_at": "2099-01-01T00:00:00Z",
        "refresh_lock": asyncio.Lock(),
    }

    assert await worker._access(item) == "new-access"
    assert item["refresh_token"] == "new-refresh"
    assert calls[0][2]["data"] == {
        "grant_type": "refresh_token",
        "refresh_token": "old-refresh",
        "client_id": "worker-client",
    }


@pytest.mark.asyncio
async def test_access_rejects_expired_absolute_session():
    item = {"authorization_expires_epoch": time.time() - 1}
    with pytest.raises(worker.HTTPException) as error:
        await worker._access(item)
    assert error.value.status_code == 401


def test_deadline_epoch_accepts_oauth_timestamp():
    assert worker._deadline_epoch("2030-01-01T00:00:00Z") == 1893456000


@pytest.mark.asyncio
async def test_internal_mcp_relay_uses_rotated_datum_token(monkeypatch):
    import httpx

    forwarded = {}
    real_client = httpx.AsyncClient

    class Upstream:
        status_code = 200
        content = b'{"jsonrpc":"2.0","id":1,"result":{}}'
        headers = {"content-type": "application/json", "mcp-session-id": "mcp-1", "x-trace-id": "trace-1"}

    class FakeClient:
        def __init__(self, **kwargs):
            forwarded["client"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def request(self, method, path, **kwargs):
            forwarded.update(method=method, path=path, request=kwargs)
            return Upstream()

    async def access(_item):
        return "rotated-datum-access"

    worker._sessions["relay-test"] = {"proxy_token": "local-relay-secret"}
    monkeypatch.setattr(worker, "_access", access)
    monkeypatch.setattr(worker.httpx, "AsyncClient", FakeClient)
    try:
        async with real_client(transport=httpx.ASGITransport(app=worker.app), base_url="http://worker") as client:
            response = await client.post(
                "/internal/mcp/relay-test/mcp",
                headers={"Authorization": "Bearer local-relay-secret", "Mcp-Session-Id": "mcp-1"},
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            )
        assert response.status_code == 200
        assert response.headers["mcp-session-id"] == "mcp-1"
        assert forwarded["request"]["headers"]["Authorization"] == "Bearer rotated-datum-access"
        assert forwarded["request"]["headers"]["mcp-session-id"] == "mcp-1"
    finally:
        worker._sessions.pop("relay-test", None)
