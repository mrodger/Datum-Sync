"""Write-only upstream credentials and explicit, reviewable agent use grants."""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from datum_sync import crypto

MAX_SECRET_BYTES = 64 * 1024
MAX_DETAIL_BYTES = 2048
MAX_TOOLS = 50


def now():
    return datetime.now(timezone.utc)


def _json(value):
    return json.loads(value) if isinstance(value, str) else value


def _bounded(value: dict[str, Any] | None) -> str:
    raw=json.dumps(value or {},separators=(',',':'),default=str)
    if len(raw.encode()) <= MAX_DETAIL_BYTES:
        return raw
    return json.dumps({'truncated':True,'keys':sorted((value or {}).keys())})


def validate_secret(secret: Any) -> dict[str, Any]:
    if not isinstance(secret,dict) or not secret:
        raise ValueError('secret must be a non-empty object')
    if any(not isinstance(key,str) or not key or len(key)>80 for key in secret):
        raise ValueError('secret field names must be 1-80 characters')
    if len(json.dumps(secret,separators=(',',':')).encode()) > MAX_SECRET_BYTES:
        raise ValueError('secret exceeds 64 KiB')
    return secret


def validate_tools(tools: Any) -> list[str]:
    if not isinstance(tools,list) or not tools or len(tools)>MAX_TOOLS:
        raise ValueError('requested_tools must contain 1-50 tool names')
    if any(not isinstance(tool,str) or not tool or len(tool)>200 for tool in tools):
        raise ValueError('tool names must be 1-200 characters')
    return list(dict.fromkeys(tools))


def aad(credential_id,version:int) -> str:
    return f'auth-credential:{credential_id}:version:{version}'


async def event(conn, credential, kind:str, *, principal=None, trace=None, detail=None):
    principal_id=(principal.id if hasattr(principal,'id') else principal['id']) if principal is not None else None
    principal_name=(principal.name if hasattr(principal,'name') else principal['name']) if principal is not None else None
    await conn.execute(
        '''INSERT INTO credential_events(credential_id,credential_label,principal_id,principal_name,trace_id,kind,detail)
           VALUES($1,$2,$3,$4,$5,$6,$7)''',
        credential['id'],credential['label'],principal_id,principal_name,
        trace,str(kind)[:80],_bounded(detail))


async def create(conn, *, connection_name:str, label:str, auth_type:str, owner_name:str,
                 secret:dict[str,Any], created_by=None, expires_at=None):
    secret=validate_secret(secret)
    credential_id=uuid.uuid4(); version=1; binding=aad(credential_id,version)
    sealed=crypto.seal(binding,secret)
    row=await conn.fetchrow(
        '''INSERT INTO auth_credentials(id,connection_name,label,auth_type,owner_name,expires_at)
           VALUES($1,$2,$3,$4,$5,$6) RETURNING *''',
        credential_id,connection_name,label[:120],auth_type,owner_name[:120],expires_at)
    version_row=await conn.fetchrow(
        '''INSERT INTO auth_credential_versions(credential_id,version,ciphertext,aad,key_id,fingerprint,secret_fields,state,created_by,activated_at)
           VALUES($1,$2,$3,$4,$5,$6,$7,'active',$8,now()) RETURNING *''',
        credential_id,version,sealed,binding,sealed[0],hashlib.sha256(sealed).hexdigest()[:16],
        sorted(secret),created_by)
    row=await conn.fetchrow('UPDATE auth_credentials SET current_version_id=$2 WHERE id=$1 RETURNING *',
                            credential_id,version_row['id'])
    await event(conn,row,'credential.created',detail={'connection':connection_name,'version':version,
                                                       'auth_type':auth_type,'secret_fields':sorted(secret)})
    return row,version_row


async def stage(conn, credential, secret, *, created_by=None):
    secret=validate_secret(secret)
    version=await conn.fetchval('SELECT COALESCE(max(version),0)+1 FROM auth_credential_versions WHERE credential_id=$1',credential['id'])
    binding=aad(credential['id'],version); sealed=crypto.seal(binding,secret)
    row=await conn.fetchrow(
        '''INSERT INTO auth_credential_versions(credential_id,version,ciphertext,aad,key_id,fingerprint,secret_fields,state,created_by)
           VALUES($1,$2,$3,$4,$5,$6,$7,'pending',$8) RETURNING *''',
        credential['id'],version,sealed,binding,sealed[0],hashlib.sha256(sealed).hexdigest()[:16],
        sorted(secret),created_by)
    await event(conn,credential,'credential.rotation.staged',detail={'version':version,'secret_fields':sorted(secret)})
    return row


