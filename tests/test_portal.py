"""Real PostgreSQL authority tests. Refuse to run against the prototype database."""
import asyncio
import base64
import hashlib
import json
import uuid
from urllib.parse import parse_qs, urlparse
from datetime import timedelta

import httpx
import pytest
import pytest_asyncio

from datum_sync import auth, config, db as database
from datum_sync.portal import app, _attempts
from datum_sync.portal_core import DEMO_GRANT, OWNER_GRANT, narrower, now

pytestmark=pytest.mark.asyncio
PASSWORD='test-only-operator-password'
HASH=auth.hash_password(PASSWORD)


@pytest_asyncio.fixture
async def portal():
    assert config.DATABASE_URL.endswith('/datum_portal_test'), 'Use demo/manage.py test; never the live database.'
    await database.init_pool()
    _attempts.clear()
    async with database.pool().acquire() as conn:
        await conn.execute('TRUNCATE mcp_server_events,credential_events,credential_grants,credential_requests,auth_credential_versions,auth_credentials,mcp_flow_events,mcp_flows,connections,jobs,service_accounts,plane_enrolments,plane_clients,plane_authorizations,plane_credentials,plane_sessions,plane_audit,plane_releases,plane_pending_calls CASCADE')
        await conn.execute("INSERT INTO plane_clients(client_id,name) VALUES('datum-local','Local test')")
        await conn.execute("INSERT INTO plane_documents(path,content) VALUES('demo/welcome.md','Welcome'),('demo/release-notes.md','Release draft'),('private/operator.md','Private') ON CONFLICT(path) DO UPDATE SET content=EXCLUDED.content")
        await conn.execute("INSERT INTO service_accounts(name,max_tier,is_admin,repo_scope,password_hash,portal_kind,portal_grant) VALUES('operator',4,true,ARRAY['*'],$1,'human',$2)",HASH,json.dumps(OWNER_GRANT))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://testserver') as client:
        result=await client.post('/api/login',json={'name':'operator','password':PASSWORD})
        assert result.status_code==200,result.text
        client.headers['X-CSRF-Token']=result.json()['csrf']
        yield client
    async with database.pool().acquire() as conn:
        await conn.execute('TRUNCATE mcp_server_events,credential_events,credential_grants,credential_requests,auth_credential_versions,auth_credentials,mcp_flow_events,mcp_flows,jobs CASCADE')
    await database.close_pool()


async def agent(client,name='test-agent',grant=None,*,grant_credentials=True):
    result=await client.post('/api/enrolment/codes',json={'grant':grant or DEMO_GRANT})
    assert result.status_code==200,result.text
    code=result.json()['code']
    result=await client.post('/enrol',json={'name':name,'code':code})
    assert result.status_code==200,result.text
    info=result.json()
    result=await client.post('/api/principals/'+str(info['id'])+'/state',json={'action':'approve'})
    assert result.status_code==200,result.text
    result=await client.post('/enrol/claim',json={'claim_code':info['claim_code']})
    assert result.status_code==200,result.text
    if grant_credentials:
        effective=grant or DEMO_GRANT
        async with database.pool().acquire() as conn:
            credentials=await conn.fetch('SELECT * FROM auth_credentials')
            for credential in credentials:
                block=effective.get('mcp',{}).get(credential['connection_name'])
                if block and block['tools']['allow']:
                    await conn.execute('''INSERT INTO credential_grants(principal_id,principal_name,credential_id,allowed_tools,source,valid_until)
                        VALUES($1,$2,$3,$4,'direct',now()+interval '1 day')''',info['id'],name,credential['id'],block['tools']['allow'])
    return info,result.json()['token']


def bearer(token): return {'Authorization':'Bearer '+token}


async def elevate(client,token,duration=900):
    result=await client.post('/oauth/device',headers=bearer(token),json={'scope':'mcp:operate','duration_seconds':duration})
    assert result.status_code==200,result.text
    device=result.json()
    result=await client.post('/api/authorizations/'+device['id']+'/decision',json={'approve':True})
    assert result.status_code==200,result.text
    result=await client.post('/oauth/token',data={'grant_type':'urn:ietf:params:oauth:grant-type:device_code','device_code':device['device_code'],'client_id':'datum-local'})
    assert result.status_code==200,result.text
    return result.json()


async def call(client,token,tool,path='demo/welcome.md',content='test revision'):
    return await client.post('/api/access/call',headers=bearer(token),json={'tool':tool,'arguments':{'path':path,'content':content}})


