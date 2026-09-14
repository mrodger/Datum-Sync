"""First MCP integration increment: exact grants, catalogue and forwarding boundary."""
import json

import pytest

from datum_sync import credential_access, db as database, federation, jobs as job_store, legacy_mcp
from datum_sync.portal_core import DEMO_GRANT, grant_valid
from test_portal import agent, bearer, portal

pytestmark = pytest.mark.asyncio


async def add_connection(tool="legacy__list_repositories"):
    upstream=tool.removeprefix("legacy__")
    async with database.pool().acquire() as conn:
        await conn.execute("""INSERT INTO connections(name,type,tier,scope,access,config,owner_id)
            VALUES('legacy-local','mcp',2,'global','read',$1,(SELECT id FROM service_accounts WHERE name='operator'))
            ON CONFLICT(name) DO UPDATE SET type='mcp',tier=2,config=EXCLUDED.config,owner_id=EXCLUDED.owner_id""",
            json.dumps({'url':'http://127.0.0.1:8211/mcp','tool_prefix':'legacy','headers':{}}))
        await conn.execute("""INSERT INTO federated_tools(connection,upstream_name,tool_name,description,input_schema)
            VALUES('legacy-local',$1,$2,'Legacy tool','{"type":"object","properties":{}}')
            ON CONFLICT(connection,upstream_name) DO UPDATE SET tool_name=EXCLUDED.tool_name""",upstream,tool)
        if not await conn.fetchval("SELECT 1 FROM auth_credentials WHERE connection_name='legacy-local'"):
            await credential_access.create(conn,connection_name='legacy-local',label='Legacy test credential',
                                           auth_type='bearer',owner_name='tests',secret={'token':'test-service-token'})


async def session(client, token):
    result=await client.post('/mcp',headers=bearer(token),json={'jsonrpc':'2.0','id':1,'method':'initialize','params':{'clientInfo':{'name':'federation-test'}}})
    assert result.status_code==200,result.text
    return {**bearer(token),'Mcp-Session-Id':result.headers['mcp-session-id']}