async def activate(conn, credential, version_row, *, principal=None, expires_at=None):
    await conn.execute("UPDATE auth_credential_versions SET state='retired',retired_at=now() WHERE credential_id=$1 AND state='active'",credential['id'])
    await conn.execute("UPDATE auth_credential_versions SET state='active',activated_at=now() WHERE id=$1 AND state='pending'",version_row['id'])
    row=await conn.fetchrow('''UPDATE auth_credentials SET current_version_id=$2,expires_at=COALESCE($3,expires_at),updated_at=now()
                               WHERE id=$1 RETURNING *''',credential['id'],version_row['id'],expires_at)
    await event(conn,row,'credential.rotated',principal=principal,
                detail={'version':version_row['version'],'fingerprint':version_row['fingerprint']})
    return row


async def fail_stage(conn, credential, version_row, message):
    await conn.execute("UPDATE auth_credential_versions SET state='failed',retired_at=now() WHERE id=$1",version_row['id'])
    await event(conn,credential,'credential.rotation.failed',detail={'version':version_row['version'],'error':str(message)[:300]})


async def secret_for_connection(conn, connection_name:str, *, require_active=True):
    row=await conn.fetchrow(
        '''SELECT c.*,v.version,v.ciphertext,v.aad,v.state AS version_state,v.fingerprint,v.secret_fields
           FROM auth_credentials c LEFT JOIN auth_credential_versions v ON v.id=c.current_version_id
           WHERE c.connection_name=$1''',connection_name)
    if not row:
        return None,None
    if require_active and (row['status']!='active' or (row['expires_at'] and row['expires_at']<=now())):
        raise PermissionError('upstream credential is disabled or expired')
    if not row['ciphertext'] or row['version_state']!='active':
        raise PermissionError('upstream credential has no active version')
    return row,crypto.open_(row['aad'],row['ciphertext'])


async def requestable_tools(conn,user,credential_id):
    credential=await conn.fetchrow('''SELECT c.*,x.tier AS connection_tier,x.portal_state AS server_state,x.owner_id AS server_owner_id
                                      FROM auth_credentials c JOIN connections x ON x.name=c.connection_name
                                      WHERE c.id=$1 AND x.owner_id=(SELECT portal_parent FROM service_accounts WHERE id=$2)''',credential_id,user.id)
    if (not credential or credential['status']!='active' or credential['server_state']!='active'
            or user.tier<credential['connection_tier']
            or (credential['expires_at'] and credential['expires_at']<=now())):
        return credential,[]
    rows=await conn.fetch('''SELECT tool_name,min_tier FROM federated_tools WHERE connection=$1
                             AND enabled=true ORDER BY tool_name''',credential['connection_name'])
    return credential,[row['tool_name'] for row in rows if user.tier>=row['min_tier']
                       and user.permits_mcp(credential['connection_name'],row['tool_name'])]


async def access_grant(conn,user,connection_name,tool_name):
    row=await conn.fetchrow(
        '''SELECT c.*,g.id AS grant_id FROM auth_credentials c JOIN credential_grants g ON g.credential_id=c.id
           JOIN connections x ON x.name=c.connection_name
           WHERE c.connection_name=$1 AND x.owner_id=(SELECT portal_parent FROM service_accounts WHERE id=$2)
             AND c.status='active' AND (c.expires_at IS NULL OR c.expires_at>now())
             AND g.principal_id=$2 AND g.state='active' AND g.valid_from<=now()
             AND (g.valid_until IS NULL OR g.valid_until>now()) AND $3=ANY(g.allowed_tools)
           ORDER BY g.valid_until DESC NULLS FIRST LIMIT 1''',connection_name,user.id,tool_name)
    return row


