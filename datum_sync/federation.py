"""Bounded Streamable HTTP MCP client and persistent catalogue for the portal."""
from __future__ import annotations

import hashlib
import json
import re
import time
from urllib.parse import urlparse

import httpx

from datum_sync import credential_access, db, proxy
from datum_sync.portal_core import fail, record, unpack

MAX_RESPONSE = 1024 * 1024
MAX_ARGUMENTS = 256 * 1024
_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]")


def tool_name(prefix, upstream):
    clean = _UNSAFE.sub('_', f'{prefix}__{upstream}')
    if len(clean) <= 48:
        return clean
    suffix = hashlib.sha256(clean.encode()).hexdigest()[:6]
    return clean[:41] + '_' + suffix


def _config(row):
    return unpack(row['config'])


def _local_url(url):
    parsed = urlparse(url)
    allowed={(8211,'/mcp'),(8212,'/office/mcp'),(8212,'/postgres/mcp'),(8212,'/harness/mcp')}
    return (parsed.scheme == 'http' and parsed.hostname in ('127.0.0.1','localhost','::1')
            and (parsed.port,parsed.path) in allowed
            and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment)


def _definition_from(row, secret, credential=None):
    config = _config(row)
    if not _local_url(config.get('url','')):
        raise RuntimeError('this prototype permits only registered loopback MCP providers')
    headers = {str(k):str(v) for k,v in (config.get('headers') or {}).items()
               if str(k).lower() not in ('authorization','cookie','host','connection')}
    params={}
    proxy.inject_auth(config, secret, headers, params)
    return {'name':row['name'],'tier':row['tier'],'config':config,'headers':headers,'params':params,
            'credential_id':str(credential['id']) if credential else None,
            'credential_version':credential['version'] if credential else None}


async def _definition(conn, name):
    row = await conn.fetchrow("SELECT * FROM connections WHERE name=$1 AND type='mcp'", name)
    if not row:
        return None
    if row['portal_state']!='active':
        raise PermissionError('MCP server is disabled or retired')
    credential,secret = await credential_access.secret_for_connection(conn,name)
    if not credential:
        raise RuntimeError('upstream connection has no managed credential')
    return _definition_from(row,secret,credential)


async def probe_secret(name, secret):
    async with db.pool().acquire() as conn:
        row=await conn.fetchrow("SELECT * FROM connections WHERE name=$1 AND type='mcp'",name)
    if not row:
        raise RuntimeError('MCP connection not found')
    definition=_definition_from(row,credential_access.validate_secret(secret))
    await _rpc(definition,'initialize',{'protocolVersion':'2025-06-18','clientInfo':{'name':'datum-credential-probe','version':'0.8.0'},'capabilities':{}})
    result=await _rpc(definition,'tools/list',{})
    tools=result.get('tools',[])
    if not isinstance(tools,list):
        raise RuntimeError('invalid upstream tool catalogue')
    return {'ok':True,'tool_count':len(tools)}


async def _rpc(definition, method, params=None):
    request = {'jsonrpc':'2.0','id':1,'method':method}
    if params is not None:
        request['params'] = params
    timeout = min(max(int(definition['config'].get('timeout_seconds',10)),1),30)
    async with httpx.AsyncClient(timeout=timeout,follow_redirects=False,trust_env=False) as client:
        async with client.stream('POST',definition['config']['url'],headers=definition['headers'],params=definition['params'],json=request) as response:
            response.raise_for_status()
            data = bytearray()
            async for chunk in response.aiter_raw():
                if len(data) + len(chunk) > MAX_RESPONSE:
                    raise RuntimeError('upstream MCP response exceeds 1 MiB')
                data.extend(chunk)
    body = json.loads(data)
    if body.get('error'):
        raise RuntimeError('upstream MCP returned an error')
    if 'result' not in body:
        raise RuntimeError('upstream MCP response has no result')
    return body['result']


def _safe_failure(exc):
    if isinstance(exc,httpx.HTTPStatusError):
        return f'upstream HTTP {exc.response.status_code}'
    if isinstance(exc,httpx.TimeoutException):
        return 'upstream request timed out'
    if isinstance(exc,httpx.RequestError):
        return 'upstream transport failed'
    return 'upstream MCP request failed'


