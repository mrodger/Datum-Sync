"""Loopback auth portal: real PostgreSQL authority and a bounded MCP resource provider."""
from __future__ import annotations

import asyncio
import base64
from contextlib import asynccontextmanager
import hashlib
import json
import os
from pathlib import Path
import secrets
import time
import uuid
from datetime import timedelta
from urllib.parse import urlencode, urlparse

import asyncpg
from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from datum_sync import auth, config, credential_access, db, federation, mcp_observe
from datum_sync.portal_core import (COOKIE, DEMO_GRANT, SCOPES, caller, fail, grant_valid,
    identity, mint, narrower, now, record, resource_call, scope_tier, token_pair, unpack)

STATIC = Path(__file__).parent/'portal_static'
SOURCE_UI = Path(__file__).parent/'static-v2'
_attempts = {}
_password_slots = asyncio.Semaphore(2)
AGENT_SESSION_DURATIONS = frozenset({900, 7200, 14400, 28800})

MCP_TEMPLATES = {
    'officecli-demo': {
        'name':'officecli-demo','display_name':'OfficeCLI document demo','provider_kind':'officecli-demo',
        'description':'Constrained OfficeCLI-compatible inspection of sample DOCX, XLSX and PPTX documents.',
        'url':'http://127.0.0.1:8212/office/mcp','tool_prefix':'office','tier':2,
        'credential_label':'OfficeCLI demo service','token_env':'OFFICE_MCP_TOKEN'},
    'postgres-demo': {
        'name':'postgres-demo','display_name':'PostgreSQL analytics demo','provider_kind':'postgres-demo',
        'description':'Read-only schema discovery and bounded order analytics against the local PostgreSQL demo schema.',
        'url':'http://127.0.0.1:8212/postgres/mcp','tool_prefix':'postgres','tier':2,
        'credential_label':'PostgreSQL demo service','token_env':'POSTGRES_MCP_TOKEN'},
    'harness-tools': {
        'name':'harness-tools','display_name':'Datum Harness tools','provider_kind':'datum-harness',
        'description':'Curated harness personas, bounded Leaflet rendering and live read-only WoRMS taxonomy lookup.',
        'url':'http://127.0.0.1:8212/harness/mcp','tool_prefix':'harness','tier':2,
        'credential_label':'Datum Harness tool service','token_env':'HARNESS_MCP_TOKEN'},
}


def throttle(key, maximum=30, window=60):
    current = time.monotonic()
    hits = [v for v in _attempts.get(key,[]) if v > current-window]
    if len(hits) >= maximum: fail(429,'RATE_LIMITED')
    if len(_attempts)>2000:
        for old in list(_attempts):
            if not _attempts[old] or _attempts[old][-1] < current-900: _attempts.pop(old,None)
        if len(_attempts)>2000: fail(429,'RATE_LIMITED')
    _attempts[key] = hits+[current]


def address(request): return request.client.host if request.client else 'test'


def number(value, default, minimum, maximum):
    value = default if value is None else value
    if type(value) is not int or not minimum <= value <= maximum: fail(400,'INVALID_PARAMETER')
    return value


def identifier(value):
    import re
    if not isinstance(value,str) or not re.fullmatch(r'[a-z][a-z0-9-]{2,47}',value) or value.startswith(('admin','system','operator')):
        fail(400,'INVALID_NAME','Use 3–48 lowercase letters, numbers or hyphens; start with a letter.')
    return value


def as_uuid(value):
    try: return uuid.UUID(str(value))
    except (ValueError,TypeError): fail(400,'INVALID_ID')


def response(value, status=200, headers=None):
    return JSONResponse(jsonable_encoder(value),status_code=status,headers=headers)


@asynccontextmanager
async def lifespan(app):
    config.require_public_url()
    if config.AUTH_DISABLED: raise RuntimeError('The auth portal cannot run with authentication disabled.')
    await db.init_pool()
    try:
        async with db.pool().acquire() as conn:
            servers=await conn.fetch("SELECT name FROM connections WHERE type='mcp' AND portal_state='active'")
        for server in servers:
            try:
                await federation.refresh(server['name'])
            except Exception:
                # Cached tools remain available for discovery; calls report the
                # adapter outage without preventing the authority server starting.
                pass
        yield
    finally: await db.close_pool()


app = FastAPI(title='Datum Auth Portal',version='0.8.0-prototype',lifespan=lifespan)


class BodyLimit:
    def __init__(self, app): self.app=app
    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http': return await self.app(scope,receive,send)
        chunks=[]; size=0
        while True:
            message=await receive()
            if message['type']=='http.disconnect': return
            chunk=message.get('body',b''); size+=len(chunk)
            if size>262144:
                return await JSONResponse({'detail':{'code':'BODY_TOO_LARGE'}},413)(scope,receive,send)
            chunks.append(chunk)
            if not message.get('more_body'): break
        sent=False
        async def replay():
            nonlocal sent
            if not sent:
                sent=True
                return {'type':'http.request','body':b''.join(chunks),'more_body':False}
            return await receive()
        await self.app(scope,replay,send)


app.add_middleware(BodyLimit)


