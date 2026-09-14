"""Operator-owned MCP registry, tool governance and local demonstration providers."""
import json

import pytest

from datum_sync import credential_access, db as database, demo_mcp, federation
from test_portal import agent, bearer, portal
from test_credential_access import open_session

pytestmark=pytest.mark.asyncio

TOOLSETS={
    'officecli-demo':[('officecli','office__officecli')],
    'postgres-demo':[
        ('list_schemas','postgres__list_schemas'),('list_tables','postgres__list_tables'),
        ('describe_table','postgres__describe_table'),('sample_orders','postgres__sample_orders'),
        ('orders_summary','postgres__orders_summary')],
    'harness-tools':[
        ('list_personas','harness__list_personas'),('render_leaflet_map','harness__render_leaflet_map'),
        ('worms_lookup','harness__worms_lookup')],
}

async def discovered(name):
    async with database.pool().acquire() as conn:
        for upstream,projected in TOOLSETS[name]:
            await conn.execute('''INSERT INTO federated_tools(connection,upstream_name,tool_name,description,input_schema)
                VALUES($1,$2,$3,'Demo tool','{"type":"object"}')''',name,upstream,projected)
    return {'status':'ok','tool_count':len(TOOLSETS[name])}


async def register(client,monkeypatch,template):
    monkeypatch.setattr(federation,'refresh',discovered)
    result=await client.post('/api/mcp-servers',json={'template':template})
    assert result.status_code==201,result.text
    return result


async def request_grant(client,token,credential_id,tools):
    requested=await client.post('/api/credential-requests',headers=bearer(token),json={
        'credential_id':credential_id,'requested_tools':tools,'duration_seconds':3600,
        'purpose':'Demonstrate governed '+tools[0]})
    assert requested.status_code==201,requested.text
    approved=await client.post('/api/credential-requests/'+requested.json()['id']+'/decision',json={'approve':True})
    assert approved.status_code==200,approved.text
    return approved.json()['grant_id']


async def tool_names(client,headers):
    result=await client.post('/mcp',headers=headers,json={'jsonrpc':'2.0','id':9,'method':'tools/list'})
    return {tool['name'] for tool in result.json()['result']['tools']}


async def test_operator_adds_demo_servers_and_inventory_never_contains_tokens(portal,monkeypatch):
    await register(portal,monkeypatch,'officecli-demo')
    await register(portal,monkeypatch,'postgres-demo')
    await register(portal,monkeypatch,'harness-tools')
    duplicate=await portal.post('/api/mcp-servers',json={'template':'postgres-demo'})
    assert duplicate.status_code==409
    dashboard=(await portal.get('/api/dashboard')).json()
    assert {row['name'] for row in dashboard['integrations']}=={'officecli-demo','postgres-demo','harness-tools'}
    assert {row['provider_kind'] for row in dashboard['integrations']}=={'officecli-demo','postgres-demo','datum-harness'}
    assert len(dashboard['managed_credentials'])==3
    raw=json.dumps(dashboard)
    assert all(name not in raw for name in ('OFFICE_MCP_TOKEN','POSTGRES_MCP_TOKEN','HARNESS_MCP_TOKEN'))
    assert 'token_env' not in raw and 'ciphertext' not in raw
    assert {row['kind'] for row in dashboard['mcp_server_events']} >= {'server.registered'}


async def test_tool_and_server_revocation_change_next_agent_catalogue(portal,monkeypatch):
    # Guard: SERVER-001, SERVER-002, SERVER-003.
    await register(portal,monkeypatch,'postgres-demo')
    info,token=await agent(portal,'postgres-fleet-agent',grant_credentials=False)
    headers=await open_session(portal,token)
    dashboard=(await portal.get('/api/dashboard')).json()
    credential=next(row for row in dashboard['managed_credentials'] if row['connection_name']=='postgres-demo')
    grant_id=await request_grant(portal,token,credential['id'],['postgres__list_schemas','postgres__sample_orders'])
    assert {'postgres__list_schemas','postgres__sample_orders'} <= await tool_names(portal,headers)

    disabled=await portal.post('/api/mcp-servers/postgres-demo/tools/postgres__sample_orders/status',json={'action':'disable'})
    assert disabled.status_code==200
    assert 'postgres__sample_orders' not in await tool_names(portal,headers)
    assert 'postgres__list_schemas' in await tool_names(portal,headers)
    await portal.post('/api/mcp-servers/postgres-demo/tools/postgres__sample_orders/status',json={'action':'enable'})

    revoked=await portal.post('/api/credential-grants/'+grant_id+'/tools/postgres__list_schemas/revoke')
    assert revoked.status_code==200 and revoked.json()['remaining_tools']==['postgres__sample_orders']
    names=await tool_names(portal,headers)
    assert 'postgres__list_schemas' not in names and 'postgres__sample_orders' in names

    stopped=await portal.post('/api/mcp-servers/postgres-demo/status',json={'action':'disable'})
    assert stopped.status_code==200
    assert 'postgres__sample_orders' not in await tool_names(portal,headers)
    dashboard=(await portal.get('/api/dashboard')).json()
    grant=next(row for row in dashboard['credential_grants'] if row['id']==grant_id)
    assert grant['effective'] is False and grant['server_state']=='disabled'
    assert {row['kind'] for row in dashboard['mcp_server_events']} >= {
        'tool.disabled','tool.enabled','server.disabled'}
    assert info['name']=='postgres-fleet-agent'