async def test_enrolment_is_single_use_and_needs_approval(portal):
    code=(await portal.post('/api/enrolment/codes',json={})).json()['code']
    pending=(await portal.post('/enrol',json={'name':'pending-agent','code':code})).json()
    assert (await portal.post('/enrol',json={'name':'second-agent','code':code})).status_code==400
    assert (await portal.post('/enrol/claim',json={'claim_code':pending['claim_code']})).status_code==409
    await portal.post('/api/principals/'+str(pending['id'])+'/state',json={'action':'approve'})
    assert (await portal.post('/enrol/claim',json={'claim_code':pending['claim_code']})).status_code==200
    assert (await portal.post('/enrol/claim',json={'claim_code':pending['claim_code']})).status_code==400


async def test_baseline_denied_write_then_elevation_allows(portal):
    # Guard: PORTAL-002.
    info,token=await agent(portal)
    assert (await call(portal,token,'documents_read')).status_code==200
    assert (await call(portal,token,'documents_write')).status_code==403
    elevated=await elevate(portal,token)
    assert (await call(portal,elevated['access_token'],'documents_write')).status_code==200
    assert (await call(portal,elevated['access_token'],'documents_read','private/operator.md')).status_code==403
    assert (await call(portal,elevated['access_token'],'documents_read','demo/../private/operator.md')).status_code==400


async def test_agent_cannot_mint_or_approve(portal):
    # Guard: PORTAL-001.
    info,token=await agent(portal)
    elevated=await elevate(portal,token)
    result=await portal.post('/api/principals/'+str(info['id'])+'/tokens',headers=bearer(elevated['access_token']),json={'label':'escape'})
    assert result.status_code==403,result.text
    assert (await portal.post('/api/enrolment/codes',headers=bearer(elevated['access_token']),json={})).status_code==403
    result=await portal.post('/oauth/device',headers=bearer(token),json={'scope':'mcp:operate'})
    request_id=result.json()['id']
    assert (await portal.post('/api/authorizations/'+request_id+'/decision',headers=bearer(elevated['access_token']),json={'approve':True})).status_code==403
    assert (await portal.post('/api/principals/'+str(info['id'])+'/tokens',json={'max_tier':4})).status_code==400


async def test_refresh_deadline_and_family_reuse(portal):
    # Guard: PORTAL-003.
    _,token=await agent(portal)
    first=await elevate(portal,token,60)
    result=await portal.post('/oauth/token',data={'grant_type':'refresh_token','refresh_token':first['refresh_token'],'client_id':'datum-local'})
    assert result.status_code==200,result.text
    second=result.json()
    assert second['authorization_expires_at']==first['authorization_expires_at']
    assert (await portal.post('/oauth/token',data={'grant_type':'refresh_token','refresh_token':first['refresh_token'],'client_id':'datum-local'})).status_code==400
    assert (await portal.get('/api/me',headers=bearer(second['access_token']))).status_code==401
    assert (await portal.get('/api/me',headers=bearer(token))).status_code==200


async def test_expiry_applies_to_access_and_refresh(portal):
    _,token=await agent(portal)
    elevated=await elevate(portal,token)
    async with database.pool().acquire() as conn:
        await conn.execute("UPDATE plane_credentials SET authorization_expires_at=now()-interval '1 second' WHERE kind IN ('access','refresh')")
    assert (await call(portal,elevated['access_token'],'documents_write')).status_code==401
    assert (await portal.post('/oauth/token',data={'grant_type':'refresh_token','refresh_token':elevated['refresh_token'],'client_id':'datum-local'})).status_code==400


async def test_restrict_preserves_deny_and_restore_keeps_current_grant(portal):
    # Guard: PORTAL-004.
    grant={**DEMO_GRANT,'read':['**'],'deny':['private/**']}
    info,token=await agent(portal,grant=grant)
    elevated=await elevate(portal,token)
    result=await portal.post('/api/principals/'+str(info['id'])+'/state',json={'action':'restrict'})
    assert result.status_code==200
    result=await portal.get('/api/me',headers=bearer(token))
    assert result.json()['user']['effective_tier']==1
    assert (await call(portal,token,'documents_read','private/operator.md')).status_code==403
    assert (await call(portal,elevated['access_token'],'documents_read')).status_code==401
    new_grant={**DEMO_GRANT,'read':['demo/release-notes.md'],'write':[],'publish':[]}
    assert (await portal.patch('/api/principals/'+str(info['id'])+'/grant',json={'grant':new_grant})).status_code==200
    await portal.post('/api/principals/'+str(info['id'])+'/state',json={'action':'restore'})
    assert (await call(portal,token,'documents_read')).status_code==403
    assert (await call(portal,token,'documents_read','demo/release-notes.md')).status_code==200