async def test_granted_federated_tool_is_listed_and_forwarded(portal,monkeypatch):
    # Guard: FED-001, FED-013.
    await add_connection()
    _,token=await agent(portal,'federated-agent')
    headers=await session(portal,token)
    listed=(await portal.post('/mcp',headers=headers,json={'jsonrpc':'2.0','id':2,'method':'tools/list'})).json()
    assert 'legacy__list_repositories' in [tool['name'] for tool in listed['result']['tools']]
    seen=[]
    async def forward(prepared):
        seen.append(prepared)
        assert 'authorization' not in prepared['definition']['headers']
        return {'content':[{'type':'text','text':'ok'}],'structuredContent':{'repositories':['Testing']},'isError':False}
    monkeypatch.setattr(federation,'forward',forward)
    called=await portal.post('/mcp',headers=headers,json={'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'legacy__list_repositories','arguments':{}}})
    assert called.json()['result']['structuredContent']=={'repositories':['Testing']}
    assert len(seen)==1 and seen[0]['upstream_name']=='list_repositories'
    async with database.pool().acquire() as conn:
        row=await conn.fetchrow("SELECT detail FROM plane_audit WHERE verb='federate.call' ORDER BY id DESC LIMIT 1")
    detail=json.loads(row['detail']) if isinstance(row['detail'],str) else row['detail']
    assert detail == {'tool':'legacy__list_repositories'}


async def test_missing_mcp_grant_hides_tool_and_never_forwards(portal,monkeypatch):
    # Guard: FED-001.
    await add_connection()
    grant={**DEMO_GRANT,'mcp':{}}
    info,token=await agent(portal,'unfederated-agent',grant)
    # Give the principal a credential grant directly so this test isolates the
    # independent portal-policy check in catalogue filtering.
    async with database.pool().acquire() as conn:
        credential_id=await conn.fetchval("SELECT id FROM auth_credentials WHERE connection_name='legacy-local'")
        await conn.execute('''INSERT INTO credential_grants(principal_id,principal_name,credential_id,allowed_tools,source,valid_until)
            VALUES($1,'unfederated-agent',$2,ARRAY['legacy__list_repositories'],'direct',now()+interval '1 day')''',info['id'],credential_id)
    headers=await session(portal,token)
    listed=(await portal.post('/mcp',headers=headers,json={'jsonrpc':'2.0','id':2,'method':'tools/list'})).json()
    assert 'legacy__list_repositories' not in [tool['name'] for tool in listed['result']['tools']]
    async def forbidden(_):
        pytest.fail('unauthorized call reached upstream forwarding')
    monkeypatch.setattr(federation,'forward',forbidden)
    result=await portal.post('/mcp',headers=headers,json={'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'legacy__list_repositories','arguments':{}}})
    assert result.json()['result']['isError'] is True


async def test_catalogue_refresh_is_persistent_and_tool_names_are_bounded(portal,monkeypatch):
    # Guard: MCP-020.
    await add_connection()
    async def rpc(_definition,method,params=None):
        if method=='initialize': return {'protocolVersion':'2025-06-18'}
        return {'tools':[{'name':'unsafe name/'+'x'*80,'description':'bounded','inputSchema':{'type':'object'}}]}
    monkeypatch.setattr(federation,'_rpc',rpc)
    status=await federation.refresh('legacy-local')
    assert status['tool_count']==1
    async with database.pool().acquire() as conn:
        name=await conn.fetchval("SELECT tool_name FROM federated_tools WHERE connection='legacy-local'")
    assert len(name)<=48 and name.replace('_','').isalnum()


async def test_arguments_are_capped_before_forwarding(portal,monkeypatch):
    # Guard: FED-020.
    await add_connection()
    _,token=await agent(portal,'argument-cap-agent')
    headers=await session(portal,token)
    async def forbidden(_):
        pytest.fail('oversized arguments reached upstream forwarding')
    monkeypatch.setattr(federation,'forward',forbidden)
    result=await portal.post('/mcp',headers=headers,json={'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'legacy__list_repositories','arguments':{'padding':'x'*(256*1024)}}})
    assert result.status_code == 413


async def test_mcp_grant_rejects_wildcard_tool_names():
    # Guard: FED-002. MCP grants in this increment name tools exactly.
    grant={**DEMO_GRANT,'mcp':{'legacy-local':{'tools':{'allow':['legacy__*'],'deny':[]}}}}
    with pytest.raises(Exception) as caught:
        grant_valid(grant)
    assert caught.value.status_code==400


async def test_job_status_and_logs_are_scoped_to_server_generated_principal(portal,monkeypatch):
    # Guard: JOB-001, JOB-002, JOB-003, JOB-004, JOB-005, JOB-008, JOB-009, JOB-010.
    info,token=await agent(portal,'job-owner')
    other,_=await agent(portal,'other-job-owner')
    async with database.pool().acquire() as conn:
        child_id=await conn.fetchval("""INSERT INTO service_accounts(name,max_tier,repo_scope,portal_kind,portal_parent,portal_grant)
            VALUES('job-owner-child',2,ARRAY[]::text[],'agent',$1,$2) RETURNING id""",info['id'],json.dumps(DEMO_GRANT))
        artifacts=json.dumps([{'name':'report.txt','type':'text/plain','primary':True,'dir':False,
                               'size':12,'file':'internal-name.txt','path':'/must/not/escape'}])
        own=await conn.fetchval("""INSERT INTO jobs(repository,workspace,status,submitted_by,portal_principal_id,artifacts)
            VALUES('Testing','CSV','complete','former-owner-name',$1,$2) RETURNING id""",info['id'],artifacts)
        child=await conn.fetchval("""INSERT INTO jobs(repository,workspace,status,submitted_by)
            VALUES('Testing','CSV','cancelled','job-owner-child') RETURNING id""")
        foreign=await conn.fetchval("""INSERT INTO jobs(repository,workspace,status,submitted_by,portal_principal_id)
            VALUES('Testing','Private','failed','other-job-owner',$1) RETURNING id""",other['id'])
        out_of_scope=await conn.fetchval("""INSERT INTO jobs(repository,workspace,status,submitted_by,portal_principal_id)
            VALUES('Hermes','Private','complete','former-owner-name',$1) RETURNING id""",info['id'])
        await job_store.log(conn,own,'durable log entry')
        await job_store.progress(conn,own,42,'halfway')
        await conn.executemany("INSERT INTO job_log(job_id,level,message) VALUES($1,'info',$2)",
                               [(own,f'line {number}') for number in range(205)])
        huge_log_id=await conn.fetchval("INSERT INTO job_log(job_id,level,message) VALUES($1,'error',$2) RETURNING id",own,'x'*5000)
        await conn.execute("UPDATE jobs SET error=$2 WHERE id=$1",own,'e'*5000)
    for tool in ('legacy__list_repositories','legacy__list_jobs','legacy__job_status','legacy__job_log','legacy__job_events','legacy__job_artifacts'):
        await add_connection(tool)
    async with database.pool().acquire() as conn:
        credential_id=await conn.fetchval("SELECT id FROM auth_credentials WHERE connection_name='legacy-local'")
        await conn.execute('''INSERT INTO credential_grants(principal_id,principal_name,credential_id,allowed_tools,source,valid_until)
            VALUES($1,'job-owner',$2,$3,'direct',now()+interval '1 day')''',info['id'],credential_id,
            ['legacy__list_repositories','legacy__list_jobs','legacy__job_status','legacy__job_log','legacy__job_events','legacy__job_artifacts'])
    async def rpc(_definition,method,params=None):
        assert method=='tools/call'
        principal=params['_meta']['io.datum.principal']
        return await legacy_mcp.call(params['name'],params['arguments'],principal)
    monkeypatch.setattr(federation,'_rpc',rpc)
    headers=await session(portal,token)
    attacker_meta={'_meta':{'io.datum.principal':{'id':other['id'],'name':'other-job-owner','repositories':['*']}}}
    listed=await portal.post('/mcp',headers=headers,json={'jsonrpc':'2.0','id':2,'method':'tools/call',
        'params':{'name':'legacy__list_jobs','arguments':attacker_meta}})
    visible_jobs=listed.json()['result']['structuredContent']['jobs']
    assert {item['id'] for item in visible_jobs}=={str(own),str(child)}
    assert str(foreign) not in {item['id'] for item in visible_jobs}
    assert str(out_of_scope) not in {item['id'] for item in visible_jobs}
    repositories=await portal.post('/mcp',headers=headers,json={'jsonrpc':'2.0','id':20,'method':'tools/call',
        'params':{'name':'legacy__list_repositories','arguments':{}}})
    assert [item['name'] for item in repositories.json()['result']['structuredContent']['repositories']]==['Testing']
    status=await portal.post('/mcp',headers=headers,json={'jsonrpc':'2.0','id':3,'method':'tools/call',
        'params':{'name':'legacy__job_status','arguments':{'job_id':str(own)}}})
    assert status.json()['result']['structuredContent']['status']=='complete'
    assert len(status.json()['result']['structuredContent']['error'])==4096
    hidden=await portal.post('/mcp',headers=headers,json={'jsonrpc':'2.0','id':4,'method':'tools/call',
        'params':{'name':'legacy__job_status','arguments':{'job_id':str(foreign)}}})
    assert hidden.json()['result']['isError'] is True
    log=await portal.post('/mcp',headers=headers,json={'jsonrpc':'2.0','id':5,'method':'tools/call',
        'params':{'name':'legacy__job_log','arguments':{'job_id':str(own),'limit':200}}})
    entries=log.json()['result']['structuredContent']['entries']
    assert len(entries)==200 and entries[0]['message']=='durable log entry' and entries[-1]['message']=='line 198'
    bounded=await portal.post('/mcp',headers=headers,json={'jsonrpc':'2.0','id':6,'method':'tools/call',
        'params':{'name':'legacy__job_log','arguments':{'job_id':str(own),'after_id':huge_log_id-1,'limit':1}}})
    assert len(bounded.json()['result']['structuredContent']['entries'][0]['message'])==4096
    events=await portal.post('/mcp',headers=headers,json={'jsonrpc':'2.0','id':7,'method':'tools/call',
        'params':{'name':'legacy__job_events','arguments':{'job_id':str(own)}}})
    timeline=events.json()['result']['structuredContent']['events']
    assert [item['kind'] for item in timeline]==['log','progress']
    assert timeline[1]['payload']['pct']==42
    event_page=await portal.post('/mcp',headers=headers,json={'jsonrpc':'2.0','id':70,'method':'tools/call',
        'params':{'name':'legacy__job_events','arguments':{'job_id':str(own),'limit':1}}})
    assert len(event_page.json()['result']['structuredContent']['events'])==1
    artifacts_result=await portal.post('/mcp',headers=headers,json={'jsonrpc':'2.0','id':8,'method':'tools/call',
        'params':{'name':'legacy__job_artifacts','arguments':{'job_id':str(own)}}})
    artifact=artifacts_result.json()['result']['structuredContent']['artifacts'][0]
    assert artifact=={'name':'report.txt','type':'text/plain','primary':True,'dir':False,'size':12}