async def refresh(name):
    async with db.pool().acquire() as conn:
        definition = await _definition(conn,name)
    if not definition:
        fail(404,'INTEGRATION_NOT_FOUND')
    try:
        await _rpc(definition,'initialize',{'protocolVersion':'2025-06-18','clientInfo':{'name':'datum-auth-gateway','version':'0.8.0'},'capabilities':{}})
        result = await _rpc(definition,'tools/list',{})
        tools = result.get('tools',[])
        if not isinstance(tools,list) or len(tools)>500:
            raise RuntimeError('invalid upstream tool catalogue')
        parsed=[]
        seen=set()
        for item in tools:
            upstream=item.get('name') if isinstance(item,dict) else None
            schema=item.get('inputSchema',{}) if isinstance(item,dict) else {}
            if not isinstance(upstream,str) or not upstream or len(upstream)>200 or not isinstance(schema,dict):
                raise RuntimeError('invalid upstream tool definition')
            projected=tool_name(definition['config']['tool_prefix'],upstream)
            if projected in seen:
                raise RuntimeError('projected tool-name collision')
            seen.add(projected)
            parsed.append((upstream,projected,str(item.get('description',''))[:1000],schema,item.get('annotations') or {}))
        async with db.pool().acquire() as conn,conn.transaction():
            upstream_names=[]
            for upstream,projected,description,schema,annotations in parsed:
                upstream_names.append(upstream)
                await conn.execute('''INSERT INTO federated_tools(connection,upstream_name,tool_name,description,input_schema,annotations)
                    VALUES($1,$2,$3,$4,$5,$6)
                    ON CONFLICT(connection,upstream_name) DO UPDATE SET tool_name=EXCLUDED.tool_name,
                      description=EXCLUDED.description,input_schema=EXCLUDED.input_schema,
                      annotations=EXCLUDED.annotations,fetched_at=now()''',name,upstream,projected,description,json.dumps(schema),json.dumps(annotations))
            await conn.execute('DELETE FROM federated_tools WHERE connection=$1 AND NOT(upstream_name=ANY($2::text[]))',name,upstream_names)
            status={'status':'ok','tool_count':len(parsed),'last_ok_at':time.time(),'last_error':None}
            await conn.execute('UPDATE connections SET federation_status=$2,last_test_at=now(),last_test_ok=true,last_test_error=NULL WHERE name=$1',name,json.dumps(status))
            await conn.execute("INSERT INTO mcp_server_events(connection_name,kind,detail) VALUES($1,'server.catalogue.refreshed',$2)",name,json.dumps({'tool_count':len(parsed)}))
            await record(conn,None,'federate.refresh',name,detail={'tool_count':len(parsed)})
        return status
    except Exception as exc:
        message=_safe_failure(exc)
        async with db.pool().acquire() as conn,conn.transaction():
            current=await conn.fetchval('SELECT federation_status FROM connections WHERE name=$1',name)
            status=unpack(current) if current else {}
            status.update({'status':'down','last_error':message})
            await conn.execute('UPDATE connections SET federation_status=$2,last_test_at=now(),last_test_ok=false,last_test_error=$3 WHERE name=$1',name,json.dumps(status),message)
            await conn.execute("INSERT INTO mcp_server_events(connection_name,kind,detail) VALUES($1,'server.catalogue.failed',$2)",name,json.dumps({'error':message}))
            await record(conn,None,'federate.unavailable',name,'error',{'error':message})
        raise


async def listing(conn,user):
    rows=await conn.fetch('''SELECT t.*,c.tier,c.federation_status FROM federated_tools t
        JOIN connections c ON c.name=t.connection WHERE c.type='mcp' AND c.portal_state='active'
          AND t.enabled=true ORDER BY t.tool_name''')
    visible=[]
    for row in rows:
        if (user.tier>=max(row['tier'],row['min_tier']) and user.permits_mcp(row['connection'],row['tool_name'])
                and await credential_access.access_grant(conn,user,row['connection'],row['tool_name'])):
            visible.append(dict(row))
    return visible


async def prepare(conn,user,name,arguments,trace=None):
    row=await conn.fetchrow('''SELECT t.*,c.tier,c.config,c.secret,c.federation_status FROM federated_tools t
        JOIN connections c ON c.name=t.connection WHERE t.tool_name=$1 AND c.type='mcp'
          AND c.portal_state='active' AND t.enabled=true''',name)
    if not row or not user.permits_mcp(row['connection'],name):
        return None
    if user.tier < max(row['tier'],row['min_tier']):
        fail(403,'TIER_REQUIRED','Elevate before calling this MCP integration.')
    credential=await credential_access.authorized(conn,user,row['connection'],name,trace=trace)
    if not credential:
        fail(403,'CREDENTIAL_ACCESS_REQUIRED','Request access to this upstream credential before calling the tool.')
    if not isinstance(arguments,dict) or len(json.dumps(arguments).encode()) > MAX_ARGUMENTS:
        fail(400,'INVALID_ARGUMENTS')
    definition=await _definition(conn,row['connection'])
    await record(conn,user,'federate.call',f"{row['connection']}:{row['upstream_name']}",trace=trace,
                 detail={'tool':row['tool_name']})
    return {'definition':definition,'upstream_name':row['upstream_name'],'arguments':arguments,'connection':row['connection'],
            'outbound_credential_id':str(credential['id']),'outbound_credential_version':definition['credential_version'],
            'principal':{'id':user.id,'name':user.name,'repositories':user.repositories()}}


async def forward(prepared):
    try:
        result=await _rpc(prepared['definition'],'tools/call',{'name':prepared['upstream_name'],'arguments':prepared['arguments'],
            '_meta':{'io.datum.principal':prepared['principal']}})
        if not isinstance(result,dict) or not isinstance(result.get('content',[]),list):
            raise RuntimeError('invalid upstream tool result')
        result.pop('_meta',None)
        return result
    except Exception as exc:
        message=_safe_failure(exc)
        async with db.pool().acquire() as conn,conn.transaction():
            await conn.execute("UPDATE connections SET last_test_at=now(),last_test_ok=false,last_test_error=$2,federation_status=jsonb_set(federation_status,'{status}','\"down\"') WHERE name=$1",prepared['connection'],message)
            await record(conn,None,'federate.unavailable',prepared['connection'],'error',{'error':message})
        return {'content':[{'type':'text','text':'UPSTREAM_UNAVAILABLE: '+message}], 'isError':True}