async def test_pending_call_rechecks_authority_and_executes_once(portal):
    # Guard: PORTAL-005.
    info,token=await agent(portal)
    elevated=(await elevate(portal,token))['access_token']
    pending=(await call(portal,elevated,'releases_publish')).json()['pending_id']
    narrowed={**DEMO_GRANT,'publish':[]}
    await portal.patch('/api/principals/'+str(info['id'])+'/grant',json={'grant':narrowed})
    result=await portal.post('/api/pending/'+pending+'/decision',json={'approve':True})
    assert result.status_code==403,result.text
    async with database.pool().acquire() as conn: assert await conn.fetchval('SELECT count(*) FROM plane_releases')==0
    await portal.patch('/api/principals/'+str(info['id'])+'/grant',json={'grant':DEMO_GRANT})
    results=await asyncio.gather(*[portal.post('/api/pending/'+pending+'/decision',json={'approve':True}) for _ in range(2)])
    assert sorted(r.status_code for r in results)==[200,409]
    async with database.pool().acquire() as conn: assert await conn.fetchval('SELECT count(*) FROM plane_releases')==1


async def test_disable_cancels_pending_and_revokes_tokens(portal):
    info,token=await agent(portal)
    elevated=(await elevate(portal,token))['access_token']
    pending=(await call(portal,elevated,'releases_publish')).json()['pending_id']
    await portal.post('/api/principals/'+str(info['id'])+'/state',json={'action':'disable'})
    assert (await portal.post('/api/pending/'+pending+'/decision',json={'approve':True})).status_code==409
    assert (await portal.get('/api/me',headers=bearer(token))).status_code==401
    await portal.post('/api/principals/'+str(info['id'])+'/state',json={'action':'enable'})
    assert (await portal.get('/api/me',headers=bearer(token))).status_code==401


async def test_sessions_are_atomic_and_bound_to_credential(portal):
    # Guard: PORTAL-006.
    info,token=await agent(portal)
    payload={'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-03-26','clientInfo':{'name':'test'}}}
    results=await asyncio.gather(*[portal.post('/mcp',headers=bearer(token),json=payload) for _ in range(4)])
    successful=[r for r in results if 'result' in r.json()]
    assert len(successful)==1
    assert sum('error' in r.json() for r in results)==3
    sid=successful[0].headers['mcp-session-id']
    second=(await portal.post('/api/principals/'+str(info['id'])+'/tokens',json={'label':'second'})).json()['token']
    assert (await portal.post('/mcp',headers={**bearer(second),'Mcp-Session-Id':sid},json={'jsonrpc':'2.0','id':2,'method':'tools/list'})).status_code==404
    result=await portal.post('/mcp',headers={**bearer(token),'Mcp-Session-Id':sid},json={'jsonrpc':'2.0','id':2,'method':'tools/list'})
    assert [t['name'] for t in result.json()['result']['tools']]==['whoami','documents_read','resources_list','resources_read','resources_create','resources_share','resources_revoke']
    assert (await portal.delete('/mcp',headers={**bearer(token),'Mcp-Session-Id':sid})).status_code==204
    assert 'result' in (await portal.post('/mcp',headers=bearer(second),json=payload)).json()


async def test_cookie_csrf_and_cross_origin_refused(portal):
    # Guard: PORTAL-007.
    csrf=portal.headers.pop('X-CSRF-Token')
    assert (await portal.post('/api/enrolment/codes',json={})).status_code==403
    portal.headers['X-CSRF-Token']=csrf
    assert (await portal.post('/api/enrolment/codes',headers={'Origin':'https://other.invalid'},json={})).status_code==403
    assert (await portal.post('/api/enrolment/codes',json={})).status_code==200
    assert (await portal.get('/api/dashboard',headers={'Host':'evil.invalid'})).status_code==400