async def authorized(conn,user,connection_name,tool_name,*,trace=None):
    credential=await access_grant(conn,user,connection_name,tool_name)
    if not credential:
        return None
    await conn.execute('UPDATE auth_credentials SET last_used_at=now() WHERE id=$1',credential['id'])
    await event(conn,credential,'credential.access.authorized',principal=user,trace=trace,
                detail={'connection':connection_name,'tool':tool_name,'grant_id':str(credential['grant_id'])})
    return credential


async def catalog(conn,user):
    credentials=await conn.fetch("SELECT * FROM auth_credentials WHERE status='active' AND (expires_at IS NULL OR expires_at>now()) ORDER BY label")
    out=[]
    for credential in credentials:
        _,tools=await requestable_tools(conn,user,credential['id'])
        if not tools:
            continue
        grants=await conn.fetch(
            '''SELECT id,allowed_tools,valid_until FROM credential_grants WHERE principal_id=$1 AND credential_id=$2
               AND state='active' AND valid_from<=now() AND (valid_until IS NULL OR valid_until>now())''',user.id,credential['id'])
        pending=await conn.fetchval("SELECT id FROM credential_requests WHERE requested_by=$1 AND credential_id=$2 AND status='pending' AND expires_at>now()",user.id,credential['id'])
        granted=sorted({tool for grant in grants for tool in grant['allowed_tools'] if tool in tools})
        out.append({'id':credential['id'],'connection':credential['connection_name'],'label':credential['label'],
                    'auth_type':credential['auth_type'],'requestable_tools':tools,'granted_tools':granted,
                    'pending_request_id':pending,'expires_at':credential['expires_at']})
    return out

async def principal_policy(conn,principal_id):
    row=await conn.fetchrow('SELECT * FROM service_accounts WHERE id=$1',principal_id)
    if not row or row['disabled'] or row['portal_kind']!='agent' or row['portal_state'] not in ('active','restricted'):
        return None,[],0
    tier=row['max_tier'] if row['portal_state']=='active' else min(row['max_tier'],1)
    grants=[_json(row['portal_grant'])]; seen={row['id']}; parent=row['portal_parent']
    while parent is not None:
        if parent in seen or len(seen)>=8:
            return None,[],0
        seen.add(parent)
        ancestor=await conn.fetchrow('SELECT * FROM service_accounts WHERE id=$1',parent)
        if not ancestor or ancestor['disabled'] or ancestor['portal_state']!='active':
            return None,[],0
        tier=min(tier,ancestor['max_tier']); grants.append(_json(ancestor['portal_grant'])); parent=ancestor['portal_parent']
    return row,grants,tier


def policy_permits(grants,connection,tool):
    for grant in grants:
        block=grant.get('mcp',{}).get(connection)
        if not block or tool in block['tools']['deny'] or tool not in block['tools']['allow']:
            return False
    return True


async def effective_grant(conn,grant):
    credential=await conn.fetchrow('''SELECT c.*,x.tier AS connection_tier,x.portal_state AS server_state,x.owner_id AS server_owner_id
                                      FROM auth_credentials c JOIN connections x ON x.name=c.connection_name
                                      WHERE c.id=$1''',grant['credential_id'])
    principal,policies,tier=await principal_policy(conn,grant['principal_id'])
    live=(grant['state']=='active' and grant['valid_from']<=now()
          and (grant['valid_until'] is None or grant['valid_until']>now()))
    credential_live=bool(credential and credential['status']=='active' and credential['server_state']=='active'
                         and principal and credential['server_owner_id']==principal['portal_parent']
                         and (credential['expires_at'] is None or credential['expires_at']>now()))
    states={} if not credential else {row['tool_name']:row for row in await conn.fetch(
        'SELECT tool_name,enabled,min_tier FROM federated_tools WHERE connection=$1 AND tool_name=ANY($2::text[])',
        credential['connection_name'],list(grant['allowed_tools']))}
    tools=[] if not principal or not credential else [tool for tool in grant['allowed_tools']
        if tool in states and states[tool]['enabled'] and tier>=max(credential['connection_tier'],states[tool]['min_tier'])
        and policy_permits(policies,credential['connection_name'],tool)]
    return {'effective':bool(live and credential_live and tools),'effective_tools':tools,
            'principal_state':principal['portal_state'] if principal else 'inactive',
            'credential_status':credential['status'] if credential else 'missing',
            'server_state':credential['server_state'] if credential else 'missing'}
