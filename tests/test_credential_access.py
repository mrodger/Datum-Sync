"""Managed upstream credentials, reviewable requests, and immediate revocation."""
import json

import pytest

from datum_sync import credential_access, crypto, db as database, federation
from test_portal import agent, bearer, portal

pytestmark=pytest.mark.asyncio


async def setup_managed():
    config={'url':'http://127.0.0.1:8211/mcp','tool_prefix':'legacy','headers':{},
            'auth_inject':{'type':'bearer','secret_field':'token'},'timeout_seconds':10}
    async with database.pool().acquire() as conn,conn.transaction():
        owner_id=await conn.fetchval("SELECT id FROM service_accounts WHERE name='operator'")
        await conn.execute("""INSERT INTO connections(name,type,tier,scope,access,description,config,owner_id)
            VALUES('legacy-local','mcp',2,'global','read','Managed test MCP',$1,$2)""",json.dumps(config),owner_id)
        await conn.execute("""INSERT INTO federated_tools(connection,upstream_name,tool_name,description,input_schema)
            VALUES('legacy-local','list_repositories','legacy__list_repositories','List repositories','{"type":"object"}'),
                  ('legacy-local','list_jobs','legacy__list_jobs','List jobs','{"type":"object"}')""")
        credential,version=await credential_access.create(conn,connection_name='legacy-local',
            label='Legacy service identity',auth_type='bearer',owner_name='Platform team',
            secret={'token':'UPSTREAM-SECRET-MUST-STAY-HIDDEN'})
    return credential,version


async def open_session(client,token):
    result=await client.post('/mcp',headers=bearer(token),json={'jsonrpc':'2.0','id':1,'method':'initialize','params':{'clientInfo':{'name':'credential-test'}}})
    assert result.status_code==200,result.text
    return {**bearer(token),'Mcp-Session-Id':result.headers['mcp-session-id']}


