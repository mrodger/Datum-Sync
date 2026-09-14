"""Focused regression tests for the proxy egress repairs."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from datum_sync import egress, proxy
from datum_sync.errors import ApiError


def test_custom_headers_never_survive_redirect_strip():
    headers={}
    proxy.inject_auth({'auth_inject':{'type':'header','header':'X-API-Key','secret_field':'key'}},
                      {'key':'synthetic'},headers,{})
    assert 'X-API-Key' not in proxy._strip_auth(headers)
    assert proxy._strip_auth({'Cookie':'secret','Accept':'application/json'})=={'Accept':'application/json'}


@pytest.mark.asyncio
async def test_transport_pins_socket_and_preserves_tls_hostname(monkeypatch):
    loop=asyncio.get_running_loop()
    monkeypatch.setattr(loop,'getaddrinfo',AsyncMock(return_value=[(2,1,6,'',('93.184.216.34',443))]))
    transport=egress.PinnedTransport()
    inner=SimpleNamespace(handle_async_request=AsyncMock(return_value=httpx.Response(200)),aclose=AsyncMock())
    original=transport.inner
    transport.inner=inner
    request=httpx.Request('GET','https://example.com/path')
    await transport.handle_async_request(request)
    forwarded=inner.handle_async_request.call_args.args[0]
    assert forwarded.url.host=='93.184.216.34'
    assert forwarded.headers['Host']=='example.com'
    assert forwarded.extensions['sni_hostname']=='example.com'
    await original.aclose()


@pytest.mark.asyncio
async def test_transport_refuses_private_resolution(monkeypatch):
    loop=asyncio.get_running_loop()
    monkeypatch.setattr(loop,'getaddrinfo',AsyncMock(return_value=[(2,1,6,'',('127.0.0.1',443))]))
    transport=egress.PinnedTransport()
    try:
        with pytest.raises(ApiError,match='private'):
            await transport.handle_async_request(httpx.Request('GET','https://example.com'))
    finally: await transport.aclose()


@pytest.mark.asyncio
async def test_redirect_is_not_followed(monkeypatch):
    calls=[]
    def upstream(request):
        calls.append(request)
        return httpx.Response(302,headers={'Location':'https://other.example/collect'})
    monkeypatch.setattr(egress,'PinnedTransport',lambda:httpx.MockTransport(upstream))
    with pytest.raises(ApiError,match='redirects'):
        await egress.fetch('GET','https://example.com',headers={'X-API-Key':'synthetic'})
    assert len(calls)==1


@pytest.mark.asyncio
async def test_response_stream_stops_at_budget(monkeypatch):
    class Stream(httpx.AsyncByteStream):
        def __init__(self): self.reads=0; self.closed=False
        async def __aiter__(self):
            for _ in range(100):
                self.reads+=1
                yield b'x'*100
        async def aclose(self): self.closed=True
    stream=Stream()
    monkeypatch.setattr(egress,'PinnedTransport',lambda:httpx.MockTransport(lambda request:httpx.Response(200,stream=stream)))
    status,headers,body,truncated=await egress.fetch('GET','https://example.com',limit=250)
    assert len(body)==250 and truncated and stream.reads==3 and stream.closed