async def test_pending_device_needs_human_decision_and_poll_backoff(portal):
    _,token=await agent(portal)
    device=(await portal.post('/oauth/device',headers=bearer(token),json={'scope':'mcp:operate'})).json()
    body={'grant_type':'urn:ietf:params:oauth:grant-type:device_code','device_code':device['device_code'],'client_id':'datum-local'}
    assert (await portal.post('/oauth/token',data=body)).json()['error']=='authorization_pending'
    assert (await portal.post('/oauth/token',data=body)).json()['error']=='slow_down'
    assert (await portal.post('/oauth/device',headers=bearer(token),json={'scope':'mcp:admin'})).status_code==400


async def test_pkce_login_binds_to_agent_and_checks_verifier(portal):
    info,token=await agent(portal)
    redirect='http://127.0.0.1:9999/callback'
    client=(await portal.post('/oauth/register',json={'client_name':'Local coding harness / test','redirect_uris':[redirect]})).json()['client_id']
    verifier='x'*64
    challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
    consent={'client_id':client,'redirect_uri':redirect,'code_challenge':challenge,'code_challenge_method':'S256','response_type':'code','scope':'mcp:operate','on_behalf_of':info['name'],'state':'test-state','duration_seconds':14400}
    rejected=await portal.post('/api/oauth/consent',json={**consent,'duration_seconds':3600})
    assert rejected.status_code==400 and rejected.json()['detail']['code']=='INVALID_SESSION_DURATION'
    result=await portal.post('/api/oauth/consent',json=consent)
    assert result.status_code==200,result.text
    async with database.pool().acquire() as conn:
        authorization=await conn.fetchrow('SELECT duration_seconds,authorized_until FROM plane_authorizations WHERE code_hash=$1',auth.hash_token(parse_qs(urlparse(result.json()['redirect']).query)['code'][0]))
    assert authorization['duration_seconds']==14400
    assert 14390 <= (authorization['authorized_until']-now()).total_seconds() <= 14400
    query=parse_qs(urlparse(result.json()['redirect']).query)
    assert query['state']==['test-state']
    exchange={'client_id':client,'code':query['code'][0],'redirect_uri':redirect,'grant_type':'authorization_code','code_verifier':'y'*64}
    assert (await portal.post('/oauth/token',data=exchange)).status_code==400
    exchange['code_verifier']=verifier
    result=await portal.post('/oauth/token',data=exchange)
    assert result.status_code==200,result.text
    issued=result.json()
    who=(await portal.get('/api/me',headers=bearer(issued['access_token']))).json()['user']
    assert who['name']==info['name'] and who['effective_tier']==3 and not who['operator']

    initialized=await portal.post('/mcp',headers=bearer(issued['access_token']),json={'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-06-18','capabilities':{},'clientInfo':{'name':'rotation-test'}}})
    assert initialized.status_code==200,initialized.text
    session_id=initialized.headers['mcp-session-id']
    refreshed=await portal.post('/oauth/token',data={'grant_type':'refresh_token','refresh_token':issued['refresh_token'],'client_id':client})
    assert refreshed.status_code==200,refreshed.text
    catalogue=await portal.post('/mcp',headers={**bearer(refreshed.json()['access_token']),'Mcp-Session-Id':session_id},json={'jsonrpc':'2.0','id':2,'method':'tools/list','params':{}})
    assert catalogue.status_code==200,catalogue.text
    assert catalogue.json()['result']['tools']


async def test_audit_contains_decisions_but_no_tokens_or_document_content(portal):
    _,token=await agent(portal)
    elevated=(await elevate(portal,token))['access_token']
    sentinel='PRIVATE-CONTENT-'+str(uuid.uuid4())
    assert (await call(portal,elevated,'documents_write',content=sentinel)).status_code==200
    async with database.pool().acquire() as conn:
        rows=await conn.fetch('SELECT * FROM plane_audit')
    raw=str([dict(r) for r in rows])
    assert 'document.write' in raw and 'elevation.approve' in raw
    assert token not in raw and elevated not in raw and sentinel not in raw


async def test_legacy_accounts_are_not_promoted_or_accepted(portal):
    async with database.pool().acquire() as conn:
        pid=await conn.fetchval("INSERT INTO service_accounts(name,max_tier,is_admin) VALUES('legacy-high-tier',4,false) RETURNING id")
        row=await conn.fetchrow('SELECT is_admin,portal_kind FROM service_accounts WHERE id=$1',pid)
        assert row['is_admin'] is False and row['portal_kind']=='legacy'
    assert narrower(DEMO_GRANT,OWNER_GRANT)
    assert not narrower({'read':['**'],'deny':[],'sessions':1},DEMO_GRANT)