async def test_request_approve_use_and_revoke_is_immediate(portal,monkeypatch):
    # Guard: CRED-001, CRED-002, CRED-003.
    credential,_=await setup_managed()
    info,token=await agent(portal,'credential-agent',grant_credentials=False)
    headers=await open_session(portal,token)

    catalog=await portal.get('/api/credential-catalog',headers=bearer(token))
    assert catalog.status_code==200
    item=catalog.json()['items'][0]
    assert item['label']=='Legacy service identity'
    assert item['granted_tools']==[]
    assert 'UPSTREAM-SECRET' not in catalog.text

    hidden=await portal.post('/mcp',headers=headers,json={'jsonrpc':'2.0','id':2,'method':'tools/list'})
    assert 'legacy__list_repositories' not in [tool['name'] for tool in hidden.json()['result']['tools']]
    denied=await portal.post('/mcp',headers=headers,json={'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'legacy__list_repositories','arguments':{}}})
    assert 'CREDENTIAL_ACCESS_REQUIRED' in denied.text

    requested=await portal.post('/api/credential-requests',headers=bearer(token),json={
        'credential_id':str(credential['id']),'requested_tools':['legacy__list_repositories'],
        'duration_seconds':3600,'purpose':'Read repository metadata for this run'})
    assert requested.status_code==201,requested.text
    request_id=requested.json()['id']
    dashboard=(await portal.get('/api/dashboard')).json()
    request_row=next(row for row in dashboard['credential_requests'] if row['id']==request_id)
    assert request_row['status']=='pending' and request_row['requested_by_name']=='credential-agent'
    assert 'UPSTREAM-SECRET' not in json.dumps(dashboard)

    approved=await portal.post('/api/credential-requests/'+request_id+'/decision',json={'approve':True})
    assert approved.status_code==200,approved.text
    grant_id=approved.json()['grant_id']

    listed=await portal.post('/mcp',headers=headers,json={'jsonrpc':'2.0','id':4,'method':'tools/list'})
    assert 'legacy__list_repositories' in [tool['name'] for tool in listed.json()['result']['tools']]
    seen=[]
    async def forward(prepared):
        seen.append(prepared)
        return {'content':[{'type':'text','text':'ok'}],'structuredContent':{'ok':True},'isError':False}
    monkeypatch.setattr(federation,'forward',forward)
    called=await portal.post('/mcp',headers=headers,json={'jsonrpc':'2.0','id':5,'method':'tools/call','params':{'name':'legacy__list_repositories','arguments':{}}})
    assert called.json()['result']['structuredContent']=={'ok':True}
    assert seen[0]['outbound_credential_id']==str(credential['id'])

    revoked=await portal.request('DELETE','/api/credential-grants/'+grant_id,json={'reason':'Run finished'})
    assert revoked.status_code==200,revoked.text
    after=await portal.post('/mcp',headers=headers,json={'jsonrpc':'2.0','id':6,'method':'tools/list'})
    assert 'legacy__list_repositories' not in [tool['name'] for tool in after.json()['result']['tools']]
    denied_again=await portal.post('/mcp',headers=headers,json={'jsonrpc':'2.0','id':7,'method':'tools/call','params':{'name':'legacy__list_repositories','arguments':{}}})
    assert 'CREDENTIAL_ACCESS_REQUIRED' in denied_again.text

    dashboard=(await portal.get('/api/dashboard')).json()
    grant=next(row for row in dashboard['credential_grants'] if row['id']==grant_id)
    assert grant['state']=='revoked' and grant['effective'] is False
    assert {event['kind'] for event in dashboard['credential_events']} >= {
        'credential.access.requested','credential.access.approved',
        'credential.access.authorized','credential.access.revoked'}
    assert 'UPSTREAM-SECRET-MUST-STAY-HIDDEN' not in json.dumps(dashboard)


async def test_approval_cannot_expand_request_and_disable_stops_all_access(portal):
    # Guard: CRED-004, CRED-006.
    credential,_=await setup_managed()
    _,token=await agent(portal,'narrow-request-agent',grant_credentials=False)
    requested=await portal.post('/api/credential-requests',headers=bearer(token),json={
        'credential_id':str(credential['id']),'requested_tools':['legacy__list_repositories'],
        'duration_seconds':3600,'purpose':'Need repository names'})
    request_id=requested.json()['id']
    expanded=await portal.post('/api/credential-requests/'+request_id+'/decision',json={
        'approve':True,'allowed_tools':['legacy__list_repositories','legacy__list_jobs']})
    assert expanded.status_code==400 and 'GRANT_EXCEEDS_REQUEST' in expanded.text
    approved=await portal.post('/api/credential-requests/'+request_id+'/decision',json={'approve':True})
    assert approved.status_code==200
    dashboard=(await portal.get('/api/dashboard')).json()
    grant=next(row for row in dashboard['credential_grants'] if row['id']==approved.json()['grant_id'])
    assert grant['effective'] is True
    disabled=await portal.post('/api/auth-credentials/'+str(credential['id'])+'/status',json={'action':'disable'})
    assert disabled.status_code==200 and disabled.json()['status']=='disabled'
    dashboard=(await portal.get('/api/dashboard')).json()
    grant=next(row for row in dashboard['credential_grants'] if row['id']==approved.json()['grant_id'])
    assert grant['effective'] is False and grant['credential_status']=='disabled'


async def test_rotation_tests_before_activation_and_never_returns_secret(portal,monkeypatch):
    # Guard: CRED-005, CRED-007.
    credential,version=await setup_managed()
    async def reject(_name,_secret): raise RuntimeError('test rejected')
    monkeypatch.setattr(federation,'probe_secret',reject)
    failed=await portal.post('/api/auth-credentials/'+str(credential['id'])+'/rotate',json={'secret':{'token':'BAD-SECRET-HIDDEN'}})
    assert failed.status_code==502
    assert 'test rejected' not in failed.text
    async with database.pool().acquire() as conn:
        current=await conn.fetchrow('SELECT * FROM auth_credentials WHERE id=$1',credential['id'])
        assert current['current_version_id']==version['id']
        assert await conn.fetchval("SELECT count(*) FROM auth_credential_versions WHERE credential_id=$1 AND state='failed'",credential['id'])==1

    async def accept(_name,_secret): return {'ok':True,'tool_count':2}
    async def refresh(_name): return {'status':'ok','tool_count':2}
    monkeypatch.setattr(federation,'probe_secret',accept)
    monkeypatch.setattr(federation,'refresh',refresh)
    rotated=await portal.post('/api/auth-credentials/'+str(credential['id'])+'/rotate',json={'secret':{'token':'NEW-SECRET-HIDDEN'},'expires_days':30})
    assert rotated.status_code==200,rotated.text
    assert rotated.json()['version']==3
    assert 'NEW-SECRET-HIDDEN' not in rotated.text
    async with database.pool().acquire() as conn:
        active,secret=await credential_access.secret_for_connection(conn,'legacy-local')
        assert active['version']==3 and secret=={'token':'NEW-SECRET-HIDDEN'}
        assert await conn.fetchval("SELECT count(*) FROM auth_credential_versions WHERE credential_id=$1 AND state='active'",credential['id'])==1
    dashboard=(await portal.get('/api/dashboard')).text
    assert 'BAD-SECRET-HIDDEN' not in dashboard and 'NEW-SECRET-HIDDEN' not in dashboard


async def test_catalogue_respects_tier_and_upstream_errors_cannot_reflect_secrets(portal,monkeypatch):
    credential,_=await setup_managed()
    _,token=await agent(portal,'tier-and-redaction-agent',grant_credentials=False)
    async with database.pool().acquire() as conn:
        await conn.execute("UPDATE connections SET tier=3 WHERE name='legacy-local'")
    catalog=await portal.get('/api/credential-catalog',headers=bearer(token))
    assert catalog.status_code==200 and catalog.json()['items']==[]

    reflected='REFLECTED-UPSTREAM-SECRET'
    async def reject(*_args,**_kwargs):
        raise RuntimeError(reflected)
    monkeypatch.setattr(federation,'_rpc',reject)
    result=await federation.forward({'definition':{},'upstream_name':'list_repositories',
        'arguments':{},'principal':{},'connection':'legacy-local'})
    assert reflected not in json.dumps(result)
    async with database.pool().acquire() as conn:
        connection=await conn.fetchrow("SELECT last_test_error,federation_status FROM connections WHERE name='legacy-local'")
        audit=await conn.fetchval("SELECT detail FROM plane_audit WHERE verb='federate.unavailable' ORDER BY id DESC LIMIT 1")
    assert reflected not in json.dumps(dict(connection),default=str)
    assert reflected not in str(audit)
    assert credential['label']=='Legacy service identity'
