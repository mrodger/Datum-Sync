"""Credential boundary for upstream MCP connections."""
import json

import pytest

from datum_sync import credential_access, crypto, db as database, federation
from test_portal import portal

pytestmark = pytest.mark.asyncio


async def test_configured_caller_authorization_is_replaced_by_upstream_secret(portal):
    # Guard: FED-011. A caller-controlled Authorization value never crosses
    # the gateway; the connection's encrypted credential is injected instead.
    config={'url':'http://127.0.0.1:8211/mcp','tool_prefix':'legacy',
            'headers':{'Authorization':'Bearer caller-value','Cookie':'caller-cookie','X-Org':'datum'},
            'auth_inject':{'type':'bearer','secret_field':'token'}}
    sealed=crypto.seal('legacy-local',{'token':'upstream-service-secret'})
    async with database.pool().acquire() as conn:
        await conn.execute("""INSERT INTO connections(name,type,tier,scope,access,config,secret)
            VALUES('legacy-local','mcp',2,'global','read',$1,$2)""",json.dumps(config),sealed)
        await credential_access.create(conn,connection_name='legacy-local',label='Legacy test credential',
                                       auth_type='bearer',owner_name='tests',secret={'token':'upstream-service-secret'})
        definition=await federation._definition(conn,'legacy-local')
    assert definition['headers']=={'X-Org':'datum','Authorization':'Bearer upstream-service-secret'}
    assert definition['params']=={}
