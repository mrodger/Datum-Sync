"""HTTP egress with address pinning and a bounded, streamed response.

The socket uses a validated IP literal. Host and TLS SNI retain the original
hostname. A transport is short-lived so separate origins cannot share a pooled
connection merely because they resolve to the same address.
"""
import asyncio
import ipaddress
import socket

import httpx

from datum_sync.errors import ApiError


def public_ip(address):
    value=ipaddress.ip_address(address)
    return value.is_global and not value.is_multicast


class PinnedTransport(httpx.AsyncBaseTransport):
    def __init__(self):
        self.inner=httpx.AsyncHTTPTransport(retries=0)

    async def handle_async_request(self, request):
        host=request.url.host
        if request.url.scheme not in ('http','https') or request.url.username or request.url.password:
            raise ApiError(400,'INVALID_URL','only HTTP(S) URLs without user information are allowed')
        port=request.url.port or (443 if request.url.scheme=='https' else 80)
        try:
            answers=await asyncio.get_running_loop().getaddrinfo(host,port,proto=socket.IPPROTO_TCP)
        except socket.gaierror:
            raise ApiError(502,'DNS_FAILED','upstream DNS lookup failed') from None
        addresses=[answer[4][0] for answer in answers]
        if not addresses or any(not public_ip(ip) for ip in addresses):
            raise ApiError(403,'BLOCKED_ADDRESS','upstream resolves to a private or reserved address')
        original=request.url
        request.headers['Host']=original.netloc.decode()
        request.extensions['sni_hostname']=host
        request.url=original.copy_with(host=addresses[0])
        return await self.inner.handle_async_request(request)

    async def aclose(self):
        await self.inner.aclose()


async def fetch(method,url,*,headers=None,params=None,content=None,limit=1048576,timeout=30):
    """Redirects are not followed: a credentialed connection is not a web browser."""
    async with asyncio.timeout(timeout):
        async with httpx.AsyncClient(transport=PinnedTransport(),trust_env=False,follow_redirects=False,timeout=timeout) as client:
            async with client.stream(method,url,headers=headers,params=params,content=content) as response:
                if response.is_redirect:
                    raise ApiError(502,'UPSTREAM_REDIRECT_REFUSED','credentialed proxy redirects are disabled')
                chunks=[]; size=0; truncated=False
                async for chunk in response.aiter_bytes():
                    remaining=limit-size
                    chunks.append(chunk[:remaining])
                    size+=min(len(chunk),remaining)
                    if len(chunk)>remaining or size>=limit:
                        truncated=True
                        break
                return response.status_code,dict(response.headers),b''.join(chunks),truncated