@app.middleware('http')
async def edge(request, call_next):
    host=request.headers.get('host','')
    # This entry point is deliberately local. Uvicorn is launched with proxy
    # headers disabled so a forwarded address cannot establish local trust.
    if host not in {'127.0.0.1:8210','localhost:8210','testserver'}:
        return response({'detail':{'code':'INVALID_HOST'}},400)
    origin=request.headers.get('origin')
    if origin and origin != config.PUBLIC_URL:
        return response({'detail':{'code':'BAD_ORIGIN'}},403)
    trace=uuid.uuid4(); request.state.trace=trace
    result=await call_next(request)
    result.headers.update({'Cache-Control':'no-store','X-Content-Type-Options':'nosniff',
                           'Referrer-Policy':'no-referrer','X-Frame-Options':'DENY',
                           'X-Trace-Id':str(trace),
                           'Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})
    if result.status_code==401:
        result.headers['WWW-Authenticate']=f'Bearer resource_metadata="{config.PUBLIC_URL}/.well-known/oauth-protected-resource"'
    if result.status_code>=400 and request.url.path not in ('/health',):
        async with db.pool().acquire() as conn:
            await record(conn,None,'request.denied',request.url.path,'denied',{'status':result.status_code},trace)
    return result


@app.exception_handler(asyncpg.UniqueViolationError)
async def duplicate(request,exc): return response({'detail':{'code':'ALREADY_EXISTS','message':'That name or active credential already exists.'}},409)


@app.get('/health')
async def health():
    async with db.pool().acquire() as conn: await conn.fetchval('SELECT 1')
    return {'status':'ok','service':'datum-auth-portal','mode':'local prototype',
            'mcp_audit_dropped':mcp_observe.dropped()}


@app.get('/')
@app.get('/oauth/verify')
async def shell(): return source_ui_shell()


def source_ui_shell():
    # Render the supplied v2 shell itself. Its stylesheet, icons and navigation
    # behavior stay at their source paths; only the feature entry point changes.
    html = (SOURCE_UI/'index.html').read_text()
    html = html.replace('<link rel="stylesheet" href="/v2/style.css">',
                        '<link rel="stylesheet" href="/v2/style.css"><link rel="stylesheet" href="/assets/portal.css">')
    html = html.replace('<script type="module" src="/v2/app.js"></script>',
                        '<script type="module" src="/assets/app.js"></script>')
    html = html.replace('<form id="signin-form" autocomplete="on">',
                        '<form id="signin-form" autocomplete="on" method="post" action="/api/login">')
    mark = (SOURCE_UI/'datum-mark.svg').read_text()
    mark = mark[mark.index('<svg'):].replace('<svg ', '<svg class="brand-mark" width="118" height="26" ', 1)
    html = html.replace('<span class="brand-name">Datum-Sync</span>', '<span class="brand-name">'+mark+'</span>')
    html = html.replace('</body>', '<dialog id="dialog"><div id="dialog-content"></div></dialog><div id="toast" role="status" hidden></div></body>')
    return HTMLResponse(html)


app.mount('/v2',StaticFiles(directory=SOURCE_UI),name='source-ui-v2')
app.mount('/assets',StaticFiles(directory=STATIC,check_dir=False),name='portal-assets')


@app.post('/api/login')
async def login(request:Request):
    throttle('login:'+address(request),10,60)
    body=await request.json()
    name=str(body.get('name',''))[:100]; password=str(body.get('password',''))[:1024]
    async with db.pool().acquire() as conn:
        row=await conn.fetchrow("SELECT * FROM service_accounts WHERE name=$1 AND portal_kind='human'",name)
        async with _password_slots:
            valid=await asyncio.to_thread(auth.verify_password,row['password_hash'] if row else None,password)
        if not valid or row['disabled'] or row['portal_state']!='active': fail(401,'INVALID_CREDENTIALS')
        async with conn.transaction():
            raw,cred=await mint(conn,row['id'],'session','operator sign-in',4,now()+timedelta(hours=8))
            full=await conn.fetchrow('SELECT * FROM plane_credentials WHERE id=$1',cred['id'])
            user=await identity(conn,full)
            await record(conn,user,'auth.login',name,trace=request.state.trace)
    out=response({'user':user.public(),'csrf':auth.hash_token(raw+':csrf')})
    out.set_cookie(COOKIE,raw,httponly=True,samesite='strict',secure=config.PUBLIC_URL.startswith('https:'),max_age=8*3600)
    return out


@app.get('/api/me')
async def me(request:Request):
    async with db.pool().acquire() as conn: user=await caller(conn,request)
    raw=request.cookies.get(COOKIE,'')
    return {'user':user.public(),'csrf':auth.hash_token(raw+':csrf') if user.credential['kind']=='session' else None}


@app.get('/api/resources')
async def api_resources(request:Request,limit:int=50):
    from datum_sync import resources
    async with db.pool().acquire() as conn:
        user=await caller(conn,request)
        if user.tier<2: fail(403,'TIER_REQUIRED')
        items=await resources.list_visible(conn,user,limit)
    return {'items':items}


@app.get('/api/resources/{resource_id}/content')
async def api_resource_content(resource_id:str,request:Request):
    from datum_sync import resources
    async with db.pool().acquire() as conn,conn.transaction():
        user=await caller(conn,request)
        if user.tier<2: fail(403,'TIER_REQUIRED')
        value=await resources.read(conn,user,{'id':resource_id},request.state.trace)
    headers={'Cache-Control':'private, no-store','Content-Disposition':'inline',
             'Content-Security-Policy':"default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data: blob:; connect-src 'none'; base-uri 'none'; form-action 'none'"}
    return Response(value['content'].encode('utf-8'),media_type=value['media_type'],headers=headers)


@app.post('/api/logout')
async def logout(request:Request):
    async with db.pool().acquire() as conn, conn.transaction():
        user=await caller(conn,request)
        await conn.execute('UPDATE plane_credentials SET revoked_at=now() WHERE id=$1',user.credential['id'])
        await record(conn,user,'auth.logout',user.name)
    out=response({'ok':True}); out.delete_cookie(COOKIE); return out


@app.get('/api/dashboard')
async def dashboard(request:Request):
    async with db.pool().acquire() as conn:
        operator=await caller(conn,request,operator=True)
        principals=await conn.fetch("SELECT id,name,portal_kind AS kind,portal_state AS state,max_tier,portal_parent AS parent_id,portal_grant AS grant,portal_metadata AS metadata,created_at FROM service_accounts WHERE portal_kind <> 'legacy' ORDER BY created_at DESC")
        authorizations=await conn.fetch('''SELECT a.id,p.name AS principal,a.scope,a.status,a.user_code,a.duration_seconds,a.requested_at,a.expires_at,a.authorized_until
            FROM plane_authorizations a JOIN service_accounts p ON p.id=a.principal_id ORDER BY requested_at DESC LIMIT 100''')
        pending=await conn.fetch('''SELECT c.*,p.name AS principal FROM plane_pending_calls c JOIN service_accounts p ON p.id=c.principal_id ORDER BY requested_at DESC LIMIT 100''')
        credentials=await conn.fetch('''SELECT c.id,p.name AS principal,c.kind,c.label,c.tier,c.scope,c.created_at,c.expires_at,c.revoked_at,c.last_used_at
            FROM plane_credentials c JOIN service_accounts p ON p.id=c.principal_id WHERE c.kind <> 'refresh' ORDER BY created_at DESC LIMIT 100''')
        sessions=await conn.fetch('''SELECT s.*,p.name AS principal FROM plane_sessions s JOIN service_accounts p ON p.id=s.principal_id ORDER BY started_at DESC LIMIT 100''')
        audit_rows=await conn.fetch('SELECT * FROM plane_audit ORDER BY id DESC LIMIT 150')
        releases=await conn.fetch('SELECT id,path,principal_id,created_at FROM plane_releases ORDER BY created_at DESC LIMIT 50')
        integrations=await conn.fetch("SELECT name,display_name,provider_kind,portal_state,owner_id,tier,description,config,federation_status,last_test_at,last_test_ok,last_test_error FROM connections WHERE type='mcp' AND owner_id=$1 ORDER BY name",operator.id)
        integration_tools=await conn.fetch('''SELECT t.connection,t.upstream_name,t.tool_name,t.description,t.input_schema,t.fetched_at,t.enabled,t.min_tier
            FROM federated_tools t JOIN connections c ON c.name=t.connection WHERE c.owner_id=$1 ORDER BY t.connection,t.tool_name''',operator.id)
        managed_credentials=await conn.fetch('''SELECT c.id,c.connection_name,c.label,c.auth_type,c.owner_name,c.status,c.expires_at,c.last_used_at,
            c.created_at,c.updated_at,v.version,v.key_id,v.fingerprint,v.secret_fields,v.activated_at
            FROM auth_credentials c JOIN connections x ON x.name=c.connection_name
            LEFT JOIN auth_credential_versions v ON v.id=c.current_version_id WHERE x.owner_id=$1 ORDER BY c.label''',operator.id)
        credential_requests=await conn.fetch('''SELECT r.*,p.name AS reviewer,c.label AS credential_label,c.connection_name
            FROM credential_requests r JOIN auth_credentials c ON c.id=r.credential_id
            JOIN connections x ON x.name=c.connection_name LEFT JOIN service_accounts p ON p.id=r.reviewed_by
            WHERE x.owner_id=$1 ORDER BY r.requested_at DESC LIMIT 150''',operator.id)
        grant_rows=await conn.fetch('''SELECT g.*,c.label AS credential_label,c.connection_name
            FROM credential_grants g JOIN auth_credentials c ON c.id=g.credential_id
            JOIN connections x ON x.name=c.connection_name WHERE x.owner_id=$1 ORDER BY g.granted_at DESC LIMIT 250''',operator.id)
        managed_grants=[]
        for grant in grant_rows:
            item=dict(grant); item.update(await credential_access.effective_grant(conn,grant)); managed_grants.append(item)
        credential_events=await conn.fetch('''SELECT e.* FROM credential_events e JOIN auth_credentials c ON c.id=e.credential_id
            JOIN connections x ON x.name=c.connection_name WHERE x.owner_id=$1 ORDER BY e.id DESC LIMIT 200''',operator.id)
        mcp_server_events=await conn.fetch('''SELECT e.* FROM mcp_server_events e JOIN connections c ON c.name=e.connection_name
            WHERE c.owner_id=$1 ORDER BY e.id DESC LIMIT 200''',operator.id)
        mcp_flows=await conn.fetch('SELECT * FROM mcp_flows ORDER BY started_at DESC LIMIT 100')
        flow_ids=[row['trace_id'] for row in mcp_flows]
        mcp_events=await conn.fetch('''SELECT * FROM mcp_flow_events
            WHERE trace_id=ANY($1::uuid[]) ORDER BY id''',flow_ids)
        resource_rows=await conn.fetch('''SELECT r.id,r.path,r.version,r.title,r.media_type,r.bytes,r.sha256,r.owner_id,
            r.owner_name,r.state,r.source_trace_id,r.source_connection,r.source_tool,r.expires_at,r.created_at
            FROM plane_resources r JOIN service_accounts owner ON owner.id=r.owner_id
            WHERE owner.portal_parent=$1 ORDER BY r.created_at DESC LIMIT 200''',operator.id)
        resource_grants=await conn.fetch('''SELECT g.id,g.resource_id,g.principal_id,g.principal_name,g.capability,g.granted_by_name,
            g.granted_at,g.expires_at,g.revoked_at FROM plane_resource_grants g
            JOIN plane_resources r ON r.id=g.resource_id JOIN service_accounts owner ON owner.id=r.owner_id
            WHERE owner.portal_parent=$1 ORDER BY g.granted_at DESC LIMIT 300''',operator.id)
        resource_events=await conn.fetch('''SELECT e.id,e.resource_id,e.trace_id,e.actor_name,e.kind,e.detail,e.created_at
            FROM plane_resource_events e JOIN plane_resources r ON r.id=e.resource_id
            JOIN service_accounts owner ON owner.id=r.owner_id WHERE owner.portal_parent=$1 ORDER BY e.id DESC LIMIT 200''',operator.id)
    items=[]
    for row in principals:
        item=dict(row); item['grant']=unpack(item['grant']); item['metadata']=unpack(item['metadata']); items.append(item)
    calls=[]
    for row in pending:
        item=dict(row); item['args']=unpack(item['args']); item.pop('grant_snapshot'); calls.append(item)
    return {'principals':items,'authorizations':[dict(x) for x in authorizations], 'pending_calls':calls,
            'credentials':[dict(x) for x in credentials],'sessions':[dict(x) for x in sessions],
            'audit':[dict(x) for x in audit_rows],'releases':[dict(x) for x in releases],
            'integrations':[{**dict(x),'config':unpack(x['config']),'federation_status':unpack(x['federation_status'])} for x in integrations],
            'integration_tools':[{**dict(x),'input_schema':unpack(x['input_schema'])} for x in integration_tools],
            'managed_credentials':[dict(x) for x in managed_credentials],
            'credential_requests':[{**dict(x),'decision_detail':unpack(x['decision_detail'])} for x in credential_requests],
            'credential_grants':managed_grants,
            'credential_events':[{**dict(x),'detail':unpack(x['detail'])} for x in credential_events],
            'mcp_server_events':[{**dict(x),'detail':unpack(x['detail'])} for x in mcp_server_events],
            'mcp_templates':[{k:v for k,v in item.items() if k!='token_env'} for item in MCP_TEMPLATES.values()],
            'mcp_flows':[dict(x) for x in mcp_flows],
            'mcp_events':[{**dict(x),'detail':unpack(x['detail'])} for x in mcp_events],
            'resources':[dict(x) for x in resource_rows],
            'resource_grants':[dict(x) for x in resource_grants],
            'resource_events':[{**dict(x),'detail':unpack(x['detail'])} for x in resource_events]}


@app.post('/api/resource-grants/{grant_id}/revoke')
async def operator_revoke_resource_grant(grant_id:str,request:Request):
    from datum_sync import resources
    async with db.pool().acquire() as conn,conn.transaction():
        operator=await caller(conn,request,operator=True,lock=True)
        grant=await conn.fetchrow('''SELECT g.*,r.owner_id,owner.portal_parent FROM plane_resource_grants g
            JOIN plane_resources r ON r.id=g.resource_id JOIN service_accounts owner ON owner.id=r.owner_id
            WHERE g.id=$1 FOR UPDATE OF g''',as_uuid(grant_id))
        if not grant or grant['portal_parent']!=operator.id: fail(404,'RESOURCE_GRANT_NOT_FOUND')
        if grant['revoked_at'] is None:
            await conn.execute('UPDATE plane_resource_grants SET revoked_at=now(),revoked_by=$2 WHERE id=$1',grant['id'],operator.id)
            await resources._event(conn,operator,grant['resource_id'],'resource.revoked',request.state.trace,
                                   {'target':grant['principal_name'],'by_operator':True})
    return {'id':str(grant['id']),'revoked':True}


@app.post('/api/enrolment/codes')
async def enrol_code(request:Request):
    body=await request.json(); template=grant_valid(body.get('grant',DEMO_GRANT))
    async with db.pool().acquire() as conn,conn.transaction():
        user=await caller(conn,request,operator=True,lock=True)
        if not all(narrower(template,g) for g in user.grants): fail(403,'GRANT_NOT_NARROWER')
        code=secrets.token_urlsafe(24)
        row=await conn.fetchrow('''INSERT INTO plane_enrolments(sponsor_id,code_hash,template,expires_at)
            VALUES($1,$2,$3,$4) RETURNING id,expires_at''',user.id,auth.hash_token(code),json.dumps(template),now()+timedelta(hours=24))
        await record(conn,user,'enrolment.invite',row['id'])
    return {'code':code,**dict(row)}


@app.post('/enrol')
async def enrol(request:Request):
    throttle('enrol:'+address(request),20,60)
    body=await request.json(); name=identifier(body.get('name'))
    async with db.pool().acquire() as conn,conn.transaction():
        invite=await conn.fetchrow('SELECT * FROM plane_enrolments WHERE code_hash=$1 FOR UPDATE',auth.hash_token(str(body.get('code',''))))
        if not invite or invite['used_at'] or invite['expires_at']<=now(): fail(400,'INVALID_ENROLMENT_CODE')
        parent=await conn.fetchrow('SELECT * FROM service_accounts WHERE id=$1 FOR UPDATE',invite['sponsor_id'])
        template=unpack(invite['template'])
        if parent['disabled'] or parent['portal_state']!='active' or not narrower(template,unpack(parent['portal_grant'])): fail(403,'SPONSOR_AUTHORITY_CHANGED')
        metadata={'purpose':str(body.get('purpose',''))[:300]}
        pid=await conn.fetchval('''INSERT INTO service_accounts(name,max_tier,is_admin,repo_scope,portal_kind,portal_state,portal_parent,portal_grant,portal_metadata)
            VALUES($1,3,false,ARRAY[]::text[],'agent','pending',$2,$3,$4) RETURNING id''',name,parent['id'],json.dumps(template),json.dumps(metadata))
        claim=secrets.token_urlsafe(32)
        await conn.execute('''UPDATE plane_enrolments SET used_at=now(),principal_id=$2,claim_hash=$3,claim_expires_at=$4 WHERE id=$1''',invite['id'],pid,auth.hash_token(claim),now()+timedelta(hours=24))
        await record(conn,None,'agent.enrol',name)
    return {'id':pid,'name':name,'state':'pending','claim_code':claim}


@app.post('/enrol/claim')
async def claim(request:Request):
    throttle('claim:'+address(request),30,60)
    body=await request.json()
    async with db.pool().acquire() as conn,conn.transaction():
        invite=await conn.fetchrow('SELECT * FROM plane_enrolments WHERE claim_hash=$1 FOR UPDATE',auth.hash_token(str(body.get('claim_code',''))))
        if not invite or invite['claimed_at'] or invite['claim_expires_at']<=now(): fail(400,'INVALID_CLAIM')
        target=await conn.fetchrow('SELECT * FROM service_accounts WHERE id=$1 FOR UPDATE',invite['principal_id'])
        if target['portal_state']!='active' or target['disabled']: fail(409,'APPROVAL_REQUIRED')
        raw,cred=await mint(conn,target['id'],'baseline','enrolment',2,now()+timedelta(days=30))
        full=await conn.fetchrow('SELECT * FROM plane_credentials WHERE id=$1',cred['id'])
        await identity(conn,full)
        await conn.execute('UPDATE plane_enrolments SET claimed_at=now() WHERE id=$1',invite['id'])
        await record(conn,None,'credential.claim',target['name'])
    return {'token':raw,'credential':cred}


@app.post('/api/principals/{pid}/state')
async def state(pid:int,request:Request):
    body=await request.json(); action=body.get('action')
    transitions={'approve':('pending','active'),'reject':('pending','rejected'),'restrict':('active','restricted'),
                 'restore':('restricted','active'),'disable':(None,'disabled'),'enable':('disabled','active'),'retire':(None,'retired')}
    if action not in transitions: fail(400,'INVALID_TRANSITION')
    async with db.pool().acquire() as conn,conn.transaction():
        user=await caller(conn,request,operator=True)
        target=await conn.fetchrow("SELECT * FROM service_accounts WHERE id=$1 AND portal_kind='agent' FOR UPDATE",pid)
        if not target or target['portal_parent']!=user.id: fail(404,'PRINCIPAL_NOT_FOUND')
        source,destination=transitions[action]
        if target['portal_state'] in ('retired','rejected') or (source and target['portal_state']!=source): fail(409,'INVALID_TRANSITION')
        await conn.execute('UPDATE service_accounts SET portal_state=$2,disabled=$3 WHERE id=$1',pid,destination,destination in ('disabled','retired','rejected'))
        if action in ('restrict','disable','retire','reject'):
            await conn.execute('UPDATE plane_sessions SET ended_at=now() WHERE principal_id=$1 AND ended_at IS NULL',pid)
            await conn.execute("UPDATE plane_pending_calls SET status='cancelled' WHERE principal_id=$1 AND status='pending'",pid)
            await conn.execute("UPDATE plane_authorizations SET status='denied' WHERE principal_id=$1 AND status IN ('pending','approved')",pid)
            await conn.execute("UPDATE plane_credentials SET revoked_at=now() WHERE principal_id=$1 AND kind IN ('access','refresh') AND revoked_at IS NULL",pid)
        if action in ('disable','retire','reject'):
            await conn.execute('UPDATE plane_credentials SET revoked_at=now() WHERE principal_id=$1 AND revoked_at IS NULL',pid)
        await record(conn,user,'principal.'+action,target['name'])
    return {'state':destination}


@app.patch('/api/principals/{pid}/grant')
async def update_grant(pid:int,request:Request):
    body=await request.json(); grant=grant_valid(body.get('grant'))
    async with db.pool().acquire() as conn,conn.transaction():
        user=await caller(conn,request,operator=True)
        target=await conn.fetchrow("SELECT * FROM service_accounts WHERE id=$1 AND portal_kind='agent' FOR UPDATE",pid)
        if not target or target['portal_parent']!=user.id: fail(404,'PRINCIPAL_NOT_FOUND')
        if not all(narrower(grant,g) for g in user.grants): fail(403,'GRANT_NOT_NARROWER')
        await conn.execute('UPDATE service_accounts SET portal_grant=$2 WHERE id=$1',pid,json.dumps(grant))
        await record(conn,user,'principal.grant',target['name'])
    return {'grant':grant}


@app.post('/api/principals/{pid}/tokens')
async def issue_token(pid:int,request:Request):
    body=await request.json()
    if set(body)-{'label'}: fail(400,'BASELINE_ONLY','Only tier-2 baseline credentials can be issued here.')
    async with db.pool().acquire() as conn,conn.transaction():
        user=await caller(conn,request,operator=True)
        target=await conn.fetchrow("SELECT * FROM service_accounts WHERE id=$1 AND portal_kind='agent' FOR UPDATE",pid)
        if not target or target['portal_parent']!=user.id: fail(404,'PRINCIPAL_NOT_FOUND')
        if target['portal_state']!='active' or target['disabled']: fail(409,'PRINCIPAL_INACTIVE')
        raw,cred=await mint(conn,pid,'baseline',str(body.get('label','baseline'))[:80],2,now()+timedelta(days=30))
        await record(conn,user,'credential.issue',target['name'])
    return {'token':raw,'credential':cred}


@app.delete('/api/credentials/{cid}')
async def revoke(cid:str,request:Request):
    async with db.pool().acquire() as conn,conn.transaction():
        user=await caller(conn,request,operator=True)
        cred=await conn.fetchrow('SELECT * FROM plane_credentials WHERE id=$1',as_uuid(cid))
        if not cred: fail(404,'CREDENTIAL_NOT_FOUND')
        target=await conn.fetchrow('SELECT * FROM service_accounts WHERE id=$1 FOR UPDATE',cred['principal_id'])
        if target['portal_parent']!=user.id and target['id']!=user.id: fail(403,'FORBIDDEN')
        await conn.execute('UPDATE plane_credentials SET revoked_at=now() WHERE family=$1',cred['family'])
        await conn.execute('UPDATE plane_sessions SET ended_at=now() WHERE credential_id IN (SELECT id FROM plane_credentials WHERE family=$1)',cred['family'])
        await record(conn,user,'credential.revoke',cid)
    return {'revoked':True}


@app.get('/.well-known/oauth-protected-resource')
async def resource_metadata():
    return {'resource':config.PUBLIC_URL+'/mcp','authorization_servers':[config.PUBLIC_URL],'scopes_supported':list(SCOPES)}


@app.get('/.well-known/oauth-authorization-server')
async def server_metadata():
    return {'issuer':config.PUBLIC_URL,'authorization_endpoint':config.PUBLIC_URL+'/oauth/authorize',
            'token_endpoint':config.PUBLIC_URL+'/oauth/token','registration_endpoint':config.PUBLIC_URL+'/oauth/register',
            'device_authorization_endpoint':config.PUBLIC_URL+'/oauth/device','scopes_supported':list(SCOPES),
            'response_types_supported':['code'],'code_challenge_methods_supported':['S256'],
            'grant_types_supported':['authorization_code','refresh_token','urn:ietf:params:oauth:grant-type:device_code'],
            'token_endpoint_auth_methods_supported':['none']}


@app.post('/oauth/register')
async def register(request:Request):
    throttle('register:'+address(request),20,3600)
    body=await request.json(); uris=body.get('redirect_uris',[])
    if not isinstance(uris,list) or len(uris)>5: fail(400,'invalid_redirect_uri')
    for uri in uris:
        if not isinstance(uri,str) or len(uri)>1000: fail(400,'invalid_redirect_uri')
        parsed=urlparse(uri)
        if parsed.scheme!='http' or parsed.hostname not in ('127.0.0.1','localhost','::1') or parsed.fragment or parsed.username or parsed.password:
            fail(400,'invalid_redirect_uri','This local prototype accepts loopback HTTP callbacks only.')
    client=secrets.token_urlsafe(20)
    async with db.pool().acquire() as conn:
        await conn.execute('INSERT INTO plane_clients(client_id,name,redirect_uris) VALUES($1,$2,$3)',client,str(body.get('client_name','MCP client'))[:100],json.dumps(uris))
    return response({'client_id':client,'redirect_uris':uris,'token_endpoint_auth_method':'none'},201)


@app.post('/oauth/device')
async def device(request:Request):
    throttle('device:'+address(request),30,60)
    body=await request.json() if request.headers.get('content-type','').startswith('application/json') else dict(await request.form())
    scope=body.get('scope','mcp:operate'); tier=scope_tier(scope)
    duration=number(body.get('duration_seconds'),900,60,3600)
    if body.get('resource',config.PUBLIC_URL+'/mcp')!=config.PUBLIC_URL+'/mcp': fail(400,'invalid_target')
    async with db.pool().acquire() as conn,conn.transaction():
        user=await caller(conn,request,lock=True)
        if user.credential['kind']=='session' or user.kind!='agent' or user.row['portal_state']!='active': fail(403,'AGENT_BEARER_REQUIRED')
        if tier>user.row['max_tier']: fail(403,'SCOPE_ABOVE_PRINCIPAL')
        client=body.get('client_id','datum-local')
        if not await conn.fetchval('SELECT 1 FROM plane_clients WHERE client_id=$1',client): fail(400,'invalid_client')
        raw=secrets.token_urlsafe(32); code=secrets.token_hex(4).upper()
        aid=await conn.fetchval('''INSERT INTO plane_authorizations(principal_id,client_id,device_hash,user_code,scope,expires_at,duration_seconds)
            VALUES($1,$2,$3,$4,$5,$6,$7) RETURNING id''',user.id,client,auth.hash_token(raw),code,scope,now()+timedelta(minutes=10),duration)
        await record(conn,user,'elevation.request',aid,detail={'scope':scope,'duration_seconds':duration})
    return {'id':aid,'device_code':raw,'user_code':code,'verification_uri':config.PUBLIC_URL+'/oauth/verify',
            'verification_uri_complete':config.PUBLIC_URL+'/oauth/verify?user_code='+code,'expires_in':600,'interval':5}


@app.post('/api/authorizations/{aid}/decision')
async def authorize_decision(aid:str,request:Request):
    body=await request.json(); approve=body.get('approve') is True
    async with db.pool().acquire() as conn,conn.transaction():
        user=await caller(conn,request,operator=True)
        pending=await conn.fetchrow('SELECT * FROM plane_authorizations WHERE id=$1 FOR UPDATE',as_uuid(aid))
        if not pending or pending['status']!='pending' or pending['expires_at']<=now(): fail(409,'REQUEST_NOT_PENDING')
        target=await conn.fetchrow('SELECT * FROM service_accounts WHERE id=$1 FOR UPDATE',pending['principal_id'])
        if target['portal_parent']!=user.id: fail(403,'FORBIDDEN')
        if target['disabled'] or target['portal_state']!='active': fail(409,'PRINCIPAL_INACTIVE')
        until=now()+timedelta(seconds=pending['duration_seconds'])
        await conn.execute('UPDATE plane_authorizations SET status=$2,decided_by=$3,decided_at=now(),authorized_until=$4 WHERE id=$1',pending['id'],'approved' if approve else 'denied',user.id,until if approve else None)
        await record(conn,user,'elevation.approve' if approve else 'elevation.deny',target['name'],detail={'scope':pending['scope'],'seconds':pending['duration_seconds']})
    return {'status':'approved' if approve else 'denied','authorized_until':until if approve else None}


@app.post('/oauth/token')
async def oauth_token(request:Request):
    throttle('token:'+address(request),120,60)
    body=dict(await request.form()); grant=body.get('grant_type'); client=body.get('client_id','datum-local')
    error=None; result=None
    async with db.pool().acquire() as conn,conn.transaction():
        if grant=='refresh_token':
            old=await conn.fetchrow("SELECT * FROM plane_credentials WHERE token_hash=$1 AND kind='refresh' FOR UPDATE",auth.hash_token(body.get('refresh_token','')))
            if not old or old['client_id']!=client: error='invalid_grant'
            elif old['consumed_at']:
                await conn.execute('UPDATE plane_credentials SET revoked_at=now() WHERE family=$1',old['family'])
                await record(conn,None,'oauth.replay',old['family'],'denied'); error='invalid_grant'
            elif old['revoked_at'] or old['authorization_expires_at']<=now(): error='invalid_grant'
            else:
                check=dict(old); check['kind']='access'
                await identity(conn,check,lock=True)
                await conn.execute('UPDATE plane_credentials SET consumed_at=now(),revoked_at=now() WHERE id=$1',old['id'])
                result=await token_pair(conn,{**dict(old),'authorized_until':old['authorization_expires_at']})
        elif grant in ('urn:ietf:params:oauth:grant-type:device_code','authorization_code'):
            field='device_hash' if grant.startswith('urn:') else 'code_hash'
            raw=body.get('device_code' if field=='device_hash' else 'code','')
            pending=await conn.fetchrow(f'SELECT * FROM plane_authorizations WHERE {field}=$1 FOR UPDATE',auth.hash_token(raw))
            if not pending or pending['client_id']!=client: error='invalid_grant'
            elif pending['status']=='consumed':
                await conn.execute('UPDATE plane_credentials SET revoked_at=now() WHERE family=$1',pending['family'])
                await record(conn,None,'oauth.replay',pending['family'],'denied'); error='invalid_grant'
            elif pending['expires_at']<=now(): error='expired_token'
            elif pending['status']=='denied': error='access_denied'
            elif field=='device_hash' and pending['last_polled_at'] and (now()-pending['last_polled_at']).total_seconds()<pending['poll_interval']:
                await conn.execute('UPDATE plane_authorizations SET poll_interval=poll_interval+5,last_polled_at=now() WHERE id=$1',pending['id']); error='slow_down'
            elif pending['status']=='pending':
                await conn.execute('UPDATE plane_authorizations SET last_polled_at=now() WHERE id=$1',pending['id']); error='authorization_pending'
            else:
                if field=='code_hash':
                    verifier=body.get('code_verifier','')
                    challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
                    if not 43<=len(verifier)<=128 or not secrets.compare_digest(challenge,pending['challenge']) or body.get('redirect_uri')!=pending['redirect_uri']:
                        fail(400,'invalid_grant')
                target=await conn.fetchrow('SELECT * FROM service_accounts WHERE id=$1 FOR UPDATE',pending['principal_id'])
                if target['disabled'] or target['portal_state']!='active': fail(400,'invalid_grant')
                parent=await conn.fetchrow('SELECT * FROM service_accounts WHERE id=$1',target['portal_parent'])
                if not parent or parent['disabled'] or parent['portal_state']!='active': fail(400,'invalid_grant')
                await conn.execute("UPDATE plane_authorizations SET status='consumed' WHERE id=$1",pending['id'])
                result=await token_pair(conn,pending)
        else: error='unsupported_grant_type'
        if result: await record(conn,None,'oauth.token',client,detail={'grant_type':grant})
    return response({'error':error},400) if error else response(result)


@app.get('/oauth/authorize')
async def authorize_page(request:Request):
    # Keep the OAuth parameters in the query; the portal renders a consent form
    # after login and submits its CSRF-protected decision to the endpoint below.
    return source_ui_shell()


@app.post('/api/oauth/consent')
async def consent(request:Request):
    body=await request.json(); scope=body.get('scope','mcp'); scope_tier(scope)
    duration=number(body.get('duration_seconds'),900,900,28800)
    if duration not in AGENT_SESSION_DURATIONS: fail(400,'INVALID_SESSION_DURATION')
    challenge=body.get('code_challenge','')
    import re
    if body.get('code_challenge_method')!='S256' or not re.fullmatch(r'[A-Za-z0-9_-]{43}',challenge): fail(400,'invalid_request')
    if body.get('response_type')!='code' or body.get('resource',config.PUBLIC_URL+'/mcp')!=config.PUBLIC_URL+'/mcp': fail(400,'invalid_request')
    async with db.pool().acquire() as conn,conn.transaction():
        user=await caller(conn,request,operator=True)
        client=await conn.fetchrow('SELECT * FROM plane_clients WHERE client_id=$1',body.get('client_id',''))
        redirect=body.get('redirect_uri','')
        if not client or redirect not in unpack(client['redirect_uris']): fail(400,'invalid_redirect_uri')
        target=await conn.fetchrow("SELECT * FROM service_accounts WHERE name=$1 AND portal_kind='agent' FOR UPDATE",body.get('on_behalf_of',''))
        if not target or target['portal_parent']!=user.id or target['disabled'] or target['portal_state']!='active': fail(403,'INVALID_AGENT')
        if scope_tier(scope)>target['max_tier']: fail(403,'SCOPE_ABOVE_PRINCIPAL')
        raw=secrets.token_urlsafe(32)
        await conn.execute('''INSERT INTO plane_authorizations(principal_id,client_id,code_hash,challenge,redirect_uri,scope,status,expires_at,duration_seconds,authorized_until,decided_by,decided_at)
            VALUES($1,$2,$3,$4,$5,$6,'approved',$7,$8,$9,$10,now())''',target['id'],client['client_id'],auth.hash_token(raw),challenge,redirect,scope,now()+timedelta(minutes=5),duration,now()+timedelta(seconds=duration),user.id)
        await record(conn,user,'oauth.consent',target['name'],detail={'scope':scope,'client_id':client['client_id'],'duration_seconds':duration})
    return {'redirect':redirect+('&' if '?' in redirect else '?')+urlencode({'code':raw,'state':body.get('state','')})}


@app.post('/api/pending/{pid}/decision')
async def pending_decision(pid:str,request:Request):
    body=await request.json()
    async with db.pool().acquire() as conn,conn.transaction():
        operator=await caller(conn,request,operator=True)
        pending=await conn.fetchrow('SELECT * FROM plane_pending_calls WHERE id=$1 FOR UPDATE',as_uuid(pid))
        if not pending or pending['status']!='pending' or pending['expires_at']<=now(): fail(409,'REQUEST_NOT_PENDING')
        target=await conn.fetchrow('SELECT * FROM service_accounts WHERE id=$1 FOR UPDATE',pending['principal_id'])
        if target['portal_parent']!=operator.id: fail(403,'FORBIDDEN')
        if body.get('approve') is not True:
            await conn.execute("UPDATE plane_pending_calls SET status='denied',decided_by=$2,decided_at=now() WHERE id=$1",pending['id'],operator.id)
            await record(conn,operator,'release.deny',pending['id']); return {'status':'denied'}
        cred=await conn.fetchrow('SELECT * FROM plane_credentials WHERE id=$1',pending['credential_id'])
        user=await identity(conn,cred)
        args=unpack(pending['args']); snapshot=unpack(pending['grant_snapshot'])
        if user.tier<3 or snapshot['tier']<3 or not user.permits('publish',args['path']): fail(403,'AUTHORITY_WITHDRAWN')
        from datum_sync import vault
        if not all(any(vault.matches(p,args['path']) for p in g.get('publish',[])) and not any(vault.matches(p,args['path']) for p in g.get('deny',[])) for g in snapshot['grants']): fail(403,'AUTHORITY_WITHDRAWN')
        rid=await conn.fetchval('INSERT INTO plane_releases(pending_id,path,content,principal_id) VALUES($1,$2,$3,$4) RETURNING id',pending['id'],args['path'],args['content'],user.id)
        result={'release_id':str(rid),'path':args['path'],'published':True}
        await conn.execute("UPDATE plane_pending_calls SET status='executed',result=$2,decided_by=$3,decided_at=now() WHERE id=$1",pending['id'],json.dumps(result),operator.id)
        await record(conn,operator,'release.approve',pending['id'])
        await record(conn,user,'release.publish',args['path'],detail={'release_id':str(rid)})
    return result


@app.post('/api/access/call')
async def access_call(request:Request):
    body=await request.json()
    async with db.pool().acquire() as conn,conn.transaction():
        user=await caller(conn,request,lock=True)
        if user.credential['kind']=='session': fail(403,'AGENT_BEARER_REQUIRED')
        return await resource_call(conn,user,body.get('tool'),body.get('arguments',{}),trace=request.state.trace)


@app.get('/api/credential-catalog')
async def credential_catalog(request:Request):
    async with db.pool().acquire() as conn:
        user=await caller(conn,request)
        if user.kind!='agent' or user.credential['kind']=='session': fail(403,'AGENT_BEARER_REQUIRED')
        return {'items':await credential_access.catalog(conn,user)}


@app.post('/api/credential-requests')
async def create_credential_request(request:Request):
    body=await request.json()
    try:
        credential_id=as_uuid(body.get('credential_id'))
        tools=credential_access.validate_tools(body.get('requested_tools'))
    except ValueError as exc:
        fail(400,'INVALID_PARAMETER',str(exc))
    duration=number(body.get('duration_seconds'),3600,300,2592000)
    purpose=str(body.get('purpose','')).strip()
    if not purpose or len(purpose)>300: fail(400,'INVALID_PURPOSE','Purpose must contain 1-300 characters.')
    async with db.pool().acquire() as conn,conn.transaction():
        user=await caller(conn,request,lock=True)
        if user.kind!='agent' or user.credential['kind']=='session': fail(403,'AGENT_BEARER_REQUIRED')
        credential,requestable=await credential_access.requestable_tools(conn,user,credential_id)
        if not credential: fail(404,'CREDENTIAL_NOT_FOUND')
        if not set(tools)<=set(requestable): fail(403,'TOOLS_OUTSIDE_AUTHORITY')
        await conn.execute("UPDATE credential_requests SET status='expired' WHERE requested_by=$1 AND credential_id=$2 AND status='pending' AND expires_at<=now()",user.id,credential_id)
        try:
            row=await conn.fetchrow('''INSERT INTO credential_requests(requested_by,requested_by_name,credential_id,requested_tools,duration_seconds,purpose)
                VALUES($1,$2,$3,$4,$5,$6) RETURNING *''',user.id,user.name,credential_id,tools,duration,purpose)
        except asyncpg.UniqueViolationError:
            fail(409,'REQUEST_ALREADY_PENDING')
        await credential_access.event(conn,credential,'credential.access.requested',principal=user,trace=request.state.trace,
                                      detail={'request_id':str(row['id']),'tools':tools,'duration_seconds':duration})
        await record(conn,user,'credential.request',row['id'],detail={'credential_id':str(credential_id),'tools':tools,'duration_seconds':duration},trace=request.state.trace)
    return response({'id':row['id'],'status':'pending','expires_at':row['expires_at']},201)


@app.post('/api/credential-requests/{request_id}/decision')
async def decide_credential_request(request_id:str,request:Request):
    body=await request.json()
    async with db.pool().acquire() as conn,conn.transaction():
        operator=await caller(conn,request,operator=True,lock=True)
        pending=await conn.fetchrow('SELECT * FROM credential_requests WHERE id=$1 FOR UPDATE',as_uuid(request_id))
        if not pending or pending['status']!='pending' or pending['expires_at']<=now(): fail(409,'REQUEST_NOT_PENDING')
        target=await conn.fetchrow('SELECT * FROM service_accounts WHERE id=$1 FOR UPDATE',pending['requested_by'])
        if not target or target['portal_parent']!=operator.id: fail(403,'FORBIDDEN')
        credential=await conn.fetchrow('''SELECT c.* FROM auth_credentials c JOIN connections x ON x.name=c.connection_name
            WHERE c.id=$1 AND x.owner_id=$2 FOR UPDATE OF c''',pending['credential_id'],operator.id)
        if body.get('approve') is not True:
            reason=str(body.get('reason',''))[:300]
            await conn.execute("UPDATE credential_requests SET status='denied',reviewed_by=$2,reviewed_at=now(),decision_detail=$3 WHERE id=$1",pending['id'],operator.id,json.dumps({'reason':reason}))
            await credential_access.event(conn,credential,'credential.access.denied',principal=target,trace=request.state.trace,
                                          detail={'request_id':str(pending['id']),'reason':reason})
            await record(conn,operator,'credential.request.deny',pending['id'],trace=request.state.trace)
            return {'status':'denied'}
        try:
            tools=credential_access.validate_tools(body.get('allowed_tools',list(pending['requested_tools'])))
        except ValueError as exc:
            fail(400,'INVALID_PARAMETER',str(exc))
        if not set(tools)<=set(pending['requested_tools']): fail(400,'GRANT_EXCEEDS_REQUEST')
        duration=number(body.get('duration_seconds'),pending['duration_seconds'],300,pending['duration_seconds'])
        principal,policies,tier=await credential_access.principal_policy(conn,pending['requested_by'])
        connection=await conn.fetchrow('SELECT tier FROM connections WHERE name=$1',credential['connection_name']) if credential else None
        if (not principal or not credential or credential['status']!='active' or not connection
                or tier<connection['tier'] or any(not credential_access.policy_permits(policies,credential['connection_name'],tool) for tool in tools)):
            fail(403,'AUTHORITY_WITHDRAWN')
        valid_until=now()+timedelta(seconds=duration)
        if credential['expires_at'] is not None: valid_until=min(valid_until,credential['expires_at'])
        grant=await conn.fetchrow('''INSERT INTO credential_grants(principal_id,principal_name,credential_id,allowed_tools,source,request_id,valid_until,granted_by)
            VALUES($1,$2,$3,$4,'request',$5,$6,$7) RETURNING *''',principal['id'],principal['name'],credential['id'],tools,pending['id'],valid_until,operator.id)
        await conn.execute("UPDATE credential_requests SET status='approved',reviewed_by=$2,reviewed_at=now(),decision_detail=$3 WHERE id=$1",pending['id'],operator.id,json.dumps({'allowed_tools':tools,'duration_seconds':duration,'grant_id':str(grant['id'])}))
        await credential_access.event(conn,credential,'credential.access.approved',principal=principal,trace=request.state.trace,
                                      detail={'request_id':str(pending['id']),'grant_id':str(grant['id']),'tools':tools,'valid_until':valid_until})
        await record(conn,operator,'credential.request.approve',pending['id'],detail={'grant_id':str(grant['id']),'tools':tools},trace=request.state.trace)
    return {'status':'approved','grant_id':grant['id'],'valid_until':valid_until}


@app.delete('/api/credential-grants/{grant_id}')
async def revoke_credential_grant(grant_id:str,request:Request):
    raw=await request.body()
    try: body=json.loads(raw) if raw else {}
    except (json.JSONDecodeError,UnicodeDecodeError): fail(400,'INVALID_PARAMETER')
    async with db.pool().acquire() as conn,conn.transaction():
        operator=await caller(conn,request,operator=True,lock=True)
        grant=await conn.fetchrow('SELECT * FROM credential_grants WHERE id=$1 FOR UPDATE',as_uuid(grant_id))
        if not grant: fail(404,'GRANT_NOT_FOUND')
        target=await conn.fetchrow('SELECT * FROM service_accounts WHERE id=$1',grant['principal_id'])
        if not target or target['portal_parent']!=operator.id: fail(403,'FORBIDDEN')
        owned=await conn.fetchval('''SELECT 1 FROM auth_credentials c JOIN connections x ON x.name=c.connection_name
                                     WHERE c.id=$1 AND x.owner_id=$2''',grant['credential_id'],operator.id)
        if not owned: fail(403,'FORBIDDEN')
        if grant['state']!='active': fail(409,'GRANT_NOT_ACTIVE')
        reason=str(body.get('reason','operator revoked access'))[:300]
        await conn.execute("UPDATE credential_grants SET state='revoked',revoked_by=$2,revoked_at=now(),revoke_reason=$3 WHERE id=$1",grant['id'],operator.id,reason)
        credential=await conn.fetchrow('SELECT * FROM auth_credentials WHERE id=$1',grant['credential_id'])
        await credential_access.event(conn,credential,'credential.access.revoked',principal=target,trace=request.state.trace,
                                      detail={'grant_id':str(grant['id']),'reason':reason})
        await record(conn,operator,'credential.grant.revoke',grant['id'],trace=request.state.trace)
    return {'status':'revoked'}


@app.post('/api/credential-grants/{grant_id}/tools/{tool_name}/revoke')
async def revoke_credential_tool(grant_id:str,tool_name:str,request:Request):
    async with db.pool().acquire() as conn,conn.transaction():
        operator=await caller(conn,request,operator=True,lock=True)
        grant=await conn.fetchrow('SELECT * FROM credential_grants WHERE id=$1 FOR UPDATE',as_uuid(grant_id))
        if not grant: fail(404,'GRANT_NOT_FOUND')
        target=await conn.fetchrow('SELECT * FROM service_accounts WHERE id=$1',grant['principal_id'])
        if not target or target['portal_parent']!=operator.id: fail(403,'FORBIDDEN')
        owned=await conn.fetchval('''SELECT 1 FROM auth_credentials c JOIN connections x ON x.name=c.connection_name
                                     WHERE c.id=$1 AND x.owner_id=$2''',grant['credential_id'],operator.id)
        if not owned: fail(403,'FORBIDDEN')
        if grant['state']!='active' or tool_name not in grant['allowed_tools']: fail(409,'TOOL_GRANT_NOT_ACTIVE')
        remaining=[tool for tool in grant['allowed_tools'] if tool!=tool_name]
        if remaining:
            await conn.execute('UPDATE credential_grants SET allowed_tools=$2 WHERE id=$1',grant['id'],remaining)
        else:
            await conn.execute("UPDATE credential_grants SET state='revoked',revoked_by=$2,revoked_at=now(),revoke_reason='Last tool revoked' WHERE id=$1",grant['id'],operator.id)
        credential=await conn.fetchrow('SELECT * FROM auth_credentials WHERE id=$1',grant['credential_id'])
        await credential_access.event(conn,credential,'credential.tool.revoked',principal=target,trace=request.state.trace,
                                      detail={'grant_id':str(grant['id']),'tool':tool_name,'remaining_tools':remaining})
        await record(conn,operator,'credential.tool.revoke',tool_name,trace=request.state.trace,
                     detail={'grant_id':str(grant['id'])})
    return {'grant_id':grant['id'],'revoked_tool':tool_name,'remaining_tools':remaining,
            'state':'active' if remaining else 'revoked'}


@app.post('/api/auth-credentials/{credential_id}/status')
async def credential_status(credential_id:str,request:Request):
    body=await request.json(); action=body.get('action')
    if action not in ('disable','enable'): fail(400,'INVALID_ACTION')
    async with db.pool().acquire() as conn,conn.transaction():
        operator=await caller(conn,request,operator=True,lock=True)
        credential=await conn.fetchrow('''SELECT c.* FROM auth_credentials c JOIN connections x ON x.name=c.connection_name
            WHERE c.id=$1 AND x.owner_id=$2 FOR UPDATE OF c''',as_uuid(credential_id),operator.id)
        if not credential: fail(404,'CREDENTIAL_NOT_FOUND')
        if action=='enable' and (not credential['current_version_id'] or (credential['expires_at'] and credential['expires_at']<=now())):
            fail(409,'CREDENTIAL_NOT_USABLE')
        status='disabled' if action=='disable' else 'active'
        credential=await conn.fetchrow('UPDATE auth_credentials SET status=$2,updated_at=now() WHERE id=$1 RETURNING *',credential['id'],status)
        await credential_access.event(conn,credential,'credential.'+('disabled' if action=='disable' else 'enabled'),principal=operator,trace=request.state.trace)
        await record(conn,operator,'credential.'+action,credential['id'],trace=request.state.trace)
    return {'id':credential['id'],'status':status}


@app.post('/api/auth-credentials/{credential_id}/rotate')
async def rotate_auth_credential(credential_id:str,request:Request):
    body=await request.json()
    try: secret=credential_access.validate_secret(body.get('secret'))
    except ValueError as exc: fail(400,'INVALID_SECRET',str(exc))
    expires_days=body.get('expires_days')
    if expires_days is not None: expires_days=number(expires_days,None,1,365)
    async with db.pool().acquire() as conn,conn.transaction():
        operator=await caller(conn,request,operator=True,lock=True)
        credential=await conn.fetchrow('''SELECT c.* FROM auth_credentials c JOIN connections x ON x.name=c.connection_name
            WHERE c.id=$1 AND x.owner_id=$2 FOR UPDATE OF c''',as_uuid(credential_id),operator.id)
        if not credential: fail(404,'CREDENTIAL_NOT_FOUND')
        staged=await credential_access.stage(conn,credential,secret,created_by=operator.id)
        await record(conn,operator,'credential.rotation.stage',credential['id'],detail={'version':staged['version']},trace=request.state.trace)
    try:
        probe=await federation.probe_secret(credential['connection_name'],secret)
    except Exception:
        async with db.pool().acquire() as conn,conn.transaction():
            current=await conn.fetchrow('SELECT * FROM auth_credentials WHERE id=$1',credential['id'])
            await credential_access.fail_stage(conn,current,staged,'UPSTREAM_TEST_FAILED')
        fail(502,'CREDENTIAL_TEST_FAILED','The replacement credential was rejected; the current version remains active.')
    async with db.pool().acquire() as conn,conn.transaction():
        operator=await caller(conn,request,operator=True,lock=True)
        current=await conn.fetchrow('SELECT * FROM auth_credentials WHERE id=$1 FOR UPDATE',credential['id'])
        pending=await conn.fetchrow("SELECT * FROM auth_credential_versions WHERE id=$1 AND state='pending' FOR UPDATE",staged['id'])
        if not pending or current['current_version_id']!=credential['current_version_id']: fail(409,'ROTATION_CONFLICT')
        expiry=now()+timedelta(days=expires_days) if expires_days is not None else None
        current=await credential_access.activate(conn,current,pending,principal=operator,expires_at=expiry)
        await record(conn,operator,'credential.rotate',current['id'],detail={'version':pending['version']},trace=request.state.trace)
    refresh_warning=None
    try:
        await federation.refresh(current['connection_name'])
    except Exception:
        # The candidate already passed the upstream probe and is active. A
        # catalogue refresh failure must not misreport that committed result.
        refresh_warning='Credential activated; tool catalogue refresh failed.'
    return {'id':current['id'],'status':current['status'],'version':pending['version'],
            'test':probe,'warning':refresh_warning}


@app.post('/api/mcp-servers')
async def add_mcp_server(request:Request):
    body=await request.json(); template=MCP_TEMPLATES.get(body.get('template'))
    if not template: fail(400,'UNKNOWN_SERVER_TEMPLATE')
    secret={'token':os.environ[template['token_env']]}
    config_value={'url':template['url'],'tool_prefix':template['tool_prefix'],'headers':{},
                  'auth_inject':{'type':'bearer','secret_field':'token'},'timeout_seconds':10}
    async with db.pool().acquire() as conn,conn.transaction():
        operator=await caller(conn,request,operator=True,lock=True)
        if await conn.fetchval('SELECT 1 FROM connections WHERE name=$1',template['name']):
            fail(409,'SERVER_ALREADY_REGISTERED')
        await conn.execute('''INSERT INTO connections(name,type,tier,scope,access,description,config,created_by,
            portal_state,provider_kind,display_name,owner_id) VALUES($1,'mcp',$2,'global','read',$3,$4,$5,
            'active',$6,$7,$8)''',template['name'],template['tier'],template['description'],json.dumps(config_value),
            operator.name,template['provider_kind'],template['display_name'],operator.id)
        credential,_=await credential_access.create(conn,connection_name=template['name'],
            label=template['credential_label'],auth_type='bearer',owner_name=operator.name,
            secret=secret,created_by=operator.id)
        await conn.execute("INSERT INTO mcp_server_events(connection_name,actor_id,actor_name,trace_id,kind,detail) VALUES($1,$2,$3,$4,'server.registered',$5)",
            template['name'],operator.id,operator.name,request.state.trace,json.dumps({'provider_kind':template['provider_kind'],'credential_id':str(credential['id'])}))
        await record(conn,operator,'mcp.server.register',template['name'],trace=request.state.trace,
                     detail={'provider_kind':template['provider_kind']})
    try:
        status=await federation.refresh(template['name'])
    except Exception:
        status={'status':'down','tool_count':0}
    return response({'name':template['name'],'status':status},201)


@app.post('/api/mcp-servers/{name}/status')
async def mcp_server_status(name:str,request:Request):
    body=await request.json(); action=body.get('action')
    if action not in ('disable','enable'): fail(400,'INVALID_ACTION')
    async with db.pool().acquire() as conn,conn.transaction():
        operator=await caller(conn,request,operator=True,lock=True)
        server=await conn.fetchrow("SELECT * FROM connections WHERE name=$1 AND type='mcp' FOR UPDATE",name)
        if not server: fail(404,'SERVER_NOT_FOUND')
        if server['owner_id']!=operator.id: fail(403,'FORBIDDEN')
        state='disabled' if action=='disable' else 'active'
        await conn.execute('UPDATE connections SET portal_state=$2,updated_at=now() WHERE name=$1',name,state)
        await conn.execute("INSERT INTO mcp_server_events(connection_name,actor_id,actor_name,trace_id,kind,detail) VALUES($1,$2,$3,$4,$5,'{}')",
            name,operator.id,operator.name,request.state.trace,'server.'+state)
        await record(conn,operator,'mcp.server.'+action,name,trace=request.state.trace)
    refresh_status=None
    if action=='enable':
        try: refresh_status=await federation.refresh(name)
        except Exception: refresh_status={'status':'down'}
    return {'name':name,'state':state,'refresh':refresh_status}


@app.post('/api/mcp-servers/{name}/tools/{tool_name}/status')
async def mcp_tool_status(name:str,tool_name:str,request:Request):
    body=await request.json(); action=body.get('action')
    if action not in ('disable','enable'): fail(400,'INVALID_ACTION')
    async with db.pool().acquire() as conn,conn.transaction():
        operator=await caller(conn,request,operator=True,lock=True)
        server=await conn.fetchrow("SELECT * FROM connections WHERE name=$1 AND type='mcp'",name)
        if not server: fail(404,'SERVER_NOT_FOUND')
        if server['owner_id']!=operator.id: fail(403,'FORBIDDEN')
        tool=await conn.fetchrow('''UPDATE federated_tools SET enabled=$3 WHERE connection=$1 AND tool_name=$2
                                    RETURNING *''',name,tool_name,action=='enable')
        if not tool: fail(404,'TOOL_NOT_FOUND')
        await conn.execute("INSERT INTO mcp_server_events(connection_name,actor_id,actor_name,trace_id,kind,detail) VALUES($1,$2,$3,$4,$5,$6)",
            name,operator.id,operator.name,request.state.trace,'tool.'+('enabled' if action=='enable' else 'disabled'),json.dumps({'tool':tool_name}))
        await record(conn,operator,'mcp.tool.'+action,tool_name,trace=request.state.trace,detail={'connection':name})
    return {'connection':name,'tool':tool_name,'enabled':action=='enable'}


@app.post('/api/integrations/{name}/refresh')
async def refresh_integration(name:str,request:Request):
    async with db.pool().acquire() as conn,conn.transaction():
        operator=await caller(conn,request,operator=True)
        server=await conn.fetchrow("SELECT 1 FROM connections WHERE name=$1 AND type='mcp' AND owner_id=$2",name,operator.id)
        if not server: fail(404,'INTEGRATION_NOT_FOUND')
        await record(conn,operator,'federate.refresh.request',name,trace=request.state.trace)
    try:
        return await federation.refresh(name)
    except Exception:
        fail(502,'UPSTREAM_UNAVAILABLE','The MCP server could not be refreshed.')


@app.delete('/api/sessions/{sid}')
async def close_session(sid:str,request:Request):
    async with db.pool().acquire() as conn,conn.transaction():
        user=await caller(conn,request,operator=True)
        row=await conn.fetchrow('SELECT * FROM plane_sessions WHERE id=$1',as_uuid(sid))
        if not row: fail(404,'SESSION_NOT_FOUND')
        target=await conn.fetchrow('SELECT * FROM service_accounts WHERE id=$1',row['principal_id'])
        if target['portal_parent']!=user.id: fail(403,'FORBIDDEN')
        await conn.execute('UPDATE plane_sessions SET ended_at=now() WHERE id=$1',row['id'])
        await record(conn,user,'session.close',sid)
    return {'closed':True}


TOOLS=[{'name':'whoami','description':'Inspect your current identity and effective authority.','inputSchema':{'type':'object','properties':{}}},
       {'name':'documents_read','description':'Read a document within your grant.','inputSchema':{'type':'object','properties':{'path':{'type':'string'}},'required':['path']}},
       {'name':'resources_list','description':'List durable resources owned by or explicitly shared with this agent.','inputSchema':{'type':'object','properties':{'limit':{'type':'integer','minimum':1,'maximum':100}}}},
       {'name':'resources_read','description':'Read one governed resource by ID.','inputSchema':{'type':'object','properties':{'id':{'type':'string'}},'required':['id']}},
       {'name':'resources_create','description':'Create a private, versioned artifact under agents/<your-agent-name>/.','inputSchema':{'type':'object','properties':{'path':{'type':'string'},'title':{'type':'string'},'media_type':{'type':'string','enum':['text/plain','text/markdown','text/html','application/json','application/geo+json','text/csv']},'content':{'type':'string'},'ttl_hours':{'type':'integer','minimum':1,'maximum':720}},'required':['path','title','media_type','content']}},
       {'name':'resources_share','description':'Explicitly grant another active agent in this fleet read access to a resource.','inputSchema':{'type':'object','properties':{'id':{'type':'string'},'target':{'type':'string'},'ttl_hours':{'type':'integer','minimum':1,'maximum':720}},'required':['id','target']}},
       {'name':'resources_revoke','description':'Immediately revoke an agent resource share.','inputSchema':{'type':'object','properties':{'id':{'type':'string'},'target':{'type':'string'}},'required':['id','target']}},
       {'name':'documents_write','description':'Write a document after elevation.','inputSchema':{'type':'object','properties':{'path':{'type':'string'},'content':{'type':'string'}},'required':['path','content']}},
       {'name':'releases_publish','description':'Request human approval to publish a frozen document revision.','inputSchema':{'type':'object','properties':{'path':{'type':'string'}},'required':['path']}}]
BASELINE_TOOLS={'whoami','documents_read','resources_list','resources_read','resources_create','resources_share','resources_revoke'}


@app.get('/mcp')
async def mcp_get(): return Response(status_code=405,headers={'Allow':'POST, DELETE'})


@app.delete('/mcp')
async def mcp_delete(request:Request):
    async with db.pool().acquire() as conn,conn.transaction():
        user=await caller(conn,request,lock=True)
        result=await conn.fetchval('UPDATE plane_sessions SET ended_at=now() WHERE id=$1 AND credential_id=$2 RETURNING id',as_uuid(request.headers.get('mcp-session-id')),user.credential['id'])
        if not result: fail(404,'SESSION_NOT_FOUND')
        await record(conn,user,'session.close',result)
    return Response(status_code=204)


@app.post('/mcp')
async def mcp(request:Request):
    raw_body=await request.body()
    trace=request.state.trace
    started=await mcp_observe.start(trace,http_method=request.method,request_bytes=len(raw_body))
    user=None

    async def complete(value,status=200,headers=None,*,outcome='ok',error_code=None):
        result=response(value,status,headers)
        await mcp_observe.finish(trace,started=started,outcome=outcome,
                                 response_bytes=len(result.body),error_code=error_code)
        return result

    async def complete_empty(status=202,*,outcome='ok',error_code=None):
        await mcp_observe.finish(trace,started=started,outcome=outcome,
                                 response_bytes=0,error_code=error_code)
        return Response(status_code=status)

    try:
        try:
            payload=json.loads(raw_body)
        except (json.JSONDecodeError,UnicodeDecodeError):
            return await complete({'detail':{'code':'INVALID_RPC','message':'request body is not valid JSON'}},
                                  400,outcome='protocol_error',error_code='INVALID_RPC')
        if not isinstance(payload,dict) or payload.get('jsonrpc')!='2.0':
            return await complete({'detail':{'code':'INVALID_RPC','message':'not a JSON-RPC 2.0 message'}},
                                  400,outcome='protocol_error',error_code='INVALID_RPC')
        method=payload.get('method'); rid=payload.get('id'); params=payload.get('params') or {}
        if not isinstance(params,dict):
            return await complete({'jsonrpc':'2.0','id':rid,'error':{'code':-32602,'message':'params must be an object'}},
                                  outcome='protocol_error',error_code=-32602)
        name=params.get('name') if method=='tools/call' else None
        await mcp_observe.mark(trace,'rpc.parsed',rpc_method=method,
                               request_id=mcp_observe.rpc_id(rid),tool_name=name,
                               detail={'method':str(method)[:80],
                                       'tool':str(name)[:200] if name is not None else None})

        sid=None; output={}; deferred=None; early=None
        async with db.pool().acquire() as conn,conn.transaction():
            user=await caller(conn,request,lock=True)
            await mcp_observe.mark(trace,'auth.accepted',conn=conn,principal=user,
                                   detail={'credential_kind':user.credential['kind']})
            if user.credential['kind']=='session': fail(403,'BEARER_REQUIRED')
            if method=='initialize':
                await conn.execute("UPDATE plane_sessions SET ended_at=now() WHERE principal_id=$1 AND ended_at IS NULL AND (expires_at<=now() OR last_seen_at<now()-interval '15 minutes')",user.id)
                count=await conn.fetchval('SELECT count(*) FROM plane_sessions WHERE principal_id=$1 AND ended_at IS NULL',user.id)
                limit=min(g.get('sessions',1) for g in user.grants)
                if count>=limit:
                    await mcp_observe.mark(trace,'session.denied',conn=conn,
                                           detail={'code':'SESSION_LIMIT','limit':limit})
                    early=({'jsonrpc':'2.0','id':rid,'error':{'code':-32000,'message':'SESSION_LIMIT','data':{'limit':limit}}},
                           200,'denied','SESSION_LIMIT')
                else:
                    client_info=params.get('clientInfo') if isinstance(params.get('clientInfo'),dict) else {}
                    client_name=str(client_info.get('name','client'))[:80]
                    sid=await conn.fetchval('''INSERT INTO plane_sessions(principal_id,credential_id,client_name,expires_at)
                        VALUES($1,$2,$3,$4) RETURNING id''',user.id,user.credential['id'],client_name,user.credential['authorization_expires_at'])
                    await record(conn,user,'session.open',sid)
                    await mcp_observe.mark(trace,'session.opened',conn=conn,session_id=sid,
                                           client_name=client_name,detail={'client_name':client_name})
                    version=params.get('protocolVersion')
                    if version not in ('2024-11-05','2025-03-26','2025-06-18'): version='2025-03-26'
                    output={'protocolVersion':version,'capabilities':{'tools':{'listChanged':False}},'serverInfo':{'name':'datum-auth-portal','version':'0.8.0'}}
            else:
                raw=request.headers.get('mcp-session-id')
                if not raw: fail(400,'SESSION_REQUIRED')
                session=await conn.fetchrow('''SELECT * FROM plane_sessions WHERE id=$1 AND credential_id=$2 AND ended_at IS NULL
                    AND expires_at>now()''',as_uuid(raw),user.credential['id'])
                if not session: fail(404,'SESSION_NOT_FOUND')
                await conn.execute('UPDATE plane_sessions SET last_seen_at=now() WHERE id=$1',session['id'])
                await mcp_observe.mark(trace,'session.validated',conn=conn,session_id=session['id'],
                                       client_name=session['client_name'])
                if method=='notifications/initialized':
                    early=(None,202,'ok',None)
                elif method=='ping':
                    output={}
                elif method=='tools/list':
                    local=TOOLS if user.tier>=3 else [tool for tool in TOOLS if tool['name'] in BASELINE_TOOLS]
                    remote=await federation.listing(conn,user)
                    projected=[{'name':row['tool_name'],'description':row['description']+' [via '+row['connection']+']',
                                'inputSchema':unpack(row['input_schema'])} for row in remote]
                    output={'tools':local+projected}
                    await mcp_observe.mark(trace,'catalogue.returned',conn=conn,
                                           detail={'tool_count':len(output['tools'])})
                elif method=='tools/call':
                    arguments=params.get('arguments') or {}
                    try:
                        deferred=await federation.prepare(conn,user,name,arguments,trace=request.state.trace)
                    except HTTPException as exc:
                        code=exc.detail['code']
                        await record(conn,user,'tool.denied',str(name or ''),'denied',{'code':code},request.state.trace)
                        await mcp_observe.mark(trace,'tool.denied',conn=conn,provider='federated',
                                               detail={'code':code})
                        output={'content':[{'type':'text','text':json.dumps(exc.detail)}],'isError':True}
                    if deferred:
                        await mcp_observe.mark(trace,'tool.authorized',conn=conn,provider='federated',
                                               connection_name=deferred['connection'],
                                               upstream_tool=deferred['upstream_name'],
                                               detail={'connection':deferred['connection'],
                                                       'upstream_tool':deferred['upstream_name'],
                                                       'credential_id':deferred['outbound_credential_id'],
                                                       'credential_version':deferred['outbound_credential_version']})
                    elif not output:
                        try:
                            async with conn.transaction():
                                value=await resource_call(conn,user,name,arguments,trace=request.state.trace)
                            output={'content':[{'type':'text','text':json.dumps(jsonable_encoder(value))}],
                                    'structuredContent':jsonable_encoder(value),'isError':False}
                            await mcp_observe.mark(trace,'tool.completed',conn=conn,provider='local',
                                                   detail={'is_error':False})
                        except HTTPException as exc:
                            code=exc.detail['code']
                            await record(conn,user,'tool.denied',str(name or ''),'denied',{'code':code},request.state.trace)
                            await mcp_observe.mark(trace,'tool.denied',conn=conn,provider='local',
                                                   detail={'code':code})
                            output={'content':[{'type':'text','text':json.dumps(exc.detail)}],'isError':True}
                else:
                    early=({'jsonrpc':'2.0','id':rid,'error':{'code':-32601,'message':'Method not found'}},
                           200,'protocol_error',-32601)

        if early:
            value,status,outcome,code=early
            if value is None:
                return await complete_empty(status,outcome=outcome,error_code=code)
            return await complete(value,status,outcome=outcome,error_code=code)

        # Upstream I/O begins only after the authority transaction releases its
        # database connection. Prepared data contains no inbound bearer credential.
        outcome='ok'; error_code=None
        if deferred:
            await mcp_observe.mark(trace,'upstream.started',provider='federated',
                                   connection_name=deferred['connection'],
                                   upstream_tool=deferred['upstream_name'])
            output=await federation.forward(deferred)
            is_error=bool(output.get('isError'))
            first=(output.get('content') or [{}])[0]
            unavailable=is_error and isinstance(first,dict) and str(first.get('text','')).startswith('UPSTREAM_UNAVAILABLE:')
            outcome='upstream_error' if unavailable else ('tool_error' if is_error else 'ok')
            error_code='UPSTREAM_UNAVAILABLE' if unavailable else ('TOOL_ERROR' if is_error else None)
            await mcp_observe.mark(trace,'upstream.completed',
                                   detail={'is_error':is_error,'outcome':outcome})
        elif method=='tools/call' and output.get('isError'):
            outcome='denied'; error_code='TOOL_DENIED'
        return await complete({'jsonrpc':'2.0','id':rid,'result':output},
                              headers={'Mcp-Session-Id':str(sid)} if sid else None,
                              outcome=outcome,error_code=error_code)
    except HTTPException as exc:
        code=exc.detail.get('code',exc.status_code) if isinstance(exc.detail,dict) else exc.status_code
        # An exception rolls back the authority transaction and its checkpoints.
        # Re-state a successfully resolved identity after releasing that
        # connection; an invalid bearer never populates user.
        if user is not None:
            await mcp_observe.mark(trace,'auth.accepted',principal=user,
                                   detail={'credential_kind':user.credential['kind']})
        stage='session.denied' if user is not None and code!='BEARER_REQUIRED' else 'auth.denied'
        await mcp_observe.mark(trace,stage,detail={'code':str(code)[:80],
                                                   'http_status':exc.status_code})
        return await complete({'detail':exc.detail},exc.status_code,
                              outcome='denied',error_code=code)
    except Exception:
        await mcp_observe.finish(trace,started=started,outcome='error',
                                 response_bytes=0,error_code='INTERNAL_ERROR')
        raise
