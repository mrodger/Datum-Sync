"""Create/update local operator and the encrypted loopback MCP connection."""
import asyncio
import json
import os
from pathlib import Path
import secrets

import asyncpg

from datum_sync import auth, config, connections, credential_access
from datum_sync.portal_core import OWNER_GRANT


async def main():
    conn = await asyncpg.connect(config.DATABASE_URL)
    try:
        operator = await conn.fetchrow("SELECT * FROM service_accounts WHERE name='operator'")
        if operator:
            await conn.execute("UPDATE service_accounts SET portal_grant=$2 WHERE id=$1", operator['id'], json.dumps(OWNER_GRANT))
            print('Operator already exists; credentials unchanged.')
        else:
            password = secrets.token_urlsafe(18)
            operator=await conn.fetchrow("""INSERT INTO service_accounts(name,max_tier,is_admin,repo_scope,password_hash,portal_kind,portal_grant)
                                  VALUES('operator',4,true,ARRAY['*'],$1,'human',$2) RETURNING *""",auth.hash_password(password),json.dumps(OWNER_GRANT))
            destination = config.REPO_ROOT/'.runtime/operator.txt'
            destination.parent.mkdir(mode=0o700, exist_ok=True)
            destination.write_text(f'Portal: {config.PUBLIC_URL}\nUsername: operator\nPassword: {password}\n')
            destination.chmod(0o600)
            print(f'Created operator. Credentials saved privately to {destination}')

        definition = {
            'url': 'http://127.0.0.1:8211/mcp',
            'tool_prefix': 'legacy',
            'headers': {},
            'auth_inject': {'type':'bearer','secret_field':'token'},
            'timeout_seconds': 10,
        }
        secret = {'token': os.environ['LEGACY_MCP_TOKEN']}
        existing = await connections.get(conn,'legacy-local')
        if existing:
            await connections.update(conn,'legacy-local',{'type':'mcp','tier':2,'scope':'global',
                'scope_targets':[],'access':'read','description':'Read-only metadata from the legacy Datum-Sync service.',
                'config':definition,'secret':secret})
        else:
            await connections.create(conn,'legacy-local','mcp',definition,secret,tier=2,access='read',
                description='Read-only metadata from the legacy Datum-Sync service.',created_by='operator')
        await conn.execute("""UPDATE connections SET owner_id=$2,provider_kind='datum-legacy',
                           display_name='Legacy Datum service',portal_state='active' WHERE name=$1""",
                           'legacy-local',operator['id'])
        upstream=await conn.fetchrow("SELECT * FROM auth_credentials WHERE connection_name='legacy-local'")
        if not upstream:
            await credential_access.create(conn,connection_name='legacy-local',label='Legacy MCP service',
                auth_type='bearer',owner_name='Datum operator',secret=secret,created_by=operator['id'] if operator else None)
        print('Legacy MCP connection configured with an encrypted, versioned service credential.')
    finally:
        await conn.close()


if __name__ == '__main__':
    asyncio.run(main())
