"""Deterministic auth-portal MCP request timelines."""
from __future__ import annotations

import json
import uuid

import pytest

from datum_sync import credential_access, db as database, federation
from test_portal import agent, bearer, portal

pytestmark = pytest.mark.asyncio


async def add_connection():
    async with database.pool().acquire() as conn:
        await conn.execute(
            """INSERT INTO connections(name,type,tier,scope,access,config,owner_id)
               VALUES('legacy-local','mcp',2,'global','read',$1,(SELECT id FROM service_accounts WHERE name='operator'))
               ON CONFLICT(name) DO UPDATE SET type='mcp',tier=2,config=EXCLUDED.config,owner_id=EXCLUDED.owner_id""",
            json.dumps({'url':'http://127.0.0.1:8211/mcp','tool_prefix':'legacy','headers':{}}),
        )
        await conn.execute(
            """INSERT INTO federated_tools(connection,upstream_name,tool_name,description,input_schema)
               VALUES('legacy-local','list_repositories','legacy__list_repositories','Legacy tool',
                      '{"type":"object","properties":{}}')
               ON CONFLICT(connection,upstream_name)
               DO UPDATE SET tool_name=EXCLUDED.tool_name""",
        )
        if not await conn.fetchval("SELECT 1 FROM auth_credentials WHERE connection_name='legacy-local'"):
            await credential_access.create(conn,connection_name='legacy-local',label='Legacy test credential',
                                           auth_type='bearer',owner_name='tests',secret={'token':'test-service-token'})


async def open_session(client, token):
    response=await client.post('/mcp',headers=bearer(token),json={
        'jsonrpc':'2.0','id':1,'method':'initialize',
        'params':{'clientInfo':{'name':'flow-test'}}})
    assert response.status_code==200,response.text
    return {**bearer(token),'Mcp-Session-Id':response.headers['mcp-session-id']}


async def recorded(response):
    trace_id=uuid.UUID(response.headers['x-trace-id'])
    async with database.pool().acquire() as conn:
        flow=await conn.fetchrow("SELECT * FROM mcp_flows WHERE trace_id=$1",trace_id)
        events=await conn.fetch(
            "SELECT kind,detail FROM mcp_flow_events WHERE trace_id=$1 ORDER BY id",trace_id)
    assert flow is not None
    return dict(flow),[{'kind':row['kind'],'detail':json.loads(row['detail'])
                       if isinstance(row['detail'],str) else row['detail']} for row in events]


async def test_federated_call_has_one_deterministic_payload_free_timeline(portal,monkeypatch):
    # Guard: MCPFLOW-001, MCPFLOW-002, MCPFLOW-004, MCPFLOW-005.
    await add_connection()
    info,token=await agent(portal,'flow-agent')
    headers=await open_session(portal,token)
    sentinel='MUST-NOT-ENTER-MCP-FLOW-LOG'

    async def forward(_prepared):
        return {'content':[{'type':'text','text':'safe result'}],
                'structuredContent':{'ok':True},'isError':False}
    monkeypatch.setattr(federation,'forward',forward)

    response=await portal.post('/mcp',headers=headers,json={
        'jsonrpc':'2.0','id':'call-7','method':'tools/call',
        'params':{'name':'legacy__list_repositories',
                  'arguments':{'secret':sentinel}}})
    assert response.status_code==200
    flow,events=await recorded(response)

    assert flow['principal_id']==info['id']
    assert flow['principal_name']=='flow-agent'
    assert flow['credential_kind']=='baseline'
    assert flow['session_id']==uuid.UUID(headers['Mcp-Session-Id'])
    assert flow['rpc_method']=='tools/call'
    assert flow['rpc_id']=='"call-7"'
    assert flow['tool_name']=='legacy__list_repositories'
    assert flow['provider']=='federated'
    assert flow['connection_name']=='legacy-local'
    assert flow['upstream_tool']=='list_repositories'
    assert flow['outcome']=='ok'
    assert flow['request_bytes']>0 and flow['response_bytes']>0
    assert flow['duration_ms'] is not None
    assert [event['kind'] for event in events]==[
        'request.received','rpc.parsed','auth.accepted','session.validated',
        'tool.authorized','upstream.started','upstream.completed','request.completed']
    assert sentinel not in json.dumps({'flow':flow,'events':events},default=str)


async def test_invalid_bearer_is_anonymous_but_still_recorded(portal):
    # Guard: MCPFLOW-003.
    response=await portal.post('/mcp',headers=bearer('not-a-real-token'),json={
        'jsonrpc':'2.0','id':2,'method':'tools/list'})
    assert response.status_code==401
    flow,events=await recorded(response)
    assert flow['principal_id'] is None
    assert flow['outcome']=='denied'
    assert flow['error_code']=='INVALID_CREDENTIAL'
    assert flow['response_bytes']>0
    assert [event['kind'] for event in events]==[
        'request.received','rpc.parsed','auth.denied','request.denied']


async def test_valid_principal_with_missing_session_is_attributable(portal):
    # Guard: MCPFLOW-006.
    info,token=await agent(portal,'sessionless-flow-agent')
    response=await portal.post('/mcp',headers=bearer(token),json={
        'jsonrpc':'2.0','id':2,'method':'tools/list'})
    assert response.status_code==400
    flow,events=await recorded(response)
    assert flow['principal_id']==info['id']
    assert flow['principal_name']=='sessionless-flow-agent'
    assert flow['outcome']=='denied'
    assert flow['error_code']=='SESSION_REQUIRED'
    assert [event['kind'] for event in events]==[
        'request.received','rpc.parsed','auth.accepted','session.denied','request.denied']

    invalid=await portal.post('/mcp',headers={**bearer(token),'Mcp-Session-Id':'not-a-uuid'},json={
        'jsonrpc':'2.0','id':3,'method':'tools/list'})
    assert invalid.status_code==400
    invalid_flow,invalid_events=await recorded(invalid)
    assert invalid_flow['principal_id']==info['id']
    assert invalid_flow['error_code']=='INVALID_ID'
    assert [event['kind'] for event in invalid_events]==[
        'request.received','rpc.parsed','auth.accepted','session.denied','request.denied']


async def test_malformed_json_is_a_protocol_flow(portal):
    response=await portal.post('/mcp',headers={'Authorization':'Bearer irrelevant',
                                               'Content-Type':'application/json'},
                               content=b'{broken')
    assert response.status_code==400
    flow,events=await recorded(response)
    assert flow['rpc_method'] is None and flow['principal_id'] is None
    assert flow['outcome']=='protocol_error'
    assert flow['error_code']=='INVALID_RPC'
    assert [event['kind'] for event in events]==[
        'request.received','request.protocol_error']
