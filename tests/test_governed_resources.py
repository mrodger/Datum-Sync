"""Governed resource isolation, sharing, revocation, and audit tests."""
import json
import uuid

from datum_sync import db as database
from test_portal import agent, bearer, portal


async def invoke(portal, token, tool, arguments):
    return await portal.post('/api/access/call', headers=bearer(token),
                             json={'tool': tool, 'arguments': arguments})


async def test_resources_are_private_then_explicitly_shared_and_revoked(portal):
    _, owner_token = await agent(portal, 'researcher')
    _, reader_token = await agent(portal, 'designer')
    sentinel = 'PRIVATE-ARTIFACT-' + str(uuid.uuid4())
    created = await invoke(portal, owner_token, 'resources_create', {
        'path': 'agents/researcher/demo/report.html', 'title': 'Research report',
        'media_type': 'text/html', 'content': '<h1>' + sentinel + '</h1>',
    })
    assert created.status_code == 200, created.text
    artifact = created.json()
    resource_id = artifact['id']

    owner_list = (await portal.get('/api/resources', headers=bearer(owner_token))).json()['items']
    reader_list = (await portal.get('/api/resources', headers=bearer(reader_token))).json()['items']
    assert owner_list[0]['id'] == resource_id and owner_list[0]['access'] == 'owner'
    assert reader_list == []
    assert (await portal.get(f'/api/resources/{resource_id}/content', headers=bearer(reader_token))).status_code == 404

    shared = await invoke(portal, owner_token, 'resources_share', {
        'id': resource_id, 'target': 'designer', 'ttl_hours': 2,
    })
    assert shared.status_code == 200, shared.text
    reader_list = (await portal.get('/api/resources', headers=bearer(reader_token))).json()['items']
    assert reader_list[0]['id'] == resource_id and reader_list[0]['access'] == 'shared'
    content = await portal.get(f'/api/resources/{resource_id}/content', headers=bearer(reader_token))
    assert content.status_code == 200 and sentinel in content.text

    revoked = await invoke(portal, owner_token, 'resources_revoke', {'id': resource_id, 'target': 'designer'})
    assert revoked.status_code == 200, revoked.text
    assert (await portal.get(f'/api/resources/{resource_id}/content', headers=bearer(reader_token))).status_code == 404
    assert (await portal.get('/api/resources', headers=bearer(reader_token))).json()['items'] == []

    assert (await invoke(portal, owner_token, 'resources_share', {'id': resource_id, 'target': 'designer'})).status_code == 200
    dashboard = (await portal.get('/api/dashboard')).json()
    grant_id = next(item['id'] for item in dashboard['resource_grants'] if item['resource_id'] == resource_id)
    assert (await portal.post(f'/api/resource-grants/{grant_id}/revoke', json={})).status_code == 200
    assert (await portal.get(f'/api/resources/{resource_id}/content', headers=bearer(reader_token))).status_code == 404

    async with database.pool().acquire() as conn:
        events = await conn.fetch('SELECT kind,detail FROM plane_resource_events WHERE resource_id=$1 ORDER BY id', uuid.UUID(resource_id))
        audit = await conn.fetch('SELECT verb,detail FROM plane_audit')
    assert [row['kind'] for row in events] == ['resource.created','resource.shared','resource.read','resource.revoked','resource.shared','resource.revoked']
    assert sentinel not in str([dict(row) for row in events])
    assert sentinel not in str([dict(row) for row in audit])


async def test_resource_namespace_media_and_fleet_boundaries(portal):
    owner, owner_token = await agent(portal, 'developer')
    other, _ = await agent(portal, 'security')
    assert (await invoke(portal, owner_token, 'resources_create', {
        'path': 'agents/security/stolen.md', 'title': 'Bad', 'media_type': 'text/markdown', 'content': 'x',
    })).status_code == 403
    assert (await invoke(portal, owner_token, 'resources_create', {
        'path': 'agents/developer/tool.bin', 'title': 'Bad', 'media_type': 'application/octet-stream', 'content': 'x',
    })).status_code == 400
    created = (await invoke(portal, owner_token, 'resources_create', {
        'path': 'agents/developer/result.json', 'title': 'Result', 'media_type': 'application/json', 'content': '{}',
    })).json()
    async with database.pool().acquire() as conn:
        parent = await conn.fetchval("INSERT INTO service_accounts(name,max_tier,is_admin,portal_kind,portal_state,portal_grant) VALUES('other-owner',4,true,'human','active',$1) RETURNING id", json.dumps({}))
        await conn.execute('UPDATE service_accounts SET portal_parent=$1 WHERE id=$2', parent, other['id'])
    denied = await invoke(portal, owner_token, 'resources_share', {'id': created['id'], 'target': 'security'})
    assert denied.status_code == 403