async def test_demo_provider_use_cases_are_bounded_and_read_only(portal,monkeypatch):
    office=demo_mcp.office_call({'command':'view quarterly-brief.docx text'})
    assert office['structuredContent']['title']=='Q3 Spatial Operations Brief'
    assert demo_mcp.office_call({'command':'remove quarterly-brief.docx'})['isError'] is True
    tables=await demo_mcp.postgres_call('list_tables',{'schema':'mcp_demo'})
    assert tables['structuredContent']['tables']==[{'name':'orders','kind':'table'}]
    sample=await demo_mcp.postgres_call('sample_orders',{'region':'Auckland','limit':10})
    assert sample['structuredContent']['count']==2
    summary=await demo_mcp.postgres_call('orders_summary',{})
    assert any(row['region']=='Wellington' for row in summary['structuredContent']['summary'])
    personas=await demo_mcp.harness_call('list_personas',{})
    assert personas['structuredContent']['count']==7
    rendered=await demo_mcp.harness_call('render_leaflet_map',{'lat':-41.29,'lon':174.78,'markers':[{'lat':-41.3,'lon':174.8,'popup':'Wellington'}]})
    assert rendered['structuredContent']['marker_count']==1
    assert '<script>' in rendered['structuredContent']['html']
    assert demo_mcp.render_leaflet_map({'lat':100,'lon':0})['isError'] is True
    assert '&lt;script&gt;' in demo_mcp.render_leaflet_map({'markers':[{'lat':0,'lon':0,'popup':'<script>'}]})['structuredContent']['html']
    async def fake_worms(names):
        assert names==['Crassostrea gigas','Unknown example']
        return [[{'match_type':'exact','status':'accepted','scientificname':'Magallana gigas','AphiaID':140656,
                  'valid_name':'Magallana gigas','valid_AphiaID':140656,'rank':'Species','kingdom':'Animalia',
                  'phylum':'Mollusca','class':'Bivalvia','order':'Ostreida','family':'Ostreidae','isMarine':True}],[]]
    monkeypatch.setattr(demo_mcp,'_fetch_worms',fake_worms)
    taxonomy=await demo_mcp.harness_call('worms_lookup',{'names':['Crassostrea gigas','Unknown example']})
    assert taxonomy['structuredContent']['results'][0]['aphia_id']==140656
    assert taxonomy['structuredContent']['results'][1]=={'query':'Unknown example','matched':False}
    assert (await demo_mcp.harness_call('worms_lookup',{'names':[]}))['isError'] is True

async def test_foreign_operator_server_is_hidden_and_cannot_be_managed(portal):
    info,token=await agent(portal,'owned-fleet-agent',grant_credentials=False)
    config={'url':'http://127.0.0.1:8212/postgres/mcp','tool_prefix':'postgres','headers':{},
            'auth_inject':{'type':'bearer','secret_field':'token'},'timeout_seconds':10}
    async with database.pool().acquire() as conn,conn.transaction():
        other=await conn.fetchval("""INSERT INTO service_accounts(name,max_tier,is_admin,repo_scope,portal_kind,portal_grant)
            VALUES('other-operator',4,true,ARRAY['*'],'human',$1) RETURNING id""",json.dumps({'mcp':{}}))
        await conn.execute("""INSERT INTO connections(name,type,tier,scope,access,description,config,owner_id,provider_kind,display_name)
            VALUES('postgres-demo','mcp',2,'global','read','Foreign fleet server',$1,$2,'postgres-demo','Foreign PostgreSQL')""",
            json.dumps(config),other)
        await conn.execute("""INSERT INTO federated_tools(connection,upstream_name,tool_name,description,input_schema)
            VALUES('postgres-demo','list_schemas','postgres__list_schemas','List schemas','{\"type\":\"object\"}')""")
        credential,_=await credential_access.create(conn,connection_name='postgres-demo',label='Foreign identity',
            auth_type='bearer',owner_name='other-operator',secret={'token':'foreign-test-token'},created_by=other)
        await conn.execute("""INSERT INTO credential_grants(principal_id,principal_name,credential_id,allowed_tools,source,valid_until)
            VALUES($1,'owned-fleet-agent',$2,ARRAY['postgres__list_schemas'],'direct',now()+interval '1 day')""",info['id'],credential['id'])

    dashboard=(await portal.get('/api/dashboard')).json()
    assert dashboard['integrations']==[] and dashboard['managed_credentials']==[] and dashboard['credential_grants']==[]
    catalog=await portal.get('/api/credential-catalog',headers=bearer(token))
    assert catalog.status_code==200 and catalog.json()['items']==[]
    headers=await open_session(portal,token)
    assert 'postgres__list_schemas' not in await tool_names(portal,headers)
    assert (await portal.post('/api/mcp-servers/postgres-demo/status',json={'action':'disable'})).status_code==403
    assert (await portal.post('/api/auth-credentials/'+str(credential['id'])+'/status',json={'action':'disable'})).status_code==404
